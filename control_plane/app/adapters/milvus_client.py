"""Async adapter over the synchronous pymilvus ``MilvusClient``.

Why ``deep_probe`` exists alongside ``:9091/healthz``
-----------------------------------------------------
``/healthz`` is a shallow liveness check: it reports that the Milvus process is
up and serving HTTP, nothing more. Milvus can answer ``/healthz`` with 200
while MinIO is down and only fail later, on a write or a flush. A control plane
that trusts ``/healthz`` reports a green cluster through an object-store
outage, which is the single most misleading thing it could do.

``deep_probe`` therefore exercises the real gRPC data path -- connect, then
``list_collections``, then ``describe_collection`` on an actual collection --
rather than an HTTP liveness handler. It catches a Milvus that is listening but
cannot serve, and it is what turns "the process is alive" into "the service
works".

**An honest limitation, because it changes what you can conclude.**
``list_collections`` and ``describe_collection`` are answered from etcd
metadata by way of RootCoord; they do not read segment data. So a deep probe
alone will usually still pass while MinIO is down, and it is *not* sufficient
for the object-store outage drill on its own. Detecting that reliably needs a
direct object-store probe (``minio_client.py``) plus an etcd probe, which is
what populates ``health_checks.object_store_reachable`` and
``metadata_store_reachable``. ``deep_probe`` narrows the gap left by
``/healthz``; it does not close it.

Concurrency and timeouts
------------------------
pymilvus is synchronous, so every call is pushed through ``asyncio.to_thread``.
Two nested deadlines guard each one:

  * the **inner** ``timeout=`` handed to pymilvus, which becomes a real gRPC
    deadline -- this is the defence that actually works, because
  * the **outer** ``asyncio.wait_for`` cannot cancel a thread already blocked
    in C code. It bounds the coroutine so the event loop is never held up, but
    the worker thread only unwinds when the gRPC deadline fires.

Without the inner deadline a paused Milvus (WP-15 scenario B) would leak one
worker thread per probe forever.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
from pymilvus import MilvusClient
from pymilvus.exceptions import (
    CollectionNotExistException,
    DescribeCollectionException,
    IndexNotExistException,
    MilvusException,
    MilvusUnavailableException,
)

from app.adapters.circuit_breaker import (
    BREAKER_OPEN_CODE,
    CircuitBreaker,
    CircuitBreakerOpenError,
)
from app.api.errors import DependencyUnavailableError
from app.config import Settings, get_settings
from app.logging_conf import get_logger

log = get_logger("milvus")

# pymilvus logs a full traceback at ERROR for every failed RPC. During an
# outage the health job would emit one every interval, burying the structured
# events that actually matter. We classify and log failures ourselves.
logging.getLogger("pymilvus").setLevel(logging.CRITICAL)


# --- stable error codes --------------------------------------------------
class MilvusErrorCode:
    UNREACHABLE = "MILVUS_UNREACHABLE"
    TIMEOUT = "MILVUS_TIMEOUT"
    AUTH_FAILED = "MILVUS_AUTH_FAILED"
    RPC_ERROR = "MILVUS_RPC_ERROR"
    COLLECTION_NOT_FOUND = "MILVUS_COLLECTION_NOT_FOUND"


# Server-side error code for "collection not found" (pymilvus ErrorCode enum).
_SERVER_COLLECTION_NOT_FOUND = 100
# pymilvus raises MilvusException(code=2) for connect failures: refused, DNS,
# and unroutable host all arrive this way rather than as a grpc.RpcError.
_CLIENT_CONNECT_FAILED = 2

_AUTH_HINTS = ("auth check", "unauthenticated", "permission denied", "invalid credential")
_NOT_FOUND_HINTS = ("can't find collection", "collection not found", "collection not exist")
_CONNECT_HINTS = ("fail connecting to server", "cannot connect", "connection refused")

# Codes that mean the channel is suspect and the client must be rebuilt.
_CONNECTION_CLASS = frozenset({MilvusErrorCode.UNREACHABLE, MilvusErrorCode.TIMEOUT})


class MilvusAdapterError(DependencyUnavailableError):
    """A classified Milvus failure.

    Subclasses DependencyUnavailableError, so if one escapes a route the WP-05
    handler renders a degradation envelope rather than a 500.
    """

    def __init__(self, message: str, *, code: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message, dependency="milvus", code=code, detail=detail)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of ping()/deep_probe(). Never raised -- always returned."""

    reachable: bool
    latency_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    server_version: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    observed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "server_version": self.server_version,
            "checks": self.checks,
            "observed_at": self.observed_at.isoformat(),
        }


def classify_error(exc: BaseException) -> tuple[str, str]:
    """Map any exception onto (stable_code, message).

    Order matters, and each branch below exists because of an observed
    behaviour of pymilvus 2.6, not a guess:

    1. ``grpc.RpcError`` is checked FIRST because ``_InactiveRpcError.code`` is
       a bound *method*, not an int. Reading ``.code`` like a MilvusException
       attribute silently yields a method object that compares equal to
       nothing, so every timeout would fall through to RPC_ERROR.
    2. ``DescribeCollectionException`` carries ``code=0`` -- the same value as
       SUCCESS -- so a missing collection must be recognised by exception type
       and message, never by numeric code alone.
    3. ``get_collection_stats``/``list_indexes`` instead raise a plain
       ``MilvusException`` with ``code=100`` for the same condition.
    """
    # 1. gRPC-level failures.
    if isinstance(exc, grpc.RpcError):
        status = exc.code() if callable(getattr(exc, "code", None)) else None
        details = ""
        if callable(getattr(exc, "details", None)):
            details = exc.details() or ""
        if status == grpc.StatusCode.DEADLINE_EXCEEDED:
            return MilvusErrorCode.TIMEOUT, f"gRPC deadline exceeded: {details}"
        if status == grpc.StatusCode.UNAVAILABLE:
            return MilvusErrorCode.UNREACHABLE, f"Milvus unavailable: {details}"
        if status in (grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED):
            return MilvusErrorCode.AUTH_FAILED, f"authentication failed: {details}"
        if status == grpc.StatusCode.NOT_FOUND:
            return MilvusErrorCode.COLLECTION_NOT_FOUND, details or "not found"
        return MilvusErrorCode.RPC_ERROR, f"gRPC error {status}: {details}"

    # 2. Our own outer deadline, and plain socket failures.
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return MilvusErrorCode.TIMEOUT, "call exceeded the configured timeout"
    if isinstance(exc, ConnectionError | OSError):
        return MilvusErrorCode.UNREACHABLE, f"connection failed: {exc}"

    # 3. pymilvus exceptions.
    if isinstance(exc, MilvusException):
        message = getattr(exc, "message", None) or str(exc)
        lowered = message.lower()

        if isinstance(exc, MilvusUnavailableException):
            return MilvusErrorCode.UNREACHABLE, message
        # Type first: DescribeCollectionException reports code=0.
        if isinstance(exc, CollectionNotExistException | DescribeCollectionException):
            return MilvusErrorCode.COLLECTION_NOT_FOUND, message
        if isinstance(exc, IndexNotExistException):
            return MilvusErrorCode.COLLECTION_NOT_FOUND, message

        code = getattr(exc, "code", None)
        if isinstance(code, int):
            if code == _SERVER_COLLECTION_NOT_FOUND:
                return MilvusErrorCode.COLLECTION_NOT_FOUND, message
            if code == _CLIENT_CONNECT_FAILED:
                return MilvusErrorCode.UNREACHABLE, message

        if any(h in lowered for h in _AUTH_HINTS):
            return MilvusErrorCode.AUTH_FAILED, message
        if any(h in lowered for h in _NOT_FOUND_HINTS):
            return MilvusErrorCode.COLLECTION_NOT_FOUND, message
        if any(h in lowered for h in _CONNECT_HINTS):
            return MilvusErrorCode.UNREACHABLE, message
        return MilvusErrorCode.RPC_ERROR, message

    return MilvusErrorCode.RPC_ERROR, f"{type(exc).__name__}: {exc}"


class MilvusAdapter:
    """One long-lived client per endpoint, rebuilt when its channel dies."""

    def __init__(
        self,
        uri: str | None = None,
        settings: Settings | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._uri = uri or self._settings.milvus_uri
        self._breaker = breaker
        self._client: MilvusClient | None = None
        self._lock = asyncio.Lock()
        self._rpc_timeout = self._settings.milvus_rpc_timeout_s
        self._connect_timeout = self._settings.milvus_connect_timeout_s
        # Margin over the inner gRPC deadline: enough for thread hand-off, not
        # so much that a stuck call blocks a caller past its own budget.
        self._outer_margin = 2.0

    @property
    def uri(self) -> str:
        return self._uri

    # --- client lifecycle -------------------------------------------------
    async def _get_client(self) -> MilvusClient:
        """Return the cached client, building it under the lock if needed.

        The lock serialises construction so a burst of concurrent callers (the
        /overview fan-out) opens one channel, not six. While Milvus is down
        this does serialise callers behind a connect timeout -- which is
        precisely the case the circuit breaker short-circuits.
        """
        async with self._lock:
            if self._client is not None:
                return self._client
            try:
                self._client = await asyncio.wait_for(
                    asyncio.to_thread(MilvusClient, uri=self._uri, timeout=self._connect_timeout),
                    timeout=self._connect_timeout + self._outer_margin,
                )
            except BaseException as exc:
                code, message = classify_error(exc)
                raise MilvusAdapterError(
                    message, code=code, detail={"uri": self._uri, "phase": "connect"}
                ) from exc
            log.debug("milvus_client_created", uri=self._uri)
            return self._client

    async def _invalidate_client(self) -> None:
        """Drop the client so the next call rebuilds it.

        A channel that refused or timed out must not be reused: pymilvus will
        happily keep handing back the same dead handler, and every subsequent
        call fails identically until the process restarts.
        """
        async with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception:
                log.debug("milvus_client_close_failed", uri=self._uri)

    async def close(self) -> None:
        await self._invalidate_client()

    # --- call plumbing ----------------------------------------------------
    async def _call(
        self,
        op: str,
        fn: Callable[[MilvusClient, float], Any],
        *,
        force: bool = False,
        rpc_timeout_s: float | None = None,
    ) -> Any:
        """Execute one client operation with breaker, timeouts and classification."""
        if self._breaker is not None and not force and not self._breaker.allow():
            raise MilvusAdapterError(
                f"circuit breaker open for milvus; skipped {op}",
                code=BREAKER_OPEN_CODE,
                detail={"op": op, "retry_after_s": self._breaker.retry_after_s()},
            )

        rpc_timeout = rpc_timeout_s if rpc_timeout_s is not None else self._rpc_timeout
        started = time.perf_counter()
        try:
            client = await self._get_client()
            result = await asyncio.wait_for(
                # The deadline is handed to pymilvus so gRPC enforces it
                # server-side-of-the-socket; wait_for alone cannot unblock a
                # thread already parked in C.
                asyncio.to_thread(fn, client, rpc_timeout),
                timeout=rpc_timeout + self._outer_margin,
            )
        except CircuitBreakerOpenError as exc:
            raise MilvusAdapterError(str(exc), code=BREAKER_OPEN_CODE, detail={"op": op}) from exc
        except BaseException as exc:
            if isinstance(exc, MilvusAdapterError):
                code, message = exc.code, exc.message
            else:
                code, message = classify_error(exc)
            if code in _CONNECTION_CLASS:
                await self._invalidate_client()
            if self._breaker is not None:
                self._breaker.record_failure(code)
            log.warning(
                "milvus_call_failed",
                op=op,
                code=code,
                uri=self._uri,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                error=message[:200],
            )
            if isinstance(exc, MilvusAdapterError):
                raise
            raise MilvusAdapterError(message, code=code, detail={"op": op}) from exc

        if self._breaker is not None:
            self._breaker.record_success()
        return result

    # --- surface ----------------------------------------------------------
    async def ping(self, *, force: bool = False, rpc_timeout_s: float | None = None) -> ProbeResult:
        """Cheapest liveness check over gRPC. Never raises.

        `rpc_timeout_s` overrides the configured deadline for this call. The
        /overview fan-out needs it: its 6s global budget is smaller than
        MILVUS_RPC_TIMEOUT_S plus the thread-handoff margin, so without a
        shorter per-branch deadline one slow probe would eat the whole budget.
        """
        started = time.perf_counter()
        try:
            version = await self._call(
                "get_server_version",
                lambda c, t: c.get_server_version(timeout=t),
                force=force,
                rpc_timeout_s=rpc_timeout_s,
            )
        except MilvusAdapterError as exc:
            return ProbeResult(
                reachable=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
                error_message=exc.message,
            )
        return ProbeResult(
            reachable=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            server_version=str(version),
        )

    async def get_server_version(self, *, force: bool = False) -> str:
        return str(
            await self._call(
                "get_server_version", lambda c, t: c.get_server_version(timeout=t), force=force
            )
        )

    async def list_collections(
        self, *, force: bool = False, rpc_timeout_s: float | None = None
    ) -> list[str]:
        result = await self._call(
            "list_collections",
            lambda c, t: c.list_collections(timeout=t),
            force=force,
            rpc_timeout_s=rpc_timeout_s,
        )
        return list(result or [])

    async def describe_collection(self, name: str, *, force: bool = False) -> dict[str, Any]:
        """Schema: fields, dimension, primary key, auto_id."""
        raw = await self._call(
            "describe_collection", lambda c, t: c.describe_collection(name, timeout=t), force=force
        )
        return _normalise_description(name, dict(raw or {}))

    async def get_collection_stats(self, name: str, *, force: bool = False) -> dict[str, Any]:
        raw = await self._call(
            "get_collection_stats",
            lambda c, t: c.get_collection_stats(name, timeout=t),
            force=force,
        )
        stats = dict(raw or {})
        row_count = stats.get("row_count")
        try:
            row_count = int(row_count) if row_count is not None else None
        except (TypeError, ValueError):
            row_count = None
        return {"collection_name": name, "row_count": row_count, "raw": stats}

    async def list_indexes(self, name: str, *, force: bool = False) -> list[str]:
        result = await self._call(
            "list_indexes", lambda c, t: c.list_indexes(name, timeout=t), force=force
        )
        return list(result or [])

    async def describe_index(
        self, name: str, index_name: str, *, force: bool = False
    ) -> dict[str, Any]:
        raw = await self._call(
            "describe_index",
            lambda c, t: c.describe_index(name, index_name, timeout=t),
            force=force,
        )
        return dict(raw or {})

    async def get_load_state(self, name: str, *, force: bool = False) -> str:
        """Load state as a plain string: Loaded, NotLoad, NotExist, Loading.

        Note this does NOT raise for an unknown collection -- Milvus answers
        ``NotExist`` instead, so callers must check the value rather than rely
        on an exception.
        """
        raw = await self._call(
            "get_load_state", lambda c, t: c.get_load_state(name, timeout=t), force=force
        )
        state = raw.get("state") if isinstance(raw, dict) else raw
        text = str(getattr(state, "name", state) or "Unknown")
        return text.split(".")[-1]

    async def deep_probe(self, *, force: bool = False) -> ProbeResult:
        """Connect + list_collections + describe one collection. Never raises.

        See the module docstring for why this exists and what it still cannot
        see (object-store outages).
        """
        started = time.perf_counter()
        checks: dict[str, Any] = {
            "connect": False,
            "list_collections": False,
            "describe_collection": None,
        }
        version: str | None = None

        try:
            version = str(
                await self._call(
                    "get_server_version", lambda c, t: c.get_server_version(timeout=t), force=force
                )
            )
            checks["connect"] = True

            collections = await self._call(
                "list_collections", lambda c, t: c.list_collections(timeout=t), force=force
            )
            collections = list(collections or [])
            checks["list_collections"] = True
            checks["collection_count"] = len(collections)

            if collections:
                probe_target = collections[0]
                await self._call(
                    "describe_collection",
                    lambda c, t: c.describe_collection(probe_target, timeout=t),
                    force=force,
                )
                checks["describe_collection"] = True
                checks["probed_collection"] = probe_target
            else:
                # No collection to describe is not a failure; record that the
                # deepest check was skipped rather than implying it passed.
                checks["describe_collection"] = None
                checks["probed_collection"] = None
        except MilvusAdapterError as exc:
            return ProbeResult(
                reachable=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
                error_message=exc.message,
                server_version=version,
                checks=checks,
            )

        return ProbeResult(
            reachable=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            server_version=version,
            checks=checks,
        )


def _normalise_description(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten describe_collection into the fields the API actually needs."""
    fields = raw.get("fields") or []
    dimension: int | None = None
    primary_key: str | None = None
    vector_field: str | None = None

    for f in fields:
        params = f.get("params") or {}
        if params.get("dim") is not None and dimension is None:
            try:
                dimension = int(params["dim"])
                vector_field = f.get("name")
            except (TypeError, ValueError):
                pass
        if f.get("is_primary"):
            primary_key = f.get("name")

    return {
        "collection_name": raw.get("collection_name", name),
        "description": raw.get("description"),
        "auto_id": raw.get("auto_id"),
        "num_partitions": raw.get("num_partitions"),
        "primary_key": primary_key,
        "vector_field": vector_field,
        "dimension": dimension,
        "fields": [
            {
                "name": f.get("name"),
                "type": str(f.get("type")),
                "is_primary": bool(f.get("is_primary", False)),
                "params": f.get("params") or {},
            }
            for f in fields
        ],
        "raw": raw,
    }
