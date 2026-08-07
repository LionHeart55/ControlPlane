"""Curated metric allowlist.

Milvus exposes ~400 metric families. The dashboard wants a couple of dozen
scalars, so selection is allowlist-driven rather than pattern-matched.

**How this list was built.** Not from documentation -- from
``MetricsAdapter.discover()`` run against a live Milvus 2.6.20, once idle and
once after a real workload (create collection, insert 2000 rows, build an HNSW
index, load, then 25 searches). Several families only exist after the component
has done work: with an idle server there are *zero* ``milvus_proxy_*`` metrics
and no ``milvus_querynode_entity_num``. Authoring this list against an idle
instance would have silently dropped the most useful half of it.

Four corrections came out of that exercise, each of which would otherwise have
produced a permanently blank tile:

  * ``milvus_storage_op_count``      -> ``internal_storage_op_count``
  * ``milvus_storage_request_latency`` -> ``internal_storage_request_latency``
  * ``milvus_querynode_num_entities`` does not exist; ``milvus_querynode_entity_num`` does
  * ``process_cpu_seconds_total`` is reported by the parser as
    ``process_cpu_seconds`` -- prometheus_client strips the ``_total`` suffix
    from counters, so the raw name from the scrape text never matches

That last one is why ``aliases`` exists and why matching is suffix-tolerant: a
name that is correct in the raw exposition format can still be wrong after
parsing.

**Aggregation.** Most families carry ``node_id``/``role_name``/``collection_name``
labels and the UI wants one number. The default rule is sum-for-counters,
max-for-gauges, but it is recorded per metric because the rule has real
exceptions: ``milvus_num_node`` is a *gauge* whose series are all ``1``, one per
node, so summing counts the nodes while max would report a constant 1.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Aggregation(enum.StrEnum):
    """How to collapse multiple label series into one scalar."""

    SUM = "sum"
    MAX = "max"
    MIN = "min"
    AVG = "avg"


class MetricKind(enum.StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    display_label: str
    unit: str
    aggregation: Aggregation
    kind: MetricKind
    # Alternative names across Milvus versions. Tried in order after `name`.
    aliases: tuple[str, ...] = ()
    description: str = ""
    # Quantiles computed from bucket counts, histograms only.
    quantiles: tuple[float, ...] = (0.5, 0.99)

    @property
    def candidate_names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


# Verified present on Milvus 2.6.20. Entries marked "needs <x>" are absent until
# that work happens; they are kept deliberately so the dashboard greys them out
# instead of hiding them -- an absent metric is information.
ALLOWLIST: tuple[MetricSpec, ...] = (
    # --- topology -------------------------------------------------------
    MetricSpec(
        name="milvus_num_node",
        display_label="Milvus nodes",
        unit="nodes",
        # Gauge, but each series is a constant 1 per (node_id, role_name):
        # summing counts nodes, max would always report 1.
        aggregation=Aggregation.SUM,
        kind=MetricKind.GAUGE,
        description="Number of Milvus nodes by role.",
    ),
    MetricSpec(
        name="milvus_rootcoord_collection_num",
        display_label="Collections",
        unit="collections",
        aggregation=Aggregation.SUM,
        kind=MetricKind.GAUGE,
        description="Collections known to RootCoord, summed across databases.",
    ),
    # --- request path (needs proxy traffic) -----------------------------
    MetricSpec(
        name="milvus_proxy_req_count",
        display_label="Proxy requests",
        unit="requests",
        aggregation=Aggregation.SUM,
        kind=MetricKind.COUNTER,
        description="Total requests handled by the proxy. Needs traffic to appear.",
    ),
    MetricSpec(
        name="milvus_proxy_req_latency",
        display_label="Proxy latency",
        unit="ms",
        aggregation=Aggregation.SUM,
        kind=MetricKind.HISTOGRAM,
        description="Proxy request latency; p50/p99 from bucket counts.",
    ),
    MetricSpec(
        name="milvus_proxy_sq_latency",
        display_label="Search/query latency",
        unit="ms",
        aggregation=Aggregation.SUM,
        kind=MetricKind.HISTOGRAM,
        description="Proxy search and query latency.",
    ),
    # --- data (needs a loaded collection) -------------------------------
    MetricSpec(
        name="milvus_querynode_entity_num",
        display_label="Loaded entities",
        unit="entities",
        aggregation=Aggregation.SUM,
        kind=MetricKind.GAUGE,
        # The spec also offered milvus_querynode_num_entities; that name does
        # not exist on 2.6.20, so it is not even listed as an alias.
        description="Entities held by query nodes. Needs a loaded collection.",
    ),
    MetricSpec(
        name="milvus_datacoord_stored_binlog_size",
        display_label="Binlog size",
        unit="bytes",
        aggregation=Aggregation.SUM,
        kind=MetricKind.GAUGE,
        description="Bytes of binlog stored in object storage.",
    ),
    MetricSpec(
        name="milvus_datacoord_segment_num",
        display_label="Segments",
        unit="segments",
        aggregation=Aggregation.SUM,
        kind=MetricKind.GAUGE,
        description="Segments tracked by DataCoord.",
    ),
    # --- object storage --------------------------------------------------
    MetricSpec(
        name="internal_storage_op_count",
        display_label="Object-store ops",
        unit="operations",
        aggregation=Aggregation.SUM,
        kind=MetricKind.COUNTER,
        aliases=("milvus_storage_op_count",),
        description="Object-store operations. Renamed from milvus_storage_op_count.",
    ),
    MetricSpec(
        name="internal_storage_request_latency",
        display_label="Object-store latency",
        unit="ms",
        aggregation=Aggregation.SUM,
        kind=MetricKind.HISTOGRAM,
        aliases=("milvus_storage_request_latency",),
        description="Object-store request latency; p50/p99 from buckets.",
    ),
    # --- process level ---------------------------------------------------
    MetricSpec(
        name="go_goroutines",
        display_label="Goroutines",
        unit="goroutines",
        aggregation=Aggregation.MAX,
        kind=MetricKind.GAUGE,
        description="Live goroutines. A steady climb indicates a leak.",
    ),
    MetricSpec(
        name="process_resident_memory_bytes",
        display_label="Resident memory",
        unit="bytes",
        aggregation=Aggregation.MAX,
        kind=MetricKind.GAUGE,
        description="Resident set size of the Milvus process.",
    ),
    MetricSpec(
        name="process_cpu_seconds",
        display_label="CPU time",
        unit="seconds",
        aggregation=Aggregation.SUM,
        kind=MetricKind.COUNTER,
        # The exposition format says process_cpu_seconds_total; prometheus_client
        # strips _total from counter family names when parsing.
        aliases=("process_cpu_seconds_total",),
        description="Cumulative CPU seconds consumed.",
    ),
    MetricSpec(
        name="process_open_fds",
        display_label="Open file descriptors",
        unit="fds",
        aggregation=Aggregation.MAX,
        kind=MetricKind.GAUGE,
        description="Open file descriptors; approaching the limit precedes failures.",
    ),
)

ALLOWLIST_BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in ALLOWLIST}


def resolve_spec(family_name: str) -> MetricSpec | None:
    """Find the spec a scraped family satisfies, tolerating name drift.

    Also strips a trailing ``_total``: a counter is exposed as ``x_total`` but
    parsed as ``x``, and either form should match.
    """
    for spec in ALLOWLIST:
        if family_name in spec.candidate_names:
            return spec
        if f"{family_name}_total" in spec.candidate_names:
            return spec
        if family_name.endswith("_total") and family_name[: -len("_total")] in spec.candidate_names:
            return spec
    return None
