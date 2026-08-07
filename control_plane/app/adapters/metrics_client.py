"""Prometheus scrape of Milvus's ``:9091/metrics`` endpoint.

Version tolerance is the design constraint. Milvus renames metric families
between minor versions, and a dashboard that hides anything it cannot find goes
quietly blank after an upgrade with nobody noticing. So every allowlisted
metric is always returned: present ones with a value, absent ones with
``value: null, available: false`` and a reason. The UI greys those out, which
makes an upgrade-induced gap visible instead of invisible.

Two failure modes, deliberately distinguished:

  * **a metric is missing from the scrape** -- normal, expected, and never an
    error. It comes back ``available: false``.
  * **the endpoint is unreachable** -- a real dependency failure, raised as
    ``DependencyUnavailableError(code="METRICS_UNAVAILABLE")`` so the route
    renders a degradation envelope rather than a page of nulls that looks like
    a healthy Milvus with no activity.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import httpx
from prometheus_client.parser import text_string_to_metric_families

from app.adapters.metric_allowlist import (
    ALLOWLIST,
    Aggregation,
    MetricKind,
    MetricSpec,
    resolve_spec,
)
from app.api.errors import DependencyUnavailableError
from app.config import Settings, get_settings
from app.logging_conf import get_logger

log = get_logger("metrics")

SCRAPE_TIMEOUT_S = 2.0
METRICS_UNAVAILABLE = "METRICS_UNAVAILABLE"


@dataclass(frozen=True)
class MetricValue:
    """One allowlisted metric, present or not."""

    name: str
    display_label: str
    unit: str
    aggregation: str
    kind: str
    value: float | None = None
    available: bool = False
    # Populated for histograms: {"p50": ..., "p99": ...}
    quantiles: dict[str, float | None] = field(default_factory=dict)
    # How many label series were collapsed into `value`.
    series_count: int = 0
    unavailable_reason: str | None = None
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.display_label,
            "unit": self.unit,
            "aggregation": self.aggregation,
            "kind": self.kind,
            "value": self.value,
            "available": self.available,
            "quantiles": self.quantiles or None,
            "series_count": self.series_count,
            "unavailable_reason": self.unavailable_reason,
            "description": self.description,
        }


@dataclass(frozen=True)
class MetricsSnapshot:
    metrics: list[MetricValue]
    observed_at: dt.datetime
    families_scraped: int
    available_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "metrics": [m.as_dict() for m in self.metrics],
            "observed_at": self.observed_at.isoformat(),
            "families_scraped": self.families_scraped,
            "available_count": self.available_count,
            "allowlisted_count": len(self.metrics),
        }


def _missing(spec: MetricSpec, reason: str) -> MetricValue:
    return MetricValue(
        name=spec.name,
        display_label=spec.display_label,
        unit=spec.unit,
        aggregation=str(spec.aggregation),
        kind=str(spec.kind),
        value=None,
        available=False,
        unavailable_reason=reason,
        description=spec.description,
    )


def _collapse(values: list[float], how: Aggregation) -> float:
    if not values:
        return 0.0
    if how is Aggregation.SUM:
        return float(sum(values))
    if how is Aggregation.MAX:
        return float(max(values))
    if how is Aggregation.MIN:
        return float(min(values))
    return float(sum(values) / len(values))


def quantile_from_buckets(buckets: list[tuple[float, float]], q: float) -> float | None:
    """Prometheus-style histogram_quantile over cumulative buckets.

    `buckets` is [(upper_bound, cumulative_count)], ascending. Summing the
    counts across label series before calling this is required: computing a
    quantile per series and averaging the results is not a quantile of
    anything.

    Interpolates linearly inside the containing bucket, which is the standard
    approximation. A value that lands in the +Inf bucket cannot be interpolated
    and returns the largest finite bound, since the true value is unbounded
    above.
    """
    if not buckets:
        return None
    ordered = sorted(buckets, key=lambda b: b[0])
    total = ordered[-1][1]
    if total <= 0:
        return None

    rank = q * total
    previous_bound = 0.0
    previous_count = 0.0
    for bound, cumulative in ordered:
        if cumulative >= rank:
            if bound == float("inf"):
                # Cannot interpolate past the last finite bound.
                finite = [b for b, _ in ordered if b != float("inf")]
                return finite[-1] if finite else None
            span = cumulative - previous_count
            if span <= 0:
                return bound
            fraction = (rank - previous_count) / span
            return previous_bound + fraction * (bound - previous_bound)
        previous_bound, previous_count = bound, cumulative
    return ordered[-1][0]


class MetricsAdapter:
    """Scrapes and curates Milvus's Prometheus endpoint."""

    def __init__(self, metrics_uri: str | None = None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._base = (metrics_uri or self._settings.milvus_metrics_uri).rstrip("/")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._discovery_logged = False

    @property
    def url(self) -> str:
        return f"{self._base}/metrics"

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.AsyncClient(timeout=SCRAPE_TIMEOUT_S)
            return self._client

    async def close(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _fetch_text(self) -> str:
        client = await self._get_client()
        try:
            response = await client.get(self.url)
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise DependencyUnavailableError(
                f"cannot scrape {self.url}: {exc}",
                dependency="milvus-metrics",
                code=METRICS_UNAVAILABLE,
                detail={"url": self.url},
            ) from exc
        return response.text

    # --- surface ----------------------------------------------------------
    async def ping(self) -> bool:
        """Is the metrics endpoint reachable? Never raises.

        Fetches without parsing. The health job runs every 15s and only needs
        reachability for rule 4; parsing ~360 families each time to discard the
        result would be pure waste. `scrape()` remains the call that produces
        values, on the slower snapshot interval.
        """
        try:
            await self._fetch_text()
        except DependencyUnavailableError:
            return False
        return True

    async def discover(self) -> list[str]:
        """Every metric family name in the scrape.

        Used to author the allowlist against a real instance rather than from
        documentation. See app/adapters/metric_allowlist.py for how the current
        list was derived.
        """
        text = await self._fetch_text()
        # Parsing is pure CPU over a ~14k-line payload; keep it off the loop.
        families = await asyncio.to_thread(_parse_families, text)
        return sorted(families)

    async def scrape(self) -> MetricsSnapshot:
        """Every allowlisted metric, present or not. Missing is not an error."""
        text = await self._fetch_text()
        parsed = await asyncio.to_thread(_collect_allowlisted, text)
        found: dict[str, dict[str, Any]] = parsed["found"]
        family_count: int = parsed["family_count"]

        metrics: list[MetricValue] = []
        for spec in ALLOWLIST:
            payload = found.get(spec.name)
            if payload is None:
                metrics.append(
                    _missing(spec, "not exposed by this Milvus version or not yet active")
                )
                continue
            metrics.append(_build_value(spec, payload))

        self._log_discovery_once(metrics)
        return MetricsSnapshot(
            metrics=metrics,
            observed_at=dt.datetime.now(dt.UTC),
            families_scraped=family_count,
            available_count=sum(1 for m in metrics if m.available),
        )

    def _log_discovery_once(self, metrics: list[MetricValue]) -> None:
        """Log the allowlist/scrape mismatch once per process.

        A permanently-missing metric usually means a rename in a Milvus upgrade;
        surfacing it in the logs at startup is how that gets noticed before
        someone wonders why a tile has been grey for a month.
        """
        if self._discovery_logged:
            return
        self._discovery_logged = True
        missing = [m.name for m in metrics if not m.available]
        if missing:
            log.warning(
                "metrics_allowlist_gaps",
                url=self.url,
                missing=missing,
                found=len(metrics) - len(missing),
                hint="renamed by a Milvus upgrade, or the component has not done work yet",
            )
        else:
            log.info("metrics_allowlist_complete", url=self.url, found=len(metrics))


def _parse_families(text: str) -> list[str]:
    return [family.name for family in text_string_to_metric_families(text)]


def _collect_allowlisted(text: str) -> dict[str, Any]:
    """Walk the scrape once, keeping only families the allowlist asks for."""
    found: dict[str, dict[str, Any]] = {}
    family_count = 0

    for family in text_string_to_metric_families(text):
        family_count += 1
        spec = resolve_spec(family.name)
        if spec is None:
            continue

        if spec.kind is MetricKind.HISTOGRAM:
            # Sum bucket counts across label series first: a quantile must be
            # computed from the combined distribution, not averaged per series.
            buckets: dict[float, float] = {}
            total_count = 0.0
            total_sum = 0.0
            series: set[tuple] = set()
            for sample in family.samples:
                labels = {k: v for k, v in sample.labels.items() if k != "le"}
                series.add(tuple(sorted(labels.items())))
                if sample.name.endswith("_bucket"):
                    bound = float(sample.labels.get("le", "inf"))
                    buckets[bound] = buckets.get(bound, 0.0) + sample.value
                elif sample.name.endswith("_count"):
                    total_count += sample.value
                elif sample.name.endswith("_sum"):
                    total_sum += sample.value
            found[spec.name] = {
                "kind": "histogram",
                "buckets": sorted(buckets.items()),
                "count": total_count,
                "sum": total_sum,
                "series_count": len(series),
            }
        else:
            values = [s.value for s in family.samples if not s.name.endswith(("_created",))]
            found[spec.name] = {
                "kind": str(spec.kind),
                "values": values,
                "series_count": len(values),
            }
    return {"found": found, "family_count": family_count}


def _build_value(spec: MetricSpec, payload: dict[str, Any]) -> MetricValue:
    if payload["kind"] == "histogram":
        buckets: list[tuple[float, float]] = payload["buckets"]
        quantiles: dict[str, float | None] = {}
        for q in spec.quantiles:
            key = f"p{int(q * 100)}"
            quantiles[key] = quantile_from_buckets(buckets, q)
        observations = payload["count"]
        if observations <= 0:
            return MetricValue(
                name=spec.name,
                display_label=spec.display_label,
                unit=spec.unit,
                aggregation=str(spec.aggregation),
                kind=str(spec.kind),
                value=None,
                available=False,
                unavailable_reason="exposed but no observations recorded yet",
                series_count=payload["series_count"],
                description=spec.description,
            )
        return MetricValue(
            name=spec.name,
            display_label=spec.display_label,
            unit=spec.unit,
            aggregation=str(spec.aggregation),
            kind=str(spec.kind),
            # The headline number for a latency histogram is p50; p99 sits
            # alongside it rather than being buried.
            value=quantiles.get("p50"),
            available=True,
            quantiles=quantiles,
            series_count=payload["series_count"],
            description=spec.description,
        )

    values: list[float] = payload["values"]
    if not values:
        return _missing(spec, "family present but reported no samples")
    return MetricValue(
        name=spec.name,
        display_label=spec.display_label,
        unit=spec.unit,
        aggregation=str(spec.aggregation),
        kind=str(spec.kind),
        value=_collapse(values, spec.aggregation),
        available=True,
        series_count=payload["series_count"],
        description=spec.description,
    )
