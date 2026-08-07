"""Alembic environment, async.

The URL comes from app.config.Settings rather than alembic.ini so that .env
remains the single source of truth and no credentials live in a committed file.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.base import Base

# Importing models has the side effect of registering every table on
# Base.metadata. Without it autogenerate sees an empty model set and cheerfully
# proposes dropping all five tables.
from app.db import models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = get_settings()


def _configure_kwargs() -> dict[str, object]:
    """Options shared by the offline and online paths."""
    return {
        "target_metadata": target_metadata,
        # Catch a column whose type drifts from the model, not just added and
        # dropped columns.
        "compare_type": True,
        # Server defaults are intentionally NOT compared. PostgreSQL normalises
        # them on the way in ('{}'::jsonb, now()), so a textual comparison
        # reports differences that do not exist and every autogenerate run
        # would produce noise.
        "compare_server_default": False,
    }


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (alembic upgrade head --sql)."""
    context.configure(
        url=settings.database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_configure_kwargs(),  # type: ignore[arg-type]
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_configure_kwargs())  # type: ignore[arg-type]
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations against an async engine.

    NullPool: a migration process is short-lived and uses one connection, so
    pooling would only keep a socket open after the work is done.
    """
    connectable = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
