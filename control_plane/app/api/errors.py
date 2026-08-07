"""Domain error hierarchy and the handlers that render it.

Every error response uses one envelope:

    {"error": {"code": "...", "message": "...", "detail": {...}}}

The central rule of this control plane is enforced here: **a dependency being
down must never produce a 5xx on a read endpoint.** Milvus, MinIO, etcd or the
Docker socket being unreachable is information the caller asked for, not a
server fault, so those come back 200 carrying a degradation envelope. Postgres
is the single exception -- without it there is no metadata to answer with at
all -- and it yields 503.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_conf import get_logger

log = get_logger("errors")

# Methods for which returning 200-with-degradation is meaningful. A failed
# write must never look like a success, so non-safe methods still get a 5xx.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


# --- exception hierarchy -------------------------------------------------
class DomainError(Exception):
    """Base class for expected, classified failures."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail = detail or {}


class NotFoundError(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(DomainError):
    status_code = 409
    code = "CONFLICT"


class ValidationError(DomainError):
    """Domain-level validation failure (distinct from pydantic's)."""

    status_code = 422
    code = "VALIDATION_ERROR"


class DependencyUnavailableError(DomainError):
    """An outbound dependency could not be reached.

    Carries the dependency name so handlers and the degradation envelope can
    tell the caller *what* is down, not merely that something is.
    """

    status_code = 200
    code = "DEPENDENCY_UNAVAILABLE"

    def __init__(
        self,
        message: str,
        *,
        dependency: str,
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        since: dt.datetime | None = None,
    ) -> None:
        super().__init__(message, code=code, detail=detail)
        self.dependency = dependency
        self.since = since


class PostgresUnavailableError(DependencyUnavailableError):
    """The control plane's own store is unreachable.

    The only dependency permitted to produce a 5xx, and only on routes that
    need stored metadata to answer at all.
    """

    status_code = 503
    code = "POSTGRES_UNAVAILABLE"

    def __init__(
        self, message: str = "control-plane database is unreachable", **kwargs: Any
    ) -> None:
        kwargs.setdefault("dependency", "postgres")
        super().__init__(message, **kwargs)


# --- response builders ---------------------------------------------------
def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def error_body(code: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "detail": detail or {}}}


def degradation_envelope(
    code: str,
    message: str,
    *,
    since: dt.datetime | None = None,
) -> dict[str, Any]:
    """The §4.1 envelope, for a dependency that could not be reached.

    Shape is identical to a successful response so the dashboard can render it
    without special-casing: `live` is null and `degraded_reason` says why.
    """
    return {
        "cluster": None,
        "live": None,
        "live_status": "unavailable",
        "observed_at": _now(),
        "stale": False,
        "degraded_reason": {
            "code": code,
            "message": message,
            "since": since.isoformat() if since else None,
        },
    }


# --- handlers ------------------------------------------------------------
async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    log.warning(
        "domain_error",
        code=exc.code,
        status=exc.status_code,
        path=request.url.path,
        message=exc.message,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.detail),
    )


async def dependency_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unreachable dependency without violating the no-5xx rule.

    Routes that compose stored and live data normally catch this themselves and
    build a full envelope with the cluster metadata attached. This is the
    backstop for anything that slips through: on a safe method it still answers
    200 with the degradation envelope rather than a 500.
    """
    assert isinstance(exc, DependencyUnavailableError)

    if isinstance(exc, PostgresUnavailableError):
        log.warning("postgres_unavailable", path=request.url.path, message=exc.message)
        return JSONResponse(
            status_code=503,
            content=error_body(exc.code, exc.message, {"dependency": exc.dependency}),
        )

    if request.method in SAFE_METHODS:
        log.warning(
            "dependency_unavailable",
            dependency=exc.dependency,
            code=exc.code,
            path=request.url.path,
        )
        return JSONResponse(
            status_code=200,
            content=degradation_envelope(exc.code, exc.message, since=exc.since),
        )

    # A write could not be performed: that is a genuine failure to report.
    log.error(
        "dependency_unavailable_on_write",
        dependency=exc.dependency,
        code=exc.code,
        path=request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content=error_body(exc.code, exc.message, {"dependency": exc.dependency}),
    )


async def sqlalchemy_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Map a dead database connection onto POSTGRES_UNAVAILABLE.

    Handling it centrally means routes do not each need a try/except to satisfy
    "Postgres down => 503 on metadata routes"; anything else from SQLAlchemy is
    a bug in our SQL and stays a 500.
    """
    if isinstance(exc, OperationalError | InterfaceError):
        log.warning("postgres_connection_error", path=request.url.path, error=str(exc)[:200])
        return JSONResponse(
            status_code=503,
            content=error_body(
                "POSTGRES_UNAVAILABLE",
                "control-plane database is unreachable",
                {"dependency": "postgres"},
            ),
        )

    log.exception("database_error", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content=error_body("DATABASE_ERROR", "an internal database error occurred"),
    )


async def request_validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content=error_body(
            "VALIDATION_ERROR",
            "request validation failed",
            {"errors": exc.errors()},
        ),
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette's HTTPException in the same envelope as everything else."""
    assert isinstance(exc, StarletteHTTPException)
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT"}.get(
        exc.status_code, "HTTP_ERROR"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, str(exc.detail)),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: log the traceback, return a sanitised body.

    The message is deliberately generic -- exception text leaks connection
    strings and internal paths. The request_id ties the response back to the
    full traceback in the logs.
    """
    log.exception("unhandled_error", path=request.url.path, error_type=type(exc).__name__)
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=500,
        content=error_body(
            "INTERNAL_ERROR",
            "an unexpected internal error occurred",
            {"request_id": request_id},
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Wire every handler. Order matters: most specific first."""
    app.add_exception_handler(PostgresUnavailableError, dependency_unavailable_handler)
    app.add_exception_handler(DependencyUnavailableError, dependency_unavailable_handler)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)
    app.add_exception_handler(DBAPIError, sqlalchemy_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
