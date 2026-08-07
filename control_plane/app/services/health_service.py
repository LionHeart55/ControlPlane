"""The single place that decides overall cluster status.

`aggregate_status` is a pure function over already-collected signals. Nothing in
it does I/O, so every rule below is testable without Docker, Milvus or a
database -- which matters, because these six rules are the difference between a
control plane you can trust and one that shows green through an outage.

The rules, applied strictly in order:

  1. Milvus gRPC unreachable ............................... unavailable
  2. Milvus reachable but the deep probe fails ............. degraded
  3. Milvus fine, but an expected component is not running . degraded
  4. Milvus and components fine, but metrics or the Docker
     socket are failing .................................... degraded
  5. Otherwise ............................................. healthy
  6. A dependency needed to *evaluate* the above is itself
     unavailable ........................................... unknown

Rule 6 is a floor, not a fallthrough: it is checked first, because "we could
not look" must never be reported as rule 5's healthy.

Three judgement calls the ordering forces, recorded because they are not
obvious from the list:

**Why a dead Docker socket is degraded and not unknown.** Rules 3 and 4 both
need Docker, so losing it means rule 3 cannot be evaluated -- which sounds like
rule 6. But rule 4 names it explicitly, and rightly: rules 1 and 2 have already
established that Milvus itself answers and serves. The cluster is demonstrably
working; what is lost is visibility. That is observability loss, not an outage,
and the spec's own wording ("must be visible") wants it surfaced rather than
hidden behind an ambiguous `unknown`.

**Why `BREAKER_OPEN` maps to unavailable rather than unknown.** A short-circuit
means the probe was skipped, so `unknown` looks like the honest answer. It is
the wrong one, for a concrete reason: the breaker only opens after `fail_max`
consecutive real failures, so there *is* recent evidence. Reporting `unknown`
would discard it -- and worse, it would break the transition contract. During a
sustained outage the status would go unavailable -> unknown the moment the
breaker tripped, emitting a second `health_transition` event for a single
incident. The acceptance criterion is "no further event rows while it stays
down", so a stable status through the whole outage is required, not incidental.
(The scheduled health job probes with `force=True` and never sees this; the
request path does.)

**Object store and metadata store.** `HealthSignals` carries
`object_store_reachable` / `metadata_store_reachable` because `health_checks`
stores them, but nothing populates them until WP-06b adds the MinIO and etcd
probes. They are `None` today, meaning "not probed", which is deliberately
distinct from `False` ("probed, and down"). Only an explicit `False` affects the
verdict, so with WP-06b unbuilt the six rules above apply exactly as written.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.adapters.circuit_breaker import BREAKER_OPEN_CODE
from app.adapters.docker_client import ComponentRuntime
from app.adapters.docker_client import ComponentStatus as LiveComponent
from app.adapters.metrics_client import METRICS_UNAVAILABLE, MetricsAdapter
from app.adapters.milvus_client import MilvusAdapter, ProbeResult
from app.api.errors import DependencyUnavailableError
from app.db.base import DeploymentStatus, HealthStatus
from app.logging_conf import get_logger

log = get_logger("health_service")

DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
COMPONENT_NOT_RUNNING = "COMPONENT_NOT_RUNNING"
OBJECT_STORE_UNREACHABLE = "OBJECT_STORE_UNREACHABLE"
METADATA_STORE_UNREACHABLE = "METADATA_STORE_UNREACHABLE"
HEALTH_INDETERMINATE = "HEALTH_INDETERMINATE"

# health_status -> deployment_status. `unknown` maps to None, meaning "leave the
# lifecycle state alone": overwriting a real state with a guess is exactly the
# false reporting rule 6 exists to prevent.
DEPLOYMENT_STATUS_FOR: dict[HealthStatus, DeploymentStatus | None] = {
    HealthStatus.HEALTHY: DeploymentStatus.RUNNING,
    HealthStatus.DEGRADED: DeploymentStatus.DEGRADED,
    HealthStatus.UNAVAILABLE: DeploymentStatus.UNAVAILABLE,
    HealthStatus.UNKNOWN: None,
}


@dataclass(frozen=True)
class HealthSignals:
    """Everything `aggregate_status` is allowed to look at.

    `None` and `False` mean different things throughout and the distinction is
    load-bearing: `None` is "not probed", `False` is "probed and failing".
    """

    # None => the probe could not be performed at all (rule 6).
    milvus: ProbeResult | None = None
    # None => the Docker socket was unreachable (rule 4).
    components: list[LiveComponent] | None = None
    components_error: str | None = None
    # None => not probed this cycle; False => scrape failed (rule 4).
    metrics_ok: bool | None = None
    metrics_error: str | None = None
    # Populated by WP-06b. None today.
    object_store_reachable: bool | None = None
    metadata_store_reachable: bool | None = None


@dataclass(frozen=True)
class HealthVerdict:
    """The decision, plus enough evidence to explain it."""

    status: HealthStatus
    # Which numbered rule fired. Makes the ordering auditable in the stored
    # row and directly assertable in tests.
    rule: int
    error_code: str | None = None
    error_message: str | None = None
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def deployment_status(self) -> DeploymentStatus | None:
        return DEPLOYMENT_STATUS_FOR[self.status]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "rule": self.rule,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "reasons": self.reasons,
            "checks": self.checks,
        }


def aggregate_status(
    signals: HealthSignals,
    *,
    expected_components: Sequence[str] = (),
) -> HealthVerdict:
    """Apply the six ordered rules. Pure: no I/O, no clock, no globals."""
    checks: dict[str, Any] = {
        "milvus_probed": signals.milvus is not None,
        "components_probed": signals.components is not None,
        "metrics_probed": signals.metrics_ok is not None,
        "object_store_reachable": signals.object_store_reachable,
        "metadata_store_reachable": signals.metadata_store_reachable,
    }

    # --- Rule 6 (checked first: it is a precondition, not a fallthrough) ---
    probe = signals.milvus
    if probe is None:
        return HealthVerdict(
            status=HealthStatus.UNKNOWN,
            rule=6,
            error_code=HEALTH_INDETERMINATE,
            error_message="Milvus was not probed, so health cannot be evaluated",
            reasons=["milvus_not_probed"],
            checks=checks,
        )

    checks.update(probe.checks)
    checks["milvus_reachable"] = probe.reachable
    checks["latency_ms"] = probe.latency_ms

    # --- Rule 1: gRPC unreachable ---------------------------------------
    # `connect` is the deep probe's first check. A failure before it means we
    # never reached Milvus at all; a failure after it means Milvus answered but
    # could not serve, which is rule 2. `ping()` sets no checks dict, so fall
    # back to `reachable` for the shallow probe.
    connected = bool(probe.checks.get("connect")) or probe.reachable
    if not connected:
        return HealthVerdict(
            status=HealthStatus.UNAVAILABLE,
            rule=1,
            error_code=probe.error_code or "MILVUS_UNREACHABLE",
            error_message=probe.error_message or "Milvus gRPC endpoint is unreachable",
            reasons=["milvus_unreachable"],
            checks=checks,
        )

    # --- Rule 2: connected, but a deeper call failed --------------------
    if not probe.reachable:
        return HealthVerdict(
            status=HealthStatus.DEGRADED,
            rule=2,
            error_code=probe.error_code or "MILVUS_DEEP_PROBE_FAILED",
            error_message=(probe.error_message or "Milvus is reachable but the deep probe failed"),
            reasons=["deep_probe_failed"],
            checks=checks,
        )

    # --- Rule 2b (WP-06b seam): a Milvus dependency is down --------------
    # Only an explicit False counts. Ordered ahead of rule 3 because losing the
    # object store breaks the data path itself, which is more serious than a
    # sibling container being down.
    store_reasons: list[str] = []
    if signals.object_store_reachable is False:
        store_reasons.append("object_store_unreachable")
    if signals.metadata_store_reachable is False:
        store_reasons.append("metadata_store_unreachable")
    if store_reasons:
        return HealthVerdict(
            status=HealthStatus.DEGRADED,
            rule=2,
            error_code=(
                OBJECT_STORE_UNREACHABLE
                if signals.object_store_reachable is False
                else METADATA_STORE_UNREACHABLE
            ),
            error_message="a storage dependency of Milvus is unreachable",
            reasons=store_reasons,
            checks=checks,
        )

    # --- Rule 3: an expected component is not running --------------------
    if signals.components is not None:
        observed = {c.component_name: c for c in signals.components}
        not_running: list[str] = []
        for name in expected_components:
            component = observed.get(name)
            # Absent from the adapter's own reconciliation counts as missing.
            # Belt and braces: the adapter already fills these in, but the
            # service must not report healthy because a name simply vanished
            # from a list it was handed.
            if component is None or not component.is_running:
                state = component.state if component else "missing"
                not_running.append(f"{name}:{state}")
        checks["components_not_running"] = not_running
        checks["component_count"] = len(signals.components)
        if not_running:
            return HealthVerdict(
                status=HealthStatus.DEGRADED,
                rule=3,
                error_code=COMPONENT_NOT_RUNNING,
                error_message=f"expected components not running: {', '.join(not_running)}",
                reasons=[f"component_not_running:{item}" for item in not_running],
                checks=checks,
            )

    # --- Rule 4: observability loss --------------------------------------
    # Reached only once Milvus is proven to answer AND serve, so the cluster is
    # working; what is missing is our ability to watch it. Visible, not fatal.
    observability: list[str] = []
    code: str | None = None
    if signals.components is None:
        observability.append("docker_unavailable")
        code = DOCKER_UNAVAILABLE
    if signals.metrics_ok is False:
        observability.append("metrics_unavailable")
        code = code or METRICS_UNAVAILABLE
    if observability:
        return HealthVerdict(
            status=HealthStatus.DEGRADED,
            rule=4,
            error_code=code,
            error_message=(
                "Milvus is serving, but the control plane lost observability: "
                + ", ".join(observability)
            ),
            reasons=observability,
            checks=checks,
        )

    # --- Rule 5 ----------------------------------------------------------
    return HealthVerdict(status=HealthStatus.HEALTHY, rule=5, reasons=[], checks=checks)


async def collect_signals(
    *,
    milvus: MilvusAdapter,
    docker: ComponentRuntime | None = None,
    metrics: MetricsAdapter | None = None,
    compose_project: str | None = None,
    force: bool = False,
    budget_s: float = 10.0,
) -> HealthSignals:
    """Gather every live signal concurrently. Never raises.

    Concurrent, not sequential: three dependencies at up to ~5s each would take
    15s on a 15s interval and the job would never keep up.

    `force=True` bypasses the circuit breaker and is what the scheduled health
    job passes. The job is the breaker's half-open driver -- if it honoured the
    breaker, nothing would ever probe a recovering Milvus and the reset timeout
    would become a floor on recovery time instead of a ceiling on load.
    """

    async def probe_milvus() -> ProbeResult:
        return await milvus.deep_probe(force=force)

    async def probe_components() -> tuple[list[LiveComponent] | None, str | None]:
        if docker is None:
            return None, "docker adapter not configured"
        try:
            return await docker.list_components(compose_project=compose_project), None
        except DependencyUnavailableError as exc:
            return None, exc.message
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def probe_metrics() -> tuple[bool | None, str | None]:
        if metrics is None:
            return None, None
        try:
            ok = await metrics.ping()
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return ok, None if ok else "metrics endpoint unreachable"

    try:
        probe, components, metrics_result = await asyncio.wait_for(
            asyncio.gather(
                probe_milvus(),
                probe_components(),
                probe_metrics(),
                return_exceptions=True,
            ),
            timeout=budget_s,
        )
    except TimeoutError:
        # The whole fan-out blew its budget. Rule 6: we do not know.
        log.warning("health_signals_budget_exceeded", budget_s=budget_s)
        return HealthSignals()

    # deep_probe() is documented never to raise; if it somehow does, that is a
    # genuine rule-6 "could not evaluate" rather than an outage to report.
    milvus_result: ProbeResult | None
    if isinstance(probe, BaseException):
        log.warning("health_probe_raised", error=f"{type(probe).__name__}: {probe}")
        milvus_result = None
    else:
        milvus_result = probe

    component_list: list[LiveComponent] | None
    components_error: str | None
    if isinstance(components, BaseException):
        component_list, components_error = None, f"{type(components).__name__}: {components}"
    else:
        component_list, components_error = components

    metrics_ok: bool | None
    metrics_error: str | None
    if isinstance(metrics_result, BaseException):
        metrics_ok, metrics_error = False, f"{type(metrics_result).__name__}: {metrics_result}"
    else:
        metrics_ok, metrics_error = metrics_result

    return HealthSignals(
        milvus=milvus_result,
        components=component_list,
        components_error=components_error,
        metrics_ok=metrics_ok,
        metrics_error=metrics_error,
    )


def is_breaker_short_circuit(verdict: HealthVerdict) -> bool:
    """Did this verdict come from a skipped probe rather than a real failure?"""
    return verdict.error_code == BREAKER_OPEN_CODE
