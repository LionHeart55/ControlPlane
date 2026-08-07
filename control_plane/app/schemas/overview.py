"""The dashboard's single aggregate call.

Seven sources fan out concurrently under one global budget. Each carries its
own status, so one dead dependency dims one panel instead of failing the page.
Partial results are always returned -- a 500 here would blank the entire
dashboard at the exact moment it is most useful.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cluster import ClusterRead
from app.schemas.collection import CollectionsLive
from app.schemas.common import DegradedReason, LiveStatus
from app.schemas.component import ComponentsLive
from app.schemas.event import EventRead
from app.schemas.health import LiveHealth
from app.schemas.logs import LogsLive
from app.schemas.metrics import MetricsLive


class OverviewSection[LiveT](BaseModel):
    """One panel's worth of data plus its own freshness.

    Same freshness vocabulary as the top-level envelope, deliberately: a client
    that can render one can render the other.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "data": None,
                    "status": "unavailable",
                    "observed_at": "2026-08-07T08:18:14Z",
                    "stale": False,
                    "degraded_reason": {
                        "code": "MILVUS_UNREACHABLE",
                        "message": "failed to connect to localhost:19530",
                        "since": None,
                    },
                    "duration_ms": 5012.4,
                }
            ]
        }
    )

    data: LiveT | None = None
    status: LiveStatus
    observed_at: dt.datetime
    stale: bool = False
    degraded_reason: DegradedReason | None = None
    duration_ms: float | None = Field(
        default=None, description="How long this branch took. Useful for spotting the slow one."
    )


class Overview(BaseModel):
    """Everything the dashboard needs, in one request."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "cluster": {
                        "id": "202b9ea6-a927-44ec-98d4-46f7ceff4a08",
                        "name": "local-standalone",
                        "last_health_status": "healthy",
                    },
                    "health": {"status": "ok", "stale": False, "duration_ms": 12.1},
                    "collections": {"status": "ok", "stale": False},
                    "metrics": {"status": "ok", "stale": False},
                    "components": {"status": "ok", "stale": False},
                    "logs": {"status": "ok", "stale": False},
                    "events": {"status": "ok", "stale": False},
                    "generated_at": "2026-08-07T08:22:55Z",
                    "budget_s": 6.0,
                    "duration_ms": 214.8,
                    "degraded": False,
                }
            ]
        }
    )

    cluster: ClusterRead | None = None
    health: OverviewSection[LiveHealth]
    collections: OverviewSection[CollectionsLive]
    metrics: OverviewSection[MetricsLive]
    components: OverviewSection[ComponentsLive]
    logs: OverviewSection[LogsLive]
    events: OverviewSection[list[EventRead]]

    generated_at: dt.datetime
    budget_s: float = Field(description="Global fan-out budget in seconds.")
    duration_ms: float
    degraded: bool = Field(
        description="True when any section is not `ok`. Lets a client decide "
        "whether to show a banner without inspecting every section."
    )
