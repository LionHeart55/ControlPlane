"""Live health probe, forced check, and the persisted time series."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.adapters.registry import (
    get_docker_adapter,
    get_metadata_store_adapter,
    get_metrics_adapter,
    get_milvus_adapter,
    get_object_store_adapter,
)
from app.api.deps import DbDep, SettingsDep
from app.api.envelope import load_cluster, resolve_live
from app.db.base import HealthStatus
from app.jobs.health_job import check_and_persist
from app.repositories import HealthCheckRepository
from app.schemas.common import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DegradedReason,
    ErrorResponse,
    LiveStatus,
    Page,
)
from app.schemas.health import HealthCheckRead, HealthEnvelope, LiveHealth
from app.services.health_service import (
    HealthSignals,
    HealthVerdict,
    aggregate_status,
    collect_signals,
)

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["health"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]

_ENVELOPE_NOTE = (
    "\n\n**Degradation envelope.** Always returns **200**, never a 5xx, however "
    "broken Milvus is -- an unreachable cluster is exactly what this endpoint "
    "exists to report.\n\n"
    "Note this route reads `live_status` slightly differently from the others, "
    "and deliberately. Elsewhere an unreachable dependency means `live: null`. "
    "Here, a probe that successfully determines *Milvus is down* is a "
    '**successful probe**: `live_status` stays `"ok"` because the answer is '
    "current, and the outage is reported inside `live` as "
    '`status: "unavailable"` with the rule number and error code. Nulling `live` '
    "would discard the very information this endpoint exists to deliver. "
    "`degraded_reason` is still populated whenever the cluster is not healthy, "
    "so a client can check that one field either way.\n\n"
    "This route also answers while **PostgreSQL is down**: `cluster` becomes "
    "null and the Milvus endpoint is resolved from the last-known-good cache, "
    "so you can still find out whether Milvus is up when the control plane's "
    "own database is not."
)


def to_live_health(verdict: HealthVerdict, signals: HealthSignals) -> LiveHealth:
    probe = signals.milvus
    return LiveHealth(
        status=verdict.status,
        rule=verdict.rule,
        milvus_reachable=bool(probe and probe.reachable),
        latency_ms=probe.latency_ms if probe else None,
        server_version=probe.server_version if probe else None,
        error_code=verdict.error_code,
        error_message=verdict.error_message,
        reasons=verdict.reasons,
        checks=verdict.checks,
    )


@router.get(
    "/health",
    response_model=HealthEnvelope,
    summary="Live health probe plus the last persisted check",
    description=(
        "Probes Milvus, Docker and the metrics endpoint concurrently and "
        "applies the six ordered aggregation rules. `live` is what is true "
        "now; `last_check` is what the scheduled job last stored -- comparing "
        'them distinguishes "just broke" from "has been broken".\n\n'
        "Honours the circuit breaker: after repeated failures the probe is "
        "short-circuited and reported as `unavailable` with code "
        "`BREAKER_OPEN`, rather than making every request wait for a timeout. "
        "The scheduled job bypasses the breaker so recovery is still detected." + _ENVELOPE_NOTE
    ),
)
async def get_health(
    cluster_id: ClusterId, session: DbDep, settings: SettingsDep
) -> HealthEnvelope:
    context = await load_cluster(session, cluster_id)

    async def probe() -> LiveHealth:
        signals = await collect_signals(
            milvus=get_milvus_adapter(context.endpoint_uri, settings),
            docker=get_docker_adapter(settings),
            metrics=get_metrics_adapter(
                context.metrics_uri or settings.milvus_metrics_uri, settings
            ),
            object_store=get_object_store_adapter(context.object_store_endpoint, settings),
            metadata_store=get_metadata_store_adapter(settings=settings),
            compose_project=context.compose_project,
            # Request path honours the breaker; the scheduled job does not.
            force=False,
            budget_s=max(settings.milvus_rpc_timeout_s * 2, 10.0),
        )
        verdict = aggregate_status(signals, expected_components=settings.cp_expected_components)
        return to_live_health(verdict, signals)

    # A probe that reports "unavailable" is a successful probe, so it is never
    # served from cache -- caching it would show a stale healthy verdict during
    # an outage, which is the one thing this endpoint must never do.
    outcome = await resolve_live(
        cluster_id=cluster_id, resource="health", fetch=probe, cacheable=False
    )

    last_check = None
    if context.postgres_available:
        row = await HealthCheckRepository(session).latest_for_cluster(cluster_id)
        last_check = HealthCheckRead.model_validate(row) if row else None

    # Three sources, in order of precedence: the probe could not be run at all;
    # the probe ran and found the cluster unhealthy; the database is gone.
    # Populating this from the verdict keeps GET /health consistent with
    # POST /health-check -- a client should be able to check one field to know
    # something is wrong, without also inspecting `live.status`.
    kwargs = outcome.envelope_kwargs()
    reason = kwargs.pop("degraded_reason")
    if reason is None and outcome.live is not None:
        reason = _reason_for_live(outcome.live)

    return HealthEnvelope(
        cluster=context.read,
        last_check=last_check,
        degraded_reason=reason or context.degraded_reason,
        **kwargs,
    )


@router.post(
    "/health-check",
    response_model=HealthEnvelope,
    summary="Force an immediate health check and persist it",
    description=(
        "Runs the same evaluation as the scheduled job, writes a "
        "`health_checks` row and updates the cluster -- and, exactly like the "
        "scheduled job, writes an `events` row **only if the status changed**. "
        "Calling this repeatedly during a steady outage produces one event, "
        "not one per call.\n\n"
        "Bypasses the circuit breaker: an explicit request to check now is a "
        "deliberate override of the backoff." + _ENVELOPE_NOTE
    ),
    responses={503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."}},
)
async def force_health_check(
    cluster_id: ClusterId, session: DbDep, settings: SettingsDep
) -> HealthEnvelope:
    context = await load_cluster(session, cluster_id)
    verdict, signals = await check_and_persist(
        cluster_id=cluster_id,
        name=context.name,
        endpoint_uri=context.endpoint_uri,
        metrics_uri=context.metrics_uri,
        compose_project=context.compose_project,
        settings=settings,
    )
    row = await HealthCheckRepository(session).latest_for_cluster(cluster_id)
    return HealthEnvelope(
        cluster=context.read,
        live=to_live_health(verdict, signals),
        # `ok` describes the freshness of the answer, not the verdict it
        # carries. The probe ran just now and returned a definite result, so
        # the answer is current even when that result is "Milvus is down" --
        # that is reported in `live.status` and `degraded_reason` instead.
        live_status=LiveStatus.OK,
        observed_at=dt.datetime.now(dt.UTC),
        stale=False,
        degraded_reason=_reason_for(verdict),
        last_check=HealthCheckRead.model_validate(row) if row else None,
    )


def _reason_for(verdict: HealthVerdict) -> DegradedReason | None:
    if verdict.status is HealthStatus.HEALTHY or verdict.error_code is None:
        return None
    return DegradedReason(
        code=verdict.error_code,
        message=verdict.error_message or "cluster is not healthy",
    )


def _reason_for_live(live: LiveHealth) -> DegradedReason | None:
    if live.status is HealthStatus.HEALTHY or live.error_code is None:
        return None
    return DegradedReason(
        code=live.error_code,
        message=live.error_message or "cluster is not healthy",
    )


@router.get(
    "/health-history",
    response_model=Page[HealthCheckRead],
    summary="Persisted health time series",
    description=(
        "Newest first, from the `health_checks` table written by the scheduled "
        "job every `CP_HEALTH_INTERVAL_S`. Rows are retained for "
        "`CP_RETENTION_DAYS`.\n\n"
        "Note this is the *sample* series -- one row per poll. For the much "
        "shorter list of moments where the status actually changed, use "
        "`/api/v1/events?event_type=health_transition`."
    ),
    responses={503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."}},
)
async def health_history(
    cluster_id: ClusterId,
    session: DbDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
    since: Annotated[
        dt.datetime | None, Query(description="Only checks at or after this time.")
    ] = None,
    status_filter: Annotated[
        HealthStatus | None, Query(alias="status", description="Filter by health status.")
    ] = None,
) -> Page[HealthCheckRead]:
    repo = HealthCheckRepository(session)
    rows = await repo.history(
        cluster_id, limit=limit, offset=offset, since=since, status=status_filter
    )
    total = await repo.count_history(cluster_id, since=since, status=status_filter)
    return Page[HealthCheckRead](
        items=[HealthCheckRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
