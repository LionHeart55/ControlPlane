"""Liveness and readiness of the control plane itself.

Mounted at the root, not under /api/v1: these describe the process, not the
resource model, and orchestrators expect them at fixed paths.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.db.session import get_engine
from app.schemas.common import ErrorResponse

router = APIRouter(tags=["system"])


class Liveness(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "ok", "service": "control-plane"}]}
    )

    status: str
    service: str


class DependencyCheck(BaseModel):
    ok: bool
    code: str | None = None
    error: str | None = None


class Readiness(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "status": "ready",
                    "checked_at": "2026-08-07T08:22:55Z",
                    "checks": {"postgres": {"ok": True, "code": None, "error": None}},
                },
                {
                    "status": "not_ready",
                    "checked_at": "2026-08-07T08:26:32Z",
                    "checks": {
                        "postgres": {
                            "ok": False,
                            "code": "POSTGRES_UNAVAILABLE",
                            "error": "ConnectionRefusedError: [Errno 61] Connection refused",
                        }
                    },
                },
            ]
        }
    )

    status: str
    checked_at: dt.datetime
    checks: dict[str, DependencyCheck]


async def check_database() -> tuple[bool, str | None]:
    """Return (reachable, error). Never raises."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:300]


@router.get(
    "/healthz",
    response_model=Liveness,
    summary="Liveness",
    description=(
        "Liveness of the control-plane process. Touches no dependency and "
        "returns 200 whenever the process can serve, so an orchestrator never "
        "restarts the API merely because Milvus or PostgreSQL is down. Use "
        "`/readyz` to decide whether to send traffic."
    ),
)
async def healthz() -> Liveness:
    return Liveness(status="ok", service="control-plane")


@router.get(
    "/readyz",
    response_model=Readiness,
    summary="Readiness",
    description=(
        "Readiness to serve metadata routes. Returns **503** with a "
        "machine-readable reason when PostgreSQL is unreachable. Milvus, MinIO "
        "and the Docker socket being down do NOT affect readiness -- those "
        "degrade individual routes via the standard envelope rather than "
        "taking the API out of service."
    ),
    responses={503: {"model": Readiness, "description": "PostgreSQL is unreachable."}},
)
async def readyz(response: Response) -> Readiness:
    reachable, error = await check_database()
    response.status_code = 200 if reachable else 503
    return Readiness(
        status="ready" if reachable else "not_ready",
        checked_at=dt.datetime.now(dt.UTC),
        checks={
            "postgres": DependencyCheck(
                ok=reachable,
                code=None if reachable else "POSTGRES_UNAVAILABLE",
                error=error,
            )
        },
    )


__all__ = ["ErrorResponse", "router"]
