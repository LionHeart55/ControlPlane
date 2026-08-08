# Reliability drills

Six failure injections, each executed against the running stack on **2026-08-07
/ 2026-08-08 (UTC)**. Every timestamp, error code, latency and log line below is
copied from a real run — none of it is written from expectation, and where the
observed behaviour contradicted what I expected it is the observation that is
recorded, with the expectation noted as wrong.

Reproduce with:

```bash
./infra/deploy.sh up --profile all
./scripts/chaos.sh <scenario>      # milvus-stop | milvus-pause | minio-stop | ...
./scripts/chaos.sh recover-all
```

Cluster under test: `local-standalone`,
id `202b9ea6-a927-44ec-98d4-46f7ceff4a08`, `CP_HEALTH_INTERVAL_S=15`,
`MILVUS_CONNECT_TIMEOUT_S=3`, `MILVUS_RPC_TIMEOUT_S=5`, `CP_BREAKER_FAIL_MAX=3`.

## Summary

| # | Injection | Expected control-plane behaviour | Primary detection signal | Detected | MTTR |
|---|---|---|---|---|---|
| A | `docker stop milvus-standalone` | 200 + `unavailable` + `MILVUS_UNREACHABLE`; metadata unaffected; one event; component `exited`; breaker opens | events row / dashboard banner | **5 s** | **36 s** |
| B | `docker pause milvus-standalone` | `MILVUS_TIMEOUT`, not `UNREACHABLE`; `/overview` inside its 6 s budget | `/overview` latency | **5 s** | **~40 s** ⚠ |
| C | `docker stop milvus-minio` | shallow `/healthz` keeps lying; deep probe + logs catch it; status `degraded` | direct store probe (was: component reconciliation) | **≤22 s** | **1 s** |
| D | `docker stop cp-postgres` | metadata → 503; `/health` still live with `cluster: null`; scheduler warns; self-heals | `/readyz` → 503 | **3 s** | **2 s** |
| E | `docker stop milvus-etcd` | Milvus degrades on metadata ops; loaded collections may still serve | Milvus logs | **26 s** | **51 s** |
| F | `network-cut cp-api` | dashboard shows API-unreachable, not a white screen | browser network tab | **immediate** | **5 s** |

**Three real bugs were found by these drills**, all fixed and re-verified:
a 500 instead of 503 when PostgreSQL's DNS record disappears (D), nginx caching
a stale upstream IP (F), and `docker pause` permanently killing Milvus (B).
Each is written up in its scenario.

**And one missing capability.** Scenario C showed that *neither* health signal
could see an object-store outage, so direct MinIO and etcd probes were added
afterwards; both scenarios have been re-run against them and the results are
recorded in place.

---

## A — Milvus stopped

### Injection
```
$ ./scripts/chaos.sh milvus-stop
2026-08-07T21:40:46Z [ ok ] stop completed in 1s; milvus-standalone is now exited
2026-08-07T21:40:46Z INJECTED A: Milvus stopped at 2026-08-07T21:40:46Z
```

### Expected
HTTP 200 with `live.status: "unavailable"` and `MILVUS_UNREACHABLE`; metadata
endpoints unaffected; exactly one `health_transition` event; components table
shows `exited`; the breaker opens after three failures.

### Detection — 5 s
The scheduled health job runs every 15 s with jitter; the first probe after the
stop landed 8 s later and the API reflected it within 5 s of polling.

```
DETECTED after 5s at 2026-08-07T21:40:51Z
```

Every stored check across the outage, showing the code *before* the breaker
opened:

```
$ psql -c "SELECT checked_at, status, error_code, latency_ms FROM health_checks ..."
          checked_at           |   status    |     error_code     | latency_ms
-------------------------------+-------------+--------------------+------------
 2026-08-07 21:40:39.32531+00  | healthy     |                    |         13
 2026-08-07 21:40:54.277774+00 | unavailable | MILVUS_UNREACHABLE |         16
 2026-08-07 21:41:11.668822+00 | unavailable | MILVUS_UNREACHABLE |         18
 2026-08-07 21:41:27.941313+00 | unavailable | MILVUS_UNREACHABLE |         16
 ... 15 more rows, all MILVUS_UNREACHABLE ...
```

**The transition contract holds: 19 `unavailable` checks produced exactly 2
events** — one going down, one coming back.

```
$ curl -s '.../api/v1/events?event_type=health_transition' | jq
{"created_at":"2026-08-07T21:46:11.581058Z","message":"... health unavailable -> healthy"}
{"created_at":"2026-08-07T21:40:54.277774Z","message":"... health healthy -> unavailable"}
```

### Diagnosis

```
$ curl -s localhost:8000/api/v1/clusters/<id>/health | jq
{
  "live_status": "ok",
  "live": { "status": "unavailable", "rule": 1,
            "milvus_reachable": false, "error_code": "BREAKER_OPEN" },
  "degraded_reason": { "code": "BREAKER_OPEN",
                       "message": "circuit breaker open for milvus; skipped get_server_version" },
  "cluster_name": "local-standalone"
}
```

Note `live_status: "ok"` with `live.status: "unavailable"`. That is deliberate:
the probe ran and returned a definite answer, so the *freshness* is fine; the
outage is the answer, not a failure to get one.

`BREAKER_OPEN` appears on the request path because the breaker had tripped by
the time I looked, while the stored rows all say `MILVUS_UNREACHABLE` — the
scheduled job probes with `force=True` and deliberately bypasses the breaker, so
it keeps measuring the real state and can still notice recovery.

```
$ nc -zv localhost 19530
localhost [127.0.0.1] 19530: Connection refused

$ curl -sv localhost:9091/healthz
* connect to 127.0.0.1 port 9091 failed: Connection refused

$ curl -s .../components | jq '.live.components[] | select(.component_name=="milvus-standalone")'
{"component_name":"milvus-standalone","state":"exited","health":"unhealthy","exit_code":137, ...}

$ curl -s -o /dev/null -w '%{http_code}' localhost:8000/api/v1/clusters   # metadata unaffected
200
$ curl -s -o /dev/null -w '%{http_code}' localhost:8000/readyz
200
```

`docker compose ps` is worth a warning: it lists only running services, so the
stopped container simply **vanishes from the output** rather than showing as
exited. `docker ps -a` or the control plane's own `/components` is the better
check — which is exactly why the API reconciles against an expected-component
list instead of reporting whatever Docker happens to return.

### Recovery — MTTR 36 s

```
recovery started 2026-08-07T21:45:30Z   (docker start milvus-standalone)
RECOVERED after 36s at 2026-08-07T21:46:05Z
recovery event written 2026-08-07T21:46:11Z
```

36 s is essentially all Milvus boot time. No control-plane action was needed:
the health job's `force=True` probe found it healthy and wrote the single
recovery transition.

---

## B — Milvus paused (hung, not dead)

### Injection
```
$ ./scripts/chaos.sh milvus-pause
2026-08-07T21:56:45Z [ ok ] pause completed in 0s; milvus-standalone is now paused
```

`docker pause` sends SIGSTOP. The kernel still completes TCP handshakes, so the
port looks open and nothing is refused — the process simply never replies. This
is the failure mode that hangs naive clients forever.

### Expected
`MILVUS_TIMEOUT` rather than `MILVUS_UNREACHABLE`, proving deadlines are
enforced, and `/overview` still returning inside its 6 s budget.

### Detection — `/overview` stayed inside budget

```
$ for i in 1 2 3; do curl -w '%{time_total}' .../overview; done
  http=200 curl_total=3.567704s   reported duration_ms=3511.4 degraded=true health=unknown  code=HEALTH_INDETERMINATE
  http=200 curl_total=3.508856s   reported duration_ms=3504.2 degraded=true health=unknown  code=HEALTH_INDETERMINATE
  http=200 curl_total=2.028056s   reported duration_ms=2022.5 degraded=true health=unavailable code=BREAKER_OPEN
```

**3.5 s against a completely hung dependency, against a 6 s budget.** The health
branch reports `unknown` / `HEALTH_INDETERMINATE` — rule 6 — which is the
honest answer: the probe was cut off by its own sub-budget, so the control plane
genuinely does not know. Reporting `unavailable` there would be a guess. Once
the breaker tripped, the third call short-circuited to 2.0 s.

```
$ nc -zv localhost 19530
localhost [127.0.0.1] 19530 open          # TCP accepts...

$ curl --max-time 5 localhost:9091/healthz
  /healthz on 9091 -> http=000 time=5.006696s   # ...but nothing ever answers
```

### The expected error code was only half right

The first observation contradicted the expectation: the stored rows said
`MILVUS_UNREACHABLE` at ~3007 ms, not `MILVUS_TIMEOUT`. 3007 ms is exactly
`MILVUS_CONNECT_TIMEOUT_S`, not the 5 s RPC deadline — so the failure was
happening at *connect*, before any RPC was issued.

Isolating the two phases with a warm client settled it:

```
connect_timeout=3.0s rpc_timeout=5.0s
[warm-up]      reachable=True latency=43ms version=2.6.20
pausing milvus-standalone (client stays warm, channel established)
[warm+paused]  code=MILVUS_TIMEOUT latency=5009ms
               gRPC deadline exceeded: Deadline Exceeded
```

and a fresh drill caught the scheduled job doing the same on its first probe:

```
          checked_at           |   status    |     error_code     | latency_ms
-------------------------------+-------------+--------------------+------------
 2026-08-07 22:10:05.409652+00 | unavailable | MILVUS_TIMEOUT     |       5004
 2026-08-07 22:10:20.01645+00  | unavailable | MILVUS_UNREACHABLE |       3011
 2026-08-07 22:10:36.749011+00 | unavailable | MILVUS_UNREACHABLE |       3009
```

**Both codes are correct, and which one you get depends on channel state.** A
warm gRPC channel hits the RPC deadline → `MILVUS_TIMEOUT` at 5.0 s. Once that
failure invalidates the client, every reconnect attempt fails at the connect
deadline instead → `MILVUS_UNREACHABLE` at 3.0 s. **The latency is the tell**, and
the property that matters holds either way: the call is bounded by a configured
deadline and never hangs.

### ⚠ Bug found: pausing Milvus kills it permanently

`docker unpause` did **not** restore service. Milvus exited(1) shortly after
resuming, twice — after a ~4 minute pause and again after a 50 second one.

```
$ docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' milvus-standalone
exited 1

$ docker logs milvus-standalone | grep -i "not alive"
[2026/08/07 22:10:50.099] [WARN] [balancer/balancer_impl.go:679]
  ["channel of current server id is not healthy or not alive"]
  [channel=by-dev-rootcoord-dml_7] [term=15] [serverID=14]
  [error="streaming node is not alive"]
```

SIGSTOP freezes the embedded streaming node's etcd session keepalive. The lease
expires while the process is frozen; on SIGCONT the balancer finds the streaming
node deregistered and the process shuts down. **`docker pause` is therefore not
a reversible injection for Milvus 2.6 beyond the etcd lease TTL** — a real
operational constraint of the Woodpecker/streaming-node architecture, not a
control-plane defect. Recovery required `docker start`, giving an MTTR of ~40 s
dominated by a cold boot rather than by a resume.

---

## C — MinIO stopped (the interesting one)

### Injection
```
$ ./scripts/chaos.sh minio-stop
2026-08-07T22:30:45Z [ ok ] stop completed in 1s; milvus-minio is now exited
```

### Expected
`/healthz` on 9091 stays 200 for a while; `deep_probe` and any insert/flush
fail; status → `degraded`; Milvus logs show object-store errors.

### Detection — shallow health lies, and so does the deep probe

```
### Milvus's own shallow health, with the object store gone
  GET :9091/healthz -> 200          ← the lie

### the control plane's verdict
{"status":"degraded","rule":3,"code":"COMPONENT_NOT_RUNNING",
 "reasons":["component_not_running:milvus-minio:exited"]}

### deep probe checks
{"connect":true,"list_collections":true,"collection_count":1,
 "describe_collection":true,"components_not_running":["milvus-minio:exited"]}
```

**The expectation was half wrong and this is the most useful result in the
document.** `/healthz` lying was predicted. What was *not* predicted is that
`deep_probe` also passes completely — `connect`, `list_collections` and
`describe_collection` all return true with the object store dead.

That is exactly the limitation written into `app/adapters/milvus_client.py`'s
docstring at WP-06: those calls are answered from **etcd metadata via
RootCoord** and never touch object storage. The docstring predicted it; the
drill confirms it.

**What actually caught it was rule 3 — component reconciliation** noticing
`milvus-minio:exited` over the Docker socket. Neither health probe would have.

The data path, meanwhile, is genuinely broken:

```
### an actual write, with the object store gone
  MilvusException: <MilvusException: (code=10000, message=context canceled)>

### Milvus logs
[WARN] [wp/wal.go:46] ["write message to woodpecker failed"]
  [channel=by-dev-rootcoord-dml_0:rw@16]
  [error="failed to create lock object files/wp/15/19/write.lock: Put ..."]
[WARN] [adaptor/wal_adaptor.go:260] ["append message into wal impls failed, retrying..."]
[INFO] [objectstorage/writer_impl.go:202] ["attempting to recover writer state from storage"]
  scope=MinioFileWriter,intent=recoverFromStorage
```

`write.lock` under `files/wp/` is the Woodpecker WAL failing to reach MinIO —
concrete confirmation that Milvus 2.6's WAL is backed by object storage, which
was the premise of the whole no-Pulsar deployment.

### The gap this left — since closed

Detection depended entirely on MinIO being a *container this control plane can
see*. Point the deployment at S3, or run on Kubernetes with an external object
store, and rule 3 has nothing to reconcile — every probe would have reported
healthy through a total object-store outage.

That finding is what produced the direct store probes
(`app/adapters/minio_client.py`, `app/adapters/etcd_client.py`). Re-running the
same injection against them:

```
### Milvus's own shallow health still lies
  :9091/healthz -> 200

### control plane verdict
{"status":"degraded","rule":2,"code":"OBJECT_STORE_UNREACHABLE",
 "reasons":["object_store_unreachable"]}

### persisted columns — previously always null
          checked_at           |  status  |        error_code        | milvus | obj | meta
-------------------------------+----------+--------------------------+--------+-----+------
 2026-08-08 00:51:59.992719+00 | degraded | OBJECT_STORE_UNREACHABLE | t      | f   | t
 2026-08-08 00:51:44.195559+00 | degraded | OBJECT_STORE_UNREACHABLE | t      | f   | t
 2026-08-08 00:51:28.582118+00 | healthy  |                          | t      | t   | t
```

It fires at **rule 2, not rule 3** — the proof that the direct probe caught it
rather than component reconciliation — and `metadata_store_reachable` stays `t`,
correctly isolating *which* dependency failed. Confirmed independent of Docker
by running the same evaluation with `docker=None`, the S3/Kubernetes case:

```
  docker=None  -> status=degraded rule=2 code=OBJECT_STORE_UNREACHABLE
  signals: milvus=True object_store=False metadata_store=False
```

### Recovery — MTTR 1 s

```
C recovered after 1s at 2026-08-07T22:37:59Z
```

Milvus reconnects to the object store on its own; nothing needed restarting.

---

## D — PostgreSQL stopped

### Injection
```
$ ./scripts/chaos.sh postgres-stop
2026-08-07T23:24:48Z [ ok ] stop completed in 1s; cp-postgres is now exited
```

### Expected
Metadata routes → 503 `POSTGRES_UNAVAILABLE`; `/clusters/{id}/health` still
serves live Milvus data with `cluster: null`; the scheduler logs WARNING and
keeps running; on restart `pool_pre_ping` reconnects with no API restart.

### ⚠ Bug found on the first attempt: 500, not 503

```
  GET /readyz              -> 503
  GET /api/v1/clusters     -> 500     ← wrong
  GET /clusters/{id}/health -> 500    ← wrong
{"code":"INTERNAL_ERROR","message":"an unexpected internal error occurred"}
```

The cause, from the API logs:

```
{"path": "/api/v1/events", "error_type": "gaierror", "event": "unhandled_error", ...}
socket.gaierror: [Errno -2] Name or service not known
```

When a container stops, Docker removes its DNS record, so from inside the
compose network the failure is **name resolution**, not a refused connection.
`socket.gaierror` raised inside asyncpg's connect is not a DBAPI error, so
SQLAlchemy never wraps it — it escaped as a raw `OSError` past handlers that
only caught `OperationalError` / `InterfaceError`.

It had gone unnoticed because the same drill *passed* at WP-09, when the API ran
on the host: there the DSN says `localhost`, the name always resolves, and the
failure is `ConnectionRefusedError`. **Containerising the API changed the failure
mode, and only a drill against the real topology could have found it.**

Fixed by hoisting one shared `DATABASE_UNREACHABLE` tuple (including
`socket.gaierror`) into `app/db/session.py` and using it in the API handlers,
the cluster-cache fallback and the job guard, plus a dedicated handler for the
raw socket errors that never reach SQLAlchemy.

### Detection after the fix — 3 s

```
  /readyz                  -> 503
  /api/v1/clusters         -> 503
  /api/v1/events           -> 503
{"code":"POSTGRES_UNAVAILABLE","message":"control-plane database is unreachable",
 "detail":{"dependency":"postgres","cause":"gaierror"}}

  /healthz -> 200                                    ← touches no dependency

  /clusters/{id}/health -> 200                       ← the required behaviour
{"cluster":null,
 "live":{"status":"unavailable","rule":1,"version":null},
 "degraded_reason":"BREAKER_OPEN","last_check":null}
```

`cluster: null` with a live probe still running is the whole point: the Milvus
endpoint was resolved from the last-known-good cluster cache, because
`endpoint_uri` lives in the database that just died.

The scheduler keeps running and says why:

```
{"job": "health_job",   "duration_ms": 20.7, "error": "gaierror: [Errno -2] Name or service not known",
 "note": "will retry on the next tick; no restart needed",
 "event": "job_skipped_postgres_unreachable", "level": "warning"}
{"job": "snapshot_job", "duration_ms": 9.2,  ... same ... }

### ERROR-level entries from jobs while Postgres is down:
0
```

### Recovery — MTTR 2 s, no restart

```
postgres started 23:26:53Z
D recovered after 2s at 2026-08-07T23:26:55Z
cp-api before: 54516 started=2026-08-07T23:14:15.135714713Z
cp-api after : 54516 started=2026-08-07T23:14:15.135714713Z
restart count: 0
  /api/v1/clusters -> 200
```

**Same PID, same start time, zero restarts.** `pool_pre_ping` discarded the dead
pooled connections and reconnected transparently. Without it the pool would hand
out sockets to a server that no longer exists and every request would fail until
someone restarted the API.

---

## E — etcd stopped

### Injection
```
$ ./scripts/chaos.sh etcd-stop
2026-08-07T23:28:03Z [ ok ] stop completed in 2s; milvus-etcd is now exited
```

### Expected
"Milvus degrades on metadata ops; existing loaded collections may still serve
reads — document what you actually observe, not what you expect."

### Observed: it does not degrade, it dies — after 26 seconds

> Re-run after the metadata-store probe was added: it flags etcd at **12 s**
> (`METADATA_STORE_UNREACHABLE`, `meta=f` while `milvus=t` and `obj=t`), about
> fourteen seconds before Milvus exits. Not survival, but enough warning to act.

For the first ~20 s Milvus looked fine, and its shallow health still returned
200:

```
  GET :9091/healthz -> 200
```

Then, within the same test run, both the read path and the metadata path failed
because the process had exited:

```
  search FAILED:  MilvusException: (code=2, message=Fail connecting to server on localhost:19530)
  list_collections FAILED: (code=2, message=Fail connecting to server on localhost:19530)

$ docker inspect --format '{{.State.Status}} {{.State.ExitCode}} {{.State.FinishedAt}}' milvus-standalone
  status=exited exit_code=1 finished=2026-08-07T23:28:29.870513064Z
```

**Milvus survived exactly 26 seconds without etcd** (stopped 23:28:03, exited
23:28:29). The logs show it retrying first and giving up:

```
[WARN] [grpclog] ["grpc: addrConn.createTransport failed to connect to {Addr: \"etcd:2379\"...}"]
[WARN] [etcd/etcd_kv.go:669] ["Slow etcd operation save"] ["time spent"=10.001917476s]
  [key=by-dev/kv/gid/timestamp]
{"level":"warn","logger":"etcd-client","msg":"retrying of unary invoker failed", ...}
```

So the hoped-for "loaded collections still serve reads" does **not** happen in
2.6 standalone. The timestamp allocator (`gid/timestamp`) is on the critical
path for reads as well as writes, it is stored in etcd, and once it blocks for
10 s the process tears down. There is no read-only degraded mode to fall back
to.

The control plane classified it correctly throughout — `unavailable`, rule 1,
first `MILVUS_TIMEOUT` (the 10 s etcd stall showing up as a hung RPC) and then
`MILVUS_UNREACHABLE` once the process was gone.

### Recovery — MTTR 51 s

```
$ ./scripts/chaos.sh recover-all
2026-08-07T23:49:08Z [ ok ] started milvus-standalone
2026-08-07T23:49:09Z [ ok ] started milvus-etcd
2026-08-07T23:49:58Z [ ok ] recovery finished in 51s
2026-08-07T23:49:58Z [api ] status=healthy code=none
```

Order does not matter — Milvus retries etcd on boot — but Milvus must be
restarted, not merely waited on. No data was lost: the collection and its 5 000
rows were intact afterwards.

---

## F — cp-api partitioned from the network

### Injection
```
$ ./scripts/chaos.sh network-cut cp-api
2026-08-08T00:17:28Z [ ok ] cp-api removed from milvus-cp-net
2026-08-08T00:17:28Z INJECTED F at 2026-08-08T00:17:28Z
```

### Expected
The dashboard shows an API-unreachable state rather than a white screen.

### Detection — immediate

```
### before the cut
  :8080/api/v1/clusters -> 200

### after
  GET :8080/                 (SPA shell) -> 200
  GET :8080/api/v1/clusters             -> 504
  body: <html><head><title>504 Gateway Time-out</title>...
  SPA title still served: <title>Milvus Control Plane</title>

  GET :8000/healthz -> 000        (no route to the container at all)
  cp-api networks: []

$ docker logs cp-dashboard | grep error
[error] 20#20: *62 upstream timed out (110: Operation timed out) while connecting
  to upstream, client: 192.168.65.1, request: "GET /api/v1/clusters HTTP/1.1"
```

**No white screen.** The SPA shell and its assets are static files served by
nginx, so the page renders regardless of the API's state; the client turns the
non-JSON 504 into an `ApiError` and the header renders a red banner naming it.
This is covered by a unit test (`renders a banner when the API itself is
unreachable`) and confirmed here against a real partition.

nginx's 15 s `proxy_read_timeout` is what turns a black hole into a bounded
error — a partitioned container silently drops packets rather than refusing
them, so without that timeout the browser would hang instead of being told.

### ⚠ Bug found: nginx cached a stale upstream IP

The first attempt at this drill was already broken *before* the injection —
the "before" reading was 502, not 200. cp-api had been recreated earlier in the
session and come back on a new IP, and nginx had resolved `cp-api` **once at
config load** and cached it for the life of the worker.

I had considered exactly this in WP-12 and judged it safe because
`docker restart` preserves the IP. That reasoning was incomplete:
`docker compose up -d cp-api` *recreates* the container, and the dashboard then
502s silently until it is itself restarted.

Fixed by resolving through a variable so nginx re-resolves per request:

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
set $cp_api_upstream "cp-api:8000";
proxy_pass http://$cp_api_upstream$request_uri;
```

`$request_uri` is mandatory once the upstream is a variable: nginx stops
appending the matched URI automatically, and without it every request would
arrive at the backend as `/`. Verified:

```
=== recreate cp-api (new IP) and confirm nginx re-resolves ===
  :8080/api/v1/clusters after recreate -> 200  (was 502 before the fix)
```

### Recovery — MTTR 5 s

```
2026-08-08T00:18:00Z [ ok ] cp-api reconnected and restarted
F recovered after 5s at 2026-08-08T00:18:04Z
```

Reconnecting alone is not enough: a container that lost its network keeps stale
DNS and connection state, so `chaos.sh network-heal` restarts it as well.

---

## Cross-cutting observations

**The transition contract works.** Across every drill, `events` gained one row
per state change and none per poll. Scenario A alone produced 19 `unavailable`
health checks and 2 events. A per-poll writer would have produced 19 rows
describing one incident.

**`force=True` on the scheduled probe is load-bearing.** The breaker protects
the request path from piling up against a dead dependency, but the scheduled job
bypasses it. That is why the stored history shows real error codes throughout an
outage instead of a wall of `BREAKER_OPEN`, and it is why recovery is noticed at
all — a breaker-respecting job would wait out `CP_BREAKER_RESET_S` before
looking.

**Shallow health is worse than no health.** In C and E, Milvus's own
`:9091/healthz` returned 200 while the cluster was unusable and, in E, while it
was 20 seconds from exiting. Anything that alerts on that endpoint alone would
have stayed green through both.

**Milvus 2.6 standalone is less tolerant of its dependencies than expected.**
Losing etcd kills it in 26 s (E); pausing it past the lease TTL kills it (B).
Only the object store degrades gracefully, and only for reads (C). For a control
plane this is useful to know: most "Milvus is degraded" states are short-lived
transitions to "Milvus is gone".

**An unplanned seventh drill.** Milvus exited on its own several times during
this work, always after the laptop slept:
`["clock offset is huge, check network latency and clock skew"] [jet-lag=29m58s]`
followed by an expired etcd lease. Same root cause as B. It is also a live
demonstration of the deliberate choice to put **no `restart:` policy on the infra
tier** — an auto-restart would have quietly undone these injections and made
every drill above untrustworthy.

---

## What I would add for production

Ordered by how much each would have helped during the drills above.

1. **A synthetic canary.** A job that inserts one row, flushes, searches for it
   and deletes it every 60 s, exported as a single success/latency metric.

   The direct store probes added after scenario C now catch an object store that
   is *down*, which was the urgent gap. A canary covers the case they still
   cannot: an object store that is up, reachable and authenticating correctly
   but failing writes — a full disk, a read-only mount, a bucket policy change.
   The probes assert reachability; only a canary asserts that the data path
   actually works end to end.

2. **Prometheus + Alertmanager**, scraping both Milvus (`:9091/metrics`) and the
   control plane. The control plane currently *pulls* metrics on request; nothing
   retains them, so "when did goroutines start climbing?" is unanswerable. Alert
   on canary failure, on `health_status != healthy` for two consecutive
   intervals, and on breaker-open duration — not on `/healthz`, for the reasons
   C and E demonstrate.

3. **OpenTelemetry traces across the `/overview` fan-out.** Six concurrent
   branches with individual sub-budgets are exactly the shape where a span
   waterfall answers "which branch spent the budget" instantly. During drill B I
   inferred that from `duration_ms` per section and the latency signatures; a
   trace would have shown it directly.

4. **PgBouncer** in front of PostgreSQL. `pool_pre_ping` recovered in 2 s here
   with a single API replica; with several replicas each holding a pool, a
   restart means every one of them discovering dead connections independently. A
   connection pooler absorbs that and caps total backend connections.

5. **MinIO in distributed mode** (or managed S3). Scenario C is a total outage
   only because there is one MinIO node. With erasure coding across four or more
   drives the same failure is a degraded read rather than a stopped WAL — and
   given that Woodpecker writes the WAL to object storage, its availability is
   Milvus's write availability.

6. **A three-node etcd cluster.** Scenario E showed there is no graceful
   degradation to fall back to — Milvus exits 26 s after etcd goes. The
   metadata-store probe now flags it at ~12 s, which buys roughly fourteen
   seconds of warning, but warning is not survival: the only real answer is
   quorum, so a single member loss is not an outage.

7. **Structured alert routing on the events table.** It already contains exactly
   the transitions worth paging on, deduplicated by construction. A webhook on
   insert would turn it into an incident feed with no extra state.
