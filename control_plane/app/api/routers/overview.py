"""The dashboard's single call: a concurrent fan-out over seven sources.

**Why `asyncio.wait` rather than `gather(..., return_exceptions=True)` inside a
`wait_for`.** The contract asks for both a 6s global timeout and "partial
results always returned", and those two cannot be satisfied by
`wait_for(gather(...))`: when the outer timeout fires it cancels the gather and
every branch is lost, including the six that had already finished. `asyncio.wait`
returns the completed set and lets us cancel only what is still running, which
is what "partial results" actually requires. `return_exceptions=True` semantics
are preserved by wrapping each branch so it cannot raise.

**Sub-budgets.** The global budget is 6s but `MILVUS_RPC_TIMEOUT_S` is 5s, and
the Milvus adapter adds a thread-handoff margin on top -- so a single slow probe
could consume the entire budget and starve every other panel. Each branch
therefore gets its own deadline, all strictly under the global, which becomes a
backstop that should never fire rather than the primary mechanism.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query
from sqlalchemy.exc import InterfaceError, OperationalError

from app.adapters.registry import get_docker_adapter, get_metrics_adapter, get_milvus_adapter
from app.api.deps import DbDep, SettingsDep
from app.api.envelope import LiveOutcome, load_cluster, resolve_live
from app.api.errors import PostgresUnavailableError
from app.api.routers.collections import summary_from_snapshot, summary_from_stat
from app.api.routers.health import to_live_health
from app.config import Settings
from app.db.session import get_sessionmaker
from app.logging_conf import get_logger
from app.repositories import CollectionSnapshotRepository, EventRepository
from app.schemas.collection import CollectionsLive
from app.schemas.common import DegradedReason, LiveStatus
from app.schemas.component import ComponentRead, ComponentsLive
from app.schemas.event import EventRead
from app.schemas.logs import LogLineRead, LogsLive
from app.schemas.metrics import MetricRead, MetricsLive
from app.schemas.overview import Overview, OverviewSection
from app.services.health_service import aggregate_status, collect_signals
from app.services.observability_service import collect_collection_stats

log = get_logger("overview")

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["overview"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]

GLOBAL_BUDGET_S = 6.0
# All strictly under the global, so the global never has to fire.
HEALTH_BUDGET_S = 4.0
COLLECTIONS_BUDGET_S = 4.5
METRICS_BUDGET_S = 3.0
COMPONENTS_BUDGET_S = 4.0
LOGS_BUDGET_S = 4.0
EVENTS_BUDGET_S = 2.5

# Milvus RPC deadline used inside the overview only. Below the configured 5s so
# a probe plus its margin still fits in HEALTH_BUDGET_S.
OVERVIEW_RPC_TIMEOUT_S = 2.5

OVERVIEW_LOG_LINES = 50
# Describing a collection is 4-5 round trips. Bounded so a cluster with
# hundreds of collections cannot blow the budget; the cap is reported rather
# than applied silently.
OVERVIEW_MAX_COLLECTIONS = 25


def _failed_section(code: str, message: str) -> OverviewSection[Any]:
    return OverviewSection(
        data=None,
        status=LiveStatus.UNAVAILABLE,
        observed_at=dt.datetime.now(dt.UTC),
        stale=False,
        degraded_reason=DegradedReason(code=code, message=message),
    )


async def _branch(
    name: str, run: Callable[[], Awaitable[LiveOutcome[Any]]]
) -> OverviewSection[Any]:
    """Run one branch so that nothing it does can fail the whole page.

    `resolve_live` already converts a dependency failure into an envelope, but
    it deliberately lets bugs propagate so they surface as 500s. Here that would
    blank the dashboard, so a bug degrades this one panel and is logged with a
    traceback instead.
    """
    try:
        return (await run()).as_section()
    except Exception as exc:
        log.exception("overview_branch_failed", branch=name)
        return _failed_section("INTERNAL_ERROR", f"{type(exc).__name__} while building {name}")


@router.get(
    "/overview",
    response_model=Overview,
    summary="Aggregate everything the dashboard needs",
    description=(
        "One request, seven sources fanned out concurrently: cluster metadata "
        "(PostgreSQL), health probe (Milvus gRPC), collections (Milvus), "
        "metrics (HTTP :9091), components (Docker socket), a 50-line log tail "
        "(Docker socket) and recent events (PostgreSQL).\n\n"
        "**Always 200, always partial-safe.** Each section carries its own "
        "`status`, `stale` flag and `degraded_reason`, so one dead dependency "
        "dims one panel instead of failing the page. `degraded` at the top "
        "level is true when any section is not `ok`, so a client can show a "
        "banner without inspecting every section.\n\n"
        f"Global budget {GLOBAL_BUDGET_S}s. Every branch has a shorter "
        "individual deadline, so a single slow dependency cannot starve the "
        "others; a branch that overruns is returned as `unavailable` with code "
        "`UPSTREAM_TIMEOUT` while the rest still render.\n\n"
        f"The collections panel describes at most {OVERVIEW_MAX_COLLECTIONS} "
        "collections to stay inside the budget; use `/collections` for the "
        "complete list."
    ),
)
async def get_overview(
    cluster_id: ClusterId,
    session: DbDep,
    settings: SettingsDep,
    log_component: Annotated[
        str, Query(description="Which component's log tail to include.")
    ] = "milvus-standalone",
) -> Overview:
    started = time.perf_counter()
    # Metadata first, outside the fan-out: every other branch needs the
    # endpoint URIs, so it is a dependency rather than a peer.
    context = await load_cluster(session, cluster_id)

    branches: dict[str, Callable[[], Coroutine[Any, Any, OverviewSection[Any]]]] = {
        "health": lambda: _branch("health", lambda: _health(context, settings, cluster_id)),
        "collections": lambda: _branch(
            "collections", lambda: _collections(context, settings, cluster_id)
        ),
        "metrics": lambda: _branch("metrics", lambda: _metrics(context, settings, cluster_id)),
        "components": lambda: _branch(
            "components", lambda: _components(context, settings, cluster_id)
        ),
        "logs": lambda: _branch(
            "logs", lambda: _logs(context, settings, cluster_id, log_component)
        ),
        "events": lambda: _branch("events", lambda: _events(cluster_id)),
    }

    tasks: dict[asyncio.Task[OverviewSection[Any]], str] = {
        asyncio.create_task(fn(), name=key): key for key, fn in branches.items()
    }
    done, pending = await asyncio.wait(tasks.keys(), timeout=GLOBAL_BUDGET_S)

    sections: dict[str, OverviewSection[Any]] = {}
    for task in done:
        sections[tasks[task]] = task.result()
    for task in pending:
        # Should not happen: every branch has a sub-budget below the global.
        # If it does, that is a bug worth seeing in the logs, not a silent gap.
        name = tasks[task]
        task.cancel()
        log.warning("overview_global_budget_exceeded", branch=name, budget_s=GLOBAL_BUDGET_S)
        sections[name] = _failed_section(
            "UPSTREAM_TIMEOUT", f"{name} exceeded the {GLOBAL_BUDGET_S}s global budget"
        )

    degraded = any(s.status is not LiveStatus.OK for s in sections.values())
    return Overview(
        cluster=context.read,
        health=sections["health"],
        collections=sections["collections"],
        metrics=sections["metrics"],
        components=sections["components"],
        logs=sections["logs"],
        events=sections["events"],
        generated_at=dt.datetime.now(dt.UTC),
        budget_s=GLOBAL_BUDGET_S,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        degraded=degraded or not context.postgres_available,
    )


# --- branches -------------------------------------------------------------
async def _health(context: Any, settings: Settings, cluster_id: uuid.UUID) -> LiveOutcome[Any]:
    async def fetch() -> Any:
        signals = await collect_signals(
            milvus=get_milvus_adapter(context.endpoint_uri, settings),
            docker=get_docker_adapter(settings),
            metrics=get_metrics_adapter(
                context.metrics_uri or settings.milvus_metrics_uri, settings
            ),
            compose_project=context.compose_project,
            force=False,
            budget_s=HEALTH_BUDGET_S - 0.5,
        )
        verdict = aggregate_status(signals, expected_components=settings.cp_expected_components)
        return to_live_health(verdict, signals)

    return await resolve_live(
        cluster_id=cluster_id,
        resource="health",
        fetch=fetch,
        cacheable=False,
        timeout_s=HEALTH_BUDGET_S,
    )


async def _collections(context: Any, settings: Settings, cluster_id: uuid.UUID) -> LiveOutcome[Any]:
    milvus = get_milvus_adapter(context.endpoint_uri, settings)

    async def fetch() -> CollectionsLive:
        names = await milvus.list_collections(rpc_timeout_s=OVERVIEW_RPC_TIMEOUT_S)
        capped = names[:OVERVIEW_MAX_COLLECTIONS]
        if len(names) > len(capped):
            log.info("overview_collections_capped", total=len(names), shown=len(capped))
        stats = await collect_collection_stats(milvus, names=capped)
        live = [summary_from_stat(s) for s in stats]

        seen = {item.collection_name for item in live}
        extra = []
        try:
            factory = get_sessionmaker()
            async with factory() as db:
                stored = await CollectionSnapshotRepository(db).latest_per_collection(cluster_id)
                extra = [summary_from_snapshot(r) for r in stored if r.collection_name not in seen]
        except (OperationalError, InterfaceError):
            # Live data is the point of this panel; losing the snapshot merge
            # costs only the "collection has vanished" annotation. Degrading
            # the whole panel because Postgres blinked would be worse.
            log.warning("overview_snapshot_merge_skipped", reason="postgres unreachable")

        merged = [*live, *extra]
        return CollectionsLive(collections=merged, count=len(merged), snapshot_only=len(extra))

    return await resolve_live(
        cluster_id=cluster_id,
        resource="collections",
        fetch=fetch,
        timeout_s=COLLECTIONS_BUDGET_S,
    )


async def _metrics(context: Any, settings: Settings, cluster_id: uuid.UUID) -> LiveOutcome[Any]:
    adapter = get_metrics_adapter(context.metrics_uri or settings.milvus_metrics_uri, settings)

    async def fetch() -> MetricsLive:
        snapshot = await adapter.scrape()
        return MetricsLive(
            metrics=[MetricRead(**m.as_dict()) for m in snapshot.metrics],
            families_scraped=snapshot.families_scraped,
            available_count=snapshot.available_count,
            allowlisted_count=len(snapshot.metrics),
        )

    return await resolve_live(
        cluster_id=cluster_id, resource="metrics", fetch=fetch, timeout_s=METRICS_BUDGET_S
    )


async def _components(context: Any, settings: Settings, cluster_id: uuid.UUID) -> LiveOutcome[Any]:
    docker = get_docker_adapter(settings)

    async def fetch() -> ComponentsLive:
        rows = await docker.list_components(compose_project=context.compose_project)
        components = [ComponentRead(**r.as_dict()) for r in rows]
        return ComponentsLive(
            components=components,
            total=len(components),
            running=sum(1 for c in components if c.state == "running"),
            missing=sum(1 for c in components if c.state == "missing"),
        )

    return await resolve_live(
        cluster_id=cluster_id, resource="components", fetch=fetch, timeout_s=COMPONENTS_BUDGET_S
    )


async def _logs(
    context: Any, settings: Settings, cluster_id: uuid.UUID, component: str
) -> LiveOutcome[Any]:
    docker = get_docker_adapter(settings)

    async def fetch() -> LogsLive:
        tail = await docker.tail_logs(component, lines=OVERVIEW_LOG_LINES)
        return LogsLive(
            component=component,
            lines=[LogLineRead(**line.as_dict()) for line in tail],
            count=len(tail),
            truncated=len(tail) >= OVERVIEW_LOG_LINES,
        )

    return await resolve_live(
        cluster_id=cluster_id,
        resource=f"logs:{component}",
        fetch=fetch,
        cacheable=False,
        timeout_s=LOGS_BUDGET_S,
    )


async def _events(cluster_id: uuid.UUID) -> LiveOutcome[Any]:
    # Its own session: AsyncSession is not safe for concurrent use, and the
    # request-scoped one may be in use by another branch.
    async def fetch() -> list[EventRead]:
        try:
            factory = get_sessionmaker()
            async with factory() as db:
                rows = await EventRepository(db).list_events(cluster_id=cluster_id, limit=20)
                return [EventRead.model_validate(r) for r in rows]
        except (OperationalError, InterfaceError) as exc:
            # Translate so resolve_live renders a proper degradation section
            # with POSTGRES_UNAVAILABLE, rather than letting it escape and be
            # reported as an internal error.
            raise PostgresUnavailableError() from exc

    return await resolve_live(
        cluster_id=cluster_id,
        resource="events",
        fetch=fetch,
        cacheable=False,
        timeout_s=EVENTS_BUDGET_S,
    )
