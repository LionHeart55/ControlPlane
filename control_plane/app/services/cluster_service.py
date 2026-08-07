"""Cluster registration and lifecycle.

Thin over `ClusterRepository`, but it owns two things a repository must not:
the uniqueness rule (409 on a duplicate name) and the `cluster_registered`
event. WP-10 mounts these behind routes.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, NotFoundError
from app.config import Settings, get_settings
from app.db.base import DeploymentStatus, DeploymentType
from app.db.models import Cluster
from app.logging_conf import get_logger
from app.repositories import ClusterRepository, EventRepository, EventType, Severity

log = get_logger("cluster_service")

DEFAULT_CLUSTER_NAME = "local-standalone"


async def register_cluster(
    session: AsyncSession,
    *,
    name: str,
    endpoint_uri: str,
    deployment_type: DeploymentType = DeploymentType.DOCKER_STANDALONE,
    metrics_uri: str | None = None,
    object_store_endpoint: str | None = None,
    compose_project: str | None = None,
    namespace: str | None = None,
    labels: dict[str, Any] | None = None,
) -> Cluster:
    """Register a cluster and record the registration event."""
    repo = ClusterRepository(session)
    existing = await repo.get_by_name(name)
    if existing is not None:
        raise ConflictError(
            f"a cluster named {name!r} is already registered",
            detail={"name": name, "cluster_id": str(existing.id)},
        )

    cluster = await repo.create(
        name=name,
        endpoint_uri=endpoint_uri,
        deployment_type=deployment_type,
        metrics_uri=metrics_uri,
        object_store_endpoint=object_store_endpoint,
        compose_project=compose_project,
        namespace=namespace,
        labels=labels,
    )
    await EventRepository(session).insert_event(
        cluster_id=cluster.id,
        event_type=EventType.CLUSTER_REGISTERED,
        severity=Severity.INFO,
        message=f"cluster {name!r} registered",
        payload={
            "name": name,
            "endpoint_uri": endpoint_uri,
            "deployment_type": str(deployment_type),
        },
    )
    log.info("cluster_registered", cluster_id=str(cluster.id), name=name, uri=endpoint_uri)
    return cluster


async def get_cluster(session: AsyncSession, cluster_id: uuid.UUID) -> Cluster:
    cluster = await ClusterRepository(session).get(cluster_id)
    if cluster is None or cluster.deployment_status is DeploymentStatus.DELETED:
        raise NotFoundError(
            f"no cluster with id {cluster_id}", detail={"cluster_id": str(cluster_id)}
        )
    return cluster


async def list_clusters(
    session: AsyncSession, *, include_deleted: bool = False, limit: int = 100, offset: int = 0
) -> list[Cluster]:
    return await ClusterRepository(session).list(
        include_deleted=include_deleted, limit=limit, offset=offset
    )


async def delete_cluster(session: AsyncSession, cluster_id: uuid.UUID) -> Cluster:
    """Soft delete, so the incident trail keeps pointing at a real row."""
    cluster = await get_cluster(session, cluster_id)
    return await ClusterRepository(session).soft_delete(cluster)


async def ensure_default_cluster(
    session: AsyncSession, settings: Settings | None = None
) -> Cluster | None:
    """Register the cluster described by .env if nothing is registered yet.

    Beyond the letter of WP-09, and deliberately so. Every scheduled job is a
    no-op with an empty `clusters` table, so without this a fresh `make up`
    produces an API that runs, logs nothing useful, and shows a blank dashboard
    until someone finds the registration endpoint. The quickstart in the build
    spec never mentions such a step, and the definition of done requires the
    dashboard to be populated straight after `make up`.

    Only ever creates the row when the table is empty, so it cannot resurrect a
    cluster someone deliberately deleted, and it never overwrites a
    hand-registered one.
    """
    cfg = settings or get_settings()
    repo = ClusterRepository(session)
    if await repo.list(include_deleted=True, limit=1):
        return None

    cluster = await register_cluster(
        session,
        name=DEFAULT_CLUSTER_NAME,
        endpoint_uri=cfg.milvus_uri,
        deployment_type=DeploymentType.DOCKER_STANDALONE,
        metrics_uri=cfg.milvus_metrics_uri,
        object_store_endpoint=cfg.minio_endpoint,
        compose_project=cfg.compose_project_name,
        labels={"source": "bootstrap", "managed_by": "control-plane"},
    )
    log.info("default_cluster_bootstrapped", cluster_id=str(cluster.id), name=cluster.name)
    return cluster
