"""Metrics parsing against a captured /metrics scrape.

`app/tests/fixtures/milvus_metrics.txt` is real output from a live Milvus
2.6.20, trimmed but not synthesised: every allowlisted family is kept complete,
with all its label series and histogram buckets. That matters because the bugs
this code has actually had were all about *shape* -- a counter exposed as
`x_total` but parsed as `x`, a histogram whose buckets are split across two
`node_id` series -- and a hand-written fixture is written to match whatever the
parser already does.

The second half of the file removes families from that same capture to simulate
a Milvus upgrade that renamed them. Deriving the degraded case from the real one
proves the two differ only in what was removed; a separately hand-edited file
could drift and quietly stop testing anything.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from app.adapters.metric_allowlist import ALLOWLIST
from app.adapters.metrics_client import MetricsAdapter, MetricsSnapshot
from app.config import Settings

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "milvus_metrics.txt"
CAPTURED = FIXTURE_PATH.read_text(encoding="utf-8")


def adapter_for(text: str) -> MetricsAdapter:
    adapter = MetricsAdapter(settings=Settings(_env_file=None))
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=text))
    )
    return adapter


def strip_families(text: str, names: set[str]) -> str:
    """Remove whole families from a scrape, as a version rename would."""
    kept: list[str] = []
    dropping = False
    for line in text.splitlines():
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            dropping = line.split()[2] in names
        elif line.startswith("#") or not line.strip():
            pass
        else:
            base = re.split(r"[{ ]", line, maxsplit=1)[0]
            for suffix in ("_bucket", "_count", "_sum", "_total", "_created"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if base in names or f"{base}_total" in names:
                dropping = True
        if not dropping:
            kept.append(line)
    return "\n".join(kept) + "\n"


async def scrape(text: str) -> MetricsSnapshot:
    return await adapter_for(text).scrape()


# --- the captured scrape --------------------------------------------------
def test_fixture_is_a_real_capture_not_a_stub() -> None:
    assert len(CAPTURED) > 20_000, "too small to be a real scrape"
    assert CAPTURED.count("# TYPE ") > 40
    assert "milvus_" in CAPTURED and "go_goroutines" in CAPTURED


async def test_every_allowlisted_metric_resolves_against_a_live_scrape() -> None:
    """The whole point of the allowlist: names must match what Milvus emits.

    A rename anywhere in ALLOWLIST fails here rather than in production, where
    it would show up months later as a tile nobody could explain.
    """
    snapshot = await scrape(CAPTURED)
    absent = [m.name for m in snapshot.metrics if not m.available]
    assert absent == [], f"allowlisted metrics missing from a real scrape: {absent}"
    assert snapshot.available_count == len(ALLOWLIST)


async def test_parser_sees_more_families_than_the_allowlist() -> None:
    """Confirms the fixture is a mixed scrape, not a pre-filtered one."""
    snapshot = await scrape(CAPTURED)
    assert snapshot.families_scraped > len(ALLOWLIST) * 2


async def test_total_suffixed_counter_resolves_after_parsing() -> None:
    """process_cpu_seconds_total in the text, process_cpu_seconds after parse.

    prometheus_client strips `_total` from counter family names, so a name
    copied correctly out of the raw exposition format still fails to match.
    """
    assert "process_cpu_seconds_total" in CAPTURED
    metric = {m.name: m for m in (await scrape(CAPTURED)).metrics}["process_cpu_seconds"]
    assert metric.available and metric.value is not None and metric.value > 0


async def test_multi_series_gauge_is_collapsed_per_spec() -> None:
    """milvus_num_node is a gauge whose series are each a constant 1 per node.

    Summing counts nodes; max would report a constant 1. This is the documented
    exception to sum-for-counters/max-for-gauges.
    """
    metric = {m.name: m for m in (await scrape(CAPTURED)).metrics}["milvus_num_node"]
    assert metric.series_count > 1
    assert metric.value == pytest.approx(metric.series_count)


async def test_histogram_quantiles_come_from_summed_buckets() -> None:
    metric = {m.name: m for m in (await scrape(CAPTURED)).metrics}["milvus_proxy_req_latency"]
    assert metric.available
    p50, p99 = metric.quantiles["p50"], metric.quantiles["p99"]
    assert p50 is not None and p99 is not None
    assert 0 < p50 <= p99, "p99 must not be below p50"
    assert metric.value == p50, "headline value is p50"


async def test_values_are_plausible_for_a_loaded_collection() -> None:
    """Cross-checks against the state the capture was taken in.

    The fixture was captured with one collection of 5 000 rows loaded, on a
    standalone deployment that runs four roles. If the aggregation were wrong
    these would be off by a factor of the series count.
    """
    by = {m.name: m for m in (await scrape(CAPTURED)).metrics}
    assert by["milvus_rootcoord_collection_num"].value == 1
    assert by["milvus_querynode_entity_num"].value == 5000
    assert by["milvus_num_node"].value == 4


# --- the same capture, with families removed ------------------------------
RENAMED = {"milvus_proxy_req_count", "milvus_querynode_entity_num", "go_goroutines"}


async def test_removed_metrics_are_reported_not_dropped() -> None:
    """A Milvus upgrade that renames a family must not silently blank a tile."""
    snapshot = await scrape(strip_families(CAPTURED, RENAMED))

    assert len(snapshot.metrics) == len(ALLOWLIST), "every allowlisted metric must be returned"
    absent = {m.name for m in snapshot.metrics if not m.available}
    assert absent >= RENAMED, "every removed family must be reported unavailable"

    for metric in snapshot.metrics:
        if metric.name in RENAMED:
            assert metric.value is None
            assert metric.unavailable_reason, "the UI needs a reason to show"
            assert metric.display_label and metric.unit, "a greyed tile still needs a label"


async def test_surviving_metrics_are_unaffected_by_the_removal() -> None:
    before = {m.name: m.value for m in (await scrape(CAPTURED)).metrics if m.available}
    after = {
        m.name: m.value
        for m in (await scrape(strip_families(CAPTURED, RENAMED))).metrics
        if m.available
    }
    assert set(before) - set(after) == RENAMED
    for name, value in after.items():
        assert before[name] == value, f"{name} changed when an unrelated family was removed"


async def test_a_version_that_renamed_everything_does_not_raise() -> None:
    """All fourteen gone is a degraded dashboard, never an exception."""
    all_names = {name for spec in ALLOWLIST for name in spec.candidate_names}
    snapshot = await scrape(strip_families(CAPTURED, all_names))
    assert snapshot.available_count == 0
    assert len(snapshot.metrics) == len(ALLOWLIST)
    assert all(m.value is None and not m.available for m in snapshot.metrics)


async def test_discover_lists_families_from_the_real_capture() -> None:
    names = await adapter_for(CAPTURED).discover()
    assert names == sorted(names)
    assert "milvus_num_node" in names
    assert len(names) > 40
