"""APScheduler setup.

Job *defaults* live here; the jobs themselves are registered by WP-09. The
defaults are the important part:

  * ``max_instances=1`` -- a probe slower than its interval must not stack up
    behind itself. Without this a Milvus that takes 20s to time out on a 15s
    interval would accumulate overlapping runs until the pool is exhausted.
  * ``coalesce=True`` -- if the loop was blocked and several runs are overdue,
    run once and move on rather than replaying the backlog.
  * ``misfire_grace_time`` -- a run more than this far late is skipped instead
    of firing against stale assumptions.

Every job body is additionally expected to catch its own exceptions: an
unhandled error inside a job removes only that job in APScheduler, and a
scheduler that quietly loses its health job would break the whole demo while
still looking alive.
"""

from __future__ import annotations

import datetime as dt

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.jobs.health_job import JOB_ID as HEALTH_JOB_ID
from app.jobs.health_job import run_health_job
from app.jobs.retention_job import JOB_ID as RETENTION_JOB_ID
from app.jobs.retention_job import run_retention_job
from app.jobs.snapshot_job import JOB_ID as SNAPSHOT_JOB_ID
from app.jobs.snapshot_job import run_snapshot_job
from app.logging_conf import get_logger

log = get_logger("scheduler")

_scheduler: AsyncIOScheduler | None = None

JOB_DEFAULTS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": 30,
}


def _jitter_for(interval_s: int) -> int:
    """Spread firings so probes do not align into a periodic thundering herd.

    Capped at 5s and a fifth of the interval: enough to decorrelate, small
    enough that "every 15 seconds" stays true to anyone reading the logs.
    """
    return max(1, min(5, interval_s // 5))


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Build a scheduler with the three jobs attached."""
    scheduler = AsyncIOScheduler(job_defaults=JOB_DEFAULTS, timezone="UTC")
    register_jobs(scheduler, settings)
    log.debug(
        "scheduler_created",
        health_interval_s=settings.cp_health_interval_s,
        snapshot_interval_s=settings.cp_snapshot_interval_s,
    )
    return scheduler


def register_jobs(scheduler: AsyncIOScheduler, settings: Settings) -> AsyncIOScheduler:
    """Attach the three scheduled jobs. Idempotent.

    Two mechanisms, because `replace_existing` alone is not enough. Before the
    scheduler starts, APScheduler queues additions in `_pending_jobs` and defers
    the replace until `start()`, so a second call leaves six queued entries that
    only collapse to three later. They do collapse -- verified -- but until then
    `get_jobs()` and anything logging from it reports double. Removing our own
    ids first makes the intermediate state honest as well as the final one.

    Every job runs immediately on startup rather than waiting a full interval:
    with a 24h retention trigger the first purge would otherwise be a day away,
    and a control plane that reports nothing for the first 15 seconds after boot
    looks broken during a demo.
    """
    for job_id in (HEALTH_JOB_ID, SNAPSHOT_JOB_ID, RETENTION_JOB_ID):
        if scheduler.get_job(job_id) is not None:
            scheduler.remove_job(job_id)

    now = dt.datetime.now(dt.UTC)
    health_interval = settings.cp_health_interval_s
    snapshot_interval = settings.cp_snapshot_interval_s

    scheduler.add_job(
        run_health_job,
        trigger=IntervalTrigger(
            seconds=health_interval,
            jitter=_jitter_for(health_interval),
            start_date=now,
        ),
        id=HEALTH_JOB_ID,
        name="health probe and status aggregation",
        replace_existing=True,
    )
    scheduler.add_job(
        run_snapshot_job,
        trigger=IntervalTrigger(
            seconds=snapshot_interval,
            jitter=_jitter_for(snapshot_interval),
            # Offset so the first snapshot does not collide with the first
            # health probe; both hit Milvus and Docker.
            start_date=now + dt.timedelta(seconds=5),
        ),
        id=SNAPSHOT_JOB_ID,
        name="component and collection snapshot",
        replace_existing=True,
    )
    scheduler.add_job(
        run_retention_job,
        # Daily at 03:17 UTC rather than midnight: off the hour, where every
        # other cron job in the world already is.
        trigger=CronTrigger(hour=3, minute=17, timezone="UTC"),
        id=RETENTION_JOB_ID,
        name="retention purge",
        replace_existing=True,
    )
    log.info(
        "jobs_registered",
        health_interval_s=health_interval,
        snapshot_interval_s=snapshot_interval,
        retention_days=settings.cp_retention_days,
        jobs=[job.id for job in scheduler.get_jobs()],
    )
    return scheduler


def start_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Start the process-wide scheduler, tolerating a double start."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = create_scheduler(settings)
    _scheduler.start()
    log.info("scheduler_started", jobs=len(_scheduler.get_jobs()))
    return _scheduler


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


def shutdown_scheduler(wait: bool = False) -> None:
    """Stop the scheduler. Never raises: shutdown must not block process exit."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        if _scheduler.running:
            _scheduler.shutdown(wait=wait)
            log.info("scheduler_stopped")
    except Exception:
        log.exception("scheduler_shutdown_failed")
    finally:
        _scheduler = None
