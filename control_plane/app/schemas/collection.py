"""Collection inventory schemas."""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.cluster import ClusterRead
from app.schemas.common import Envelope

_SUMMARY_EXAMPLE: dict[str, Any] = {
    "collection_name": "milvus_cp_demo",
    "row_count": 5000,
    "num_partitions": 1,
    "dimension": 384,
    "index_type": "HNSW",
    "metric_type": "COSINE",
    "is_loaded": True,
    "load_state": "Loaded",
    "source": "live",
    "observed_at": "2026-08-07T08:22:55Z",
    "error_code": None,
}


class CollectionSummary(BaseModel):
    """One collection, from a live call or from the last stored snapshot."""

    model_config = ConfigDict(json_schema_extra={"examples": [_SUMMARY_EXAMPLE]})

    collection_name: str
    row_count: int | None = None
    num_partitions: int | None = None
    dimension: int | None = None
    index_type: str | None = None
    metric_type: str | None = None
    is_loaded: bool | None = None
    load_state: str | None = None
    # Per-collection provenance, because a merged list can hold both: a
    # collection Milvus answered for is "live", one recovered from the last
    # snapshot is "snapshot" and may already be gone.
    source: str = Field(default="live", description='"live" or "snapshot".')
    observed_at: dt.datetime | None = None
    error_code: str | None = Field(
        default=None,
        description="Set when this individual collection could not be described. "
        "One bad collection does not fail the list.",
    )
    error_message: str | None = None


class CollectionField(BaseModel):
    name: str | None = None
    type: str | None = None
    is_primary: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class CollectionDetail(CollectionSummary):
    """A single collection, with its schema."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    **_SUMMARY_EXAMPLE,
                    "description": "demo collection",
                    "auto_id": False,
                    "primary_key": "id",
                    "vector_field": "embedding",
                    "fields": [
                        {"name": "id", "type": "DataType.INT64", "is_primary": True, "params": {}},
                        {
                            "name": "embedding",
                            "type": "DataType.FLOAT_VECTOR",
                            "is_primary": False,
                            "params": {"dim": 384},
                        },
                    ],
                }
            ]
        }
    )

    description: str | None = None
    auto_id: bool | None = None
    primary_key: str | None = None
    vector_field: str | None = None
    fields: list[CollectionField] = Field(default_factory=list)


class CollectionsLive(BaseModel):
    """The live half of the collections response."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"collections": [_SUMMARY_EXAMPLE], "count": 1, "snapshot_only": 0}]
        }
    )

    collections: list[CollectionSummary]
    count: int
    snapshot_only: int = Field(
        default=0,
        description="How many entries came from the stored snapshot rather than a "
        "live call, i.e. collections Milvus no longer reports.",
    )


class CollectionsEnvelope(Envelope[CollectionsLive]):
    cluster: ClusterRead | None = None


class CollectionEnvelope(Envelope[CollectionDetail]):
    cluster: ClusterRead | None = None
