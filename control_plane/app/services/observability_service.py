"""Collection inventory and statistics.

Used by the snapshot job now and by the collections router in WP-10, so it
returns plain dataclasses rather than ORM rows or response models.

Describing a collection takes four or five separate gRPC round trips (schema,
stats, load state, index). Done sequentially at a five-second timeout each, a
handful of collections would exceed any sane budget, so the calls for one
collection run concurrently and the collections themselves run under a
semaphore -- bounded, because fanning out unbounded over hundreds of
collections would open hundreds of worker threads.

A per-collection failure is captured on that collection rather than raised. One
collection whose index has just been dropped must not blank the whole panel.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.adapters.milvus_client import MilvusAdapter, MilvusAdapterError
from app.logging_conf import get_logger

log = get_logger("observability")

# Concurrent collections in flight. Each holds up to ~4 worker threads, so this
# bounds the thread pool at a manageable ~16.
DEFAULT_CONCURRENCY = 4


@dataclass(frozen=True)
class CollectionStat:
    """One collection as the dashboard and the snapshot table want it."""

    collection_name: str
    row_count: int | None = None
    num_partitions: int | None = None
    dimension: int | None = None
    index_type: str | None = None
    metric_type: str | None = None
    is_loaded: bool | None = None
    load_state: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "collection_name": self.collection_name,
            "row_count": self.row_count,
            "num_partitions": self.num_partitions,
            "dimension": self.dimension,
            "index_type": self.index_type,
            "metric_type": self.metric_type,
            "is_loaded": self.is_loaded,
            "load_state": self.load_state,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


async def collect_collection_stats(
    milvus: MilvusAdapter,
    *,
    names: list[str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    force: bool = False,
) -> list[CollectionStat]:
    """Statistics for every collection. Raises only if listing itself fails."""
    collections = names if names is not None else await milvus.list_collections(force=force)
    if not collections:
        return []

    semaphore = asyncio.Semaphore(concurrency)

    async def one(name: str) -> CollectionStat:
        async with semaphore:
            return await describe_one(milvus, name, force=force)

    return list(await asyncio.gather(*(one(name) for name in collections)))


async def describe_one(milvus: MilvusAdapter, name: str, *, force: bool = False) -> CollectionStat:
    """Everything known about one collection. Never raises."""
    schema, stats, load_state, index = await asyncio.gather(
        milvus.describe_collection(name, force=force),
        milvus.get_collection_stats(name, force=force),
        milvus.get_load_state(name, force=force),
        _describe_first_index(milvus, name, force=force),
        return_exceptions=True,
    )

    error_code: str | None = None
    error_message: str | None = None
    for outcome in (schema, stats, load_state, index):
        if isinstance(outcome, MilvusAdapterError):
            error_code, error_message = outcome.code, outcome.message
            break
        if isinstance(outcome, BaseException):
            error_code = "COLLECTION_DESCRIBE_FAILED"
            error_message = f"{type(outcome).__name__}: {outcome}"
            break

    schema_d = schema if isinstance(schema, dict) else {}
    stats_d = stats if isinstance(stats, dict) else {}
    index_d = index if isinstance(index, dict) else {}
    state = load_state if isinstance(load_state, str) else None

    if error_code is not None:
        log.debug("collection_describe_partial", collection=name, error_code=error_code)

    return CollectionStat(
        collection_name=name,
        row_count=stats_d.get("row_count"),
        num_partitions=_as_int(schema_d.get("num_partitions")),
        dimension=_as_int(schema_d.get("dimension")),
        index_type=index_d.get("index_type"),
        metric_type=index_d.get("metric_type"),
        # None, not False, when the state could not be read: "not loaded" and
        # "we could not tell" are different facts and the column is nullable
        # precisely so they stay distinguishable.
        is_loaded=(state == "Loaded") if state else None,
        load_state=state,
        error_code=error_code,
        error_message=error_message,
        raw={"schema": schema_d, "stats": stats_d.get("raw", {}), "index": index_d},
    )


async def _describe_first_index(
    milvus: MilvusAdapter, name: str, *, force: bool = False
) -> dict[str, Any]:
    """Index type and metric for a collection's first index.

    A collection with no index is normal, not an error -- data can be inserted
    long before anyone builds one -- so that case returns an empty dict.
    """
    indexes = await milvus.list_indexes(name, force=force)
    if not indexes:
        return {}
    described = await milvus.describe_index(name, indexes[0], force=force)
    return {
        "index_name": indexes[0],
        "index_type": described.get("index_type"),
        "metric_type": described.get("metric_type"),
        "field_name": described.get("field_name"),
        "index_count": len(indexes),
    }


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
