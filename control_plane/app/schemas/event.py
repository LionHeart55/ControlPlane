"""Event (audit / incident trail) schemas."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventRead(BaseModel):
    """One row from the incident trail.

    Rows exist only for transitions, never per poll, so the count here is
    small and every row means something happened.
    """

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": [
                {
                    "id": 3,
                    "cluster_id": "202b9ea6-a927-44ec-98d4-46f7ceff4a08",
                    "event_type": "health_transition",
                    "severity": "error",
                    "message": "cluster 'local-standalone' health healthy -> unavailable",
                    "payload": {
                        "from": "healthy",
                        "to": "unavailable",
                        "error_code": "MILVUS_UNREACHABLE",
                        "rule": 1,
                    },
                    "created_at": "2026-08-07T08:18:14Z",
                }
            ]
        },
    )

    id: int
    cluster_id: uuid.UUID | None = None
    event_type: str = Field(
        description="cluster_registered, health_transition, component_state_change, "
        "dependency_failure, dependency_recovered, breaker_opened or breaker_closed."
    )
    severity: str = Field(description="info, warning or error.")
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: dt.datetime
