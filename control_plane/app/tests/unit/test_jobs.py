"""Job guard and scheduler wiring.

The theme is that a control plane which silently stopped observing is worse
than one that is obviously down, so these tests are about failure containment
rather than happy paths.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from structlog.testing import capture_logs

from app.api.errors import PostgresUnavailableError
from app.config import Settings
from app.jobs.base import guarded, is_postgres_down
from app.jobs.health_job import JOB_ID as HEALTH_JOB_ID
from app.jobs.retention_job import JOB_ID as RETENTION_JOB_ID
from app.jobs.scheduler import JOB_DEFAULTS, create_scheduler, register_jobs
from app.jobs.snapshot_job import JOB_ID as SNAPSHOT_JOB_ID


def settings() -> Settings:
    return Settings(_env_file=None)


def op_error(message: str = "connection refused") -> OperationalError:
    return OperationalError(f"SELECT 1 -- {message}", {}, Exception(message))


# --- nothing escapes a job ------------------------------------------------
async def test_arbitrary_exception_never_escapes() -> None:
    """APScheduler keeps running, but a job that always raises never works."""

    @guarded("boom")
    async def job() -> str:
        raise RuntimeError("unexpected")

    assert await job() is None


async def test_success_passes_the_return_value_through() -> None:
    @guarded("fine")
    async def job() -> int:
        return 7

    assert await job() == 7


async def test_cancellation_is_not_swallowed() -> None:
    """CancelledError is a BaseException, not an Exception, and must pass
    through: swallowing it would make scheduler shutdown hang."""

    @guarded("cancellable")
    async def job() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await job()


# --- Postgres down is a skip, not a failure -------------------------------
# capture_logs, not caplog: structlog is configured with its own factory and
# only routes through stdlib once configure_logging() has run, which it has not
# in a unit test. caplog would silently see nothing and the assertions would
# pass vacuously.
async def test_postgres_down_logs_warning_not_error() -> None:
    @guarded("health_job")
    async def job() -> None:
        raise op_error()

    with capture_logs() as entries:
        assert await job() is None

    levels = [e["log_level"] for e in entries]
    assert "warning" in levels, "an unreachable database must still be reported"
    assert "error" not in levels, (
        "a stopped Postgres is an expected condition during a drill, not an error"
    )
    skipped = next(e for e in entries if e["log_level"] == "warning")
    assert skipped["event"] == "job_skipped_postgres_unreachable"
    assert skipped["job"] == "health_job"


@pytest.mark.parametrize(
    "exc",
    [
        op_error(),
        InterfaceError("stmt", {}, Exception("connection already closed")),
        PostgresUnavailableError(),
        ConnectionRefusedError("no route"),
    ],
)
def test_connection_faults_are_recognised(exc: BaseException) -> None:
    assert is_postgres_down(exc) is True


def test_real_sql_bugs_are_not_mistaken_for_an_outage() -> None:
    """IntegrityError is a bug. Retrying it forever as if Postgres were down
    would hide it completely."""
    assert is_postgres_down(IntegrityError("stmt", {}, Exception("duplicate key"))) is False
    assert is_postgres_down(ValueError("nope")) is False


async def test_real_bug_is_logged_with_a_traceback() -> None:
    @guarded("snapshot_job")
    async def job() -> None:
        raise IntegrityError("stmt", {}, Exception("duplicate key"))

    with capture_logs() as entries:
        assert await job() is None

    errors = [e for e in entries if e["log_level"] == "error"]
    assert errors, "a bug must be reported at ERROR, not hidden as a skip"
    assert errors[0]["event"] == "job_failed"
    assert errors[0].get("exc_info") is True, "a bug must keep its traceback"


# --- scheduler wiring -----------------------------------------------------
def test_all_three_jobs_are_registered() -> None:
    scheduler = create_scheduler(settings())
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {HEALTH_JOB_ID, SNAPSHOT_JOB_ID, RETENTION_JOB_ID}


def test_job_defaults_prevent_stacking() -> None:
    """A probe slower than its interval must not queue behind itself."""
    assert JOB_DEFAULTS["max_instances"] == 1
    assert JOB_DEFAULTS["coalesce"] is True


async def test_defaults_are_applied_to_every_job() -> None:
    """Must be asserted after start().

    APScheduler leaves `max_instances`/`coalesce` unset on a queued job and
    only merges the scheduler's job_defaults when it is really added, so a
    pre-start assertion raises AttributeError. Checking JOB_DEFAULTS alone
    would assert on a constant rather than on what the jobs actually run with.
    """
    scheduler = create_scheduler(settings())
    scheduler.start()
    try:
        jobs = scheduler.get_jobs()
        assert len(jobs) == 3
        for job in jobs:
            assert job.max_instances == 1, f"{job.id} could stack"
            assert job.coalesce is True, f"{job.id} would replay a backlog"
    finally:
        scheduler.shutdown(wait=False)


def test_health_interval_and_jitter_come_from_settings() -> None:
    cfg = Settings(_env_file=None, cp_health_interval_s=15)
    scheduler = create_scheduler(cfg)
    health = next(j for j in scheduler.get_jobs() if j.id == HEALTH_JOB_ID)
    assert health.trigger.interval.total_seconds() == 15
    # Jitter decorrelates probes without making the stated interval a lie.
    assert 0 < (health.trigger.jitter or 0) <= 5


async def test_registering_twice_does_not_duplicate_jobs() -> None:
    """A double start must not double the event stream.

    Asserted before AND after start: APScheduler defers `replace_existing`
    until the scheduler runs, so only the second assertion would hold if
    register_jobs relied on that flag alone.
    """
    cfg = settings()
    scheduler = create_scheduler(cfg)
    register_jobs(scheduler, cfg)
    register_jobs(scheduler, cfg)

    ids = [job.id for job in scheduler.get_jobs()]
    assert len(ids) == len(set(ids)) == 3, "queued duplicates misreport in the logs"

    scheduler.start()
    try:
        running_ids = [job.id for job in scheduler.get_jobs()]
        assert len(running_ids) == len(set(running_ids)) == 3
    finally:
        scheduler.shutdown(wait=False)


def test_retention_runs_daily_not_on_an_interval() -> None:
    scheduler = create_scheduler(settings())
    job = next(j for j in scheduler.get_jobs() if j.id == RETENTION_JOB_ID)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "3" and fields["minute"] == "17"


def test_snapshot_starts_offset_from_health() -> None:
    """Both hit Milvus and Docker; colliding every cycle is avoidable."""
    scheduler = create_scheduler(settings())
    by_id: dict[str, Any] = {j.id: j for j in scheduler.get_jobs()}
    health_start = by_id[HEALTH_JOB_ID].trigger.start_date
    snapshot_start = by_id[SNAPSHOT_JOB_ID].trigger.start_date
    assert (snapshot_start - health_start).total_seconds() >= 5
