# Architecture

> Component responsibilities, the degradation envelope, the data model and the
> Docker-socket security trade-off are written up in WP-16. The sections below
> are recorded as their work packages land, while the evidence is fresh.

## Metrics: how the allowlist was chosen

Milvus 2.6.20 exposes **397 metric families**. The dashboard needs a couple of
dozen scalars, so `app/adapters/metric_allowlist.py` is an explicit allowlist
rather than a pattern match.

That list was **not** written from documentation. It was derived by running
`MetricsAdapter.discover()` against a live instance, twice:

1. **Idle**, straight after `deploy.sh up` — 254 families.
2. **After a real workload** — create a collection, insert 2 000 rows, build an
   HNSW index, load it, run 25 searches — **397 families**.

The second pass is what made the list correct. Several families do not exist
until the component has done work: on an idle server there are **zero**
`milvus_proxy_*` metrics and no `milvus_querynode_entity_num`. An allowlist
authored against an idle instance would have silently dropped the most useful
half of it, and every one of those tiles would have been permanently grey.

### Corrections the exercise produced

The starting list in the build spec was mostly right, but four entries would
each have produced a tile that never populated:

| Specified | Reality on 2.6.20 |
|---|---|
| `milvus_storage_op_count` | renamed → `internal_storage_op_count` |
| `milvus_storage_request_latency` | renamed → `internal_storage_request_latency` |
| `milvus_querynode_num_entities` | does not exist; `milvus_querynode_entity_num` does |
| `process_cpu_seconds_total` | parsed as `process_cpu_seconds` |

The last one is the subtlest and the reason `MetricSpec.aliases` exists.
`prometheus_client`'s parser **strips the `_total` suffix** from counter family
names, so a name copied correctly out of the raw exposition text still fails to
match after parsing. Name resolution is therefore suffix-tolerant and checks
aliases, so both spellings resolve.

### Aggregation

Most families carry `node_id` / `role_name` / `collection_name` labels and the
UI wants one number per metric. The documented default is **sum for counters,
max for gauges**, but the rule is recorded per metric because it has real
exceptions — `milvus_num_node` is a *gauge* whose series are each a constant
`1`, one per node, so summing counts the nodes while max would report a
constant `1`.

Histograms are handled by summing **bucket counts across label series first**,
then computing quantiles from the combined distribution. Computing a quantile
per series and averaging the results is not a quantile of anything. The
implementation follows Prometheus's `histogram_quantile`, including the detail
that the first bucket interpolates from **0** rather than from its own upper
bound — otherwise every latency concentrated in the fastest bucket is
systematically overstated. It is cross-checked against numpy ground truth on
lognormal, uniform and bimodal distributions.

### Why missing metrics are returned rather than omitted

Metric names drift between Milvus minor versions. A dashboard that hides what
it cannot find goes quietly blank after an upgrade and nobody notices. So every
allowlisted metric is always returned: absent ones carry
`value: null, available: false` and a reason, and the UI greys them. A gap
becomes visible instead of invisible.

Two failure modes are deliberately distinguished:

- **a metric is missing from the scrape** — expected, never an error,
  `available: false`;
- **the endpoint is unreachable** — a real dependency failure, raised as
  `DependencyUnavailableError(code="METRICS_UNAVAILABLE")` so the route renders
  a degradation envelope rather than a page of nulls that would look like a
  healthy Milvus doing nothing.

On startup the adapter logs, once, which allowlisted metrics were not found —
that is how a rename gets noticed before someone wonders why a tile has been
grey for a month.

### Re-running discovery after a Milvus upgrade

```python
from app.adapters.metrics_client import MetricsAdapter
names = await MetricsAdapter().discover()   # every family name in the scrape
```

Do it with a workload running, not against an idle server.
