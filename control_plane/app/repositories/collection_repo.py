"""Append-only per-collection statistics."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CollectionSnapshot
from app.repositories.base import execute_delete


class CollectionSnapshotRepository:
    """Persistence for `collection_snapshots`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_collection_snapshot(
        self,
        *,
        cluster_id: uuid.UUID,
        collection_name: str,
        row_count: int | None = None,
        num_partitions: int | None = None,
        dimension: int | None = None,
        index_type: str | None = None,
        metric_type: str | None = None,
        is_loaded: bool | None = None,
        observed_at: dt.datetime | None = None,
        raw: dict[str, Any] | None = None,
    ) -> CollectionSnapshot:
        row = CollectionSnapshot(
            cluster_id=cluster_id,
            collection_name=collection_name,
            row_count=row_count,
            num_partitions=num_partitions,
            dimension=dimension,
            index_type=index_type,
            metric_type=metric_type,
            is_loaded=is_loaded,
            observed_at=observed_at or dt.datetime.now(dt.UTC),
            raw=raw or {},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def latest_per_collection(self, cluster_id: uuid.UUID) -> list[CollectionSnapshot]:
        """Newest snapshot for each collection. See component_repo on DISTINCT ON."""
        stmt = (
            sa.select(CollectionSnapshot)
            .where(CollectionSnapshot.cluster_id == cluster_id)
            .distinct(CollectionSnapshot.collection_name)
            .order_by(
                CollectionSnapshot.collection_name.asc(),
                CollectionSnapshot.observed_at.desc(),
                CollectionSnapshot.id.desc(),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def latest_for_collection(
        self, cluster_id: uuid.UUID, collection_name: str
    ) -> CollectionSnapshot | None:
        result = await self._session.execute(
            sa.select(CollectionSnapshot)
            .where(
                CollectionSnapshot.cluster_id == cluster_id,
                CollectionSnapshot.collection_name == collection_name,
            )
            .order_by(CollectionSnapshot.observed_at.desc(), CollectionSnapshot.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def history(
        self,
        cluster_id: uuid.UUID,
        *,
        collection_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        since: dt.datetime | None = None,
    ) -> list[CollectionSnapshot]:
        stmt = sa.select(CollectionSnapshot).where(CollectionSnapshot.cluster_id == cluster_id)
        if collection_name is not None:
            stmt = stmt.where(CollectionSnapshot.collection_name == collection_name)
        if since is not None:
            stmt = stmt.where(CollectionSnapshot.observed_at >= since)
        stmt = (
            stmt.order_by(CollectionSnapshot.observed_at.desc(), CollectionSnapshot.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def purge_older_than(self, cutoff: dt.datetime) -> int:
        return await execute_delete(
            self._session,
            sa.delete(CollectionSnapshot).where(CollectionSnapshot.observed_at < cutoff),
        )
