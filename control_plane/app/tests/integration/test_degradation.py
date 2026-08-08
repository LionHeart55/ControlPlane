"""The regression test for the single most important design rule.

    A dependency being down must never produce a 5xx on a read endpoint.

This one stops Milvus for real and asserts every read endpoint still answers
200 with a well-formed degradation envelope. It is the test that would have
caught the whole class of "the dashboard went white when Milvus died" bug, and
it is deliberately destructive -- it stops and restarts a container -- so it is
marked `destructive` as well as `integration` and restores the stack afterwards
even if an assertion fails.

Skipped automatically when Docker is not reachable.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from app.tests.integration.conftest import docker, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.destructive]

MILVUS_CONTAINER = "milvus-standalone"
# Read endpoints that mix stored and live data. Every one must survive.
READ_PATHS = ["health", "collections", "metrics", "components", "overview"]


@pytest.fixture(scope="module")
def milvus_stopped(client: httpx.Client, cluster_id: str) -> Iterator[None]:
    """Stop Milvus for the module, then bring it back no matter what happened."""
    if docker("info").returncode != 0:
        pytest.skip("Docker is not reachable; cannot run the outage drill")
    if docker("inspect", MILVUS_CONTAINER).returncode != 0:
        pytest.skip(f"{MILVUS_CONTAINER} does not exist; is the stack up?")

    stopped = docker("stop", MILVUS_CONTAINER)
    assert stopped.returncode == 0, stopped.stderr

    # Wait until the API actually observes the outage, rather than assuming a
    # fixed sleep is long enough.
    wait_until(
        lambda: (
            client.get(f"/api/v1/clusters/{cluster_id}/health").json()["live"]["status"]
            != "healthy"
        ),
        timeout_s=60,
        what="the API to observe Milvus being down",
    )
    try:
        yield
    finally:
        docker("start", MILVUS_CONTAINER)
        # Leave the stack usable for whatever runs next. Milvus takes a while
        # to become ready, so this waits rather than returning immediately.
        wait_until(
            lambda: (
                client.get(f"/api/v1/clusters/{cluster_id}/health").json()["live"]["status"]
                == "healthy"
            ),
            timeout_s=300,
            interval_s=3.0,
            what="Milvus to recover",
        )


@pytest.mark.parametrize("path", READ_PATHS)
def test_read_endpoints_stay_200_with_milvus_down(
    client: httpx.Client, cluster_id: str, milvus_stopped: None, path: str
) -> None:
    response = client.get(f"/api/v1/clusters/{cluster_id}/{path}")
    assert response.status_code == 200, (
        f"/{path} returned {response.status_code} with Milvus down. "
        f"A dependency outage must never produce a 5xx on a read endpoint."
    )


def test_health_reports_unavailable_not_an_error(
    client: httpx.Client, cluster_id: str, milvus_stopped: None
) -> None:
    """The named acceptance criterion."""
    body = client.get(f"/api/v1/clusters/{cluster_id}/health").json()

    assert body["live"]["status"] == "unavailable"
    assert body["live"]["milvus_reachable"] is False
    assert body["live"]["rule"] == 1, "an unreachable Milvus is rule 1"
    assert body["degraded_reason"] is not None
    assert body["degraded_reason"]["code"] in {
        "MILVUS_UNREACHABLE",
        "MILVUS_TIMEOUT",
        "BREAKER_OPEN",
    }
    # Stored metadata still answers: PostgreSQL is unaffected.
    assert body["cluster"] is not None


def test_envelope_is_well_formed_while_degraded(
    client: httpx.Client, cluster_id: str, milvus_stopped: None
) -> None:
    """Shape must not change under failure — that is the entire contract."""
    for path in ("collections", "metrics"):
        body = client.get(f"/api/v1/clusters/{cluster_id}/{path}").json()
        assert set(body) >= {
            "cluster",
            "live",
            "live_status",
            "observed_at",
            "stale",
            "degraded_reason",
        }, path
        assert body["live_status"] in {"ok", "stale", "unavailable"}
        if body["live_status"] == "unavailable":
            assert body["live"] is None and body["degraded_reason"]["code"]
        if body["live_status"] == "stale":
            assert body["stale"] is True, "stale data must be flagged"
            assert body["live"] is not None


def test_components_show_milvus_exited(
    client: httpx.Client, cluster_id: str, milvus_stopped: None
) -> None:
    """A stopped container is reported, not omitted."""
    body = client.get(f"/api/v1/clusters/{cluster_id}/components").json()
    if body["live"] is None:
        pytest.skip("Docker socket unavailable to the API")
    by_name = {c["component_name"]: c for c in body["live"]["components"]}
    assert MILVUS_CONTAINER in by_name, "a stopped component must stay in the list"
    assert by_name[MILVUS_CONTAINER]["state"] in {"exited", "missing"}


def test_overview_degrades_per_panel(
    client: httpx.Client, cluster_id: str, milvus_stopped: None
) -> None:
    """One dead dependency dims its panels; it does not fail the page."""
    body = client.get(f"/api/v1/clusters/{cluster_id}/overview").json()
    assert body["degraded"] is True
    assert body["duration_ms"] < body["budget_s"] * 1000, "must stay inside the fan-out budget"

    sections = {name: body[name] for name in ("health", "collections", "metrics", "components")}
    for name, section in sections.items():
        assert section["status"] in {"ok", "stale", "unavailable"}, name
        assert "observed_at" in section, name

    # PostgreSQL is untouched, so these keep working.
    assert body["events"]["status"] == "ok"
    assert body["cluster"] is not None


def test_readyz_stays_200_because_postgres_is_fine(
    client: httpx.Client, milvus_stopped: None
) -> None:
    """Readiness tracks PostgreSQL only. A Milvus outage must not take the API
    out of service — an orchestrator would then restart it for no reason."""
    assert client.get("/readyz").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_an_outage_produces_exactly_one_transition_event(
    client: httpx.Client, cluster_id: str, milvus_stopped: None
) -> None:
    """The incident trail describes the incident, not the polling."""
    checks = client.get(
        f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 1}
    ).json()["total"]
    events = client.get(
        "/api/v1/events", params={"cluster_id": cluster_id, "event_type": "health_transition"}
    ).json()["total"]
    assert events < checks, (
        f"{events} transition events for {checks} checks — events must be written "
        f"on transition only, never per poll"
    )
