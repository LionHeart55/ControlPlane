"""The ops script and the API agree about what is in Milvus.

Runs `ops/milvus_demo.py --keep` and asserts the collection it created shows up
through `/collections` with the same numbers. This is the end-to-end check that
the demo script and the control plane are looking at the same cluster -- they
reach Milvus by different routes (host vs container network), which is exactly
the kind of thing that silently diverges.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.tests.integration.conftest import REPO_ROOT

pytestmark = pytest.mark.integration

COLLECTION = f"it_demo_{uuid.uuid4().hex[:8]}"
ROWS = 500
DIM = 32


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """Run the ops script once for the module, then drop what it created."""
    out = tmp_path_factory.mktemp("demo") / "results.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "ops" / "milvus_demo.py"),
            "--uri",
            "http://localhost:19530",
            "--collection",
            COLLECTION,
            "--rows",
            str(ROWS),
            "--dim",
            str(DIM),
            "--batch",
            "250",
            "--topk",
            "5",
            "--drop-existing",
            "--keep",
            "--json-out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        pytest.skip(
            f"ops/milvus_demo.py exited {result.returncode}; Milvus may not be reachable "
            f"from the host.\n{result.stdout[-1500:]}"
        )

    summary = json.loads(Path(out).read_text())
    yield summary

    # Drop what the run left behind (it was invoked with --keep so the
    # assertions had something to look at). Best effort: a failure to clean up
    # must not turn a passing suite red.
    try:
        from pymilvus import MilvusClient

        milvus = MilvusClient(uri="http://localhost:19530", timeout=30)
        if COLLECTION in milvus.list_collections():
            milvus.drop_collection(COLLECTION)
    except Exception as exc:
        print(f"warning: could not drop {COLLECTION}: {exc}")


def test_demo_script_succeeds(demo_run: dict[str, Any]) -> None:
    assert demo_run["ok"] is True
    assert demo_run["insert"]["rows"] == ROWS
    assert demo_run["load_state"] == "Loaded"
    assert len(demo_run["stages"]) == 11, "all eleven stages must run"
    assert len(demo_run["search"]["results"]) == 5


def test_collections_endpoint_reflects_the_new_collection(
    client: httpx.Client, cluster_id: str, demo_run: dict[str, Any]
) -> None:
    body = client.get(f"/api/v1/clusters/{cluster_id}/collections").json()
    assert body["live_status"] in {"ok", "stale"}
    assert body["live"] is not None

    by_name = {c["collection_name"]: c for c in body["live"]["collections"]}
    assert COLLECTION in by_name, f"{COLLECTION} missing from {sorted(by_name)}"

    found = by_name[COLLECTION]
    assert found["row_count"] == ROWS
    assert found["dimension"] == DIM
    assert found["index_type"] == demo_run["index_type"]
    assert found["metric_type"] == demo_run["metric_type"]
    assert found["is_loaded"] is True
    assert found["source"] == "live"


def test_single_collection_endpoint_returns_the_schema(
    client: httpx.Client, cluster_id: str, demo_run: dict[str, Any]
) -> None:
    body = client.get(f"/api/v1/clusters/{cluster_id}/collections/{COLLECTION}").json()
    assert body["live"] is not None
    detail = body["live"]
    assert detail["collection_name"] == COLLECTION
    assert detail["dimension"] == DIM
    field_names = {f["name"] for f in detail["fields"]}
    assert {"id", "vector", "text", "category", "created_at"} <= field_names
    assert detail["primary_key"] == "id"


def test_unknown_collection_is_404(client: httpx.Client, cluster_id: str) -> None:
    response = client.get(f"/api/v1/clusters/{cluster_id}/collections/does_not_exist_xyz")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_overview_collections_panel_sees_it(
    client: httpx.Client, cluster_id: str, demo_run: dict[str, Any]
) -> None:
    overview = client.get(f"/api/v1/clusters/{cluster_id}/overview").json()
    section = overview["collections"]
    assert section["status"] in {"ok", "stale"}
    names = {c["collection_name"] for c in section["data"]["collections"]}
    assert COLLECTION in names


def test_metrics_reflect_the_loaded_entities(
    client: httpx.Client, cluster_id: str, demo_run: dict[str, Any]
) -> None:
    """querynode_entity_num should now count at least the rows just inserted."""
    body = client.get(f"/api/v1/clusters/{cluster_id}/metrics").json()
    if body["live"] is None:
        pytest.skip("metrics endpoint unavailable")
    by_name = {m["name"]: m for m in body["live"]["metrics"]}
    entities = by_name.get("milvus_querynode_entity_num")
    if entities is None or not entities["available"]:
        pytest.skip("milvus_querynode_entity_num not exposed yet")
    assert entities["value"] >= ROWS
