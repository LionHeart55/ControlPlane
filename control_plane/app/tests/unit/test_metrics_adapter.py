"""Metrics adapter: allowlist tolerance, aggregation and bucket quantiles.

No infrastructure. The fixtures below are trimmed from a real Milvus 2.6.20
scrape, so the label sets and bucket boundaries are the ones the adapter will
actually meet.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.adapters.metric_allowlist import ALLOWLIST, Aggregation, resolve_spec
from app.adapters.metrics_client import (
    METRICS_UNAVAILABLE,
    MetricsAdapter,
    quantile_from_buckets,
)
from app.api.errors import DependencyUnavailableError
from app.config import Settings

# Trimmed from a live scrape. milvus_num_node has one series per node; the
# proxy histogram has two node_id series that must be summed before any
# quantile is taken.
FIXTURE = """\
# HELP milvus_num_node Number of nodes
# TYPE milvus_num_node gauge
milvus_num_node{node_id="1",role_name="rootcoord"} 1
milvus_num_node{node_id="2",role_name="querynode"} 1
milvus_num_node{node_id="3",role_name="proxy"} 1
# HELP milvus_rootcoord_collection_num collections
# TYPE milvus_rootcoord_collection_num gauge
milvus_rootcoord_collection_num{db_name="default"} 3
milvus_rootcoord_collection_num{db_name="other"} 2
# HELP go_goroutines goroutines
# TYPE go_goroutines gauge
go_goroutines 789
# HELP process_cpu_seconds_total cpu
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 32.95
# HELP milvus_proxy_req_count requests
# TYPE milvus_proxy_req_count counter
milvus_proxy_req_count{node_id="1",status="success"} 100
milvus_proxy_req_count{node_id="2",status="success"} 7
# HELP milvus_proxy_req_latency latency
# TYPE milvus_proxy_req_latency histogram
milvus_proxy_req_latency_bucket{node_id="1",le="1.0"} 40
milvus_proxy_req_latency_bucket{node_id="1",le="10.0"} 45
milvus_proxy_req_latency_bucket{node_id="1",le="100.0"} 50
milvus_proxy_req_latency_bucket{node_id="1",le="+Inf"} 50
milvus_proxy_req_latency_count{node_id="1"} 50
milvus_proxy_req_latency_sum{node_id="1"} 300
milvus_proxy_req_latency_bucket{node_id="2",le="1.0"} 40
milvus_proxy_req_latency_bucket{node_id="2",le="10.0"} 45
milvus_proxy_req_latency_bucket{node_id="2",le="100.0"} 50
milvus_proxy_req_latency_bucket{node_id="2",le="+Inf"} 50
milvus_proxy_req_latency_count{node_id="2"} 50
milvus_proxy_req_latency_sum{node_id="2"} 300
# HELP internal_storage_request_latency latency
# TYPE internal_storage_request_latency histogram
internal_storage_request_latency_bucket{le="1.0"} 0
internal_storage_request_latency_bucket{le="+Inf"} 0
internal_storage_request_latency_count 0
internal_storage_request_latency_sum 0
"""


def make_adapter(handler: Any) -> MetricsAdapter:
    adapter = MetricsAdapter(
        settings=Settings(_env_file=None, milvus_metrics_uri="http://milvus:9091")
    )
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


def ok_handler(text: str = FIXTURE) -> Any:
    return lambda request: httpx.Response(200, text=text)


# --- quantiles -----------------------------------------------------------
def test_quantile_interpolates_inside_the_containing_bucket() -> None:
    """Rank 50 of 100 falls in the (1.0, 10.0] bucket.

    40 observations are <=1.0 and 90 are <=10.0, so the median is 10/50 of the
    way through that bucket: 1.0 + (50-40)/(90-40) * 9.0 = 2.8.
    """
    got = quantile_from_buckets([(1.0, 40.0), (10.0, 90.0), (float("inf"), 100.0)], 0.5)
    assert got == pytest.approx(2.8)


def test_first_bucket_interpolates_from_zero() -> None:
    """Prometheus semantics: the first bucket's lower bound is 0, not its own upper bound.

    With 80 of 100 observations <=1.0 the median sits INSIDE that bucket, at
    0 + (50/80) * 1.0 = 0.625. Returning 1.0 would systematically overstate
    every latency whose distribution is concentrated in the fastest bucket --
    which, for a healthy service, is most of them.
    """
    buckets = [(1.0, 80.0), (10.0, 90.0), (100.0, 100.0), (float("inf"), 100.0)]
    assert quantile_from_buckets(buckets, 0.5) == pytest.approx(0.625)


def test_quantile_is_not_merely_the_sum_or_mean() -> None:
    buckets = [(1.0, 10.0), (10.0, 90.0), (100.0, 100.0), (float("inf"), 100.0)]
    p50 = quantile_from_buckets(buckets, 0.5)
    p99 = quantile_from_buckets(buckets, 0.99)
    assert p50 is not None and p99 is not None
    assert p50 < p99, "p99 must exceed p50"
    assert 1.0 < p50 < 10.0
    assert 10.0 < p99 <= 100.0


def test_quantile_of_empty_histogram_is_none() -> None:
    assert quantile_from_buckets([], 0.5) is None
    assert quantile_from_buckets([(1.0, 0.0), (float("inf"), 0.0)], 0.5) is None


def test_quantile_in_inf_bucket_returns_last_finite_bound() -> None:
    """Nothing above the last finite bound can be interpolated."""
    buckets = [(1.0, 10.0), (float("inf"), 100.0)]
    assert quantile_from_buckets(buckets, 0.99) == 1.0


# --- aggregation ---------------------------------------------------------
async def test_gauge_series_are_summed_or_maxed_per_spec() -> None:
    snap = await make_adapter(ok_handler()).scrape()
    by = {m.name: m for m in snap.metrics}

    # SUM even though it is a gauge: each series is a constant 1 per node, so
    # summing counts nodes and max would report 1.
    assert by["milvus_num_node"].value == 3
    assert by["milvus_num_node"].series_count == 3
    # Summed across databases.
    assert by["milvus_rootcoord_collection_num"].value == 5
    # MAX: a per-process gauge.
    assert by["go_goroutines"].value == 789


async def test_counter_series_are_summed() -> None:
    snap = await make_adapter(ok_handler()).scrape()
    by = {m.name: m for m in snap.metrics}
    assert by["milvus_proxy_req_count"].value == 107


async def test_histogram_buckets_summed_across_series_before_quantile() -> None:
    """Two identical node series must double the counts, not the quantile."""
    snap = await make_adapter(ok_handler()).scrape()
    m = {x.name: x for x in snap.metrics}["milvus_proxy_req_latency"]
    assert m.available is True
    assert m.series_count == 2
    # Each node contributes 40 obs <=1.0 out of 50. Summed: 80 of 100, so rank
    # 50 lands inside the first bucket -> 0 + (50/80) * 1.0 = 0.625.
    # Averaging the two per-series quantiles would be meaningless; summing the
    # bucket counts first is the only correct order.
    p50, p99 = m.quantiles["p50"], m.quantiles["p99"]
    assert p50 is not None and p99 is not None
    assert p50 == pytest.approx(0.625)
    assert p99 > p50
    assert m.value == p50, "headline value is p50"


# --- the _total trap ------------------------------------------------------
def test_counter_total_suffix_resolves() -> None:
    """Exposed as process_cpu_seconds_total, parsed as process_cpu_seconds."""
    parsed_form = resolve_spec("process_cpu_seconds")
    raw_form = resolve_spec("process_cpu_seconds_total")
    assert parsed_form is not None and raw_form is not None
    assert parsed_form.name == raw_form.name == "process_cpu_seconds"


def test_renamed_metric_resolves_via_alias() -> None:
    spec = resolve_spec("milvus_storage_op_count")
    assert spec is not None and spec.name == "internal_storage_op_count"


async def test_total_suffixed_counter_is_available() -> None:
    snap = await make_adapter(ok_handler()).scrape()
    m = {x.name: x for x in snap.metrics}["process_cpu_seconds"]
    assert m.available is True
    assert m.value == pytest.approx(32.95)


# --- missing metrics are reported, never dropped or raised ----------------
async def test_absent_metrics_are_returned_unavailable() -> None:
    snap = await make_adapter(ok_handler()).scrape()
    assert len(snap.metrics) == len(ALLOWLIST), "every allowlisted metric must appear"
    absent = [m for m in snap.metrics if not m.available]
    assert absent, "the fixture omits several allowlisted metrics"
    for m in absent:
        assert m.value is None
        assert m.unavailable_reason, "the UI needs a reason to show"
        assert m.display_label and m.unit, "greyed tiles still need a label"


async def test_empty_scrape_yields_all_unavailable_but_no_error() -> None:
    """A version that renamed everything must not blank the dashboard silently."""
    snap = await make_adapter(ok_handler("# nothing here\n")).scrape()
    assert len(snap.metrics) == len(ALLOWLIST)
    assert snap.available_count == 0
    assert all(m.available is False and m.value is None for m in snap.metrics)


async def test_histogram_with_no_observations_is_unavailable() -> None:
    """Buckets exist but count is 0: a quantile would be a fabrication."""
    snap = await make_adapter(ok_handler()).scrape()
    m = {x.name: x for x in snap.metrics}["internal_storage_request_latency"]
    assert m.available is False
    assert m.value is None
    assert "no observations" in (m.unavailable_reason or "")


async def test_unknown_families_are_ignored_not_errors() -> None:
    text = FIXTURE + '\n# TYPE some_new_metric gauge\nsome_new_metric{a="b"} 1\n'
    snap = await make_adapter(ok_handler(text)).scrape()
    assert "some_new_metric" not in {m.name for m in snap.metrics}
    assert snap.available_count >= 4


# --- endpoint failures ----------------------------------------------------
async def test_unreachable_endpoint_raises_dependency_error() -> None:
    """Distinct from a missing metric: this is a real dependency failure."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(DependencyUnavailableError) as ei:
        await make_adapter(boom).scrape()
    assert ei.value.code == METRICS_UNAVAILABLE
    assert ei.value.dependency == "milvus-metrics"


async def test_http_error_raises_dependency_error() -> None:
    with pytest.raises(DependencyUnavailableError):
        await make_adapter(lambda r: httpx.Response(503, text="down")).scrape()


# --- discover -------------------------------------------------------------
async def test_discover_returns_all_family_names() -> None:
    names = await make_adapter(ok_handler()).discover()
    assert "milvus_num_node" in names
    assert "process_cpu_seconds" in names
    assert names == sorted(names)


# --- allowlist hygiene ----------------------------------------------------
def test_allowlist_entries_are_complete_and_unique() -> None:
    names = [s.name for s in ALLOWLIST]
    assert len(names) == len(set(names)), "duplicate metric name"
    for s in ALLOWLIST:
        assert s.display_label and s.unit and s.description
        assert isinstance(s.aggregation, Aggregation)
