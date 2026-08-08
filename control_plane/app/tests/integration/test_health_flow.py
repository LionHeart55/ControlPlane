"""Registration, forced health check, and the persisted row.

Against a live stack. The point is the round trip: a probe that happened must
end up in `health_checks`, must update the cluster, and must write an `events`
row only if the status actually changed.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_cluster_is_registered(client: httpx.Client, cluster_id: str) -> None:
    response = client.get(f"/api/v1/clusters/{cluster_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["endpoint_uri"].startswith("http")
    assert body["deployment_status"] != "deleted"


def test_duplicate_registration_is_a_409(client: httpx.Client, cluster_id: str) -> None:
    name = client.get(f"/api/v1/clusters/{cluster_id}").json()["name"]
    response = client.post(
        "/api/v1/clusters",
        json={"name": name, "endpoint_uri": "http://milvus-standalone:19530"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


def test_forced_check_persists_a_row(client: httpx.Client, cluster_id: str) -> None:
    """POST /health-check must write to health_checks, not just answer."""
    before = client.get(
        f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 1}
    ).json()["total"]

    forced = client.post(f"/api/v1/clusters/{cluster_id}/health-check")
    assert forced.status_code == 200
    live = forced.json()["live"]
    assert live["status"] in {"healthy", "degraded", "unavailable", "unknown"}
    assert 1 <= live["rule"] <= 6

    after = client.get(f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 1}).json()
    assert after["total"] == before + 1, "the forced check must persist exactly one row"

    newest = after["items"][0]
    assert newest["status"] == live["status"], "the stored row must match what was returned"
    assert newest["cluster_id"] == cluster_id


def test_forced_check_updates_the_cluster_row(client: httpx.Client, cluster_id: str) -> None:
    client.post(f"/api/v1/clusters/{cluster_id}/health-check")
    cluster = client.get(f"/api/v1/clusters/{cluster_id}").json()
    assert cluster["last_health_check_at"] is not None
    assert cluster["last_health_status"] in {"healthy", "degraded", "unavailable", "unknown"}


def test_repeated_checks_do_not_manufacture_events(client: httpx.Client, cluster_id: str) -> None:
    """The transition contract, exercised through the API.

    Three forced checks in a row against a stable cluster add three rows to
    health_checks and ZERO to events. A per-poll write would put three rows in
    the incident trail describing nothing.
    """
    events_before = client.get(
        "/api/v1/events", params={"cluster_id": cluster_id, "event_type": "health_transition"}
    ).json()["total"]
    checks_before = client.get(
        f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 1}
    ).json()["total"]

    statuses = set()
    for _ in range(3):
        statuses.add(
            client.post(f"/api/v1/clusters/{cluster_id}/health-check").json()["live"]["status"]
        )

    checks_after = client.get(
        f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 1}
    ).json()["total"]
    events_after = client.get(
        "/api/v1/events", params={"cluster_id": cluster_id, "event_type": "health_transition"}
    ).json()["total"]

    assert checks_after == checks_before + 3, "every check is a sample"
    if len(statuses) == 1:
        assert events_after == events_before, "a stable status must not add transition events"


def test_history_is_newest_first(client: httpx.Client, cluster_id: str) -> None:
    items = client.get(f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 5}).json()[
        "items"
    ]
    stamps = [row["checked_at"] for row in items]
    assert stamps == sorted(stamps, reverse=True)


def test_pagination_reports_a_real_total(client: httpx.Client, cluster_id: str) -> None:
    page = client.get(
        f"/api/v1/clusters/{cluster_id}/health-history", params={"limit": 2, "offset": 0}
    ).json()
    assert len(page["items"]) <= 2
    assert page["total"] >= len(page["items"])
    assert page["limit"] == 2 and page["offset"] == 0


def test_unknown_cluster_is_404(client: httpx.Client) -> None:
    response = client.get(f"/api/v1/clusters/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_soft_delete_keeps_the_events(client: httpx.Client) -> None:
    """A hard delete would detach the incident history from its cluster."""
    created = client.post(
        "/api/v1/clusters",
        json={
            "name": f"throwaway-{uuid.uuid4().hex[:8]}",
            "endpoint_uri": "http://milvus-standalone:19530",
        },
    )
    assert created.status_code == 201
    victim = created.json()["id"]

    registered = client.get(
        "/api/v1/events", params={"cluster_id": victim, "event_type": "cluster_registered"}
    ).json()["total"]
    assert registered == 1, "registration must be recorded"

    deleted = client.delete(f"/api/v1/clusters/{victim}")
    assert deleted.status_code == 200
    assert deleted.json()["deployment_status"] == "deleted"

    # Gone from the default listing, but the row and its events survive.
    listed = client.get("/api/v1/clusters", params={"limit": 500}).json()["items"]
    assert victim not in [row["id"] for row in listed]
    still_there = client.get(
        "/api/v1/events", params={"cluster_id": victim, "event_type": "cluster_registered"}
    ).json()["total"]
    assert still_there == 1, "soft delete must not take the audit trail with it"
