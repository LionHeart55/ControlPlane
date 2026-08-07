"""The six ordered health rules. Pure function, no infrastructure.

Each test asserts the rule *number* as well as the status, because the ordering
is the specification: a change that made rule 4 fire where rule 3 should have
would still produce `degraded` and silently pass a status-only assertion.
"""

from __future__ import annotations

from app.adapters.circuit_breaker import BREAKER_OPEN_CODE
from app.adapters.docker_client import ComponentStatus as LiveComponent
from app.adapters.metrics_client import METRICS_UNAVAILABLE
from app.adapters.milvus_client import MilvusErrorCode, ProbeResult
from app.db.base import DeploymentStatus, HealthStatus
from app.services.health_service import (
    COMPONENT_NOT_RUNNING,
    DOCKER_UNAVAILABLE,
    HealthSignals,
    aggregate_status,
)

EXPECTED = ["milvus-etcd", "milvus-minio", "milvus-standalone", "cp-postgres"]


def healthy_probe() -> ProbeResult:
    return ProbeResult(
        reachable=True,
        latency_ms=8,
        server_version="v2.6.20",
        checks={"connect": True, "list_collections": True, "collection_count": 1},
    )


def running(name: str) -> LiveComponent:
    return LiveComponent(component_name=name, state="running", health="healthy")


def all_running() -> list[LiveComponent]:
    return [running(n) for n in EXPECTED]


def good_signals(**overrides: object) -> HealthSignals:
    base: dict[str, object] = {
        "milvus": healthy_probe(),
        "components": all_running(),
        "metrics_ok": True,
    }
    base.update(overrides)
    return HealthSignals(**base)  # type: ignore[arg-type]


# --- rule 5: the happy path ----------------------------------------------
def test_everything_up_is_healthy() -> None:
    verdict = aggregate_status(good_signals(), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.HEALTHY
    assert verdict.rule == 5
    assert verdict.error_code is None
    assert verdict.deployment_status is DeploymentStatus.RUNNING


# --- rule 1: Milvus unreachable ------------------------------------------
def test_milvus_unreachable_is_unavailable() -> None:
    probe = ProbeResult(
        reachable=False,
        error_code=MilvusErrorCode.UNREACHABLE,
        error_message="connection refused",
        checks={"connect": False},
    )
    verdict = aggregate_status(good_signals(milvus=probe), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.UNAVAILABLE
    assert verdict.rule == 1
    assert verdict.error_code == MilvusErrorCode.UNREACHABLE
    assert verdict.deployment_status is DeploymentStatus.UNAVAILABLE


def test_rule_1_outranks_everything_else() -> None:
    """A dead Milvus is reported as an outage, not as a component problem."""
    probe = ProbeResult(reachable=False, error_code=MilvusErrorCode.TIMEOUT, checks={})
    verdict = aggregate_status(
        HealthSignals(milvus=probe, components=[], metrics_ok=False),
        expected_components=EXPECTED,
    )
    assert verdict.rule == 1
    assert verdict.status is HealthStatus.UNAVAILABLE


def test_breaker_short_circuit_is_unavailable_not_unknown() -> None:
    """Load-bearing for the transition contract.

    If this mapped to `unknown`, a sustained outage would emit a SECOND
    health_transition (unavailable -> unknown) the moment the breaker tripped,
    breaking "no further event rows while it stays down".
    """
    probe = ProbeResult(
        reachable=False,
        error_code=BREAKER_OPEN_CODE,
        error_message="circuit breaker open for milvus",
        checks={"connect": False},
    )
    verdict = aggregate_status(good_signals(milvus=probe), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.UNAVAILABLE
    assert verdict.rule == 1


# --- rule 2: connected, deeper call failed --------------------------------
def test_connected_but_deep_probe_failed_is_degraded() -> None:
    """Milvus answered get_server_version, then failed list_collections."""
    probe = ProbeResult(
        reachable=False,
        error_code=MilvusErrorCode.RPC_ERROR,
        error_message="list_collections failed",
        server_version="v2.6.20",
        checks={"connect": True, "list_collections": False},
    )
    verdict = aggregate_status(good_signals(milvus=probe), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.rule == 2
    assert "deep_probe_failed" in verdict.reasons


# --- rule 3: a component is not running -----------------------------------
def test_stopped_component_is_degraded() -> None:
    components = [running(n) for n in EXPECTED if n != "milvus-minio"]
    components.append(LiveComponent(component_name="milvus-minio", state="exited", exit_code=137))
    verdict = aggregate_status(good_signals(components=components), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.rule == 3
    assert verdict.error_code == COMPONENT_NOT_RUNNING
    assert verdict.error_message is not None
    assert "milvus-minio:exited" in verdict.error_message


def test_missing_component_is_degraded() -> None:
    verdict = aggregate_status(
        good_signals(components=[running(n) for n in EXPECTED if n != "cp-postgres"]),
        expected_components=EXPECTED,
    )
    assert verdict.rule == 3
    assert verdict.error_message is not None
    assert "cp-postgres:missing" in verdict.error_message


def test_rule_3_outranks_rule_4() -> None:
    """A stopped container is the real problem; lost metrics are a symptom."""
    components = [running(n) for n in EXPECTED if n != "milvus-standalone"]
    verdict = aggregate_status(
        HealthSignals(milvus=healthy_probe(), components=components, metrics_ok=False),
        expected_components=EXPECTED,
    )
    assert verdict.rule == 3


def test_unexpected_extra_component_does_not_affect_status() -> None:
    verdict = aggregate_status(
        good_signals(components=[*all_running(), running("cp-dashboard")]),
        expected_components=EXPECTED,
    )
    assert verdict.status is HealthStatus.HEALTHY


# --- rule 4: observability loss -------------------------------------------
def test_docker_socket_down_is_degraded_not_unknown() -> None:
    """Milvus is proven to serve, so this is lost visibility, not an outage."""
    verdict = aggregate_status(
        HealthSignals(milvus=healthy_probe(), components=None, metrics_ok=True),
        expected_components=EXPECTED,
    )
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.rule == 4
    assert verdict.error_code == DOCKER_UNAVAILABLE


def test_metrics_scrape_failure_is_degraded() -> None:
    verdict = aggregate_status(good_signals(metrics_ok=False), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.DEGRADED
    assert verdict.rule == 4
    assert verdict.error_code == METRICS_UNAVAILABLE


def test_metrics_not_probed_is_not_a_failure() -> None:
    """None means 'not probed'; only False means 'probed and failing'."""
    verdict = aggregate_status(good_signals(metrics_ok=None), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.HEALTHY


# --- rule 6: could not evaluate -------------------------------------------
def test_unprobed_milvus_is_unknown_never_healthy() -> None:
    verdict = aggregate_status(
        HealthSignals(milvus=None, components=all_running(), metrics_ok=True),
        expected_components=EXPECTED,
    )
    assert verdict.status is HealthStatus.UNKNOWN
    assert verdict.rule == 6


def test_unknown_leaves_deployment_status_untouched() -> None:
    """Overwriting a real lifecycle state with a guess is what rule 6 prevents."""
    verdict = aggregate_status(HealthSignals(), expected_components=EXPECTED)
    assert verdict.status is HealthStatus.UNKNOWN
    assert verdict.deployment_status is None


def test_empty_signals_never_yield_healthy() -> None:
    assert aggregate_status(HealthSignals()).status is not HealthStatus.HEALTHY


# --- WP-06b seam ----------------------------------------------------------
def test_object_store_unreachable_is_degraded_when_probed() -> None:
    verdict = aggregate_status(
        good_signals(object_store_reachable=False), expected_components=EXPECTED
    )
    assert verdict.status is HealthStatus.DEGRADED
    assert "object_store_unreachable" in verdict.reasons


def test_unprobed_stores_do_not_affect_the_verdict() -> None:
    """None today, because WP-06b is not built. The six rules apply as written."""
    verdict = aggregate_status(
        good_signals(object_store_reachable=None, metadata_store_reachable=None),
        expected_components=EXPECTED,
    )
    assert verdict.status is HealthStatus.HEALTHY
    assert verdict.rule == 5


# --- evidence -------------------------------------------------------------
def test_verdict_carries_enough_evidence_to_explain_itself() -> None:
    verdict = aggregate_status(good_signals(), expected_components=EXPECTED)
    assert verdict.checks["milvus_reachable"] is True
    assert verdict.checks["component_count"] == 4
    assert verdict.checks["milvus_probed"] is True
    assert verdict.as_dict()["rule"] == 5
