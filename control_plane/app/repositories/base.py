"""Shared repository helpers."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def execute_delete(session: AsyncSession, stmt: Delete) -> int:
    """Run a DELETE and return the row count.

    `AsyncSession.execute` is typed as returning `Result`, which has no
    `rowcount`; DML actually yields a `CursorResult`, which does. The cast
    records that rather than silencing it with an ignore comment.
    """
    result = cast("CursorResult[Any]", await session.execute(stmt))
    return int(result.rowcount or 0)
