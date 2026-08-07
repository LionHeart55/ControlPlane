"""Health probe and history schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.base import HealthStatus
from app.schemas.cluster import ClusterRead
from app.schemas.common import Envelope

_LIVE_EXAMPLE: dict[str, Any] = {
    "status": "healthy",
    "rule": 5,
    "milvus_reachable": True,
    "latency_ms": 8,
    "server_version": "v2.6.20",
    "error_code": None,
    "error_message": None,
    "reasons": [],
    "checks": {"connect": True, "list_collections": True, "collection_count": 1},
}


class LiveHealth(BaseModel):
    """Outcome of a live probe."""

    model_config = ConfigDict(json_schema_extra={"examples": [_LIVE_EXAMPLE]})

    status: HealthStatus
    rule: int = Field(
        description="Which of the six ordered aggregation rules decided this status. "
        "1 unreachable, 2 deep-probe failed, 3 component down, 4 observability "
        "loss, 5 healthy, 6 could not evaluate."
    )
    milvus_reachable: bool
    latency_ms: int | None = None
    server_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    reasons: list[str] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)


class HealthCheckRead(BaseModel):
    """One persisted row from the health time series."""

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": 41,
                    "cluster_id": "202b9ea6-a927-44ec-98d4-46f7ceff4a08",
                    "checked_at": "2026-08-07T08:18:14Z",
                    "status": "unavailable",
                    "latency_ms": 5012,
                    "milvus_reachable": False,
                    "object_store_reachable": None,
                    "metadata_store_reachable": None,
                    "server_version": None,
                    "error_code": "MILVUS_UNREACHABLE",
                    "error_message": "failed to connect to localhost:19530",
                }
            ]
        },
    )

    id: int
    cluster_id: uuid.UUID
    checked_at: dt.datetime
    status: HealthStatus
    latency_ms: int | None = None
    milvus_reachable: bool
    # Null means "not probed this cycle", which is a different fact from false.
    object_store_reachable: bool | None = None
    metadata_store_reachable: bool | None = None
    server_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class HealthEnvelope(Envelope[LiveHealth]):
    """The standard envelope plus the most recent persisted check.

    `live` is what Milvus says right now; `last_check` is what the scheduled
    job last wrote. They differ during an outage, and seeing both is how you
    tell "just broke" from "has been broken".
    """

    cluster: ClusterRead | None = None
    last_check: HealthCheckRead | None = None
