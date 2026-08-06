# 03 — BACKEND SPECIFICATION, MODULE BY MODULE, FUNCTION BY FUNCTION

> Every file in `control_plane/app/`, with exact signatures, behaviours, raised exceptions, and acceptance checks. Build in the order given; each module only imports from ones already built.

**Import direction (enforce in review):** `api → services → repositories → db` and `services → adapters`. Adapters never import services. Repositories never import adapters. A repository that calls Milvus is a bug.

---

## Build order

```
M-01 config.py
M-02 logging_conf.py
M-03 db/base.py, db/models.py, db/session.py        (spec in Doc 02)
M-04 schemas/common.py
M-05 adapters/circuit_breaker.py
M-06 adapters/cache.py
M-07 adapters/milvus_client.py
M-08 adapters/docker_client.py
M-09 adapters/metrics_client.py + metric_allowlist.py
M-10 repositories/*.py
M-11 services/health_service.py
M-12 services/cluster_service.py
M-13 services/observability_service.py
M-14 services/overview_service.py
M-15 jobs/*.py
M-16 schemas/*.py (remaining)
M-17 api/errors.py, api/deps.py
M-18 api/routers/*.py
M-19 main.py
```

---

## M-01 — `app/config.py`

### Contents

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      case_sensitive=False, extra="ignore")
```

One field per `.env` key from Doc 00 §0.9, with these types and validators:

| Field | Type | Validator |
|---|---|---|
| `postgres_user`, `postgres_password`, `postgres_db`, `postgres_host` | `str` | non-empty |
| `postgres_port` | `int` | 1–65535 |
| `milvus_uri`, `milvus_metrics_uri` | `str` | must match `^(http\|https\|grpc)://` |
| `milvus_connect_timeout_s`, `milvus_rpc_timeout_s` | `float` | `> 0`, `<= 60` |
| `cp_log_level` | `Literal["DEBUG","INFO","WARNING","ERROR"]` | |
| `cp_log_format` | `Literal["json","console"]` | |
| `cp_health_interval_s`, `cp_snapshot_interval_s` | `int` | `>= 5` |
| `cp_cache_ttl_s`, `cp_stale_max_age_s` | `int` | `stale_max_age_s >= cache_ttl_s` (model validator) |
| `cp_breaker_fail_max` | `int` | `>= 1` |
| `cp_breaker_reset_s` | `int` | `>= 1` |
| `cp_retention_days` | `int` | `>= 1` |
| `cp_overview_budget_s` | `float` | `> 0`, `<= 30` |
| `cp_expected_components` | `list[str]` | `field_validator(mode="before")` splitting a comma string |
| `cp_docker_socket` | `str` | |
| `cp_seed_cluster_name` | `str` | |

### Computed properties

```python
@property
def postgres_dsn_async(self) -> str:
    # postgresql+asyncpg://user:pass@host:port/db
@property
def postgres_dsn_sync(self) -> str:
    # postgresql+psycopg://user:pass@host:port/db   (Alembic)
@property
def postgres_dsn_safe(self) -> str:
    # same as async but password replaced with '***'  — THIS is what gets logged
```

### Module function
```python
@lru_cache(maxsize=1)
def get_settings() -> Settings: ...
```

### Rules
- **Never log `postgres_dsn_async`.** Only `postgres_dsn_safe`. Add a `__repr__` on Settings that redacts `postgres_password` and `minio_root_password` so an accidental `print(settings)` cannot leak them.
- Validation failure must abort startup with a readable message naming the offending env var — a control plane that starts with a malformed Milvus URI and reports "unavailable" forever is worse than one that refuses to start.

### Acceptance
```bash
python -c "from app.config import get_settings; s=get_settings(); print(s.postgres_dsn_safe); print(s.cp_expected_components)"
# expect: postgresql+asyncpg://controlplane:***@cp-postgres:5432/controlplane
#         ['milvus-standalone', 'milvus-etcd', 'milvus-minio', 'cp-postgres']
MILVUS_URI=nonsense python -c "from app.config import get_settings; get_settings()"
# expect: ValidationError naming milvus_uri
```

---

## M-02 — `app/logging_conf.py`

### Functions

```python
def configure_logging(level: str, fmt: Literal["json","console"]) -> None
```
Sets up `structlog` with this processor chain: `merge_contextvars` → `add_log_level` → `TimeStamper(fmt="iso", utc=True)` → `StackInfoRenderer` → `format_exc_info` → (`JSONRenderer` | `ConsoleRenderer`). Routes stdlib `logging` through structlog so `uvicorn`, `sqlalchemy`, `apscheduler` and `docker` log lines are structured too. Sets `sqlalchemy.engine` to WARNING and `apscheduler.executors` to WARNING (otherwise every 15-second job logs twice).

```python
class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next) -> Response
```
- Reads `X-Request-ID` or generates `uuid4().hex`.
- `structlog.contextvars.bind_contextvars(request_id=..., path=..., method=...)`.
- Times the call with `time.perf_counter()`.
- Logs one line at completion: `event="http_request"`, `status_code`, `duration_ms` (rounded to 2dp), `client_ip`.
- Adds `X-Request-ID` to the response headers.
- `finally: clear_contextvars()` — without this, contextvars leak across requests on the same worker task and your logs will attribute one request's id to another.

### Acceptance
Every startup log line is valid JSON with `timestamp`, `level`, `event`. `curl -H 'X-Request-ID: abc123' localhost:8000/healthz -i` echoes `X-Request-ID: abc123` and produces exactly one `http_request` log line containing `abc123`.

---

## M-04 — `app/schemas/common.py`

The degradation envelope lives here, and everything else depends on it.

```python
class LiveStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    UNAVAILABLE = "unavailable"

class DegradedReason(BaseModel):
    code: str                      # MILVUS_UNREACHABLE, DOCKER_UNAVAILABLE, ...
    message: str
    dependency: str                # "milvus" | "docker" | "metrics" | "postgres"
    since: datetime | None = None

T = TypeVar("T")

class Envelope(BaseModel, Generic[T]):
    live: T | None
    live_status: LiveStatus
    observed_at: datetime | None
    stale: bool = False
    degraded_reason: DegradedReason | None = None

    @classmethod
    def ok(cls, value: T) -> "Envelope[T]"
    @classmethod
    def stale_value(cls, value: T, observed_at: datetime,
                    reason: DegradedReason) -> "Envelope[T]"
    @classmethod
    def unavailable(cls, reason: DegradedReason) -> "Envelope[T]"

class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] = {}

class ErrorResponse(BaseModel):
    error: ErrorBody
```

**The three constructors are the enforcement mechanism.** Services must build envelopes only through them, never by hand — that is what guarantees `stale=True` is always accompanied by an `observed_at`, and that `live=None` is always accompanied by a `degraded_reason`.

### Canonical error codes — the complete list

| Code | Dependency | Meaning |
|---|---|---|
| `MILVUS_UNREACHABLE` | milvus | TCP refused, DNS failure, channel dead |
| `MILVUS_TIMEOUT` | milvus | RPC exceeded `milvus_rpc_timeout_s` |
| `MILVUS_RPC_ERROR` | milvus | server returned an error status |
| `MILVUS_COLLECTION_NOT_FOUND` | milvus | named collection absent |
| `MILVUS_AUTH_FAILED` | milvus | credentials rejected |
| `METRICS_UNREACHABLE` | metrics | 9091 scrape failed |
| `METRICS_PARSE_ERROR` | metrics | non-Prometheus body |
| `DOCKER_UNAVAILABLE` | docker | socket missing or permission denied |
| `DOCKER_CONTAINER_NOT_FOUND` | docker | component not present |
| `POSTGRES_UNAVAILABLE` | postgres | connection failed |
| `BREAKER_OPEN` | * | short-circuited by the circuit breaker |
| `CLUSTER_NOT_FOUND` | — | 404 |
| `CLUSTER_NAME_CONFLICT` | — | 409 |
| `VALIDATION_ERROR` | — | 422 |
| `INTERNAL_ERROR` | — | 500 |

Freeze this list. The dashboard switches on these strings; inventing a new one at call sites will silently render as "unknown error".

---

## M-05 — `app/adapters/circuit_breaker.py`

No third-party dependency; ~80 lines.

```python
class BreakerState(str, Enum):
    CLOSED = "closed"; OPEN = "open"; HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, name: str, fail_max: int, reset_timeout_s: float,
                 on_open: Callable[[str,int],Awaitable[None]] | None = None,
                 on_close: Callable[[str],Awaitable[None]] | None = None) -> None

    @property
    def state(self) -> BreakerState        # computes HALF_OPEN lazily from the clock
    @property
    def consecutive_failures(self) -> int
    @property
    def opened_at(self) -> datetime | None

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T
    def snapshot(self) -> dict            # for /health diagnostics
    def reset(self) -> None               # test helper only
```

### `call()` semantics, exactly
1. If `state is OPEN` → raise `BreakerOpenError(name, opened_at, retry_after_s)` **without invoking `fn`**. This is the point of the breaker: a stopped Milvus must not cost 3 seconds of connect timeout on every request.
2. If `state is HALF_OPEN` → allow exactly **one** concurrent probe (`asyncio.Lock` + a `_probe_in_flight` flag); other callers get `BreakerOpenError` immediately.
3. Await `fn()`. On success: reset failure count, and if the previous state was OPEN or HALF_OPEN, transition to CLOSED and fire `on_close`.
4. On exception: increment; if count `>= fail_max` and state was CLOSED, transition to OPEN, record `opened_at`, fire `on_open`. Re-raise the original exception, not a wrapped one.

**Do not count `MILVUS_COLLECTION_NOT_FOUND` or other application-level errors as failures** — only transport-level ones. Pass an `is_failure: Callable[[Exception], bool]` predicate, defaulting to "connection or timeout errors only". A breaker that opens because someone queried a missing collection is worse than no breaker.

### Acceptance (unit, no infra)
- 3 consecutive failures with `fail_max=3` → state OPEN, `on_open` called exactly once.
- 4th call raises `BreakerOpenError` and `fn` was **not** awaited (assert with a mock's `call_count`).
- After `reset_timeout_s` with a frozen clock → state HALF_OPEN; one success → CLOSED, `on_close` fired once.
- HALF_OPEN with two concurrent callers → exactly one invokes `fn`.
- An exception for which `is_failure` returns False does not increment the counter.

---

## M-06 — `app/adapters/cache.py`

```python
@dataclass(frozen=True)
class CachedValue(Generic[T]):
    value: T
    observed_at: datetime

    def age_s(self, now: datetime | None = None) -> float

class LastKnownGoodCache:
    def __init__(self, ttl_s: int, stale_max_age_s: int, maxsize: int = 512)

    def set(self, key: tuple[str, ...], value: Any) -> None
    def get_fresh(self, key) -> CachedValue | None    # None if age > ttl_s
    def get_stale(self, key) -> CachedValue | None    # None if age > stale_max_age_s
    def invalidate(self, key) -> None
    def stats(self) -> dict                            # hits, misses, size — exposed on /healthz
```

Key convention: `(str(cluster_id), resource)` where resource ∈ `{"health","collections","metrics","components"}`. Backed by `cachetools.TTLCache` sized at `stale_max_age_s` (not `ttl_s`) so stale reads remain possible; `get_fresh` applies the tighter bound itself.

**This is not a performance cache.** Its job is to let the dashboard show "as of 12:03:41 (stale)" during an outage instead of an empty panel. Write that in the docstring.

---

## M-07 — `app/adapters/milvus_client.py`

The most important adapter. `pymilvus` is synchronous, so every call is wrapped.

### Value objects
```python
@dataclass(frozen=True)
class ProbeResult:
    reachable: bool
    latency_ms: int | None
    server_version: str | None
    error_code: str | None
    error_message: str | None
    deep_probe_ok: bool | None = None
    collection_count: int | None = None

@dataclass(frozen=True)
class CollectionInfo:
    name: str; row_count: int | None; num_partitions: int | None
    num_fields: int | None; dimension: int | None; vector_field: str | None
    index_type: str | None; metric_type: str | None
    is_loaded: bool | None; load_progress: int | None; raw: dict
```

### Exceptions
```python
class MilvusAdapterError(Exception):
    def __init__(self, code: str, message: str, cause: Exception | None = None)
class MilvusUnreachable(MilvusAdapterError)
class MilvusTimeout(MilvusAdapterError)
class MilvusRpcError(MilvusAdapterError)
class MilvusCollectionNotFound(MilvusAdapterError)
```

### Class
```python
class MilvusAdapter:
    def __init__(self, uri: str, connect_timeout_s: float, rpc_timeout_s: float,
                 breaker: CircuitBreaker) -> None

    async def _client(self) -> MilvusClient            # lazy, lock-guarded
    async def _invalidate_client(self) -> None         # drop on transport failure
    async def _call(self, op_name: str, fn: Callable[[MilvusClient], Any]) -> Any

    async def ping(self) -> ProbeResult
    async def deep_probe(self) -> ProbeResult
    async def get_server_version(self) -> str
    async def list_collections(self) -> list[str]
    async def describe_collection(self, name: str) -> dict
    async def get_collection_stats(self, name: str) -> dict
    async def list_indexes(self, name: str) -> list[str]
    async def describe_index(self, name: str, index_name: str) -> dict
    async def get_load_state(self, name: str) -> tuple[bool, int | None]
    async def get_collection_info(self, name: str) -> CollectionInfo
    async def get_all_collections_info(self) -> list[CollectionInfo]
    async def close(self) -> None
```

### `_call()` — the single choke point. Exact behaviour:
1. `await self._breaker.call(inner)` where `inner`:
2. gets the client via `_client()`,
3. runs `await asyncio.wait_for(asyncio.to_thread(fn, client), timeout=self._rpc_timeout_s)`,
4. catches and translates (see table),
5. on any *transport* failure, calls `_invalidate_client()` before re-raising — **a dead gRPC channel never recovers on its own; reusing it means the system stays "down" after Milvus comes back**. This one line is the difference between auto-recovery and needing an API restart, and it is the most commonly missed detail in this whole build.

### Exception translation table — implement exactly

| Caught | Condition | Raise |
|---|---|---|
| `asyncio.TimeoutError` | — | `MilvusTimeout("MILVUS_TIMEOUT", f"{op_name} exceeded {t}s")` |
| `MilvusException` | `.code` in `{1,2}` or message contains `connect`/`unavailable`/`refused` | `MilvusUnreachable("MILVUS_UNREACHABLE", …)` |
| `MilvusException` | message contains `can't find collection` / `collection not found` | `MilvusCollectionNotFound("MILVUS_COLLECTION_NOT_FOUND", …)` |
| `MilvusException` | message contains `auth`/`unauthenticated`/`permission` | `MilvusAdapterError("MILVUS_AUTH_FAILED", …)` |
| `MilvusException` | anything else | `MilvusRpcError("MILVUS_RPC_ERROR", …)` |
| `grpc.RpcError` | `UNAVAILABLE`/`DEADLINE_EXCEEDED` | `MilvusUnreachable` / `MilvusTimeout` |
| `ConnectionError`, `OSError`, `socket.gaierror` | — | `MilvusUnreachable` |
| `BreakerOpenError` | — | `MilvusUnreachable("BREAKER_OPEN", …)` |

Match on codes first, substrings second, and **write a unit test per row** — `pymilvus` message wording changes between versions and this table is where that will bite you.

### `ping()` vs `deep_probe()` — module docstring must explain this
- `ping()`: `get_server_version()` only. Cheap, ~5 ms. Answers "is the proxy accepting gRPC".
- `deep_probe()`: `get_server_version()` → `list_collections()` → if any exist, `describe_collection(first)` and `get_collection_stats(first)`. ~30 ms. Answers "can Milvus actually serve metadata", which requires etcd **and** exercises paths that fail when the object store is down.

**Why both exist:** Milvus's `:9091/healthz` and a bare `ping()` both keep returning success while MinIO is unavailable, because the proxy is alive and the etcd metadata is cached. Only a deeper call surfaces the fault. Failure drill C in Doc 05 exists to demonstrate exactly this, and `deep_probe` is what detects it.

### Concurrency
One `MilvusAdapter` per cluster endpoint, held in a module-level registry `dict[str, MilvusAdapter]` keyed by URI, created under an `asyncio.Lock`. `MilvusClient` is not documented as thread-safe for concurrent mutation; all access funnels through `_call`, and `to_thread` uses the default executor, so concurrency is bounded by that pool. Set `--workers 1` (Doc 01, I-7) so there is exactly one registry.

### Acceptance
- Unit: every translation-table row, with a mocked `MilvusClient` raising the corresponding error.
- Unit: a transport failure triggers `_invalidate_client`, and the next call constructs a new client (assert constructor call count == 2).
- Integration: against the live stack, `ping().reachable is True` and `deep_probe().deep_probe_ok is True`.
- Integration: `docker stop milvus-standalone` → `ping()` returns `reachable=False, error_code="MILVUS_UNREACHABLE"` within `connect_timeout_s + 0.5`; after 3 attempts the breaker is OPEN and the 4th returns in **under 50 ms**.
- Integration: `docker start milvus-standalone`, wait `breaker_reset_s` → next call succeeds *without restarting the API*. This is the auto-recovery proof.

---

## M-08 — `app/adapters/docker_client.py`

```python
@dataclass(frozen=True)
class ComponentStatus:
    name: str; kind: str; runtime_id: str | None; image: str | None
    state: str; health: str | None; restart_count: int
    exit_code: int | None; started_at: datetime | None; raw: dict

@dataclass(frozen=True)
class LogLine:
    timestamp: datetime | None; stream: str; message: str

class DockerAdapter:
    def __init__(self, socket_path: str, expected_components: list[str],
                 stack_label: str = "milvus-control-plane", call_timeout_s: float = 3.0)

    async def ping(self) -> bool
    async def list_components(self, compose_project: str | None = None) -> list[ComponentStatus]
    async def tail_logs(self, component: str, lines: int = 200,
                        since: timedelta | None = None) -> list[LogLine]
    async def get_component(self, component: str) -> ComponentStatus | None
    async def close(self) -> None
```

### `list_components()` — exact algorithm
1. `client.containers.list(all=True, filters={"label": f"com.milvus-cp.stack={stack_label}"})`. `all=True` is mandatory: a stopped container must appear as `exited`, not vanish.
2. Map each to `ComponentStatus`, reading `com.milvus-cp.component` for `name` (fall back to `container.name`), and from `attrs["State"]`: `Status`, `Health.Status` (may be absent), `RestartCount`, `ExitCode`, `StartedAt` (ISO-8601 with nanoseconds — truncate to microseconds before `fromisoformat`, or it raises).
3. **Reconcile against `expected_components`.** Any expected name with no container gets a synthetic `ComponentStatus(state="missing", runtime_id=None, restart_count=0)`.
4. Sort: expected components in configured order first, extras after.

Step 3 is the whole point. Without it a `docker rm` produces an empty table and the dashboard implies health.

### `tail_logs()` — exact behaviour
- Validate `component` against `expected_components` **inside the adapter**; unknown → `DockerAdapterError("DOCKER_CONTAINER_NOT_FOUND")`. Never interpolate caller input into a lookup.
- Clamp `lines` to `[1, 1000]`.
- `container.logs(tail=lines, since=<epoch int>, timestamps=True, stdout=True, stderr=True, stream=False)`.
- Decode `errors="replace"` (Milvus emits non-UTF-8 bytes occasionally and a `UnicodeDecodeError` here would break the log panel at exactly the wrong moment).
- Split each line on the first space: RFC3339 timestamp + message. Lines that do not parse get `timestamp=None` and the raw text — do not drop them.
- Docker interleaves stdout/stderr in one stream when not TTY-attached; set `stream="stdout"` as a default label and note the limitation rather than pretending to demultiplex.

### Failure handling
`docker.errors.DockerException`, `PermissionError`, `FileNotFoundError` on the socket → `DockerAdapterError("DOCKER_UNAVAILABLE", …)`. The service layer turns this into `live_status="unavailable"` for the components panel **only** — Milvus health must remain independently reported. Losing Docker introspection is an observability degradation, not a Milvus outage.

### Acceptance
- Live stack → 4 components, all `running`, `health="healthy"` on the four with healthchecks.
- `docker stop milvus-standalone` → that entry `state="exited"`, `exit_code` populated, others unaffected.
- `docker rm -f milvus-standalone` → `state="missing"`, list length still 4.
- `tail_logs("../../etc/passwd")` → `DOCKER_CONTAINER_NOT_FOUND`, no filesystem access attempted.
- Socket unmounted → `DOCKER_UNAVAILABLE`, and `/api/v1/clusters/{id}/health` still returns real Milvus data.

---

## M-09 — `app/adapters/metrics_client.py` + `metric_allowlist.py`

### `metric_allowlist.py`
```python
class MetricKind(str, Enum): COUNTER="counter"; GAUGE="gauge"; HISTOGRAM="histogram"

@dataclass(frozen=True)
class MetricSpec:
    name: str                 # exact Prometheus family name
    label: str                # human display label
    kind: MetricKind
    unit: str                 # "count","bytes","seconds","ms",""
    aggregation: Literal["sum","max","min","avg","last"]
    quantiles: tuple[float,...] = ()      # histograms only

ALLOWLIST: tuple[MetricSpec, ...] = (...)
```

**Starting allowlist — treat as a hypothesis, not fact.** Milvus metric names change between minors. Populate it from a real scrape (see `discover()` below) before committing.

| Family | Label | Kind | Agg |
|---|---|---|---|
| `milvus_num_node` | Active nodes | gauge | sum |
| `milvus_rootcoord_collection_num` | Collections | gauge | max |
| `milvus_rootcoord_entity_num` | Entities | gauge | max |
| `milvus_proxy_req_count` | Proxy requests | counter | sum |
| `milvus_proxy_req_latency` | Proxy latency | histogram | — (p50, p95, p99) |
| `milvus_querynode_num_entities` | Queryable entities | gauge | sum |
| `milvus_datacoord_stored_binlog_size` | Stored binlog | gauge | sum (bytes) |
| `milvus_storage_op_count` | Object-store ops | counter | sum |
| `milvus_storage_request_latency` | Object-store latency | histogram | — |
| `go_goroutines` | Goroutines | gauge | max |
| `process_resident_memory_bytes` | Resident memory | gauge | max |
| `process_cpu_seconds_total` | CPU seconds | counter | max |

### `metrics_client.py`
```python
@dataclass(frozen=True)
class MetricValue:
    name: str; label: str; value: float | None; unit: str
    available: bool; quantiles: dict[str,float] | None = None

class MetricsAdapter:
    def __init__(self, base_uri: str, timeout_s: float = 2.0)
    async def scrape_raw(self) -> str
    async def collect(self) -> list[MetricValue]
    async def discover(self) -> list[dict]      # {name, type, sample_count} for every family
    async def healthz(self) -> bool             # GET /healthz -> bool
    async def close(self) -> None
```

### `collect()` — exact algorithm
1. `GET {base_uri}/metrics` with `httpx.AsyncClient(timeout=2.0)`. Non-200 → `MetricsError("METRICS_UNREACHABLE")`. Connect failure → same. Body that fails to parse → `METRICS_PARSE_ERROR`.
2. `prometheus_client.parser.text_string_to_metric_families(body)` → index by family name.
3. For each `MetricSpec`:
   - family missing → `MetricValue(value=None, available=False)`. **Emit the entry; never omit it and never raise.** A silently shorter list is how a metrics rename becomes an invisible regression.
   - counter/gauge → collapse samples across label dimensions with the spec's aggregation.
   - histogram → compute quantiles from cumulative buckets by linear interpolation within the containing bucket; if the top bucket is `+Inf` and holds the quantile, report the last finite bucket boundary and flag it. **Do not report a bucket sum as a latency.**
4. Return in allowlist order so the dashboard layout is stable across polls.

### `discover()` — how you build the real allowlist
Run once during development:
```bash
curl -s localhost:9091/metrics | grep '^# TYPE' | awk '{print $3, $4}' | sort | head -80
```
or `python -c "import asyncio, ...; print(await MetricsAdapter(...).discover())"`. Record in `docs/ARCHITECTURE.md` that the allowlist was derived empirically from `v2.6.20`, with the date. That sentence is worth real credit — it shows the list is not guessed.

### Acceptance
- Unit: parse a captured `tests/fixtures/milvus_metrics.txt` → ≥ 8 entries `available=True`.
- Unit: same fixture with three families deleted → those three come back `available=False, value=None`, list length unchanged, no exception.
- Unit: a hand-built histogram fixture with known buckets → p50/p99 within 5% of the analytic answer.
- Integration: live stack → ≥ 6 available; `docker stop milvus-standalone` → `METRICS_UNREACHABLE` within 2.5 s.

---

## M-10 — `app/repositories/`

Thin, async, no business logic, no adapter imports. Signatures:

### `cluster_repo.py`
```python
async def create(s: AsyncSession, data: ClusterCreate) -> Cluster
async def get_by_id(s, cluster_id: UUID) -> Cluster | None
async def get_by_name(s, name: str) -> Cluster | None
async def list_clusters(s, *, status: DeploymentStatus | None,
                        limit: int, offset: int) -> tuple[list[Cluster], int]
async def update(s, cluster_id: UUID, data: ClusterUpdate) -> Cluster | None
async def soft_delete(s, cluster_id: UUID) -> bool
async def update_health_denorm(s, cluster_id: UUID, *, status: HealthStatus,
                               checked_at: datetime, milvus_version: str | None,
                               deployment_status: DeploymentStatus) -> None
```

### `health_repo.py`
```python
async def insert(s, cluster_id: UUID, result: HealthCheckRecord) -> HealthCheck
async def latest(s, cluster_id: UUID) -> HealthCheck | None
async def history(s, cluster_id: UUID, *, since: timedelta, limit: int) -> list[HealthCheck]
async def previous_status(s, cluster_id: UUID) -> HealthStatus | None
async def purge_older_than(s, days: int) -> int
```

### `component_repo.py`
```python
async def bulk_insert(s, cluster_id: UUID, items: list[ComponentStatusRecord]) -> int
async def latest_per_component(s, cluster_id: UUID) -> list[ComponentStatusRow]   # DISTINCT ON
async def previous_states(s, cluster_id: UUID) -> dict[str, str]
async def purge_older_than(s, days: int) -> int
```

### `collection_repo.py`
```python
async def bulk_insert(s, cluster_id, items: list[CollectionSnapshotRecord]) -> int
async def latest_per_collection(s, cluster_id) -> list[CollectionSnapshotRow]     # DISTINCT ON
async def history_for(s, cluster_id, name: str, limit: int) -> list[CollectionSnapshotRow]
async def purge_older_than(s, days: int) -> int
```

### `event_repo.py`
```python
async def insert(s, *, cluster_id: UUID | None, event_type: str, severity: str,
                 message: str, payload: dict) -> Event
async def list_events(s, *, cluster_id: UUID | None, event_type: str | None,
                      limit: int, offset: int) -> tuple[list[Event], int]
async def purge_older_than(s, days: int) -> int
```

**Transactional rule:** repositories never commit. The caller owns the transaction (`async with session.begin():`). The only exception is `purge_older_than`, which commits per table to avoid one long transaction — document that exception in the module docstring.

---

## M-11 — `app/services/health_service.py`

The most important service. All status decisions live here and nowhere else.

```python
@dataclass(frozen=True)
class HealthAssessment:
    status: HealthStatus
    latency_ms: int | None
    milvus_reachable: bool
    milvus_deep_probe_ok: bool | None
    object_store_reachable: bool | None
    metadata_store_reachable: bool | None
    docker_reachable: bool | None
    server_version: str | None
    collection_count: int | None
    error_code: str | None
    error_message: str | None
    component_issues: list[str]
    raw: dict

class HealthService:
    def __init__(self, milvus: MilvusAdapter, docker: DockerAdapter,
                 metrics: MetricsAdapter, settings: Settings)

    async def probe(self, cluster: Cluster) -> HealthAssessment
    def aggregate_status(self, *, milvus: ProbeResult,
                         components: list[ComponentStatus] | None,
                         metrics_ok: bool, docker_ok: bool,
                         expected: list[str]) -> tuple[HealthStatus, str | None]
    async def run_check_and_persist(self, s: AsyncSession, cluster: Cluster) -> HealthCheck
```

### `probe()` — concurrent, bounded
`asyncio.gather(milvus.deep_probe(), docker.list_components(...), metrics.healthz(), return_exceptions=True)` under `asyncio.wait_for(..., timeout=settings.cp_overview_budget_s)`. Each exception becomes a `None`/`False` sub-result plus an error code; one failing branch never aborts the others.

### `aggregate_status()` — the ordered rule set. Implement as an explicit if-chain in this order; do not reorder for elegance.

| # | Condition | Result | Reason code |
|---|---|---|---|
| 1 | `milvus.reachable is False` | `UNAVAILABLE` | `milvus.error_code` |
| 2 | `milvus.reachable and milvus.deep_probe_ok is False` | `DEGRADED` | `MILVUS_DEEP_PROBE_FAILED` |
| 3 | `docker_ok and any(expected component state != "running")` | `DEGRADED` | `COMPONENT_NOT_RUNNING` |
| 4 | `docker_ok is False or metrics_ok is False` | `DEGRADED` | `OBSERVABILITY_DEGRADED` |
| 5 | `milvus.reachable is None` (probe could not run at all) | `UNKNOWN` | `PROBE_FAILED` |
| 6 | otherwise | `HEALTHY` | `None` |

Rule 4 is a judgement call worth defending explicitly in the README: losing the ability to observe is not the same as an outage, but a control plane that reports `healthy` while blind is lying. `DEGRADED` with reason `OBSERVABILITY_DEGRADED` communicates both facts.

Rule 5 must never be reachable *after* rules 1–4 in practice; it exists as a guard so that an unhandled path yields `UNKNOWN` rather than falling through to `HEALTHY`. **Write the function so `HEALTHY` is only reachable by explicit positive evidence.**

### `run_check_and_persist()` — exact sequence
```
assessment = await self.probe(cluster)
async with session.begin():
    prev = await health_repo.previous_status(s, cluster.id)
    row  = await health_repo.insert(s, cluster.id, record_from(assessment))
    await cluster_repo.update_health_denorm(s, cluster.id, status=..., checked_at=...,
                                            milvus_version=..., deployment_status=map_status(...))
    if prev != assessment.status:
        await event_repo.insert(s, cluster_id=cluster.id,
            event_type="health_transition",
            severity=severity_for(assessment.status),
            message=f"health changed {prev} -> {assessment.status}",
            payload={"from": prev, "to": assessment.status,
                     "error_code": assessment.error_code,
                     "latency_ms": assessment.latency_ms})
return row
```

`deployment_status` mapping: `healthy→running`, `degraded→degraded`, `unavailable→unavailable`, `unknown→` *leave unchanged*.

`severity_for`: `healthy→info`, `degraded→warning`, `unavailable→error`, `unknown→warning`.

**The transition guard is the entire feature.** Without `if prev != status`, you write 5,760 event rows a day and the incident strip is useless.

### Acceptance — a truth-table unit test, one case per rule
Six parametrised cases minimum, plus: reachable + deep-probe-ok + one component `exited` → `DEGRADED`; everything fine but Docker socket down → `DEGRADED/OBSERVABILITY_DEGRADED` (**not** `HEALTHY`); and an integration test asserting that ten consecutive checks with an unchanged status produce exactly one event row.

---

## M-12 — `app/services/cluster_service.py`

```python
class ClusterService:
    async def register(self, s, payload: ClusterCreate) -> Cluster
    async def get(self, s, cluster_id: UUID) -> Cluster
    async def list(self, s, *, status, limit, offset) -> tuple[list[Cluster], int]
    async def update(self, s, cluster_id: UUID, payload: ClusterUpdate) -> Cluster
    async def delete(self, s, cluster_id: UUID) -> None
    async def resolve_default(self, s) -> Cluster | None
```
- `register`: check name uniqueness → `ConflictError("CLUSTER_NAME_CONFLICT")`; default `expected_components` from settings when omitted; write a `cluster_registered` event in the same transaction.
- `get`/`update`/`delete` raise `NotFoundError("CLUSTER_NOT_FOUND")` on miss.
- `resolve_default`: returns the single active cluster if there is exactly one, else `None`. This is what lets the dashboard boot without a cluster id in the URL.

---

## M-13 — `app/services/observability_service.py`

Wraps the three read-only adapters in envelopes and the cache.

```python
class ObservabilityService:
    def __init__(self, milvus, docker, metrics, cache: LastKnownGoodCache, settings)

    async def collections(self, cluster) -> Envelope[list[CollectionInfo]]
    async def collection_detail(self, cluster, name: str) -> Envelope[CollectionInfo]
    async def metrics(self, cluster) -> Envelope[list[MetricValue]]
    async def components(self, cluster) -> Envelope[list[ComponentStatus]]
    async def logs(self, cluster, component: str, lines: int,
                   since: timedelta | None) -> Envelope[list[LogLine]]
```

### The identical pattern in each — implement it once as a helper, then reuse
```python
async def _with_envelope(self, key, fetch, dependency) -> Envelope:
    fresh = self._cache.get_fresh(key)
    if fresh: return Envelope.ok(fresh.value)
    try:
        value = await fetch()
        self._cache.set(key, value)
        return Envelope.ok(value)
    except AdapterError as e:
        stale = self._cache.get_stale(key)
        reason = DegradedReason(code=e.code, message=str(e), dependency=dependency)
        if stale:
            return Envelope.stale_value(stale.value, stale.observed_at, reason)
        return Envelope.unavailable(reason)
```

**Never let an adapter exception escape this layer.** Everything above assumes envelopes. A single unhandled `MilvusUnreachable` reaching the router turns a graceful degradation into a 500 and fails Requirement 5.

`logs` is the exception to caching — always fetch live, never serve stale log lines. Stale logs during an incident actively mislead.

---

## M-14 — `app/services/overview_service.py`

```python
class OverviewService:
    async def build(self, s, cluster_id: UUID) -> OverviewResponse
```

Sequence:
1. `cluster = await cluster_service.get(s, cluster_id)` — if Postgres is down this raises and the router returns 503. Accepted: overview is a metadata-anchored view.
2. Fan out concurrently under a single `asyncio.wait_for(..., settings.cp_overview_budget_s)`:
   `health_service.probe`, `obs.collections`, `obs.metrics`, `obs.components`, `obs.logs(component=primary, lines=50)`, `health_repo.latest`, `health_repo.history(since=1h, limit=60)`, `event_repo.list_events(limit=10)`.
3. `return_exceptions=True`; each branch that raised becomes an `Envelope.unavailable(...)`. A `TimeoutError` on the whole gather still returns whatever completed — implement with `asyncio.wait(..., timeout=...)` and cancel the stragglers rather than `gather` + `wait_for`, so partial results survive.
4. Assemble `OverviewResponse` with a top-level `overall_status` computed by `health_service.aggregate_status`, plus a `degraded_dependencies: list[DegradedReason]` collected from every non-ok envelope.

**Acceptance:** with Milvus stopped, `/overview` returns **HTTP 200** in under 7 s with `cluster` populated, `health.live_status="unavailable"`, `components.live` showing `exited`, and `degraded_dependencies` containing one entry. This single test is the strongest evidence for Requirement 5 in the whole submission.

---

## M-15 — `app/jobs/`

### `scheduler.py`
```python
def build_scheduler(settings) -> AsyncIOScheduler
async def start_scheduler(app_state) -> None
async def stop_scheduler(app_state) -> None
```
Every job registered with: `max_instances=1`, `coalesce=True`, `misfire_grace_time=<interval>`, and `jitter=<interval//5>` (jitter prevents all jobs firing on the same tick after a restart).

### `health_job.py`
```python
async def run_health_job(ctx: JobContext) -> None
```
Opens its own session; iterates active clusters; calls `health_service.run_check_and_persist` per cluster; **wraps each cluster in its own try/except** so one bad cluster does not skip the rest; catches `OperationalError` (Postgres down) and logs at WARNING with `event="health_job_skipped"` then returns cleanly.

**Non-negotiable:** the outermost body is `try: ... except Exception: log.exception(...)`. APScheduler removes a job that raises repeatedly in some configurations, and a silently dead health job means a dashboard that shows stale-but-plausible data forever.

### `snapshot_job.py`
```python
async def run_snapshot_job(ctx) -> None
```
Components + collections → bulk insert. Compares `component_repo.previous_states` and emits `component_state_change` **only on transition**, with payload `{component, from, to, restart_count}`.

### `retention_job.py`
```python
async def run_retention_job(ctx) -> None
```
Daily at 03:17 (`CronTrigger(hour=3, minute=17)` — an odd minute so it never collides with anything else). Purges the three time-series tables at `cp_retention_days` and `events` at `cp_retention_days * 4`. Logs deleted counts per table.

### Acceptance
- Stop Milvus → within 2 intervals, `clusters.last_health_status='unavailable'`, exactly one new `health_transition` event; wait 5 more intervals → still exactly one.
- Start Milvus → exactly one recovery event; `deployment_status` back to `running`.
- `docker stop cp-postgres` → job logs `health_job_skipped` at WARNING each interval, scheduler still alive; restart Postgres → jobs resume with **no API restart** (this is `pool_pre_ping` doing its work).

---

## M-17 — `app/api/errors.py` and `app/api/deps.py`

### `errors.py`
```python
class DomainError(Exception):
    code: str; http_status: int; message: str; detail: dict
class NotFoundError(DomainError)        # 404
class ConflictError(DomainError)        # 409
class ValidationFailure(DomainError)    # 422
class DependencyUnavailable(DomainError)# 503  — Postgres only
```
Handlers registered on the app: `DomainError` → `ErrorResponse` at `e.http_status`; `RequestValidationError` → 422 with field details; `SQLAlchemyError`/`OperationalError` → 503 `POSTGRES_UNAVAILABLE` with `Retry-After: 5`; bare `Exception` → 500 `INTERNAL_ERROR`, logs the traceback, returns **no** traceback in the body.

**The rule this file enforces:** Milvus/Docker/metrics adapter errors must never reach here. If one does, it means a service skipped its envelope — add a defensive handler that logs `event="envelope_leak"` at ERROR so the bug is loud in testing rather than silent in the demo.

### `deps.py`
```python
async def get_session() -> AsyncIterator[AsyncSession]
def get_settings_dep() -> Settings
def get_cluster_service(request) -> ClusterService
def get_health_service(request) -> HealthService
def get_observability_service(request) -> ObservabilityService
def get_overview_service(request) -> OverviewService
async def get_cluster_or_404(cluster_id: UUID, s=Depends(get_session)) -> Cluster
def pagination(limit: int = Query(50, ge=1, le=500),
               offset: int = Query(0, ge=0)) -> Pagination
```
Adapters and services are singletons constructed in `lifespan` and stored on `app.state`; the `get_*_service` deps read from `request.app.state`. Do not construct a `MilvusAdapter` per request — you would lose the connection reuse and the breaker state that make the whole degradation model work.

---

## M-18 — `app/api/routers/`

Endpoints, one router file per resource. Full contract:

| Method | Path | Response model | Status codes |
|---|---|---|---|
| GET | `/healthz` | `SelfHealth` | 200 always |
| GET | `/readyz` | `Readiness` | 200 / 503 |
| GET | `/api/v1/clusters` | `Page[ClusterOut]` | 200 / 503 |
| POST | `/api/v1/clusters` | `ClusterOut` | 201 / 409 / 422 |
| GET | `/api/v1/clusters/{id}` | `ClusterOut` | 200 / 404 |
| PATCH | `/api/v1/clusters/{id}` | `ClusterOut` | 200 / 404 / 422 |
| DELETE | `/api/v1/clusters/{id}` | — | 204 / 404 |
| GET | `/api/v1/clusters/{id}/health` | `HealthResponse` | **200 always** (or 404) |
| POST | `/api/v1/clusters/{id}/health-check` | `HealthResponse` | 200 / 404 |
| GET | `/api/v1/clusters/{id}/health-history` | `Page[HealthCheckOut]` | 200 / 404 |
| GET | `/api/v1/clusters/{id}/collections` | `Envelope[list[CollectionOut]]` | 200 |
| GET | `/api/v1/clusters/{id}/collections/{name}` | `Envelope[CollectionOut]` | 200 / 404 |
| GET | `/api/v1/clusters/{id}/metrics` | `Envelope[list[MetricOut]]` | 200 |
| GET | `/api/v1/clusters/{id}/components` | `Envelope[list[ComponentOut]]` | 200 |
| GET | `/api/v1/clusters/{id}/logs` | `Envelope[list[LogLineOut]]` | 200 / 422 |
| GET | `/api/v1/clusters/{id}/overview` | `OverviewResponse` | 200 |
| GET | `/api/v1/events` | `Page[EventOut]` | 200 |
| GET | `/api/v1/system/info` | `SystemInfo` | 200 |

Router requirements:
- `response_model=` on **every** route, `status_code=` where non-200.
- `responses={...}` documenting the error shapes so `/docs` is complete.
- An OpenAPI `description` on each degradable route stating: *"Returns 200 even when the dependency is unavailable; inspect `live_status` and `degraded_reason`."*
- At least one `examples` entry per route — one healthy, and for the degradable ones a second showing the unavailable envelope. The degraded example is what makes `/docs` a demo artifact rather than boilerplate.
- `/logs` validates `component` against the allowlist and returns 422 with the valid values listed.
- `/system/info` returns app version, git SHA (from a build arg), settings **with secrets redacted**, breaker snapshots, cache stats, and scheduler job list with next-run times. This is your first-stop diagnostic during the failure drills.

---

## M-19 — `app/main.py`

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]
def create_app() -> FastAPI
app = create_app()
```

### `lifespan` — exact order
1. `configure_logging(...)`.
2. Create the async engine and session factory → `app.state.db`.
3. **Try** `SELECT 1`. On failure: `log.error(event="postgres_unavailable_at_startup")` and **continue**. Do not raise. An API that refuses to start because Postgres is down cannot report that Postgres is down.
4. Construct singletons: two `CircuitBreaker`s (milvus, docker), `LastKnownGoodCache`, `MilvusAdapter`, `DockerAdapter`, `MetricsAdapter`, then the four services → `app.state`.
5. Wire breaker callbacks to write `breaker_opened`/`breaker_closed` events (best-effort; swallow failures — you cannot let event-writing failure break the breaker).
6. Build and start the scheduler; write a `scheduler_started` event.
7. `yield`.
8. Shutdown in reverse: `scheduler.shutdown(wait=False)` → `adapter.close()` × 3 → `engine.dispose()`.

### `create_app()`
Sets `title="Milvus Control Plane"`, `version`, `docs_url="/docs"`, `openapi_tags`, adds `RequestContextMiddleware`, registers error handlers, includes all routers, and adds `GZipMiddleware(minimum_size=1000)` (the metrics and logs payloads compress well).

### Acceptance
```bash
docker stop cp-postgres milvus-standalone
docker start cp-api        # or restart it
curl -s localhost:8000/healthz | jq      # 200, status ok
curl -s localhost:8000/readyz  | jq      # 503, postgres unavailable
docker start cp-postgres
sleep 10
curl -s localhost:8000/readyz  | jq      # 200 — WITHOUT restarting cp-api
```
That last line is the `pool_pre_ping` proof. If it requires an API restart, the engine configuration in Doc 02 was not applied.

---

## Consolidated Claude Code prompts

**Prompt 1 (M-01, M-02, M-04):**
> "Implement `app/config.py`, `app/logging_conf.py` and `app/schemas/common.py` per these specs: [paste M-01, M-02, M-04]. Settings must validate every field as tabulated, expose `postgres_dsn_async`/`postgres_dsn_sync`/`postgres_dsn_safe`, and redact secrets in `__repr__`. The Envelope class must be constructible only through its three classmethods. Include the complete frozen error-code table as a module-level constant."

**Prompt 2 (M-05 – M-09):**
> "Implement the four adapters per these specs: [paste M-05…M-09]. For `milvus_client.py`, implement the exception-translation table exactly as tabulated and write one unit test per row; `_call` must invalidate and rebuild the client on any transport-level failure — include a comment explaining that a dead gRPC channel never self-heals. For `docker_client.py`, reconcile against `expected_components` so absent containers report `state='missing'`. For `metrics_client.py`, missing allowlist entries must return `available=False` rather than being omitted or raising."

**Prompt 3 (M-10 – M-14):**
> "Implement the repositories and services per [paste M-10…M-14]. `aggregate_status` must be an explicit if-chain in the tabulated order where HEALTHY is reachable only via positive evidence. `run_check_and_persist` must run in one transaction and write a `health_transition` event only when the status differs from the previous check. `ObservabilityService` must convert every adapter exception into an Envelope — no adapter exception may escape the service layer."

**Prompt 4 (M-15, M-17 – M-19):**
> "Implement the jobs, error handling, dependencies, routers and `main.py` per [paste M-15, M-17, M-18, M-19]. Every job body catches all exceptions so the scheduler cannot die. The lifespan must tolerate an unreachable Postgres at startup. Every route needs a response_model, an OpenAPI description explaining the envelope, and both a healthy and a degraded example. `/api/v1/clusters/{id}/health` must return 200 even when Milvus is unreachable."

---

## Next

Proceed to **04_SCRIPTS_AND_UI.md**.
