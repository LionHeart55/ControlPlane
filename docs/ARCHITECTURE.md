# Architecture

How the pieces fit, and why the load-bearing decisions were made the way they
were. Anything measured rather than assumed says so and points at the evidence.

## Contents

- [Component responsibilities](#component-responsibilities)
- [The degradation envelope](#the-degradation-envelope)
- [Health aggregation: six ordered rules](#health-aggregation-six-ordered-rules)
- [The data model](#the-data-model)
- [Concurrency and timeouts](#concurrency-and-timeouts)
- [The Docker socket mount: a deliberate security trade-off](#the-docker-socket-mount-a-deliberate-security-trade-off)
- [Metrics: how the allowlist was chosen](#metrics-how-the-allowlist-was-chosen)

---

## Component responsibilities

### `cp-dashboard` — nginx + the SPA

Serves the built React bundle and reverse-proxies `/api/` to `cp-api`. One
origin, so there is no CORS configuration and no environment-specific base URL
in the client: the same relative fetch paths work behind nginx in production and
behind Vite's dev proxy locally.

Two details are non-obvious and both were bugs first. `proxy_pass` has **no
trailing slash** — adding one makes nginx strip the matched `/api/` prefix, so
every route 404s. And the upstream is resolved through a **variable with an
explicit `resolver`**, because a literal hostname in `proxy_pass` is resolved
once at config load and cached for the life of the worker; recreating `cp-api`
then 502s the dashboard silently until nginx itself restarts. Using a variable
forces per-request resolution, and once it is a variable `$request_uri` must be
appended by hand or every request arrives at the backend as `/`.

### `cp-api` — FastAPI

Four layers, deliberately strict about what each may do:

| Layer | May | May not |
|---|---|---|
| **routers** (`app/api/routers/`) | Shape requests and responses, build envelopes | Contain business logic or talk to adapters directly for decisions |
| **services** (`app/services/`) | Decide status, orchestrate fan-outs | Touch the database directly, or know about HTTP |
| **repositories** (`app/repositories/`) | Thin async CRUD, return ORM objects | Contain any business logic, open transactions, or write events |
| **adapters** (`app/adapters/`) | Talk to Milvus, Docker, Prometheus, MinIO, etcd | Know about clusters, the database, or the API contract |

The split is what makes the six health rules testable without any
infrastructure: `aggregate_status()` is a pure function over a `HealthSignals`
dataclass, so the entire truth table runs in milliseconds against no containers.

Adapters are process-wide singletons held in `app/adapters/registry.py`, keyed
by endpoint. That matters more than it looks: a fresh `MilvusAdapter` per call
would open a new gRPC channel every 15 seconds *and* discard the circuit
breaker's failure count, so it could never reach `fail_max` and open.

### `cp-migrate` — one-shot

`alembic upgrade head`, `restart: "no"`, and `cp-api` depends on it with
`condition: service_completed_successfully`. Migrations deliberately do **not**
run from the API entrypoint: with more than one replica every one of them would
race on `alembic_version`, and the loser either deadlocks or half-applies a
revision. As a separate service the ordering is explicit, and a failed migration
stops the API from starting at all rather than letting it serve queries against
a half-migrated schema.

### The scheduler

Three jobs inside the API process, `AsyncIOScheduler`, all with
`max_instances=1` and `coalesce=True` so a probe slower than its interval cannot
stack up behind itself.

| Job | Interval | Does |
|---|---|---|
| `health_job` | `CP_HEALTH_INTERVAL_S` (15 s, jittered) | Probe → aggregate → persist → **event only on transition** |
| `snapshot_job` | `CP_SNAPSHOT_INTERVAL_S` (60 s) | Component and collection snapshots; `component_state_change` on transition |
| `retention_job` | daily at 03:17 UTC | Purge aged rows |

Every job body is wrapped so nothing it raises reaches the scheduler, and an
unreachable PostgreSQL is a logged **skip** rather than a failure — a control
plane that silently stopped health-checking is worse than one that is obviously
down.

The scheduled probe passes `force=True`, bypassing the circuit breaker. This is
load-bearing in both directions: the breaker protects the *request* path from
piling up against a dead dependency, while the job keeps measuring reality so
the stored history shows real error codes instead of a wall of `BREAKER_OPEN`,
and so recovery is noticed without waiting out `CP_BREAKER_RESET_S`.

---

## The degradation envelope

The single most important rule in the system:

> **A dependency being down never produces a 5xx on a read endpoint.**
> Only PostgreSQL can cause a 503, and only on routes that cannot answer
> without it.

Every endpoint mixing stored and live data returns the same shape regardless of
what is broken:

```jsonc
{
  "cluster": { /* from PostgreSQL, or null */ },
  "live":    { /* or null */ },
  "live_status": "ok" | "stale" | "unavailable",
  "observed_at": "2026-08-08T00:51:28Z",
  "stale": false,
  "degraded_reason": null | { "code": "...", "message": "...", "since": null }
}
```

| `live_status` | Means | `live` | UI |
|---|---|---|---|
| `ok` | Fetched just now | populated | render normally |
| `stale` | Real data, but the dependency did not answer *this* time | populated, from cache | **dim it**, show `observed_at` |
| `unavailable` | Nothing usable | `null` | show the code from `degraded_reason` |

Three decisions inside that are easy to get wrong:

**`observed_at` is when the data was true, not when it was served.** A stale
response carries the original timestamp, which is the only thing that makes
"as of 12:03:41 (stale)" honest.

**Cached data is always flagged `stale`, even inside the fresh TTL.** The value
may be two seconds old, but the dependency did not answer *now*, so it is not
verifiable and must not be presented as current.

**Some resources are never served from cache.** Logs and live health verdicts
are marked uncacheable: a stale log tail is indistinguishable from a live one
and would send someone debugging the wrong minute, and a cached "healthy"
verdict during an outage is the exact lie the whole design exists to prevent.

It is centralised in `app/api/envelope.py::resolve_live()` rather than
per-route, because a per-route `try/except` would be forgotten exactly once — on
the route that mattered. Bugs are deliberately **not** caught there: a
`DependencyUnavailableError` gets the envelope, anything else stays a 500, so a
broken code path cannot quietly serve nulls forever.

`/clusters/{id}/health` reads `live_status` slightly differently and it is
worth knowing why. A probe that successfully determines *Milvus is down* has
**succeeded** — so `live_status` stays `ok` and the outage is reported inside
`live` as `status: "unavailable"` with the rule number and error code. Nulling
`live` would discard exactly the information the endpoint exists to deliver.
`degraded_reason` is populated either way, so a client has one field to check.

### Surviving a PostgreSQL outage

`/clusters/{id}/health` must keep answering with live Milvus data and
`cluster: null` when the database is down — but `endpoint_uri` *lives* in that
database. It is resolved from a long-window last-known-good cache of the cluster
row, populated on every successful metadata read.

One trap found by drilling it: when a container stops, Docker removes its DNS
record, so from inside the compose network the failure is `socket.gaierror`
(name resolution), **not** a refused connection — and a gaierror raised inside
asyncpg's connect is not a DBAPI error, so SQLAlchemy never wraps it. It escaped
as a raw `OSError` and every metadata route returned 500 instead of 503. All
"database is unreachable" detection now shares one `DATABASE_UNREACHABLE` tuple
in `app/db/session.py`.

---

## Health aggregation: six ordered rules

`app/services/health_service.py::aggregate_status()` is the single place that
decides overall status. Order is the specification, not an implementation
detail.

| # | Condition | Status |
|---|---|---|
| 6 | Milvus was not probed at all — checked **first**, as a floor | `unknown` |
| 1 | Milvus gRPC unreachable | `unavailable` |
| 2 | Connected, but the deep probe failed | `degraded` |
| 2b | Object store or metadata store probed and down | `degraded` |
| 3 | An expected component is not running | `degraded` |
| 4 | Metrics scrape or Docker socket failing | `degraded` |
| 5 | Otherwise | `healthy` |

Rule 6 is checked first because it is a precondition: "we could not look" must
never fall through to rule 5's `healthy`.

**Why a dead Docker socket is `degraded`, not `unknown`.** Rules 3 and 4 both
need Docker, so losing it means rule 3 cannot be evaluated — which sounds like
rule 6. But rules 1 and 2 have already established that Milvus answers *and*
serves. The cluster is demonstrably working; what is lost is visibility. That is
observability loss, and it should be visible rather than hidden behind an
ambiguous `unknown`.

**Why `BREAKER_OPEN` maps to `unavailable`, not `unknown`.** A short-circuit
means the probe was skipped, so `unknown` looks honest. It is wrong twice over:
the breaker only opens after `fail_max` consecutive real failures, so there *is*
recent evidence — and reporting `unknown` would flip the status mid-outage
(`unavailable → unknown`) the moment the breaker tripped, emitting a second
`health_transition` event for a single incident. A stable status through an
outage is required, not incidental.

**Why rule 2b exists at all.** With MinIO stopped, Milvus's `/healthz` returns
200 *and* the deep probe passes completely — `list_collections` and
`describe_collection` are answered from etcd metadata via RootCoord and never
touch object storage. The only thing that noticed was component reconciliation,
which works solely because MinIO happens to be a container this control plane
can see. Against S3, or on Kubernetes, that signal disappears. The direct store
probes make detection independent of Docker. Measured in
[RELIABILITY.md scenario C](RELIABILITY.md).

---

## The data model

Five tables. One is mutable state; four are append-only.

```
clusters (UUID PK)  ──┬─< health_checks        (BIGSERIAL, append-only, ON DELETE CASCADE)
                      ├─< component_status     (BIGSERIAL, append-only, ON DELETE CASCADE)
                      ├─< collection_snapshots (BIGSERIAL, append-only, ON DELETE CASCADE)
                      └─< events               (BIGSERIAL, append-only, ON DELETE SET NULL)
```

| Table | Shape | Notes |
|---|---|---|
| `clusters` | mutable, UUID PK | Registered deployments. Soft-deleted (`deployment_status = 'deleted'`) |
| `health_checks` | append-only time series | One row per probe. Index `(cluster_id, checked_at DESC)` |
| `component_status` | append-only | One row per component per snapshot. `DISTINCT ON` reconstructs the current view |
| `collection_snapshots` | append-only | Per-collection stats over time |
| `events` | append-only | The incident and audit trail |

**Three native PostgreSQL enums** (`deployment_type`, `deployment_status`,
`health_status`) are created before the tables that reference them and dropped
after, in a hand-written migration. They are declared with `create_type=False`
so SQLAlchemy does not try to emit `CREATE TYPE` implicitly per table, and with
`values_callable` so the lowercase *values* are stored rather than the Python
member names. Vocabularies expected to grow — component state, event type,
severity — are plain `TEXT` instead, because widening a `TEXT` column costs
nothing whereas `ALTER TYPE ... ADD VALUE` cannot run inside a transaction.

**Why `component_status` is append-only despite an "upsert" in the brief.** The
schema gives it `BIGSERIAL` + `observed_at` and prunes it by age, which is an
append-only series by definition. An upsert would keep one row per component and
make retention meaningless — and, more importantly, destroy the previous
observation that `component_state_change`-on-transition depends on. With a
single mutable row there is nothing to compare against.

**`events.cluster_id` is `ON DELETE SET NULL`**, alone among the four. If a
cluster is ever hard-deleted the incident history must survive it; that history
is the entire point of the table. The other three cascade because a sample
series about a cluster that no longer exists is noise.

### The transition contract

`events` gets a row **only when something changes**, never per poll. At a 15 s
interval a per-poll writer would add ~5 760 rows a day and bury the handful that
describe an actual incident. Measured across the drills: **19 consecutive
`unavailable` health checks produced exactly 2 events** — one going down, one
coming back.

The previous status is read from `clusters.last_health_status` under a row lock
inside the same transaction as the write, not from process memory. An in-memory
version would emit a spurious transition every time the API restarted — which,
during an incident, is exactly when it would lie.

---

## Concurrency and timeouts

pymilvus is synchronous, so every Milvus call is pushed through
`asyncio.to_thread` with **two nested deadlines**:

- the **inner** `timeout=` handed to pymilvus, which becomes a real gRPC
  deadline — this is the one that actually works;
- the **outer** `asyncio.wait_for`, which bounds the coroutine so the event loop
  is never held up, but *cannot* cancel a thread already blocked in C code.

Without the inner deadline a paused Milvus leaks one worker thread per probe,
forever. The drills show both deadlines in the latency signature: a warm channel
against a hung Milvus fails at 5 004 ms (`MILVUS_RPC_TIMEOUT_S`), while a
reconnecting one fails at ~3 010 ms (`MILVUS_CONNECT_TIMEOUT_S`).

### The `/overview` fan-out

Seven sources, concurrent, under a 6 s global budget. Two things make that work:

**Per-branch sub-budgets, all strictly under the global.** `MILVUS_RPC_TIMEOUT_S`
is 5 s and the adapter adds a thread-handoff margin, so a single slow probe
could otherwise consume the entire budget and starve every other panel. The
global becomes a backstop that should never fire.

**`asyncio.wait`, not `gather(..., return_exceptions=True)` inside a
`wait_for`.** The contract asks for both a global timeout *and* "partial results
always returned", and those are contradictory as usually written: when the outer
`wait_for` fires it cancels the gather and every branch is lost, including the
ones that already finished. `asyncio.wait` returns the completed set and cancels
only what is still running.

Measured against a fully hung Milvus: **3.5 s**, inside budget, with the health
panel honestly reporting `unknown`.

---

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
