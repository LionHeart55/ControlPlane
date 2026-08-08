"""Cluster registration and metadata. Pure PostgreSQL: no live probing here.

These are the only routes allowed to return 503, and only because they cannot
answer at all without the database.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import DbDep
from app.db.base import DeploymentStatus
from app.repositories import ClusterRepository
from app.schemas.cluster import ClusterCreate, ClusterRead, ClusterUpdate
from app.schemas.common import DEFAULT_LIMIT, MAX_LIMIT, ErrorResponse, Page
from app.services import cluster_service

router = APIRouter(prefix="/clusters", tags=["clusters"])

ClusterId = Annotated[uuid.UUID, Path(description="Cluster UUID.")]

_DEGRADATION_NOTE = (
    "\n\n**Degradation.** This route reads only from PostgreSQL, so it returns "
    "**503 POSTGRES_UNAVAILABLE** when the database is unreachable. It is one of "
    "the few that can: routes mixing stored and live data return 200 with a "
    "degradation envelope instead."
)


@router.get(
    "",
    response_model=Page[ClusterRead],
    summary="List registered clusters",
    description="Registered clusters, newest registration last." + _DEGRADATION_NOTE,
    responses={503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."}},
)
async def list_clusters(
    session: DbDep,
    status_filter: Annotated[
        DeploymentStatus | None,
        Query(
            alias="status",
            description="Filter by lifecycle status. Soft-deleted clusters are "
            "hidden unless you ask for them explicitly with `?status=deleted`.",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ClusterRead]:
    repo = ClusterRepository(session)
    rows = await repo.list(status=status_filter, limit=limit, offset=offset)
    total = await repo.count(status=status_filter)
    return Page[ClusterRead](
        items=[ClusterRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ClusterRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a cluster",
    description=(
        "Registers cluster metadata and writes a `cluster_registered` event. "
        "Names are unique; re-registering one returns **409**." + _DEGRADATION_NOTE
    ),
    responses={
        409: {"model": ErrorResponse, "description": "A cluster with that name exists."},
        503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."},
    },
)
async def register_cluster(payload: ClusterCreate, session: DbDep) -> ClusterRead:
    cluster = await cluster_service.register_cluster(
        session,
        name=payload.name,
        endpoint_uri=payload.endpoint_uri,
        deployment_type=payload.deployment_type,
        metrics_uri=payload.metrics_uri,
        object_store_endpoint=payload.object_store_endpoint,
        compose_project=payload.compose_project,
        namespace=payload.namespace,
        labels=payload.labels,
    )
    await session.commit()
    return ClusterRead.model_validate(cluster)


@router.get(
    "/{cluster_id}",
    response_model=ClusterRead,
    summary="Get cluster metadata",
    description=(
        "Stored metadata including `last_health_status` and "
        "`last_health_check_at` from the most recent scheduled check. For a "
        "live probe use `/clusters/{cluster_id}/health`." + _DEGRADATION_NOTE
    ),
    responses={
        404: {"model": ErrorResponse, "description": "No such cluster."},
        503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."},
    },
)
async def get_cluster(cluster_id: ClusterId, session: DbDep) -> ClusterRead:
    cluster = await cluster_service.get_cluster(session, cluster_id)
    return ClusterRead.model_validate(cluster)


@router.patch(
    "/{cluster_id}",
    response_model=ClusterRead,
    summary="Update mutable fields",
    description=(
        "Partial update. Omitted fields are left unchanged; `name` and "
        "`deployment_type` are immutable because the event trail refers to "
        "them." + _DEGRADATION_NOTE
    ),
    responses={
        404: {"model": ErrorResponse, "description": "No such cluster."},
        503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."},
    },
)
async def update_cluster(
    cluster_id: ClusterId, payload: ClusterUpdate, session: DbDep
) -> ClusterRead:
    cluster = await cluster_service.get_cluster(session, cluster_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        await ClusterRepository(session).update(cluster, **changes)
        await session.commit()
        # Required, not defensive. `updated_at` carries onupdate=now(), so the
        # UPDATE leaves that attribute expired and reading it needs a SELECT.
        # Serialising without this raises MissingGreenlet inside pydantic --
        # a 500 from an otherwise successful write.
        await session.refresh(cluster)
    return ClusterRead.model_validate(cluster)


@router.delete(
    "/{cluster_id}",
    response_model=ClusterRead,
    summary="Deregister a cluster (soft delete)",
    description=(
        "Sets `deployment_status` to `deleted` rather than removing the row. "
        "The incident trail in `events` references the cluster, and a hard "
        "delete would leave that history pointing at nothing. The cluster "
        "disappears from list results and scheduled jobs stop probing it." + _DEGRADATION_NOTE
    ),
    responses={
        404: {"model": ErrorResponse, "description": "No such cluster."},
        503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."},
    },
)
async def delete_cluster(cluster_id: ClusterId, session: DbDep) -> ClusterRead:
    cluster = await cluster_service.delete_cluster(session, cluster_id)
    await session.commit()
    # Same reason as PATCH: the soft delete is an UPDATE, so `updated_at` is
    # expired and must be reloaded before serialisation.
    await session.refresh(cluster)
    return ClusterRead.model_validate(cluster)
