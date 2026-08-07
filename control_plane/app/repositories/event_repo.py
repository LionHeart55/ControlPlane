"""The audit and incident trail.

This table is what makes the reliability requirement demonstrable, so it earns
one rule the other repositories do not have: rows are written only on
transition. The repository cannot enforce that -- callers decide -- but the
vocabulary below is deliberately narrow so an accidental per-poll write is
obvious in review.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event
from app.repositories.base import execute_delete


class EventType(enum.StrEnum):
    """The fixed vocabulary from the schema spec."""

    CLUSTER_REGISTERED = "cluster_registered"
    HEALTH_TRANSITION = "health_transition"
    COMPONENT_STATE_CHANGE = "component_state_change"
    DEPENDENCY_FAILURE = "dependency_failure"
    DEPENDENCY_RECOVERED = "dependency_recovered"
    BREAKER_OPENED = "breaker_opened"
    BREAKER_CLOSED = "breaker_closed"


class Severity(enum.StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventRepository:
    """Persistence for `events`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_event(
        self,
        *,
        event_type: EventType | str,
        severity: Severity | str,
        message: str,
        cluster_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        created_at: dt.datetime | None = None,
    ) -> Event:
        row = Event(
            cluster_id=cluster_id,
            event_type=str(event_type),
            severity=str(severity),
            message=message,
            payload=payload or {},
            created_at=created_at or dt.datetime.now(dt.UTC),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_events(
        self,
        *,
        cluster_id: uuid.UUID | None = None,
        event_type: EventType | str | None = None,
        severity: Severity | str | None = None,
        since: dt.datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Event]:
        stmt = sa.select(Event)
        if cluster_id is not None:
            stmt = stmt.where(Event.cluster_id == cluster_id)
        if event_type is not None:
            stmt = stmt.where(Event.event_type == str(event_type))
        if severity is not None:
            stmt = stmt.where(Event.severity == str(severity))
        if since is not None:
            stmt = stmt.where(Event.created_at >= since)
        stmt = stmt.order_by(Event.created_at.desc(), Event.id.desc()).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_events(
        self,
        *,
        cluster_id: uuid.UUID | None = None,
        event_type: EventType | str | None = None,
        since: dt.datetime | None = None,
    ) -> int:
        stmt = sa.select(sa.func.count()).select_from(Event)
        if cluster_id is not None:
            stmt = stmt.where(Event.cluster_id == cluster_id)
        if event_type is not None:
            stmt = stmt.where(Event.event_type == str(event_type))
        if since is not None:
            stmt = stmt.where(Event.created_at >= since)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def purge_older_than(self, cutoff: dt.datetime) -> int:
        return await execute_delete(
            self._session, sa.delete(Event).where(Event.created_at < cutoff)
        )
