"""Cluster CRUD. Thin: no probing, no status decisions, no event writing.

The one non-obvious method is `get_for_update`, which takes a row lock. The
health job reads the previous status and writes the new one in a single
transaction, and the lock is what makes "exactly one health_transition event"
a database guarantee rather than a timing assumption.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import DeploymentStatus, DeploymentType, HealthStatus
from app.db.models import Cluster


class ClusterRepository:
    """Persistence for `clusters`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        name: str,
        endpoint_uri: str,
        deployment_type: DeploymentType,
        metrics_uri: str | None = None,
        object_store_endpoint: str | None = None,
        compose_project: str | None = None,
        namespace: str | None = None,
        milvus_version: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> Cluster:
        cluster = Cluster(
            name=name,
            endpoint_uri=endpoint_uri,
            deployment_type=deployment_type,
            metrics_uri=metrics_uri,
            object_store_endpoint=object_store_endpoint,
            compose_project=compose_project,
            namespace=namespace,
            milvus_version=milvus_version,
            labels=labels or {},
        )
        self._session.add(cluster)
        await self._session.flush()
        return cluster

    async def get(self, cluster_id: uuid.UUID) -> Cluster | None:
        return await self._session.get(Cluster, cluster_id)

    async def get_for_update(self, cluster_id: uuid.UUID) -> Cluster | None:
        """Fetch a cluster holding a row lock until the transaction ends.

        Serialises the read-compare-write that decides whether a status change
        is a transition. Without it, two writers could both observe the same
        previous status and both emit an event for one change.
        """
        result = await self._session.execute(
            sa.select(Cluster).where(Cluster.id == cluster_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Cluster | None:
        result = await self._session.execute(sa.select(Cluster).where(Cluster.name == name))
        return result.scalar_one_or_none()

    def _filtered(
        self, *, include_deleted: bool, status: DeploymentStatus | None
    ) -> sa.Select[Any]:
        stmt = sa.select(Cluster)
        if status is not None:
            # An explicit ?status=deleted must work, so the status filter wins
            # over the default hiding of soft-deleted rows.
            stmt = stmt.where(Cluster.deployment_status == status)
        elif not include_deleted:
            stmt = stmt.where(Cluster.deployment_status != DeploymentStatus.DELETED)
        return stmt

    async def list(
        self,
        *,
        include_deleted: bool = False,
        status: DeploymentStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Cluster]:
        stmt = (
            self._filtered(include_deleted=include_deleted, status=status)
            .order_by(Cluster.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count(
        self, *, include_deleted: bool = False, status: DeploymentStatus | None = None
    ) -> int:
        inner = self._filtered(include_deleted=include_deleted, status=status).subquery()
        result = await self._session.execute(sa.select(sa.func.count()).select_from(inner))
        return int(result.scalar_one())

    async def update(self, cluster: Cluster, **fields: Any) -> Cluster:
        """Assign whitelisted columns. Unknown keys are a programming error."""
        for key, value in fields.items():
            if not hasattr(cluster, key):
                raise AttributeError(f"clusters has no column {key!r}")
            setattr(cluster, key, value)
        await self._session.flush()
        return cluster

    async def apply_health(
        self,
        cluster: Cluster,
        *,
        status: HealthStatus,
        checked_at: dt.datetime,
        deployment_status: DeploymentStatus | None = None,
        milvus_version: str | None = None,
    ) -> Cluster:
        """Write the outcome of one health evaluation onto the cluster row.

        Deliberately dumb: the caller decides the status and the deployment
        mapping. `deployment_status=None` means "leave it alone", which is how
        an `unknown` verdict avoids overwriting a real lifecycle state with a
        guess.
        """
        cluster.last_health_status = status
        cluster.last_health_check_at = checked_at
        if deployment_status is not None:
            cluster.deployment_status = deployment_status
        # Never blank a known version because one probe could not read it.
        if milvus_version:
            cluster.milvus_version = milvus_version
        await self._session.flush()
        return cluster

    async def soft_delete(self, cluster: Cluster) -> Cluster:
        """Mark deleted rather than removing the row.

        `events.cluster_id` is ON DELETE SET NULL, so a hard delete would
        detach the incident history from the cluster it describes.
        """
        cluster.deployment_status = DeploymentStatus.DELETED
        await self._session.flush()
        return cluster
