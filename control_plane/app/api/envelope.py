"""Building the degradation envelope.

Every read route that mixes stored and live data goes through `resolve_live`.
Centralising it is the only way the "no 5xx because a dependency is down" rule
stays true as routes are added -- a per-route try/except would be forgotten
exactly once, on the route that mattered.

Three outcomes, one shape:

  * the call succeeded            -> `ok`,          `stale: false`
  * it failed but we have a cached
    value inside the stale window -> `stale`,       `stale: true`, original
                                     `observed_at`, and `degraded_reason` set
                                     so the client knows why it is old
  * it failed with nothing cached -> `unavailable`, `live: null`

Serving cached data always reports `stale: true`, even when the entry is still
inside the fresh TTL. The value may be seconds old, but the dependency did not
answer *now*, so it is not verifiable and must not be presented as current.

Bugs are deliberately NOT caught here. A `DependencyUnavailableError` is a
dependency being down and gets the envelope; anything else is our own defect
and must still surface as a 500, or the API would quietly serve nulls forever
while a broken code path went unnoticed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.cache import LastKnownGoodCache
from app.adapters.registry import get_cluster_cache, get_live_cache
from app.api.errors import DependencyUnavailableError, NotFoundError, PostgresUnavailableError
from app.db.base import DeploymentStatus
from app.db.session import DATABASE_UNREACHABLE
from app.logging_conf import get_logger
from app.repositories import ClusterRepository
from app.schemas.cluster import ClusterRead
from app.schemas.common import DegradedReason, LiveStatus
from app.schemas.overview import OverviewSection

log = get_logger("envelope")


@dataclass(frozen=True)
class LiveOutcome[T]:
    """The live half of an envelope, plus how fresh it is."""

    live: T | None
    live_status: LiveStatus
    observed_at: dt.datetime
    stale: bool
    degraded_reason: DegradedReason | None
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.live_status is LiveStatus.OK

    def envelope_kwargs(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "live_status": self.live_status,
            "observed_at": self.observed_at,
            "stale": self.stale,
            "degraded_reason": self.degraded_reason,
        }

    def as_section(self) -> OverviewSection[Any]:
        return OverviewSection(
            data=self.live,
            status=self.live_status,
            observed_at=self.observed_at,
            stale=self.stale,
            degraded_reason=self.degraded_reason,
            duration_ms=round(self.duration_ms, 1),
        )


async def resolve_live[T](
    *,
    cluster_id: Any,
    resource: str,
    fetch: Callable[[], Awaitable[T]],
    cache: LastKnownGoodCache | None = None,
    timeout_s: float | None = None,
    cacheable: bool = True,
) -> LiveOutcome[T]:
    """Run `fetch`, falling back to last-known-good on a dependency failure.

    `timeout_s` bounds this branch specifically. It matters for /overview,
    whose 6s global budget is smaller than the 5s RPC timeout plus the
    adapter's thread-handoff margin -- without a per-branch bound a single slow
    call could consume the whole budget.

    `cacheable=False` is for data that must never be served from cache. Logs
    are the case: a stale log tail looks like a live one and would send someone
    debugging the wrong minute.
    """
    store = cache if cache is not None else get_live_cache()
    started = time.perf_counter()

    try:
        awaitable = fetch()
        value = await (asyncio.wait_for(awaitable, timeout_s) if timeout_s else awaitable)
    except (DependencyUnavailableError, TimeoutError) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        reason = _reason_from(exc, timeout_s)
        if cacheable:
            entry = store.get_stale(cluster_id, resource)
            if entry is not None:
                log.info(
                    "serving_last_known_good",
                    resource=resource,
                    code=reason.code,
                    age_s=round(entry.age_s(), 1),
                )
                return LiveOutcome(
                    live=entry.value,
                    live_status=LiveStatus.STALE,
                    observed_at=entry.observed_at,
                    # Always true when served after a failure: the value was
                    # not verifiable at request time, whatever its age.
                    stale=True,
                    degraded_reason=reason,
                    duration_ms=elapsed,
                )
        return LiveOutcome(
            live=None,
            live_status=LiveStatus.UNAVAILABLE,
            observed_at=dt.datetime.now(dt.UTC),
            stale=False,
            degraded_reason=reason,
            duration_ms=elapsed,
        )

    elapsed = (time.perf_counter() - started) * 1000
    if cacheable:
        store.set(cluster_id, resource, value)
    return LiveOutcome(
        live=value,
        live_status=LiveStatus.OK,
        observed_at=dt.datetime.now(dt.UTC),
        stale=False,
        degraded_reason=None,
        duration_ms=elapsed,
    )


def _reason_from(exc: BaseException, timeout_s: float | None) -> DegradedReason:
    if isinstance(exc, DependencyUnavailableError):
        return DegradedReason(code=exc.code, message=exc.message, since=exc.since)
    return DegradedReason(
        code="UPSTREAM_TIMEOUT",
        message=f"the call exceeded its {timeout_s}s budget",
        since=None,
    )


# --- cluster resolution ---------------------------------------------------
@dataclass(frozen=True)
class ClusterContext:
    """Everything a route needs about a cluster, however it was obtained.

    `cluster` is None when PostgreSQL is unreachable and the endpoints came
    from cache. The endpoints are always populated, which is what lets a live
    probe still run.
    """

    cluster_id: uuid.UUID
    read: ClusterRead | None
    endpoint_uri: str
    metrics_uri: str | None
    object_store_endpoint: str | None
    compose_project: str | None
    name: str
    postgres_available: bool

    @property
    def degraded_reason(self) -> DegradedReason | None:
        if self.postgres_available:
            return None
        return DegradedReason(
            code="POSTGRES_UNAVAILABLE",
            message="cluster metadata served from cache; the database is unreachable",
            since=None,
        )


async def load_cluster(session: AsyncSession, cluster_id: uuid.UUID) -> ClusterContext:
    """Load a cluster, falling back to cache when PostgreSQL is down.

    On the happy path the row is cached as a side effect, which is what makes
    the fallback possible later. The spec requires
    `/clusters/{id}/health` to answer from live Milvus with `cluster: null`
    while Postgres is down -- but `endpoint_uri` lives in Postgres, so without
    this cache there would be nothing to probe.
    """
    cache = get_cluster_cache()
    try:
        cluster = await ClusterRepository(session).get(cluster_id)
    except DATABASE_UNREACHABLE as exc:
        entry = cache.get_stale(cluster_id, "cluster")
        if entry is None:
            raise PostgresUnavailableError() from exc
        cached: dict[str, Any] = entry.value
        log.warning(
            "cluster_metadata_from_cache",
            cluster_id=str(cluster_id),
            age_s=round(entry.age_s(), 1),
        )
        return ClusterContext(
            cluster_id=cluster_id,
            read=None,
            endpoint_uri=cached["endpoint_uri"],
            metrics_uri=cached.get("metrics_uri"),
            object_store_endpoint=cached.get("object_store_endpoint"),
            compose_project=cached.get("compose_project"),
            name=cached.get("name", str(cluster_id)),
            postgres_available=False,
        )

    if cluster is None or cluster.deployment_status is DeploymentStatus.DELETED:
        raise NotFoundError(
            f"no cluster with id {cluster_id}", detail={"cluster_id": str(cluster_id)}
        )

    cache.set(
        cluster_id,
        "cluster",
        {
            "name": cluster.name,
            "endpoint_uri": cluster.endpoint_uri,
            "metrics_uri": cluster.metrics_uri,
            "object_store_endpoint": cluster.object_store_endpoint,
            "compose_project": cluster.compose_project,
        },
    )
    return ClusterContext(
        cluster_id=cluster_id,
        read=ClusterRead.model_validate(cluster),
        endpoint_uri=cluster.endpoint_uri,
        metrics_uri=cluster.metrics_uri,
        object_store_endpoint=cluster.object_store_endpoint,
        compose_project=cluster.compose_project,
        name=cluster.name,
        postgres_available=True,
    )
