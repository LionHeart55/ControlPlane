"""Shared plumbing for scheduled jobs: session handling and the failure guard.

Two rules, both non-negotiable:

**No exception escapes a job.** APScheduler does not kill the scheduler on an
unhandled job error, but it does log it and move on -- and a job that raises
every time is a job that never does its work while the process still looks
alive. A control plane that silently stopped health-checking is worse than one
that is obviously down, so every job body is wrapped.

**An unreachable Postgres is a skip, not a failure.** During the Postgres
chaos drill the API must stay up and keep working. Jobs log at WARNING and
return; the next tick retries, and `pool_pre_ping` reconnects transparently
with no restart.
"""

from __future__ import annotations

import functools
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import PostgresUnavailableError
from app.db.session import get_sessionmaker
from app.logging_conf import get_logger

log = get_logger("jobs")

T = TypeVar("T")

# Deliberately narrow. Catching DBAPIError wholesale would also swallow
# IntegrityError and ProgrammingError -- real bugs that must not be mistaken
# for "Postgres is down" and quietly retried forever.
POSTGRES_DOWN: tuple[type[BaseException], ...] = (
    OperationalError,
    InterfaceError,
    PostgresUnavailableError,
    ConnectionRefusedError,
)


def is_postgres_down(exc: BaseException) -> bool:
    """Is this exception an unreachable database rather than a bad query?"""
    if isinstance(exc, POSTGRES_DOWN):
        return True
    # asyncpg surfaces some connection faults as a bare DBAPIError with
    # connection_invalidated set rather than as OperationalError.
    return isinstance(exc, DBAPIError) and bool(getattr(exc, "connection_invalidated", False))


@asynccontextmanager
async def job_session() -> AsyncIterator[AsyncSession]:
    """One transaction per unit of work: commit on success, roll back on error.

    Callers must not hold this open across an outbound probe. The health job
    takes a row lock inside it, and pinning a lock behind a five-second gRPC
    timeout would block every reader for the duration of an outage.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


JobBody = Callable[..., Awaitable[T]]
GuardedJob = Callable[..., Awaitable[T | None]]


def guarded(name: str) -> Callable[[JobBody[T]], GuardedJob[T]]:
    """Wrap a job body so nothing it raises can reach the scheduler."""

    def decorator(fn: JobBody[T]) -> GuardedJob[T]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T | None:
            started = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                if is_postgres_down(exc):
                    log.warning(
                        "job_skipped_postgres_unreachable",
                        job=name,
                        duration_ms=elapsed,
                        error=f"{type(exc).__name__}: {exc}"[:300],
                        note="will retry on the next tick; no restart needed",
                    )
                else:
                    # exception() keeps the traceback: an unexpected failure
                    # here is a bug and must not be reduced to one line.
                    log.exception("job_failed", job=name, duration_ms=elapsed)
                return None
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            log.debug("job_completed", job=name, duration_ms=elapsed)
            return result

        return wrapper

    return decorator
