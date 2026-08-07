"""Component (container / pod) schemas."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cluster import ClusterRead
from app.schemas.common import Envelope

_RUNNING_EXAMPLE: dict[str, Any] = {
    "component_name": "milvus-standalone",
    "kind": "container",
    "runtime_id": "9f3c1a2b4d5e",
    "image": "milvusdb/milvus:v2.6.20",
    "state": "running",
    "health": "healthy",
    "restart_count": 0,
    "started_at": "2026-08-07T08:21:22Z",
    "exit_code": None,
}

_MISSING_EXAMPLE: dict[str, Any] = {
    "component_name": "milvus-minio",
    "kind": "container",
    "runtime_id": None,
    "image": None,
    "state": "missing",
    "health": None,
    "restart_count": 0,
    "started_at": None,
    "exit_code": None,
}


class ComponentRead(BaseModel):
    """Observed (or absent) state of one component.

    An expected component with no container is reported as `state: "missing"`,
    never omitted. Omitting it would turn an outage into an empty row in the
    UI, which reads as "fine".
    """

    model_config = ConfigDict(json_schema_extra={"examples": [_RUNNING_EXAMPLE, _MISSING_EXAMPLE]})

    component_name: str
    kind: str = "container"
    runtime_id: str | None = None
    image: str | None = None
    state: str = Field(description="running, exited, paused, restarting, dead or missing.")
    health: str | None = Field(
        default=None, description="Docker healthcheck verdict, absent when the image defines none."
    )
    restart_count: int = 0
    started_at: dt.datetime | None = None
    exit_code: int | None = Field(
        default=None, description="Set only once a container has actually exited."
    )


class ComponentsLive(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "components": [_RUNNING_EXAMPLE, _MISSING_EXAMPLE],
                    "total": 2,
                    "running": 1,
                    "missing": 1,
                }
            ]
        }
    )

    components: list[ComponentRead]
    total: int
    running: int
    missing: int


class ComponentsEnvelope(Envelope[ComponentsLive]):
    cluster: ClusterRead | None = None
