"""Adapter behaviour against a mocked MilvusClient. No infrastructure."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from pymilvus.exceptions import MilvusException

from app.adapters.circuit_breaker import BREAKER_OPEN_CODE, CircuitBreaker
from app.adapters.milvus_client import MilvusAdapter, MilvusAdapterError, MilvusErrorCode
from app.config import Settings


class FakeMilvusClient:
    """Records calls and replays scripted results or exceptions.

    `behaviour` is CLASS-level on purpose. The adapter rebuilds its client
    after a connection-class failure, so a per-instance script would hand the
    rebuilt client a healthy server and the breaker would reset on the next
    call -- a down server would look like it recovered every other probe. A
    shared script models the server's state, which is what these tests mean.
    """

    instances: ClassVar[list[FakeMilvusClient]] = []
    behaviour: ClassVar[dict[str, Any]] = {}

    def __init__(self, uri: str = "", timeout: float | None = None, **_: Any) -> None:
        self.uri = uri
        self.closed = False
        self.calls: list[str] = []
        FakeMilvusClient.instances.append(self)

    def _run(self, op: str, default: Any = None) -> Any:
        self.calls.append(op)
        outcome = FakeMilvusClient.behaviour.get(op, default)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    def get_server_version(self, **_: Any) -> str:
        return self._run("get_server_version", "2.6.20")

    def list_collections(self, **_: Any) -> list[str]:
        return self._run("list_collections", ["demo_docs"])

    def describe_collection(self, name: str, **_: Any) -> dict[str, Any]:
        return self._run(
            "describe_collection",
            {
                "collection_name": name,
                "auto_id": True,
                "fields": [
                    {"name": "id", "type": "INT64", "is_primary": True, "params": {}},
                    {"name": "vector", "type": "FLOAT_VECTOR", "params": {"dim": 384}},
                ],
            },
        )

    def get_collection_stats(self, name: str, **_: Any) -> dict[str, Any]:
        return self._run("get_collection_stats", {"row_count": "5000"})

    def list_indexes(self, name: str, **_: Any) -> list[str]:
        return self._run("list_indexes", ["vector_index"])

    def describe_index(self, name: str, index_name: str, **_: Any) -> dict[str, Any]:
        return self._run("describe_index", {"index_type": "HNSW", "metric_type": "COSINE"})

    def get_load_state(self, name: str, **_: Any) -> dict[str, Any]:
        return self._run("get_load_state", {"state": "Loaded"})

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset() -> Any:
    FakeMilvusClient.instances.clear()
    FakeMilvusClient.behaviour.clear()
    with patch("app.adapters.milvus_client.MilvusClient", FakeMilvusClient):
        yield


def make_adapter(breaker: CircuitBreaker | None = None) -> MilvusAdapter:
    settings = Settings(_env_file=None, milvus_rpc_timeout_s=1.0, milvus_connect_timeout_s=1.0)
    return MilvusAdapter(uri="http://fake:19530", settings=settings, breaker=breaker)


# --- happy path ----------------------------------------------------------
async def test_ping_reports_reachable_with_version() -> None:
    probe = await make_adapter().ping()
    assert probe.reachable is True
    assert probe.server_version == "2.6.20"
    assert probe.error_code is None
    assert probe.latency_ms is not None and probe.latency_ms >= 0


async def test_describe_collection_is_flattened() -> None:
    desc = await make_adapter().describe_collection("demo_docs")
    assert desc["dimension"] == 384
    assert desc["primary_key"] == "id"
    assert desc["vector_field"] == "vector"
    assert desc["auto_id"] is True


async def test_collection_stats_coerces_row_count_to_int() -> None:
    stats = await make_adapter().get_collection_stats("demo_docs")
    assert stats["row_count"] == 5000
    assert isinstance(stats["row_count"], int)


async def test_get_load_state_returns_plain_string() -> None:
    assert await make_adapter().get_load_state("demo_docs") == "Loaded"


async def test_deep_probe_runs_all_three_stages() -> None:
    adapter = make_adapter()
    probe = await adapter.deep_probe()
    assert probe.reachable is True
    assert probe.checks["connect"] is True
    assert probe.checks["list_collections"] is True
    assert probe.checks["describe_collection"] is True
    assert probe.checks["probed_collection"] == "demo_docs"


async def test_deep_probe_marks_describe_skipped_when_no_collections() -> None:
    """No collection to describe must not be reported as a passed check."""
    adapter = make_adapter()
    await adapter._get_client()
    FakeMilvusClient.behaviour["list_collections"] = []
    probe = await adapter.deep_probe()
    assert probe.reachable is True
    assert probe.checks["describe_collection"] is None
    assert probe.checks["collection_count"] == 0


# --- failure paths -------------------------------------------------------
async def test_ping_never_raises_and_carries_code() -> None:
    adapter = make_adapter()
    await adapter._get_client()
    FakeMilvusClient.behaviour["get_server_version"] = MilvusException(
        2, "Fail connecting to server on fake:19530, server unavailable"
    )
    probe = await adapter.ping()
    assert probe.reachable is False
    assert probe.error_code == MilvusErrorCode.UNREACHABLE
    assert probe.error_message


async def test_deep_probe_reports_stage_that_failed() -> None:
    adapter = make_adapter()
    await adapter._get_client()
    FakeMilvusClient.behaviour["list_collections"] = MilvusException(1, "boom")
    probe = await adapter.deep_probe()
    assert probe.reachable is False
    assert probe.checks["connect"] is True
    assert probe.checks["list_collections"] is False
    assert probe.error_code == MilvusErrorCode.RPC_ERROR


async def test_typed_methods_raise_adapter_error() -> None:
    adapter = make_adapter()
    await adapter._get_client()
    FakeMilvusClient.behaviour["describe_collection"] = MilvusException(
        100, "collection not found[collection=nope]"
    )
    with pytest.raises(MilvusAdapterError) as ei:
        await adapter.describe_collection("nope")
    assert ei.value.code == MilvusErrorCode.COLLECTION_NOT_FOUND
    # Must be renderable as a degradation envelope, not a 500.
    assert ei.value.dependency == "milvus"


async def test_hard_timeout_is_enforced() -> None:
    """A call slower than the timeout fails fast rather than hanging."""
    adapter = make_adapter()
    await adapter._get_client()

    def _hang() -> str:
        import time as _t

        _t.sleep(10)
        return "never"

    FakeMilvusClient.behaviour["get_server_version"] = _hang
    probe = await asyncio.wait_for(adapter.ping(), timeout=8)
    assert probe.reachable is False
    assert probe.error_code == MilvusErrorCode.TIMEOUT


# --- client lifecycle ----------------------------------------------------
async def test_client_is_reused_across_calls() -> None:
    adapter = make_adapter()
    await adapter.ping()
    await adapter.ping()
    assert len(FakeMilvusClient.instances) == 1


async def test_client_rebuilt_after_connection_class_failure() -> None:
    """A dead channel must not be reused."""
    adapter = make_adapter()
    await adapter.ping()
    assert len(FakeMilvusClient.instances) == 1
    first = FakeMilvusClient.instances[0]
    FakeMilvusClient.behaviour["get_server_version"] = MilvusException(
        2, "Fail connecting to server on fake:19530, server unavailable"
    )

    assert (await adapter.ping()).error_code == MilvusErrorCode.UNREACHABLE
    assert first.closed is True, "the dead client should have been closed"

    await adapter.ping()
    assert len(FakeMilvusClient.instances) == 2, "a fresh client should have been built"


async def test_client_not_rebuilt_after_application_error() -> None:
    """A missing collection says nothing about the channel; keep it."""
    adapter = make_adapter()
    await adapter.ping()
    FakeMilvusClient.behaviour["describe_collection"] = MilvusException(100, "collection not found")
    with pytest.raises(MilvusAdapterError):
        await adapter.describe_collection("nope")
    assert len(FakeMilvusClient.instances) == 1
    assert FakeMilvusClient.instances[0].closed is False


async def test_concurrent_calls_create_one_client() -> None:
    """The asyncio.Lock must prevent a thundering herd of channels."""
    adapter = make_adapter()
    await asyncio.gather(*(adapter.ping() for _ in range(8)))
    assert len(FakeMilvusClient.instances) == 1


# --- breaker integration -------------------------------------------------
async def test_breaker_opens_then_short_circuits() -> None:
    breaker = CircuitBreaker("milvus", fail_max=3, reset_timeout_s=60)
    adapter = make_adapter(breaker=breaker)
    await adapter._get_client()
    FakeMilvusClient.behaviour["get_server_version"] = MilvusException(
        2, "Fail connecting to server, server unavailable"
    )

    for _ in range(3):
        await adapter.ping()
    assert breaker.state == "open"

    probe = await adapter.ping()
    assert probe.error_code == BREAKER_OPEN_CODE, "an open breaker must not call through"


async def test_force_bypasses_open_breaker() -> None:
    """The health job drives recovery by probing regardless of the breaker."""
    breaker = CircuitBreaker("milvus", fail_max=1, reset_timeout_s=60)
    adapter = make_adapter(breaker=breaker)
    await adapter._get_client()
    FakeMilvusClient.behaviour["get_server_version"] = MilvusException(2, "down")
    await adapter.ping()
    assert breaker.state == "open"

    # Recovered, but the breaker is still open and its timer has not expired.
    FakeMilvusClient.behaviour.pop("get_server_version", None)

    assert (await adapter.ping()).error_code == BREAKER_OPEN_CODE
    forced = await adapter.ping(force=True)
    assert forced.reachable is True
    assert breaker.state == "closed", "a forced success must close the breaker for everyone"
