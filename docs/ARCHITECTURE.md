# Architecture

> Component responsibilities, the degradation envelope and the data model are
> written up in WP-16. The sections below are recorded as their work packages
> land, while the evidence is fresh.

## The Docker socket mount: a deliberate security trade-off

`cp-api` mounts the host's Docker socket:

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

**This grants root-equivalent access to the host.** That is not an overstatement
and the `:ro` flag does not change it.

### Why `:ro` is not a security control

`:ro` makes the *file* read-only. It says nothing about what may be sent over
it. The Docker socket is a full control API, and reads and writes both travel
as HTTP requests over the same Unix socket — so `:ro` does not prevent a single
API call. Anything that can talk to this socket can:

- start a container with `--privileged`, or with `/` bind-mounted, and read or
  write any file on the host;
- add itself to `/etc/sudoers` or drop a key in `root`'s `authorized_keys`;
- read every environment variable and secret of every other container.

There is no meaningful privilege boundary between "can reach the Docker socket"
and "is root on the host". Running the API as a non-root user (uid 10001, see
`control_plane/Dockerfile`) is worth doing and is done, but it does not close
this: the privilege comes from the socket, not from the process's own uid.

### Why it is accepted here

The control plane has to report container state — `running`, `exited`, `missing`
— and tail container logs. On a single-tenant local demo, the socket is the
direct way to do that, the host is the developer's own machine, and the only
code that can reach the socket is code they already ran.

The exposure is narrowed, if not removed:

- the adapter is **read-only in practice**: it calls `containers.list`,
  `containers.get` and `container.logs`, and never creates, starts, stops or
  execs anything;
- component names are checked against an **allowlist inside the adapter**, not
  merely in the router, so a request parameter can never reach a container
  lookup;
- the socket path is configurable via `DOCKER_SOCKET`, so it can be pointed at a
  proxy rather than the real socket;
- an unreachable socket **degrades rather than fails** — the control plane keeps
  working without container visibility, so removing the mount is a supported
  configuration, not a breakage.

### What production would do instead

1. **A socket proxy.** Put [`docker-socket-proxy`][proxy] in front of it and
   allow only `GET /containers/json`, `GET /containers/{id}/json` and
   `GET /containers/{id}/logs`. The API then talks to the proxy over the
   network and never sees the socket. This is the smallest change with the
   largest reduction in blast radius, and it is what I would do first.
2. **The Docker API over mTLS.** Expose the daemon on a TLS port with client
   certificates and set `DOCKER_HOST`. Removes the socket mount entirely, at
   the cost of certificate management. Still grants full API access unless
   combined with (1).
3. **On Kubernetes, no socket at all.** The equivalent adapter uses the
   Kubernetes API with a ServiceAccount bound to a Role granting only
   `get`/`list`/`watch` on `pods` and `get` on `pods/log`, namespace-scoped.
   This is genuinely least-privilege, and it is the reason the Docker adapter
   sits behind the `ComponentRuntime` interface rather than being called
   directly: swapping in a `KubernetesAdapter` selected by
   `clusters.deployment_type` is a new file, not a rewrite.

[proxy]: https://github.com/Tecnativa/docker-socket-proxy

### Other limitations of this build, stated plainly

Matching honesty about the rest of the posture, all acceptable for a local demo
and none acceptable in production:

| Limitation | Production answer |
|---|---|
| No authentication or authorisation on the API | OIDC/JWT at an ingress, plus per-route scopes |
| Default credentials in `.env` (`minioadmin`, `controlplane`) | A secrets manager; no credentials in the repo |
| No TLS on any endpoint | TLS terminated at the ingress; internal mTLS |
| CORS not restricted | Same-origin only, which nginx already provides |
| Single-tenant: any caller sees every cluster | Tenant scoping on `clusters` and every query |

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
