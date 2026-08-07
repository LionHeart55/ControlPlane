"""Append-only component observations.

**Insert, not upsert.** The build spec asks for `upsert_component_status`, but
that contradicts its own schema: `component_status` is BIGSERIAL + `observed_at`
and is pruned by the retention job, which is an append-only time series. An
upsert would keep one row per component and make `purge_older_than` meaningless.
It would also destroy the only thing that lets `component_state_change` events
be written on transition alone -- with a single mutable row there is no previous
observation left to compare against.

So this appends, and `latest_per_component` reconstructs the current view.

The ORM model is imported as `ComponentStatusRow` throughout to keep it distinct
from the adapter's `ComponentStatus` dataclass, which is a live observation
rather than a stored row.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ComponentStatus as ComponentStatusRow
from app.repositories.base import execute_delete


class ComponentStatusRepository:
    """Persistence for `component_status`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_component_status(
        self,
        *,
        cluster_id: uuid.UUID,
        component_name: str,
        state: str,
        kind: str = "container",
        runtime_id: str | None = None,
        image: str | None = None,
        health: str | None = None,
        restart_count: int = 0,
        started_at: dt.datetime | None = None,
        observed_at: dt.datetime | None = None,
        raw: dict[str, Any] | None = None,
    ) -> ComponentStatusRow:
        row = ComponentStatusRow(
            cluster_id=cluster_id,
            component_name=component_name,
            kind=kind,
            runtime_id=runtime_id,
            image=image,
            state=state,
            health=health,
            restart_count=restart_count,
            started_at=started_at,
            observed_at=observed_at or dt.datetime.now(dt.UTC),
            raw=raw or {},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def latest_per_component(self, cluster_id: uuid.UUID) -> list[ComponentStatusRow]:
        """Newest observation for each component.

        DISTINCT ON is PostgreSQL-specific and matches
        ix_component_status_cluster_id_component_name_observed_at exactly, so
        this stays an index scan as the table grows between retention runs.
        The ORDER BY prefix must equal the DISTINCT ON column or PostgreSQL
        rejects the query outright.
        """
        stmt = (
            sa.select(ComponentStatusRow)
            .where(ComponentStatusRow.cluster_id == cluster_id)
            .distinct(ComponentStatusRow.component_name)
            .order_by(
                ComponentStatusRow.component_name.asc(),
                ComponentStatusRow.observed_at.desc(),
                ComponentStatusRow.id.desc(),
            )
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def history(
        self,
        cluster_id: uuid.UUID,
        *,
        component_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        since: dt.datetime | None = None,
    ) -> list[ComponentStatusRow]:
        stmt = sa.select(ComponentStatusRow).where(ComponentStatusRow.cluster_id == cluster_id)
        if component_name is not None:
            stmt = stmt.where(ComponentStatusRow.component_name == component_name)
        if since is not None:
            stmt = stmt.where(ComponentStatusRow.observed_at >= since)
        stmt = (
            stmt.order_by(ComponentStatusRow.observed_at.desc(), ComponentStatusRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def purge_older_than(self, cutoff: dt.datetime) -> int:
        return await execute_delete(
            self._session,
            sa.delete(ComponentStatusRow).where(ComponentStatusRow.observed_at < cutoff),
        )
