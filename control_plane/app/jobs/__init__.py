"""Background jobs.

Three jobs, all sharing the same two guarantees (see `app.jobs.base`): no
exception escapes into the scheduler, and an unreachable Postgres is a logged
skip rather than a failure.
"""

from __future__ import annotations

from app.jobs.health_job import run_health_job
from app.jobs.retention_job import run_retention_job
from app.jobs.scheduler import (
    create_scheduler,
    get_scheduler,
    register_jobs,
    shutdown_scheduler,
    start_scheduler,
)
from app.jobs.snapshot_job import run_snapshot_job

__all__ = [
    "create_scheduler",
    "get_scheduler",
    "register_jobs",
    "run_health_job",
    "run_retention_job",
    "run_snapshot_job",
    "shutdown_scheduler",
    "start_scheduler",
]
