"""Container log schemas."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cluster import ClusterRead
from app.schemas.common import Envelope

_LINE_EXAMPLE: dict[str, Any] = {
    "timestamp": "2026-08-07T08:21:41.882Z",
    "stream": "stdout",
    "message": "[server] Milvus Proxy successfully started",
}


class LogLineRead(BaseModel):
    """One log line.

    `timestamp` is null for a continuation line -- a wrapped stack trace, say.
    Such a line keeps its position directly after the line it continues rather
    than being given a timestamp it never had, which would tear the trace apart
    when sorted.
    """

    model_config = ConfigDict(json_schema_extra={"examples": [_LINE_EXAMPLE]})

    timestamp: dt.datetime | None = None
    stream: str = Field(description="stdout or stderr.")
    message: str


class LogsLive(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "component": "milvus-standalone",
                    "lines": [_LINE_EXAMPLE],
                    "count": 1,
                    "truncated": False,
                }
            ]
        }
    )

    component: str
    lines: list[LogLineRead]
    count: int
    truncated: bool = Field(
        default=False, description="True when the requested line count hit the server-side cap."
    )


class LogsEnvelope(Envelope[LogsLive]):
    cluster: ClusterRead | None = None
