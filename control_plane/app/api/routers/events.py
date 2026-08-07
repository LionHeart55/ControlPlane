"""The incident and audit trail."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DbDep
from app.repositories import EventRepository
from app.schemas.common import DEFAULT_LIMIT, MAX_LIMIT, ErrorResponse, Page
from app.schemas.event import EventRead

router = APIRouter(prefix="/events", tags=["events"])


@router.get(
    "",
    response_model=Page[EventRead],
    summary="Incident and audit trail",
    description=(
        "Newest first.\n\n"
        "**Rows are written only on transition, never per poll.** The health "
        "job runs every 15 seconds; if it appended here each time it would add "
        "roughly 5 800 rows a day and bury the handful that describe an actual "
        "incident. A ten-minute Milvus outage produces exactly two rows -- one "
        "going down, one coming back -- so this endpoint reads as a timeline "
        "of what happened rather than a log of what was sampled.\n\n"
        "Events outlive the sample tables by 4x: an audit record that expired "
        "on the same schedule as the routine samples it explains would be gone "
        "by the time anyone asked what happened last week."
    ),
    responses={503: {"model": ErrorResponse, "description": "PostgreSQL is unreachable."}},
)
async def list_events(
    session: DbDep,
    cluster_id: Annotated[uuid.UUID | None, Query(description="Restrict to one cluster.")] = None,
    event_type: Annotated[
        str | None,
        Query(
            description="e.g. `health_transition`, `component_state_change`, "
            "`cluster_registered`, `breaker_opened`."
        ),
    ] = None,
    severity: Annotated[str | None, Query(description="`info`, `warning` or `error`.")] = None,
    since: Annotated[
        dt.datetime | None, Query(description="Only events at or after this time.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[EventRead]:
    repo = EventRepository(session)
    rows = await repo.list_events(
        cluster_id=cluster_id,
        event_type=event_type,
        severity=severity,
        since=since,
        limit=limit,
        offset=offset,
    )
    total = await repo.count_events(cluster_id=cluster_id, event_type=event_type, since=since)
    return Page[EventRead](
        items=[EventRead.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
