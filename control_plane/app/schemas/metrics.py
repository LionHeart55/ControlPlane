"""Curated metric schemas.

An allowlisted metric absent from the scrape is returned with
`value: null, available: false` and a reason -- never omitted. A dashboard that
hides what it cannot find goes quietly blank after a Milvus upgrade renames a
family, and nobody notices. A greyed tile is information.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cluster import ClusterRead
from app.schemas.common import Envelope

_METRIC_EXAMPLE: dict[str, Any] = {
    "name": "milvus_num_node",
    "label": "Milvus nodes",
    "unit": "nodes",
    "aggregation": "sum",
    "kind": "gauge",
    "value": 4.0,
    "available": True,
    "quantiles": None,
    "series_count": 4,
    "unavailable_reason": None,
    "description": "Number of Milvus nodes by role.",
}

_ABSENT_EXAMPLE: dict[str, Any] = {
    **_METRIC_EXAMPLE,
    "name": "milvus_querynode_entity_num",
    "label": "Loaded entities",
    "unit": "entities",
    "value": None,
    "available": False,
    "series_count": 0,
    "unavailable_reason": "not exposed by this Milvus version or not yet active",
    "description": "Entities held by query nodes. Needs a loaded collection.",
}


class MetricRead(BaseModel):
    """One allowlisted metric, present or not."""

    model_config = ConfigDict(json_schema_extra={"examples": [_METRIC_EXAMPLE, _ABSENT_EXAMPLE]})

    name: str
    label: str
    unit: str
    aggregation: str = Field(description="How label series were collapsed: sum, max, min or avg.")
    kind: str = Field(description="counter, gauge or histogram.")
    value: float | None = None
    available: bool
    quantiles: dict[str, float | None] | None = Field(
        default=None, description="Histograms only: p50/p99 computed from bucket counts."
    )
    series_count: int = 0
    unavailable_reason: str | None = None
    description: str = ""


class MetricsLive(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "metrics": [_METRIC_EXAMPLE, _ABSENT_EXAMPLE],
                    "families_scraped": 361,
                    "available_count": 11,
                    "allowlisted_count": 14,
                }
            ]
        }
    )

    metrics: list[MetricRead]
    families_scraped: int = Field(description="Total metric families in the raw scrape.")
    available_count: int
    allowlisted_count: int


class MetricsEnvelope(Envelope[MetricsLive]):
    cluster: ClusterRead | None = None
