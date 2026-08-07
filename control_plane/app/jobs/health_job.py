"""Periodic health evaluation.

Per cluster, per tick:

    probe (no DB) -> aggregate -> persist a health_checks row
                  -> update the cluster row
                  -> write an events row ONLY if the status changed

The two-phase shape is deliberate. Probing happens outside any transaction so a
five-second gRPC timeout never pins a row lock; persistence then happens inside
one short transaction that takes the lock, reads the previous status, writes the
new one and decides on the event atomically.

**The transition contract.** `events` gets a row only when the status differs
from the previous check. At a 15s interval a per-poll write would add 5 760 rows
a day and bury the handful that describe an actual incident. The previous status
is read from `clusters.last_health_status` rather than from process memory, so
it survives an API restart -- an in-memory version would emit a spurious
transition every time the container restarted, which during a chaos drill is
exactly when it would lie.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from app.adapters.registry import get_docker_adapter, get_metrics_adapter, get_milvus_adapter
from app.config import Settings, get_settings
from app.db.base import HealthStatus
from app.jobs.base import guarded, job_session
from app.logging_conf import get_logger
from app.repositories import (
    ClusterRepository,
    EventRepository,
    EventType,
    HealthCheckRepository,
    Severity,
)
from app.services.health_service import (
    HealthSignals,
    HealthVerdict,
    aggregate_status,
    collect_signals,
)

log = get_logger("health_job")

JOB_ID = "health_job"

# Severity of the *destination* status: a transition into a bad state is the
# alarm, a transition out of one is the all-clear.
_SEVERITY_FOR: dict[HealthStatus, Severity] = {
    HealthStatus.HEALTHY: Severity.INFO,
    HealthStatus.DEGRADED: Severity.WARNING,
    HealthStatus.UNAVAILABLE: Severity.ERROR,
    HealthStatus.UNKNOWN: Severity.WARNING,
}


@guarded(JOB_ID)
async def run_health_job(settings: Settings | None = None) -> int:
    """Evaluate every registered cluster. Returns how many were checked."""
    cfg = settings or get_settings()

    # Phase 1: read the inventory in its own short transaction, and copy out
    # the plain values the probe needs. Holding ORM objects across the probe
    # would keep a session (and a pooled connection) open for its duration.
    async with job_session() as session:
        clusters = await ClusterRepository(session).list(limit=100)
        targets = [
            (c.id, c.name, c.endpoint_uri, c.metrics_uri, c.compose_project) for c in clusters
        ]

    if not targets:
        log.debug("health_job_no_clusters")
        return 0

    for cluster_id, name, endpoint_uri, metrics_uri, compose_project in targets:
        await check_and_persist(
            cluster_id=cluster_id,
            name=name,
            endpoint_uri=endpoint_uri,
            metrics_uri=metrics_uri,
            compose_project=compose_project,
            settings=cfg,
        )
    return len(targets)


async def check_and_persist(
    *,
    cluster_id: Any,
    name: str,
    endpoint_uri: str,
    metrics_uri: str | None,
    compose_project: str | None,
    settings: Settings,
) -> tuple[HealthVerdict, HealthSignals]:
    """Probe one cluster, persist the result, emit an event only on transition.

    Shared with `POST /clusters/{id}/health-check`, deliberately. A forced check
    must obey exactly the same transition contract as the scheduled one --
    if it wrote an event unconditionally, hitting that endpoint twice would
    manufacture an incident that never happened.
    """
    # --- Phase 1: probe, outside any transaction -------------------------
    signals = await collect_signals(
        milvus=get_milvus_adapter(endpoint_uri, settings),
        docker=get_docker_adapter(settings),
        metrics=get_metrics_adapter(metrics_uri or settings.milvus_metrics_uri, settings),
        compose_project=compose_project,
        # The scheduled job is the breaker's half-open driver: it must probe
        # even while the breaker is open, or a recovered Milvus would never be
        # noticed until a user happened to hit the API.
        force=True,
        budget_s=max(settings.milvus_rpc_timeout_s * 2, 10.0),
    )
    verdict = aggregate_status(signals, expected_components=settings.cp_expected_components)
    checked_at = dt.datetime.now(dt.UTC)
    probe = signals.milvus

    # --- Phase 2: persist, in one short locked transaction ---------------
    async with job_session() as session:
        clusters = ClusterRepository(session)
        cluster = await clusters.get_for_update(cluster_id)
        if cluster is None:
            log.warning("health_job_cluster_vanished", cluster_id=str(cluster_id))
            return verdict, signals

        previous = cluster.last_health_status

        await HealthCheckRepository(session).insert_health_check(
            cluster_id=cluster_id,
            status=verdict.status,
            checked_at=checked_at,
            latency_ms=probe.latency_ms if probe else None,
            milvus_reachable=bool(probe and probe.reachable),
            object_store_reachable=signals.object_store_reachable,
            metadata_store_reachable=signals.metadata_store_reachable,
            server_version=probe.server_version if probe else None,
            error_code=verdict.error_code,
            error_message=verdict.error_message,
            raw={
                "verdict": verdict.as_dict(),
                "components_error": signals.components_error,
                "metrics_error": signals.metrics_error,
                "metrics_ok": signals.metrics_ok,
            },
        )

        await clusters.apply_health(
            cluster,
            status=verdict.status,
            checked_at=checked_at,
            deployment_status=verdict.deployment_status,
            milvus_version=probe.server_version if probe else None,
        )

        if verdict.status != previous:
            await EventRepository(session).insert_event(
                cluster_id=cluster_id,
                event_type=EventType.HEALTH_TRANSITION,
                severity=_SEVERITY_FOR[verdict.status],
                message=f"cluster {name!r} health {previous} -> {verdict.status}",
                payload={
                    "from": str(previous),
                    "to": str(verdict.status),
                    "error_code": verdict.error_code,
                    "error_message": verdict.error_message,
                    "rule": verdict.rule,
                    "reasons": verdict.reasons,
                },
                created_at=checked_at,
            )
            log.info(
                "health_transition",
                cluster=name,
                **{"from": str(previous)},
                to=str(verdict.status),
                rule=verdict.rule,
                error_code=verdict.error_code,
            )
        else:
            log.debug(
                "health_unchanged", cluster=name, status=str(verdict.status), rule=verdict.rule
            )

    return verdict, signals
