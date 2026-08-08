"""Direct metadata-store probe.

etcd is where Milvus keeps every piece of metadata: collection schemas, segment
assignments, the timestamp allocator, and the session leases that keep the
embedded streaming node registered. The WP-15 drill showed how little slack
there is -- **Milvus survived exactly 26 seconds without etcd and then exited
with code 1**, after a `["Slow etcd operation save"] ["time spent"=10.0s]` on
`by-dev/kv/gid/timestamp`. There is no read-only degraded mode to fall back to.

That is precisely why this probe is worth having separately: for those 26
seconds Milvus still answers `:9091/healthz` with 200 and still serves gRPC, so
every existing signal says healthy while the cluster is already doomed. Probing
etcd directly turns that window into a warning instead of a surprise.

No etcd client library is needed. etcd 3.5 serves `/health` over plain HTTP on
the client port and returns `{"health":"true","reason":""}`; verified against
the running cluster. httpx is already a dependency, whereas the Python etcd
clients are heavy, poorly maintained, or both.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.adapters.minio_client import StoreProbeResult
from app.config import Settings, get_settings
from app.logging_conf import get_logger

log = get_logger("metadata_store")

PROBE_TIMEOUT_S = 3.0

METADATA_STORE_UNREACHABLE = "METADATA_STORE_UNREACHABLE"
METADATA_STORE_UNHEALTHY = "METADATA_STORE_UNHEALTHY"


@dataclass(frozen=True)
class _Endpoint:
    scheme: str
    host: str


def _split(raw: str) -> _Endpoint:
    if "://" in raw:
        parsed = urlsplit(raw)
        return _Endpoint(parsed.scheme, parsed.netloc)
    return _Endpoint("http", raw)


class MetadataStoreAdapter:
    """Probes the etcd cluster Milvus depends on."""

    def __init__(self, endpoint: str | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        parsed = _split(endpoint or self._settings.etcd_endpoint)
        self._scheme = parsed.scheme
        self._host = parsed.host
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self._host}"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=PROBE_TIMEOUT_S)
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def probe(self) -> StoreProbeResult:
        """GET /health. Never raises.

        etcd distinguishes "I am not answering" from "I am answering and I am
        not healthy" -- typically no quorum, or the alarm list is non-empty.
        Both are fatal for Milvus, but only the second tells you the cluster is
        up and has decided it cannot serve, so they get different codes.
        """
        started = time.perf_counter()
        url = f"{self.base_url}/health"

        try:
            response = await self._get_client().get(url)
        except (httpx.HTTPError, OSError) as exc:
            return StoreProbeResult(
                reachable=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=METADATA_STORE_UNREACHABLE,
                error_message=f"cannot reach {url}: {exc}"[:300],
                detail={"endpoint": self.base_url},
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        detail: dict[str, Any] = {"endpoint": self.base_url, "status": response.status_code}

        # etcd answers 503 with a body when unhealthy, so the body is parsed
        # regardless of status rather than only on 200.
        healthy = False
        reason = ""
        try:
            body = json.loads(response.text)
            # The field is the STRING "true", not a boolean.
            healthy = str(body.get("health", "")).lower() == "true"
            reason = str(body.get("reason", "") or "")
        except (json.JSONDecodeError, AttributeError):
            detail["body"] = response.text[:120]

        if healthy and response.status_code == 200:
            return StoreProbeResult(reachable=True, latency_ms=elapsed, detail=detail)

        return StoreProbeResult(
            reachable=False,
            latency_ms=elapsed,
            error_code=METADATA_STORE_UNHEALTHY,
            error_message=(
                f"etcd reports unhealthy (HTTP {response.status_code})"
                + (f": {reason}" if reason else "")
            ),
            detail=detail,
        )

    async def ping(self) -> bool:
        return (await self.probe()).reachable
