"""Curated runtime metrics scraped from Milvus's Prometheus endpoint."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path

from app.adapters.registry import get_metrics_adapter
from app.api.deps import DbDep, SettingsDep
from app.api.envelope import load_cluster, resolve_live
from app.schemas.metrics import MetricRead, MetricsEnvelope, MetricsLive

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["metrics"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]


@router.get(
    "/metrics",
    response_model=MetricsEnvelope,
    summary="Curated runtime metrics",
    description=(
        "A curated allowlist scraped from `:9091/metrics`, not the full ~360 "
        "families. Counter series are summed and gauge series collapsed per "
        "metric; histograms are turned into p50/p99 by summing bucket counts "
        "across label series *before* computing the quantile.\n\n"
        "**Missing metrics are returned, never omitted.** An allowlisted "
        "metric absent from the scrape comes back with `value: null`, "
        "`available: false` and an `unavailable_reason`. Metric names drift "
        "between Milvus minor versions, and a dashboard that hides what it "
        "cannot find goes quietly blank after an upgrade with nobody noticing. "
        "A greyed tile is information.\n\n"
        "**Degradation envelope.** An unreachable metrics endpoint is a "
        "different failure from a missing metric: it returns **200** with "
        '`live: null` and `degraded_reason.code = "METRICS_UNAVAILABLE"`, or '
        "cached values with `stale: true` if a recent scrape is available."
    ),
)
async def get_metrics(
    cluster_id: ClusterId, session: DbDep, settings: SettingsDep
) -> MetricsEnvelope:
    context = await load_cluster(session, cluster_id)
    adapter = get_metrics_adapter(context.metrics_uri or settings.milvus_metrics_uri, settings)

    async def fetch() -> MetricsLive:
        snapshot = await adapter.scrape()
        return MetricsLive(
            metrics=[MetricRead(**m.as_dict()) for m in snapshot.metrics],
            families_scraped=snapshot.families_scraped,
            available_count=snapshot.available_count,
            allowlisted_count=len(snapshot.metrics),
        )

    outcome = await resolve_live(cluster_id=cluster_id, resource="metrics", fetch=fetch)
    return MetricsEnvelope(cluster=context.read, **outcome.envelope_kwargs())
