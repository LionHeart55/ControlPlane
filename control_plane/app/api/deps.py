"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.session import get_sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session.

    Connection failures are deliberately not caught here. SQLAlchemy raises
    OperationalError/InterfaceError lazily, on first use rather than at
    checkout, and the handler in app.api.errors maps those onto a 503
    POSTGRES_UNAVAILABLE. Swallowing them here would hand routes a session that
    fails later in a much less obvious place.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
