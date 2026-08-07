"""Daily retention purge.

The three time-series tables are trimmed to `CP_RETENTION_DAYS`. `events` is
kept 4x longer, on purpose: it is the incident trail, it grows by a handful of
rows per incident rather than thousands per day, and an audit record that
expires on the same schedule as the routine samples it explains is of no use
when someone asks what happened last week.
"""

from __future__ import annotations

import datetime as dt

from app.config import Settings, get_settings
from app.jobs.base import guarded, job_session
from app.logging_conf import get_logger
from app.repositories import (
    CollectionSnapshotRepository,
    ComponentStatusRepository,
    EventRepository,
    HealthCheckRepository,
)

log = get_logger("retention_job")

JOB_ID = "retention_job"

# Events outlive samples by this factor.
EVENT_RETENTION_MULTIPLIER = 4


@guarded(JOB_ID)
async def run_retention_job(settings: Settings | None = None) -> dict[str, int]:
    """Delete aged rows. Returns per-table delete counts."""
    cfg = settings or get_settings()
    now = dt.datetime.now(dt.UTC)
    sample_cutoff = now - dt.timedelta(days=cfg.cp_retention_days)
    event_cutoff = now - dt.timedelta(days=cfg.cp_retention_days * EVENT_RETENTION_MULTIPLIER)

    async with job_session() as session:
        deleted = {
            "health_checks": await HealthCheckRepository(session).purge_older_than(sample_cutoff),
            "component_status": await ComponentStatusRepository(session).purge_older_than(
                sample_cutoff
            ),
            "collection_snapshots": await CollectionSnapshotRepository(session).purge_older_than(
                sample_cutoff
            ),
            "events": await EventRepository(session).purge_older_than(event_cutoff),
        }

    total = sum(deleted.values())
    if total:
        log.info(
            "retention_purged",
            total=total,
            retention_days=cfg.cp_retention_days,
            event_retention_days=cfg.cp_retention_days * EVENT_RETENTION_MULTIPLIER,
            **deleted,
        )
    else:
        log.debug("retention_nothing_to_purge", retention_days=cfg.cp_retention_days)
    return deleted
