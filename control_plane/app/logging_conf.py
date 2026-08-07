"""Structured JSON logging and per-request context.

Every log line is a JSON object on stdout, which is what Docker captures and
what the /logs endpoint later reads back. Each line carries the request_id, so
a single request can be followed across the concurrent fan-out in /overview.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

# Endpoints polled by container healthchecks and the dashboard. Logging them at
# INFO would bury real traffic: /healthz alone is hit every 15 seconds forever.
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})


def configure_logging(level: str = "INFO") -> None:
    """Route structlog AND stdlib logging through one JSON renderer.

    Third-party libraries (APScheduler, SQLAlchemy, docker, uvicorn) log via
    the stdlib. Configuring structlog alone leaves those lines as plain text,
    so a stream that is supposed to be newline-delimited JSON ends up mixed --
    and one unparseable line is enough to break a log shipper or the /logs
    viewer. ProcessorFormatter feeds stdlib records through the same processor
    chain, so every line on stdout is a JSON object regardless of origin.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Applied to structlog events and to foreign stdlib records alike, so both
    # carry the same timestamp, level and bound request_id.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # uvicorn ships its own handlers; drop them or every request is logged
    # twice, once plain and once as JSON.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # Our own middleware emits the access line, with request_id and duration.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request_id and emit one access line per request.

    The id is taken from an inbound X-Request-ID when present so a caller's
    trace id survives, and echoed back on the response either way.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id

        log = get_logger("http")
        started = time.perf_counter()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            # Echo the id back so a caller can correlate their request with
            # these log lines without having to guess.
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        except Exception:
            # Anything reaching here escaped the exception handlers. Log the
            # traceback; the access line below still records the timing.
            log.exception(
                "unhandled_exception",
                method=request.method,
                path=request.url.path,
            )
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            if request.url.path not in _QUIET_PATHS:
                log.info(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=duration_ms,
                )
            else:
                log.debug(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=duration_ms,
                )
            structlog.contextvars.clear_contextvars()
