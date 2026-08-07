"""Collection inventory: live from Milvus, merged with the last snapshot."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path

from app.adapters.registry import get_milvus_adapter
from app.api.deps import DbDep, SettingsDep
from app.api.envelope import load_cluster, resolve_live
from app.api.errors import NotFoundError
from app.db.models import CollectionSnapshot
from app.repositories import CollectionSnapshotRepository
from app.schemas.collection import (
    CollectionDetail,
    CollectionEnvelope,
    CollectionField,
    CollectionsEnvelope,
    CollectionsLive,
    CollectionSummary,
)
from app.schemas.common import ErrorResponse
from app.services.observability_service import (
    CollectionStat,
    collect_collection_stats,
    describe_one,
)

router = APIRouter(prefix="/clusters/{cluster_id}", tags=["collections"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]

_ENVELOPE_NOTE = (
    "\n\n**Degradation envelope.** Returns **200** even when Milvus is "
    "unreachable. If a recent result is cached it comes back with "
    '`live_status: "stale"`, `stale: true` and the original `observed_at`, so '
    "the UI can dim it rather than presenting old numbers as current. With "
    "nothing cached, `live` is null and `degraded_reason` explains why."
)


def summary_from_stat(stat: CollectionStat) -> CollectionSummary:
    return CollectionSummary(
        collection_name=stat.collection_name,
        row_count=stat.row_count,
        num_partitions=stat.num_partitions,
        dimension=stat.dimension,
        index_type=stat.index_type,
        metric_type=stat.metric_type,
        is_loaded=stat.is_loaded,
        load_state=stat.load_state,
        source="live",
        error_code=stat.error_code,
        error_message=stat.error_message,
    )


def summary_from_snapshot(row: CollectionSnapshot) -> CollectionSummary:
    return CollectionSummary(
        collection_name=row.collection_name,
        row_count=row.row_count,
        num_partitions=row.num_partitions,
        dimension=row.dimension,
        index_type=row.index_type,
        metric_type=row.metric_type,
        is_loaded=row.is_loaded,
        source="snapshot",
        observed_at=row.observed_at,
    )


@router.get(
    "/collections",
    response_model=CollectionsEnvelope,
    summary="List collections with statistics",
    description=(
        "Live collection list from Milvus, each with row count, dimension, "
        "index type, metric type and load state.\n\n"
        "Merged with the most recent stored snapshot: a collection Milvus no "
        'longer reports still appears, tagged `source: "snapshot"` with the '
        "`observed_at` of that snapshot. A collection that has vanished is "
        "information, and silently dropping it from the list would hide a "
        "deletion.\n\n"
        "A collection that fails to describe individually carries its own "
        "`error_code`; one bad collection does not fail the list." + _ENVELOPE_NOTE
    ),
)
async def list_collections(
    cluster_id: ClusterId, session: DbDep, settings: SettingsDep
) -> CollectionsEnvelope:
    context = await load_cluster(session, cluster_id)
    milvus = get_milvus_adapter(context.endpoint_uri, settings)

    stored: list[CollectionSnapshot] = []
    if context.postgres_available:
        stored = await CollectionSnapshotRepository(session).latest_per_collection(cluster_id)

    async def fetch() -> CollectionsLive:
        stats = await collect_collection_stats(milvus)
        live = [summary_from_stat(s) for s in stats]
        seen = {item.collection_name for item in live}
        extra = [summary_from_snapshot(row) for row in stored if row.collection_name not in seen]
        merged = [*live, *extra]
        return CollectionsLive(collections=merged, count=len(merged), snapshot_only=len(extra))

    outcome = await resolve_live(cluster_id=cluster_id, resource="collections", fetch=fetch)
    return CollectionsEnvelope(cluster=context.read, **outcome.envelope_kwargs())


@router.get(
    "/collections/{name}",
    response_model=CollectionEnvelope,
    summary="Describe one collection",
    description=(
        "Full schema (fields, primary key, vector field, dimension) plus index "
        "type, metric type, load state and row count." + _ENVELOPE_NOTE
    ),
    responses={404: {"model": ErrorResponse, "description": "No such collection."}},
)
async def get_collection(
    cluster_id: ClusterId,
    name: Annotated[str, Path(description="Collection name.")],
    session: DbDep,
    settings: SettingsDep,
) -> CollectionEnvelope:
    context = await load_cluster(session, cluster_id)
    milvus = get_milvus_adapter(context.endpoint_uri, settings)

    async def fetch() -> CollectionDetail:
        names = await milvus.list_collections()
        if name not in names:
            # 404 rather than an empty envelope: the collection genuinely does
            # not exist, which is a client error, not a degraded dependency.
            raise NotFoundError(
                f"no collection named {name!r}",
                detail={"collection": name, "known": names[:50]},
            )
        stat = await describe_one(milvus, name)
        schema = stat.raw.get("schema", {})
        return CollectionDetail(
            **summary_from_stat(stat).model_dump(),
            description=schema.get("description"),
            auto_id=schema.get("auto_id"),
            primary_key=schema.get("primary_key"),
            vector_field=schema.get("vector_field"),
            fields=[CollectionField(**f) for f in schema.get("fields", [])],
        )

    outcome = await resolve_live(cluster_id=cluster_id, resource=f"collection:{name}", fetch=fetch)
    return CollectionEnvelope(cluster=context.read, **outcome.envelope_kwargs())
