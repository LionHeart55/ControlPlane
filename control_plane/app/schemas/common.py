"""Shared response shapes: the degradation envelope, pagination, errors.

The envelope is the contract that makes the whole API safe to render. A
dashboard reading it never has to special-case a failure: the shape is
identical whether the dependency answered, answered a while ago, or is down.
Only the values change.

    live_status = "ok"           live is current
    live_status = "stale"        live is real data, but old -- `observed_at`
                                 is when it was true, and `stale` is true so
                                 the UI can dim it
    live_status = "unavailable"  live is null and `degraded_reason` says why

Showing an old number as though it were current is worse than showing none, so
"stale" is a distinct state rather than a quiet fallback.
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class LiveStatus(enum.StrEnum):
    """Freshness of the `live` half of a response."""

    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class DegradedReason(BaseModel):
    """Why live data is missing or stale."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "code": "MILVUS_UNREACHABLE",
                    "message": "failed to connect to localhost:19530",
                    "since": "2026-08-07T08:18:14Z",
                }
            ]
        }
    )

    code: str = Field(description="Stable machine-readable code, safe to switch on.")
    message: str = Field(description="Human-readable detail. Do not parse.")
    since: dt.datetime | None = Field(
        default=None, description="When the dependency was first observed failing, if known."
    )


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """The error envelope used by every non-2xx response."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "POSTGRES_UNAVAILABLE",
                        "message": "control-plane database is unreachable",
                        "detail": {"dependency": "postgres"},
                    }
                }
            ]
        }
    )

    error: ErrorDetail


class Envelope[LiveT](BaseModel):
    """Stored metadata plus live data, with the freshness of the live half.

    `cluster` is null when PostgreSQL is unreachable but the live probe still
    worked -- the health route is required to answer in that case rather than
    503, because the moment you most need to know whether Milvus is up is
    exactly when the control plane's own database has fallen over.
    """

    cluster: Any | None = Field(
        default=None, description="Stored metadata. Null if PostgreSQL is unreachable."
    )
    live: LiveT | None = Field(
        default=None, description="Live data. Null when the dependency is unavailable."
    )
    live_status: LiveStatus
    observed_at: dt.datetime = Field(
        description="When `live` was actually observed. For stale data this is the "
        "original observation time, not now."
    )
    stale: bool = Field(default=False, description="True when `live` is older than the TTL.")
    degraded_reason: DegradedReason | None = None


class Page[ItemT](BaseModel):
    """A page of results plus the total matching the filter."""

    items: list[ItemT]
    total: int = Field(description="Total rows matching the filter, ignoring limit/offset.")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PaginationParams(BaseModel):
    """Query parameters shared by every list endpoint."""

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)
