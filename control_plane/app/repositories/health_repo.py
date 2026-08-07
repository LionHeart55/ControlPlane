"""Append-only health time series."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import HealthStatus
from app.db.models import HealthCheck
from app.repositories.base import execute_delete


class HealthCheckRepository:
    """Persistence for `health_checks`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_health_check(
        self,
        *,
        cluster_id: uuid.UUID,
        status: HealthStatus,
        milvus_reachable: bool,
        checked_at: dt.datetime | None = None,
        latency_ms: int | None = None,
        object_store_reachable: bool | None = None,
        metadata_store_reachable: bool | None = None,
        server_version: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        raw: dict[str, Any] | None = None,
    ) -> HealthCheck:
        row = HealthCheck(
            cluster_id=cluster_id,
            checked_at=checked_at or dt.datetime.now(dt.UTC),
            status=status,
            latency_ms=latency_ms,
            milvus_reachable=milvus_reachable,
            object_store_reachable=object_store_reachable,
            metadata_store_reachable=metadata_store_reachable,
            server_version=server_version,
            error_code=error_code,
            # Bound the stored text: an error message is a diagnostic, and a
            # driver traceback pasted verbatim every 15s would dwarf the table.
            error_message=error_message[:1000] if error_message else None,
            raw=raw or {},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def latest_for_cluster(self, cluster_id: uuid.UUID) -> HealthCheck | None:
        result = await self._session.execute(
            sa.select(HealthCheck)
            .where(HealthCheck.cluster_id == cluster_id)
            .order_by(HealthCheck.checked_at.desc(), HealthCheck.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def history(
        self,
        cluster_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
        since: dt.datetime | None = None,
        status: HealthStatus | None = None,
    ) -> list[HealthCheck]:
        stmt = sa.select(HealthCheck).where(HealthCheck.cluster_id == cluster_id)
        if since is not None:
            stmt = stmt.where(HealthCheck.checked_at >= since)
        if status is not None:
            stmt = stmt.where(HealthCheck.status == status)
        stmt = (
            stmt.order_by(HealthCheck.checked_at.desc(), HealthCheck.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_history(
        self,
        cluster_id: uuid.UUID,
        *,
        since: dt.datetime | None = None,
        status: HealthStatus | None = None,
    ) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(HealthCheck)
            .where(HealthCheck.cluster_id == cluster_id)
        )
        if since is not None:
            stmt = stmt.where(HealthCheck.checked_at >= since)
        if status is not None:
            stmt = stmt.where(HealthCheck.status == status)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def purge_older_than(self, cutoff: dt.datetime) -> int:
        return await execute_delete(
            self._session, sa.delete(HealthCheck).where(HealthCheck.checked_at < cutoff)
        )
