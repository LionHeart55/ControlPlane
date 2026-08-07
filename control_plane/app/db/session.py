"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build the async engine.

    pool_pre_ping is the single most important option here. Pooled connections
    survive in the pool across a Postgres restart, but the sockets behind them
    are dead; without pre-ping the next checkout hands out a broken connection
    and every request fails until the process is restarted. With it, SQLAlchemy
    issues a cheap liveness check on checkout, transparently discards dead
    connections and reconnects.

    That is exactly what makes the WP-15 scenario D drill pass: stop
    cp-postgres, start it again, and the API recovers on its own with no
    restart and no manual intervention.

    pool_recycle=300 covers the other half of the problem: idle connections cut
    by a NAT or proxy timeout are retired before they are ever handed out.
    """
    cfg = settings or get_settings()
    return create_async_engine(
        cfg.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=300,
        future=True,
        echo=False,
    )


def get_engine() -> AsyncEngine:
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Process-wide session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session, rolling back on error and always closing.

    Deliberately does not swallow connection errors: callers decide whether an
    unreachable Postgres means 503 (metadata routes) or a degraded envelope.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close pooled connections. Called from the FastAPI lifespan shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
