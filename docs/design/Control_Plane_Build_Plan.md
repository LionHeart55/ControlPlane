# Control Plane for Milvus 2.6 — Operational Build Plan

**Purpose of this document:** a build specification decomposed into independently executable work packages (WPs). Each WP is self-contained and includes a ready-to-paste Claude Code prompt. Nothing here is source code; everything here is instructions to produce source code.

---

## 0. Locked technical decisions

| Concern | Decision | Rationale |
|---|---|---|
| Milvus deployment | **Docker Compose, standalone mode**, image `milvusdb/milvus:v2.6.20` (pin exact patch) | Assignment permits Docker; standalone gives full API surface with ~4 GB RAM. K8s/Operator is documented as an alternative path (WP-16) but not the primary. |
| Milvus dependencies | `quay.io/coreos/etcd:v3.5.25` (metadata) + `minio/minio:RELEASE.2024-05-28T17-19-04Z` (object store + Woodpecker WAL backend) | In 2.6 the default compose ships **Woodpecker** as the message queue with MinIO as its WAL backend, so **no Pulsar/Kafka container is needed**. Three infra containers only. |
| Object store | **MinIO** (S3-compatible), bucket `milvus-bucket` | Satisfies "Docker with MinIO or another S3-compatible object store". |
| Relational DB | **PostgreSQL 16** (`postgres:16-alpine`), database `controlplane` | JSONB for raw payload snapshots, mature async driver, easy migrations. |
| Control-plane backend | **Python 3.12 + FastAPI + Uvicorn** | Same language as `pymilvus` (the only first-class Milvus SDK) and the `docker` SDK. One runtime, one dependency set, no FFI/shell-out to get Milvus state. |
| ORM / migrations | **SQLAlchemy 2.0 (async, asyncpg) + Alembic** | Assignment explicitly asks for "database schema or migrations". |
| Scheduler | **APScheduler** `AsyncIOScheduler` in-process | Background health checks without adding Celery/Redis. |
| Dashboard | **React 18 + TypeScript + Vite + TanStack Query**, served by `nginx:alpine` which also reverse-proxies `/api` | Single-origin in the browser → no CORS class of bugs during the demo. |
| Milvus ops script | Standalone Python CLI, `pymilvus>=2.6`, random vectors by default, optional `sentence-transformers` MiniLM | Runs with zero model download by default; grader is not blocked on a 90 MB model pull. |
| Container orchestration for our own services | Same Compose file, separate **profiles** (`infra`, `app`) | One `docker compose up` for the whole demo; ability to run backend on host for debugging. |
| Config | `.env` + `pydantic-settings`, single source of truth | |

**Naming convention used throughout:** compose project `milvus-cp`, container names `milvus-etcd`, `milvus-minio`, `milvus-standalone`, `cp-postgres`, `cp-api`, `cp-dashboard`.

---

## 1. Repository layout (create exactly this)

```
milvus-control-plane/
├── README.md
├── Makefile
├── .env.example
├── .gitignore
├── docs/
│   ├── ARCHITECTURE.md
│   ├── RELIABILITY.md            # failure drills + diagnosis writeup
│   └── AI_USAGE.md
├── infra/
│   ├── docker-compose.yml        # infra + app, profile-gated
│   ├── milvus/user.yaml          # Milvus config overrides (optional mount)
│   ├── postgres/initdb.sql       # role/db creation only; tables come from Alembic
│   ├── nginx/dashboard.conf
│   ├── deploy.sh                 # THE automation entrypoint
│   └── lib/
│       ├── preflight.sh
│       ├── wait_for.sh
│       └── colors.sh
├── control_plane/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── alembic.ini
│   ├── migrations/
│   │   ├── env.py
│   │   └── versions/
│   └── app/
│       ├── main.py
│       ├── config.py
│       ├── logging_conf.py
│       ├── db/            (session.py, base.py, models.py)
│       ├── schemas/       (cluster.py, health.py, collection.py, metrics.py, logs.py, common.py)
│       ├── repositories/  (cluster_repo.py, health_repo.py, component_repo.py,
│       │                   collection_repo.py, event_repo.py)
│       ├── adapters/      (milvus_client.py, docker_client.py, metrics_client.py,
│       │                   minio_client.py, circuit_breaker.py, cache.py)
│       ├── services/      (cluster_service.py, health_service.py, observability_service.py,
│       │                   overview_service.py)
│       ├── jobs/          (scheduler.py, health_job.py, snapshot_job.py, retention_job.py)
│       ├── api/           (deps.py, errors.py, routers/*.py)
│       └── tests/         (unit/, integration/)
├── ops/
│   ├── milvus_demo.py            # Requirement 3
│   ├── embeddings.py
│   └── requirements.txt
├── scripts/
│   ├── chaos.sh                  # Requirement 5 driver
│   ├── smoke_test.sh
│   └── seed_cluster.sh
└── dashboard/
    ├── package.json
    ├── vite.config.ts
    ├── Dockerfile
    └── src/ (main.tsx, App.tsx, api/client.ts, hooks/, components/, styles.css)
```

---

## 2. Port, credential, and endpoint map

| Service | Container | Host port | Internal | Notes |
|---|---|---|---|---|
| Milvus gRPC | `milvus-standalone` | 19530 | 19530 | pymilvus target |
| Milvus metrics/health/WebUI | `milvus-standalone` | 9091 | 9091 | `/healthz`, `/metrics`, `/webui/` |
| etcd | `milvus-etcd` | *not published* | 2379 | intentionally internal |
| MinIO API | `milvus-minio` | 9000 | 9000 | |
| MinIO console | `milvus-minio` | 9001 | 9001 | diagnosis aid |
| PostgreSQL | `cp-postgres` | 5432 | 5432 | |
| Control-plane API | `cp-api` | 8000 | 8000 | `/docs` = OpenAPI UI |
| Dashboard | `cp-dashboard` | 8080 | 80 | proxies `/api` → `cp-api:8000` |

`.env.example` keys (exhaustive — WP-01 must produce all of them):

```
COMPOSE_PROJECT_NAME=milvus-cp
DOCKER_VOLUME_DIRECTORY=./volumes
MILVUS_VERSION=v2.6.20
ETCD_VERSION=v3.5.25
MINIO_VERSION=RELEASE.2024-05-28T17-19-04Z
POSTGRES_VERSION=16-alpine

MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin
MINIO_BUCKET=milvus-bucket

POSTGRES_USER=controlplane
POSTGRES_PASSWORD=controlplane
POSTGRES_DB=controlplane
POSTGRES_HOST=cp-postgres
POSTGRES_PORT=5432

MILVUS_URI=http://milvus-standalone:19530
MILVUS_METRICS_URI=http://milvus-standalone:9091
MILVUS_CONNECT_TIMEOUT_S=3
MILVUS_RPC_TIMEOUT_S=5

DOCKER_SOCKET=/var/run/docker.sock
CP_API_PORT=8000
CP_LOG_LEVEL=INFO
CP_HEALTH_INTERVAL_S=15
CP_SNAPSHOT_INTERVAL_S=60
CP_CACHE_TTL_S=5
CP_BREAKER_FAIL_MAX=3
CP_BREAKER_RESET_S=30
CP_RETENTION_DAYS=7
DASHBOARD_PORT=8080
```

---

## 3. Data model (PostgreSQL)

Five tables. All timestamps `TIMESTAMPTZ` stored UTC. All ids `UUID` except append-only tables which use `BIGSERIAL`.

### 3.1 `clusters`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK, default `gen_random_uuid()` | |
| `name` | TEXT UNIQUE NOT NULL | e.g. `local-milvus-standalone` |
| `deployment_type` | ENUM `deployment_type` (`docker_standalone`,`docker_distributed`,`k8s_operator`) | |
| `deployment_status` | ENUM `deployment_status` (`pending`,`provisioning`,`running`,`degraded`,`unavailable`,`stopped`,`deleted`) | maintained by health job |
| `milvus_version` | TEXT NULL | learned live, cached here |
| `endpoint_uri` | TEXT NOT NULL | gRPC URI |
| `metrics_uri` | TEXT NULL | `:9091` base |
| `object_store_endpoint` | TEXT NULL | |
| `compose_project` | TEXT NULL | for docker label filtering |
| `namespace` | TEXT NULL | k8s path only |
| `created_at` / `updated_at` | TIMESTAMPTZ NOT NULL | |
| `last_health_check_at` | TIMESTAMPTZ NULL | **explicitly required by assignment** |
| `last_health_status` | ENUM `health_status` (`healthy`,`degraded`,`unavailable`,`unknown`) | |
| `labels` | JSONB DEFAULT `'{}'` | free-form metadata |

Indexes: unique on `name`; btree on `deployment_status`.

### 3.2 `health_checks` (append-only time series)
`id BIGSERIAL PK`, `cluster_id UUID FK→clusters ON DELETE CASCADE`, `checked_at TIMESTAMPTZ`, `status health_status`, `latency_ms INT NULL`, `milvus_reachable BOOL`, `object_store_reachable BOOL NULL`, `metadata_store_reachable BOOL NULL`, `server_version TEXT NULL`, `error_code TEXT NULL`, `error_message TEXT NULL`, `raw JSONB`.
Index: `(cluster_id, checked_at DESC)`.

### 3.3 `component_status`
`id BIGSERIAL PK`, `cluster_id UUID FK`, `component_name TEXT` (`milvus-standalone`,`milvus-etcd`,`milvus-minio`,`cp-postgres`), `kind TEXT` (`container`|`pod`), `runtime_id TEXT`, `image TEXT`, `state TEXT` (`running`,`exited`,`paused`,`restarting`,`missing`), `health TEXT NULL` (docker healthcheck verdict), `restart_count INT`, `started_at TIMESTAMPTZ NULL`, `observed_at TIMESTAMPTZ`, `raw JSONB`.
Index: `(cluster_id, component_name, observed_at DESC)`.

### 3.4 `collection_snapshots`
`id BIGSERIAL PK`, `cluster_id UUID FK`, `collection_name TEXT`, `row_count BIGINT NULL`, `num_partitions INT NULL`, `dimension INT NULL`, `index_type TEXT NULL`, `metric_type TEXT NULL`, `is_loaded BOOL NULL`, `observed_at TIMESTAMPTZ`, `raw JSONB`.
Index: `(cluster_id, collection_name, observed_at DESC)`.

### 3.5 `events` (audit + incident trail — this is what makes Requirement 5 demonstrable)
`id BIGSERIAL PK`, `cluster_id UUID FK NULL`, `event_type TEXT` (`cluster_registered`,`health_transition`,`component_state_change`,`dependency_failure`,`dependency_recovered`,`breaker_opened`,`breaker_closed`), `severity TEXT` (`info`,`warning`,`error`), `message TEXT`, `payload JSONB`, `created_at TIMESTAMPTZ`.
Index: `(created_at DESC)`, `(cluster_id, created_at DESC)`.

**Rule:** events are written *only on transition*, never on every poll. A health job that writes an event every 15 seconds is a bug.

---

## 4. REST API contract (freeze this before writing code)

Base path `/api/v1`. All errors use RFC-7807-ish envelope: `{"error":{"code":"...","message":"...","detail":{...}}}`.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/healthz` | liveness of control plane itself | never touches DB; always 200 if process alive |
| GET | `/readyz` | readiness | 503 if Postgres unreachable |
| GET | `/api/v1/clusters` | list registered clusters (DB only) | supports `?status=` |
| POST | `/api/v1/clusters` | register cluster metadata | 201; 409 on duplicate name |
| GET | `/api/v1/clusters/{id}` | metadata + last known health | |
| PATCH | `/api/v1/clusters/{id}` | update mutable fields | |
| DELETE | `/api/v1/clusters/{id}` | soft delete (`deleted`) | |
| GET | `/api/v1/clusters/{id}/health` | **live probe** + persisted last check | always 200; body carries `live_error` when down |
| POST | `/api/v1/clusters/{id}/health-check` | force immediate check, persist result | |
| GET | `/api/v1/clusters/{id}/health-history?limit=50` | time series from `health_checks` | |
| GET | `/api/v1/clusters/{id}/collections` | live list + stats, merged with last snapshot | |
| GET | `/api/v1/clusters/{id}/collections/{name}` | schema, index, load state, row count | |
| GET | `/api/v1/clusters/{id}/metrics` | curated runtime metrics from `:9091/metrics` | |
| GET | `/api/v1/clusters/{id}/components` | container/pod state via Docker SDK | |
| GET | `/api/v1/clusters/{id}/logs?component=&lines=200&since=10m` | recent container logs | component must be in allowlist |
| GET | `/api/v1/clusters/{id}/overview` | **the dashboard's single call** — fan-out aggregate | |
| GET | `/api/v1/events?limit=100&cluster_id=` | incident/audit trail | |

### 4.1 The degradation envelope (single most important design rule)

Every endpoint that mixes stored + live data returns this shape:

```
{
  "cluster": { ...metadata from Postgres... },
  "live": { ...or null... },
  "live_status": "ok" | "stale" | "unavailable",
  "observed_at": "2026-08-06T12:00:00Z",
  "stale": false,
  "degraded_reason": null | { "code": "MILVUS_UNREACHABLE", "message": "...", "since": "..." }
}
```

Consequences, enforced in code review:
- **A dependency being down must never produce a 5xx on a read endpoint.** It produces `live: null` + `live_status: "unavailable"` + `degraded_reason`. HTTP 200.
- If cached data exists within `CP_CACHE_TTL_S * 12`, return it with `stale: true` and the original `observed_at`.
- Postgres down is the *only* dependency allowed to cause 503, and only on metadata routes; `/api/v1/clusters/{id}/health` must still answer from live Milvus with `cluster: null`.

### 4.2 `/overview` composition

Concurrent fan-out with `asyncio.gather(..., return_exceptions=True)` over: metadata (PG), health probe (Milvus gRPC), collections (Milvus), metrics (HTTP 9091), components (Docker socket), logs tail (Docker socket, 50 lines), recent events (PG). Each sub-result carries its own status. Global timeout 6 s; partial results always returned.

---

## 5. Work packages

> Each WP below is designed to be handed to Claude Code as one session. Do them in order. Each ends with an acceptance check that must pass before moving on.

---

### WP-01 — Repository scaffold, tooling, Makefile

**Produces:** directory tree from §1 (empty packages with `__init__.py`), `.env.example` (§2, all keys), `.gitignore` (volumes/, .env, __pycache__, node_modules, dist), `control_plane/pyproject.toml`, `ops/requirements.txt`, `Makefile`.

`Makefile` targets (exact names — the README and demo depend on them):
`up`, `down`, `destroy`, `logs`, `ps`, `status`, `migrate`, `seed`, `demo`, `smoke`, `test`, `chaos-milvus`, `chaos-minio`, `chaos-postgres`, `chaos-recover`, `dashboard`, `fmt`, `lint`.

Python deps to pin: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `pymilvus>=2.6,<2.7`, `docker`, `httpx`, `prometheus-client`, `apscheduler`, `structlog`, `cachetools`, `tenacity`; dev: `pytest`, `pytest-asyncio`, `httpx`, `ruff`, `mypy`, `testcontainers` (optional).

**Acceptance:** `make lint` runs clean on an empty tree; `cp .env.example .env` produces a file with no placeholders left as `CHANGEME`.

> **Claude Code prompt:** "Create the repository scaffold for a Milvus 2.6 control plane exactly matching this directory tree: [paste §1]. Generate .env.example with exactly these keys: [paste §2]. Generate control_plane/pyproject.toml with these pinned dependencies: [paste list], configured for Python 3.12 with ruff and mypy settings. Generate a Makefile with these targets: [paste list] — each target may be a stub echoing 'TODO' for now except fmt/lint which must actually run ruff. Generate .gitignore. Do not write application logic yet."

---

### WP-02 — Compose stack: infrastructure tier

**Produces:** `infra/docker-compose.yml` (services `etcd`, `minio`, `standalone`, `postgres`), `infra/postgres/initdb.sql`, `infra/milvus/user.yaml`.

Specifics:
- Base the Milvus/etcd/MinIO service definitions on the **official Milvus 2.6 standalone compose file**, but pin image tags from `.env` variables, not `latest`.
- `standalone` env: `ETCD_ENDPOINTS=etcd:2379`, `MINIO_ADDRESS=minio:9000`, `MINIO_REGION=us-east-1`; `security_opt: [seccomp:unconfined]`; command `["milvus","run","standalone"]`.
- Healthchecks (these are what the control plane and `deploy.sh` key off):
  - etcd: `etcdctl endpoint health`, interval 10 s, retries 5
  - minio: `mc ready local`, interval 10 s, retries 5
  - standalone: `curl -f http://localhost:9091/healthz`, interval 15 s, **`start_period: 120s`**, retries 5
  - postgres: `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB`, interval 5 s, retries 10
- `depends_on` with `condition: service_healthy` for standalone → (etcd, minio).
- Named-path volumes under `${DOCKER_VOLUME_DIRECTORY:-./volumes}/{etcd,minio,milvus,postgres}`.
- Single user-defined bridge network `cp-net`.
- Add label `com.milvus-cp.component=<name>` to every service — the Docker adapter filters on this label, not on hardcoded container names.
- Profiles: infra services in profile `infra`; leave `api`/`dashboard` slots for WP-13.
- `initdb.sql`: create extension `pgcrypto` (for `gen_random_uuid()`), nothing else. **Tables come from Alembic only** — never from initdb.

**Acceptance:** `docker compose --profile infra up -d` → `docker compose ps` shows 4 containers `healthy` within 180 s; `curl -f localhost:9091/healthz` returns 200; `psql -h localhost -U controlplane -c '\dx'` lists pgcrypto.

> **Claude Code prompt:** "Write infra/docker-compose.yml for a Milvus 2.6.20 standalone stack. Services: etcd (quay.io/coreos/etcd:${ETCD_VERSION}), minio (minio/minio:${MINIO_VERSION}), standalone (milvusdb/milvus:${MILVUS_VERSION}), postgres (postgres:${POSTGRES_VERSION}). Milvus 2.6 uses Woodpecker as its embedded WAL with MinIO as backend, so do NOT add Pulsar or Kafka. Apply exactly these healthchecks, depends_on conditions, volumes, labels, ports, and profiles: [paste WP-02 specifics]. Also write infra/postgres/initdb.sql creating only the pgcrypto extension, and an empty-but-documented infra/milvus/user.yaml showing how to override log level and mq type."

---

### WP-03 — `deploy.sh`: the infrastructure automation script (Requirement 1)

**Produces:** `infra/deploy.sh` + helpers in `infra/lib/`.

Subcommands (each must be idempotent and re-runnable):

| Command | Behaviour |
|---|---|
| `preflight` | verify docker ≥ 24, compose v2, ≥ 8 GB RAM available to Docker, ≥ 20 GB disk, ports 19530/9091/9000/9001/5432/8000/8080 free; fail fast with actionable message per check |
| `up [--mode standalone\|distributed] [--profile infra\|all]` | preflight → `.env` bootstrap from `.env.example` if missing → `docker compose up -d` → `wait_for` each healthcheck with timeout → create MinIO bucket via one-shot `mc` container → run `alembic upgrade head` → `seed_cluster.sh` → print endpoint summary table |
| `status` | compose ps + per-service health + `curl /healthz` + `psql SELECT 1` + row counts, in a formatted table |
| `logs [service] [-f]` | thin wrapper over compose logs |
| `restart <service>` | targeted restart with re-wait |
| `down` | `docker compose down` — **containers removed, volumes kept** |
| `destroy` | `docker compose down -v --remove-orphans` + `rm -rf volumes/` after an interactive `y/N` confirm (skippable with `--yes`) |
| `reset` | `destroy --yes` then `up` |

Engineering requirements:
- `set -euo pipefail`; every function `local`-scoped; `trap` on ERR printing the failing line.
- `wait_for.sh` implements: `wait_for_healthy <container> <timeout_s>` polling `docker inspect -f '{{.State.Health.Status}}'`, and `wait_for_http <url> <timeout_s>`, and `wait_for_tcp <host> <port> <timeout_s>`. Emit a progress dot per second, and on timeout **dump the last 50 log lines of the failing container** before exiting non-zero.
- `--mode distributed` should not silently lie: either implement it by selecting a second compose file (`docker-compose.distributed.yml`) or exit with a clear "not implemented in this submission; see docs/ARCHITECTURE.md §alternatives". Pick one and document it.
- Colorized output but degrade cleanly when `NO_COLOR` is set or stdout is not a TTY.
- `deploy.sh --help` prints the full command table.

**Acceptance:** on a clean machine, `./infra/deploy.sh up` exits 0 within 5 minutes and `./infra/deploy.sh status` shows every component green; running `up` a second time is a no-op that still exits 0; `destroy --yes && up` produces a working stack from zero.

> **Claude Code prompt:** "Write infra/deploy.sh plus infra/lib/{preflight.sh,wait_for.sh,colors.sh} implementing exactly these subcommands and behaviours: [paste WP-03 table + engineering requirements]. Bash with set -euo pipefail, POSIX-portable where practical, must work on macOS bash 3.2 and Linux bash 5. Every subcommand idempotent. On any wait timeout, dump the last 50 log lines of the failing container to stderr before exiting non-zero."

---

### WP-04 — Database models and Alembic migrations

**Produces:** `app/db/base.py`, `app/db/models.py`, `app/db/session.py`, `alembic.ini`, `migrations/env.py`, one initial revision.

- SQLAlchemy 2.0 declarative with `Mapped[]` / `mapped_column()` typing.
- Native PG ENUM types created explicitly in the migration (`deployment_type`, `deployment_status`, `health_status`) — do not rely on autogenerate for enum creation order.
- Async engine with `pool_pre_ping=True`, `pool_size=5`, `max_overflow=5`, `pool_recycle=300`. `pool_pre_ping` is what makes the Postgres-restart drill in WP-15 recover without an API restart — call this out in a code comment.
- `migrations/env.py` must run in async mode and read the URL from `app.config.Settings`, not from `alembic.ini`.
- Add a partial index and the retention-friendly ordering indexes listed in §3.

**Acceptance:** `alembic upgrade head` on empty DB creates 5 tables + 3 enums; `alembic downgrade base` cleanly reverses; `alembic revision --autogenerate` immediately after produces an *empty* migration (proves models and schema agree).

> **Claude Code prompt:** "Implement SQLAlchemy 2.0 async models for these five tables with exactly these columns, types, enums, FKs, defaults and indexes: [paste §3]. Then configure Alembic for async operation reading the DB URL from app.config.Settings, and generate the initial migration by hand (not autogenerate) so that the three PG ENUM types are created before the tables that reference them and dropped after. Include a downgrade that fully reverses. Add pool_pre_ping=True to the engine and comment why."

---

### WP-05 — Application skeleton: config, logging, lifespan, error handling

**Produces:** `app/config.py`, `app/logging_conf.py`, `app/main.py`, `app/api/errors.py`, `app/api/deps.py`, `control_plane/Dockerfile`.

- `Settings(BaseSettings)` mirroring every `.env` key from §2, with types and validators (e.g. URI fields validated as URLs, intervals > 0). `@lru_cache` accessor.
- `structlog` JSON logging to stdout; bind `request_id` (from `X-Request-ID` header or generated UUID4) via middleware; log line for every request with method, path, status, duration_ms.
- FastAPI `lifespan`: create engine → verify DB reachable (log a warning and continue if not; **do not crash the API because Postgres is down** — that would defeat Requirement 5) → start APScheduler → yield → graceful shutdown of scheduler and engine.
- Exception handlers: `DomainError` hierarchy (`NotFoundError`→404, `ConflictError`→409, `ValidationError`→422, `DependencyUnavailableError`→ **200 with degradation envelope where applicable, 503 only for Postgres on metadata routes**), plus a catch-all 500 that logs traceback and returns a sanitized body.
- `Dockerfile`: multi-stage, `python:3.12-slim`, non-root user, `curl` installed for the container healthcheck, `HEALTHCHECK CMD curl -f localhost:8000/healthz`.

**Acceptance:** `uvicorn app.main:app` starts with Postgres stopped, `/healthz` returns 200, `/readyz` returns 503 with a machine-readable reason.

> **Claude Code prompt:** "Implement app/config.py (pydantic-settings mirroring these env keys: [paste §2]), app/logging_conf.py (structlog JSON + request_id middleware), app/main.py (FastAPI with lifespan that tolerates an unreachable Postgres at startup — log and continue, never crash), app/api/errors.py (DomainError hierarchy and handlers mapping to these statuses: [paste mapping]), app/api/deps.py (DB session and settings dependencies), and a multi-stage Dockerfile running as non-root with a curl-based HEALTHCHECK."

---

### WP-06 — Milvus adapter

**Produces:** `app/adapters/milvus_client.py`, `app/adapters/circuit_breaker.py`, `app/adapters/cache.py`.

Adapter surface (all async — wrap the sync `MilvusClient` with `asyncio.to_thread`, `pymilvus` is not async):

```
ping() -> ProbeResult(reachable, latency_ms, error_code, error_message)
get_server_version() -> str
list_collections() -> list[str]
describe_collection(name) -> dict          # fields, dim, primary key, auto_id
get_collection_stats(name) -> dict         # row_count
list_indexes(name) / describe_index(name, index_name) -> dict
get_load_state(name) -> str
deep_probe() -> ProbeResult                # ping + list_collections + describe on one collection
```

Rules:
- One long-lived `MilvusClient` per cluster endpoint, lazily created, guarded by an `asyncio.Lock`. Recreate the client on connection-class errors (do **not** reuse a client whose channel is dead).
- Hard timeouts on every call from `MILVUS_RPC_TIMEOUT_S`. A hung call is worse than a failed one.
- Error classification into stable codes the UI can switch on: `MILVUS_UNREACHABLE` (connection refused / DNS), `MILVUS_TIMEOUT`, `MILVUS_AUTH_FAILED`, `MILVUS_RPC_ERROR`, `MILVUS_COLLECTION_NOT_FOUND`. Map from `pymilvus` `MilvusException` codes plus `grpc` status codes.
- `circuit_breaker.py`: minimal 3-state breaker (closed/open/half-open), `fail_max` and `reset_timeout` from settings, per-dependency instance, emits `breaker_opened`/`breaker_closed` callbacks (wired to the events table in WP-09). Do not pull in a heavy library.
- `cache.py`: `TTLCache`-backed last-known-good store keyed by `(cluster_id, resource)`, storing `(value, observed_at)`. Provides `get_fresh()`, `get_stale()`, `set()`.

**Why `deep_probe` exists — write this in the module docstring:** `:9091/healthz` is a shallow liveness check. Milvus can report healthy while MinIO is down, and only fail on write/flush. The deep probe is what actually catches the object-store outage in the WP-15 drill.

**Acceptance:** unit tests with a mocked `MilvusClient` cover each error classification; with the stack up, an ad-hoc script calling `ping()` and `deep_probe()` returns `reachable=True` and a version string.

> **Claude Code prompt:** "Implement app/adapters/milvus_client.py exposing exactly this async surface: [paste surface]. Wrap the synchronous pymilvus MilvusClient with asyncio.to_thread, one lazily-created client per endpoint guarded by an asyncio.Lock, recreated on connection-class failures. Enforce a hard timeout from settings on every call. Classify all failures into these stable codes: [paste codes] by inspecting pymilvus MilvusException and grpc status codes. Also implement a dependency-free 3-state circuit breaker and a TTL last-known-good cache with get_fresh/get_stale/set. Document in the module docstring why deep_probe is needed in addition to /healthz."

---

### WP-07 — Docker adapter (component status + logs)

**Produces:** `app/adapters/docker_client.py`.

- Uses `docker` SDK over the mounted socket; **all calls wrapped in `asyncio.to_thread` with a 3 s timeout**.
- `list_components(compose_project) -> list[ComponentStatus]`: filter containers by label `com.milvus-cp.component`, include stopped ones (`all=True`) so a stopped Milvus shows as `exited`, not as absent. Report `state`, `health` (from `State.Health.Status` when present), `restart_count`, `started_at`, `image`, `exit_code`.
- Expected-components list comes from config, so a component that has vanished entirely is reported as `state: "missing"` rather than silently omitted. **This is the difference between a dashboard that shows an outage and one that shows nothing.**
- `tail_logs(component, lines, since) -> list[LogLine]`: `container.logs(tail=..., since=..., timestamps=True, stdout=True, stderr=True)`, decode with `errors="replace"`, split timestamp from message, tag stream. Cap `lines` at 1000 server-side.
- Component name allowlist enforced in the adapter, not just the router — never interpolate user input into a container lookup.
- If the Docker socket is unavailable (permission denied, not mounted), raise `DependencyUnavailableError(code="DOCKER_UNAVAILABLE")` and let the envelope handle it. The control plane must remain useful without Docker access.

**Acceptance:** with the stack up, adapter returns 4 components all `running`; after `docker stop milvus-standalone`, it returns that component as `exited` with the exit code; after `docker rm`, as `missing`.

> **Claude Code prompt:** "Implement app/adapters/docker_client.py per this spec: [paste WP-07]. Use the docker SDK over the socket, wrap every call in asyncio.to_thread with a 3s timeout, filter by the com.milvus-cp.component label with all=True, and reconcile against a configured expected-components list so vanished containers are reported as state='missing'. Enforce a component-name allowlist inside the adapter. Raise DependencyUnavailableError with code DOCKER_UNAVAILABLE if the socket cannot be reached."

---

### WP-08 — Metrics adapter

**Produces:** `app/adapters/metrics_client.py`, `app/adapters/metric_allowlist.py`.

- `httpx.AsyncClient` GET `{metrics_uri}/metrics`, 2 s timeout, parse with `prometheus_client.parser.text_string_to_metric_families`.
- **Allowlist-driven, version-tolerant.** Metric names drift between Milvus minor versions; hardcoding them makes the dashboard silently blank after an upgrade. Define an allowlist of `(metric_name, display_label, unit, aggregation)` and, at startup, run a discovery pass that logs which allowlisted metrics were **not** found. Missing metrics are returned as `null` with `available: false`, and the dashboard renders them greyed rather than omitting them.
- Starting allowlist (verify against your running instance and prune): `milvus_num_node`, `milvus_proxy_req_count`, `milvus_proxy_req_latency` (histogram → p50/p99), `milvus_rootcoord_collection_num`, `milvus_querynode_entity_num` / `milvus_querynode_num_entities`, `milvus_datacoord_stored_binlog_size`, `milvus_storage_op_count`, `milvus_storage_request_latency`, plus process-level `go_goroutines`, `process_resident_memory_bytes`, `process_cpu_seconds_total`.
- Also expose `discover()` returning all family names, used once during development to build the real allowlist. Document in `docs/ARCHITECTURE.md` that this was how the list was chosen.
- Histogram handling: compute quantiles from buckets, don't just sum.
- Labels: collapse by `node_id`/`role` with a documented aggregation (sum for counters, max for gauges) so the UI gets scalars.

**Acceptance:** `GET /api/v1/clusters/{id}/metrics` returns ≥ 6 metrics with `available: true`, and unknown metrics appear with `available: false` rather than causing an error.

> **Claude Code prompt:** "Implement app/adapters/metrics_client.py scraping Milvus's Prometheus endpoint at {metrics_uri}/metrics with a 2s httpx timeout and parsing via prometheus_client.parser. Drive selection from an allowlist module with this starting list: [paste list]. Metrics missing from the scrape must be returned as {value: null, available: false}, never omitted or raised. Compute p50/p99 from histogram buckets. Collapse label dimensions with sum-for-counters / max-for-gauges. Also expose an async discover() that returns every metric family name found, for allowlist authoring."

---

### WP-09 — Repositories, services, and the background scheduler

**Produces:** `app/repositories/*.py`, `app/services/*.py`, `app/jobs/*.py`.

**Repositories** — thin, no business logic, async, return ORM objects or DTOs: CRUD for clusters; `insert_health_check` + `latest_for_cluster` + `history`; `upsert_component_status`; `insert_collection_snapshot` + `latest_per_collection`; `insert_event` + `list_events`; `purge_older_than(days)`.

**`health_service.aggregate_status()`** — the single place that decides overall status. Rules, in order:

1. Milvus gRPC unreachable → `unavailable`.
2. Milvus reachable but `deep_probe` fails (e.g. `list_collections` errors) → `degraded`.
3. Milvus reachable, deep probe OK, but any expected component not `running` → `degraded`.
4. Milvus reachable, deep probe OK, all components running, but metrics scrape or Docker socket failing → `degraded` (observability loss ≠ outage, but must be visible).
5. Otherwise → `healthy`.
6. Any dependency needed to *evaluate* the above being unavailable → `unknown`, never a false `healthy`.

**`health_job`** (every `CP_HEALTH_INTERVAL_S`, default 15 s, with jitter): probe → aggregate → persist a row in `health_checks` → update `clusters.last_health_check_at` / `last_health_status` / `deployment_status` / `milvus_version` → **write an `events` row only if the status differs from the previous check** (`health_transition`), with `from`/`to`/`error_code` in the payload.

**`snapshot_job`** (every `CP_SNAPSHOT_INTERVAL_S`): components + collection stats → persist; emit `component_state_change` events on transition only.

**`retention_job`** (daily): purge `health_checks`, `component_status`, `collection_snapshots` older than `CP_RETENTION_DAYS`; keep `events` for 4× that.

Scheduler requirements: `max_instances=1` and `coalesce=True` per job (a slow probe must not stack); every job body wrapped in try/except that logs and continues — **a scheduler that dies on the first exception silently breaks the whole demo**; jobs skip cleanly and log at WARNING when Postgres is unreachable.

**Acceptance:** stop Milvus → within 2 intervals `clusters.last_health_status = 'unavailable'`, exactly one new `events` row of type `health_transition`, and no further event rows while it stays down; restart Milvus → exactly one recovery event.

> **Claude Code prompt:** "Implement the repository layer (thin async CRUD, no business logic) for these five tables: [paste §3]. Then implement health_service.aggregate_status() applying these ordered rules exactly: [paste rules]. Then implement APScheduler jobs health_job, snapshot_job, retention_job with these intervals and semantics: [paste]. Critical: events rows are written only on status transitions, never on every poll; each job uses max_instances=1 and coalesce=True; each job body catches and logs all exceptions so the scheduler never dies; jobs degrade to a WARNING log when Postgres is unreachable."

---

### WP-10 — REST routers and schemas

**Produces:** `app/schemas/*.py`, `app/api/routers/{clusters,health,collections,metrics,components,logs,events,overview,system}.py`.

- Implement every endpoint in §4 with the §4.1 envelope.
- Pydantic v2 response models for everything; `response_model` set on every route so OpenAPI is complete and accurate.
- Pagination on list endpoints: `limit` (default 50, max 500) + `offset`, with `total`.
- `/overview` implemented per §4.2 with `asyncio.gather(return_exceptions=True)` and a global 6 s budget.
- OpenAPI metadata: title, version, tags per router, and a `description` on each route explaining the envelope semantics.
- Every route must have at least one example response in the schema — this is what makes `/docs` usable as a demo artifact.

**Acceptance:** `scripts/smoke_test.sh` walks every endpoint against a live stack and asserts status codes and required fields; `/docs` renders every route with examples.

> **Claude Code prompt:** "Implement Pydantic v2 schemas and FastAPI routers for this exact API contract: [paste §4 table + §4.1 envelope + §4.2]. Every route needs a response_model, an OpenAPI description explaining the degradation envelope, and at least one example. List endpoints paginate with limit/offset/total. /overview fans out concurrently with asyncio.gather(return_exceptions=True) under a 6-second global budget and always returns partial results. Read endpoints must never return 5xx because Milvus, MinIO or Docker is down — only because Postgres is down on metadata routes."

---

### WP-11 — Milvus operations script (Requirement 3)

**Produces:** `ops/milvus_demo.py`, `ops/embeddings.py`, `ops/requirements.txt`.

CLI:
```
python ops/milvus_demo.py \
  --uri http://localhost:19530 \
  --collection demo_docs \
  --dim 384 --rows 5000 --batch 1000 \
  --index HNSW --metric COSINE \
  --embedder random|minilm \
  --topk 5 --filter 'category == "tech"' \
  --drop-existing --keep --json-out results.json -v
```

Ordered steps, each printed as a labelled, timed stage:

1. **Connect** — `MilvusClient(uri=...)`; print server version; fail fast with a readable message if unreachable.
2. **Schema** — `create_schema(auto_id=True, enable_dynamic_field=True)`; fields: `id INT64` primary, `vector FLOAT_VECTOR dim=<dim>`, `text VARCHAR(512)`, `category VARCHAR(64)`, `created_at INT64`.
3. **Create collection** — drop first if `--drop-existing`; create *without* index so index creation is a visible separate stage.
4. **Generate embeddings** — `random`: `numpy` normal, L2-normalized (required for meaningful COSINE); `minilm`: `sentence-transformers/all-MiniLM-L6-v2` over a small built-in corpus of sentences, dim forced to 384.
5. **Insert** — batches of `--batch`, progress line per batch, then `flush()`. Report rows/sec.
6. **Build index** — `prepare_index_params()` → `add_index(field_name="vector", index_type=..., metric_type=..., params=...)`; HNSW `{M:16, efConstruction:200}`, IVF_FLAT `{nlist:128}`; also add a scalar index on `category` (`index_type="INVERTED"`) to make the filtered search meaningful. `create_index(..., sync=True)`, then `describe_index` and print the result.
7. **Load** — `load_collection`, poll `get_load_state` until `Loaded`.
8. **Search** — one query vector; `search_params` per index type (HNSW `{"ef":64}`, IVF `{"nprobe":16}`); `limit=--topk`; `output_fields=["text","category"]`; optionally `filter=--filter`.
9. **Display** — aligned table: rank, id, distance/score, category, text truncated to 60 chars. Print query latency.
10. **Stats** — `get_collection_stats` row count, `describe_collection` summary.
11. **Cleanup** — drop unless `--keep`.

Engineering: `argparse`, exit codes (0 ok, 2 bad args, 3 connect failure, 4 milvus operation failure), `--json-out` writing a machine-readable summary (used by `smoke_test.sh`), all Milvus calls wrapped so the error message includes the stage name, no global state, everything in functions so the file is unit-testable.

**Acceptance:** `python ops/milvus_demo.py --rows 5000` completes in under 60 s on a laptop and prints 5 ranked results; `--embedder minilm` returns semantically sensible neighbours for the built-in corpus; running twice in a row succeeds (idempotent via `--drop-existing`).

> **Claude Code prompt:** "Write ops/milvus_demo.py, a standalone pymilvus 2.6 CLI implementing these 11 labelled, timed stages with this argparse interface: [paste CLI + steps]. Use MilvusClient (not the legacy connections/Collection API). Random vectors must be L2-normalized. Add an INVERTED scalar index on category so filtered search is meaningful. Search params must vary by index type. Print results as an aligned table. Exit codes: 0/2/3/4 as specified. Support --json-out for machine-readable output. Put every stage in its own function so the module is unit-testable, and wrap Milvus errors so the message names the failing stage."

---

### WP-12 — Dashboard (Requirement 4)

**Produces:** the `dashboard/` app + `infra/nginx/dashboard.conf` + a compose service.

Single page, six panels, all fed by one `GET /api/v1/clusters/{id}/overview` polled every 5 s via TanStack Query (`refetchInterval: 5000`, `retry: 1`, keep previous data on error):

1. **Header / ConnectionBanner** — cluster name, deployment type, overall status pill (green/amber/red/grey), Milvus version, "last checked N s ago". When `live_status !== "ok"`, a persistent amber/red banner naming the failed dependency and its error code.
2. **Metadata card** — every column of the `clusters` row.
3. **Component table** — name, state, health, restart count, uptime, image; row background reflects state.
4. **Collections table** — name, rows, dim, index type, metric, load state; empty-state text "no collections — run `make demo`".
5. **Metrics panel** — the allowlisted metrics as label/value tiles; `available: false` renders greyed with "not exposed by this version".
6. **Log viewer** — last 100 lines, component dropdown, auto-scroll toggle, monospace, severity-tinted, "paused" indicator when auto-scroll is off.
7. **Events strip** — last 10 events with severity colour and relative time. *(This is what you point at during the reliability demo.)*

Rules:
- Three distinct render states everywhere: loading, error, empty. No panel may render a bare blank box.
- **Stale data must be visually distinct** — when `stale: true`, dim the panel and show `observed_at` as "as of 12:03:41 (stale)". A dashboard that shows an old number as if it were current is worse than one that shows nothing.
- No polling storm: one query for `/overview`, plus one for `/logs`. Do not give every component its own request.
- `nginx.conf`: serve `/` from the SPA build with `try_files ... /index.html`; `location /api/ { proxy_pass http://cp-api:8000; }`; `proxy_read_timeout 15s`.
- Styling: one plain CSS file. Explicitly do not add a component library — visual polish is not graded and it adds review surface you'd have to defend.

**Acceptance:** with the stack healthy, all six panels populate within 5 s of load; `docker stop milvus-standalone` → within ~15 s the banner turns red, component table shows `exited`, collections/metrics panels go stale-dimmed, metadata panel stays live; no console errors, no white screen.

> **Claude Code prompt:** "Build a Vite + React 18 + TypeScript single-page dashboard with TanStack Query. One GET /api/v1/clusters/{id}/overview poll every 5s feeds these six panels: [paste panel list]. Implement explicit loading/error/empty states for every panel, and render stale data dimmed with an 'as of <time> (stale)' label whenever the envelope has stale:true. Do not add a component library — one plain CSS file. Also write infra/nginx/dashboard.conf serving the SPA with try_files fallback and proxying /api/ to cp-api:8000 with a 15s read timeout, plus a multi-stage Dockerfile (node build → nginx:alpine)."

---

### WP-13 — Wire the app tier into Compose

**Produces:** `cp-api` and `cp-dashboard` services added to `infra/docker-compose.yml` under profile `app`; `deploy.sh up --profile all` support.

- `cp-api`: build from `control_plane/`, mount `/var/run/docker.sock:ro`, depends_on postgres healthy, `restart: unless-stopped`, env from `.env`, its own HEALTHCHECK.
- Migrations run as a **one-shot `cp-migrate` service** (`command: alembic upgrade head`, `restart: "no"`) that `cp-api` depends on with `condition: service_completed_successfully`. Do not run migrations in the API entrypoint — concurrent replicas would race.
- Document the Docker socket mount as a security trade-off in `docs/ARCHITECTURE.md`: it grants root-equivalent host access, is acceptable for a local demo, and in production would be replaced by the Docker API over TLS or, on K8s, a ServiceAccount with a read-only Role.

**Acceptance:** `./infra/deploy.sh up --profile all` brings up 7 containers; `http://localhost:8080` renders the dashboard; `http://localhost:8000/docs` renders the API.

> **Claude Code prompt:** "Add cp-migrate (one-shot alembic upgrade head), cp-api and cp-dashboard services to infra/docker-compose.yml under the 'app' profile, per this spec: [paste WP-13]. cp-api depends on cp-migrate with condition: service_completed_successfully and on postgres with condition: service_healthy. Extend deploy.sh to accept --profile all."

---

### WP-14 — Tests

**Produces:** `app/tests/unit/*`, `app/tests/integration/*`, `scripts/smoke_test.sh`.

- **Unit (no infra):** error classification in the Milvus adapter for each code; `aggregate_status()` truth table — one test per rule in WP-09, including the `unknown` cases; circuit breaker state transitions incl. half-open; metrics parsing against a captured `/metrics` fixture file, plus a fixture with metrics *removed* to prove `available: false`; envelope serialization.
- **Integration (stack up):** register cluster → force health check → assert persisted row; run `milvus_demo.py --keep` → assert `/collections` reflects it; stop Milvus → assert `/health` returns 200 with `live_status: "unavailable"` (this is the regression test for the single most important design rule).
- `smoke_test.sh`: curl every endpoint, assert HTTP codes and required JSON fields with `jq`, exit non-zero on first failure. This is what you run live in front of a reviewer.

**Acceptance:** `make test` green; `make smoke` green against a running stack.

> **Claude Code prompt:** "Write pytest unit tests (pytest-asyncio, no infrastructure required) covering: [paste unit list], including a truth-table test with one case per aggregate_status rule. Write integration tests that assume the stack is up, covering: [paste integration list]. Write scripts/smoke_test.sh using curl and jq to exercise every endpoint in the API contract, asserting status codes and required fields, exiting non-zero on the first failure."

---

### WP-15 — Reliability drills and diagnosis writeup (Requirement 5)

**Produces:** `scripts/chaos.sh`, `docs/RELIABILITY.md`.

`chaos.sh` subcommands: `milvus-stop`, `milvus-pause`, `minio-stop`, `postgres-stop`, `etcd-stop`, `network-cut <service>` (`docker network disconnect`), `recover-all`, `status`. Each prints a timestamped banner before and after so log timelines line up with the writeup.

Run each drill and record, in `docs/RELIABILITY.md`, a table per scenario with these five columns: **injection command / expected behaviour / how it was detected / how it was diagnosed / recovery + observed MTTR**.

| # | Injection | Expected control-plane behaviour | Primary detection signal |
|---|---|---|---|
| A | `docker stop milvus-standalone` | `/health` → 200, `live_status: unavailable`, code `MILVUS_UNREACHABLE`; metadata endpoints unaffected; one `health_transition` event; components table shows `exited`; breaker opens after 3 failures | dashboard banner red within ~15 s; `events` row |
| B | `docker pause milvus-standalone` | code `MILVUS_TIMEOUT`, **not** `MILVUS_UNREACHABLE` — proves timeouts are enforced and a hung dependency can't hang the API | `/overview` still returns within its 6 s budget |
| C | `docker stop milvus-minio` | `/healthz` on 9091 may stay 200 for a while; `deep_probe` and any insert/flush fail; status → `degraded`; Milvus logs show S3/object-store connection errors | **the interesting one** — shallow health lies; `deep_probe` + Milvus logs catch it |
| D | `docker stop cp-postgres` | metadata routes → 503 with `POSTGRES_UNAVAILABLE`; `/api/v1/clusters/{id}/health` still serves live Milvus data with `cluster: null`; scheduler logs WARNING and keeps running; **on restart, `pool_pre_ping` reconnects with no API restart** | `/readyz` flips to 503 |
| E | `docker stop milvus-etcd` | Milvus degrades on metadata ops; existing loaded collections may still serve reads — document what you actually observe, not what you expect | Milvus container logs |
| F | `network-cut cp-api` from `cp-net` | dashboard shows API-unreachable state rather than a white screen | browser network tab |

Diagnosis section must, for each scenario, name the **exact commands used** and paste representative (trimmed) output: `docker compose ps`, `docker logs --tail 100 <c>`, `curl -sv localhost:9091/healthz`, `nc -zv localhost 19530`, `curl -s localhost:8000/api/v1/clusters/<id>/health | jq`, `curl -s localhost:8000/api/v1/events | jq`, `psql -c 'select * from events order by created_at desc limit 10'`, and the Milvus WebUI at `http://localhost:9091/webui/`.

Close with a short **"what I'd add for production"** list: Prometheus + Alertmanager scraping both Milvus and the control plane, OpenTelemetry traces across the fan-out, a synthetic canary that inserts and searches every 60 s (the only check that would have caught scenario C immediately), PgBouncer, and MinIO in distributed mode.

**Acceptance:** every scenario in the table has been actually executed, with real timestamps and real trimmed output pasted in. Do not write this document from expectation.

> **Claude Code prompt:** "Write scripts/chaos.sh with these subcommands: [paste list], each printing a timestamped banner before and after injection so logs can be correlated. Then write a docs/RELIABILITY.md skeleton with one section per scenario A–F: [paste table], each with subsections Injection / Expected / Detection / Diagnosis commands / Recovery and MTTR, and placeholders marked TODO-PASTE-OUTPUT for real captured output. Include a closing 'production hardening' section."

---

### WP-16 — README, architecture docs, AI-usage doc

**Produces:** `README.md`, `docs/ARCHITECTURE.md`, `docs/AI_USAGE.md`.

`README.md` required sections, in this order:
1. **What this is** — three sentences + the endpoint table from §2.
2. **Architecture diagram** — ASCII or Mermaid: browser → nginx → FastAPI → {Postgres, Milvus gRPC, Milvus :9091, Docker socket}; Milvus → {etcd, MinIO}.
3. **Prerequisites** — Docker ≥ 24, Compose v2, 8 GB RAM allocated to Docker, ~20 GB disk, ports listed.
4. **Quickstart** — literally: `cp .env.example .env` → `make up` → `make demo` → open `http://localhost:8080` → `make smoke`. Must be copy-pasteable and must have been executed verbatim on a clean machine before submission.
5. **Command reference** — every `deploy.sh` subcommand and every Make target.
6. **API reference** — the §4 table plus a link to `/docs`, with 2–3 example `curl` calls and trimmed responses.
7. **Milvus operations script** — full CLI options and one sample run's output.
8. **Technology choices and trade-offs** — table from §0 with the *why* column, plus what was rejected and why (K8s/Operator: heavier setup for the same demo surface; Go/Node backend: no first-class Milvus SDK; Grafana: would have satisfied the dashboard requirement but hides the API-composition work being evaluated).
9. **Assumptions and known limitations** — be specific and unflinching. Minimum: single-cluster in practice though the schema is multi-cluster; no authn/authz on the control plane; Docker socket mounted read-only but still root-equivalent; default credentials; metrics allowlist may drift across Milvus versions; standalone only (no HA); no TLS; logs read from Docker rather than a log aggregator; `distributed` mode not implemented; retention is time-based with no downsampling.
10. **Teardown** — `make down` vs `make destroy` and exactly what each deletes.
11. **Troubleshooting** — the five failures you actually hit (Milvus needs ~120 s to become healthy on first boot; port conflicts; Docker memory limits; socket permissions on Linux; MinIO bucket missing).
12. **AI usage** — link to `docs/AI_USAGE.md`.

`docs/AI_USAGE.md` must be honest and concrete: which tool, which parts it drafted (compose file, boilerplate CRUD, React panels), which parts you designed and it did not (degradation envelope, `aggregate_status` rule ordering, deep-probe rationale, the metrics allowlist), what it got **wrong** that you caught (this section is the one a reviewer actually reads — e.g. suggested Pulsar for 2.6, hallucinated metric names, proposed a shallow `/healthz`-only health model, returned 500 on dependency failure), and how you verified everything.

**Acceptance:** a colleague with Docker installed and no context follows the Quickstart verbatim and reaches a working dashboard without asking a question.

> **Claude Code prompt:** "Write README.md with exactly these twelve sections in this order: [paste list], including a Mermaid architecture diagram, copy-pasteable quickstart, the full API table, and a specific unflinching limitations section. Then write docs/ARCHITECTURE.md expanding on component responsibilities, the degradation envelope, the data model, and the Docker-socket security trade-off. Then write docs/AI_USAGE.md with a section for what AI drafted, what I designed, what AI got wrong and I corrected, and how I verified the output."

---

### WP-17 (optional, only if time remains) — Kubernetes / Milvus Operator path

**Produces:** `infra/k8s/` with kind cluster config, Milvus Operator install steps, a `Milvus` CR for standalone mode, a Postgres manifest, and control-plane Deployment/Service/Ingress.

Also requires: a `KubernetesAdapter` implementing the same interface as `docker_client.py` (pod status + `read_namespaced_pod_log`), selected by `clusters.deployment_type`. **The fact that WP-07's adapter is behind an interface is what makes this a two-file change rather than a rewrite** — say so in `ARCHITECTURE.md` even if you don't build it.

Only attempt this after WP-01 through WP-16 are complete and demoed.

---

## 6. Execution order and dependency graph

```
WP-01 ─┬─> WP-02 ──> WP-03 ──────────────┐
       │                                  │
       └─> WP-05 ─┬─> WP-04 ─┐            │
                  ├─> WP-06 ─┤            │
                  ├─> WP-07 ─┼─> WP-09 ──> WP-10 ─┬─> WP-12 ──> WP-13 ──> WP-15 ──> WP-16
                  └─> WP-08 ─┘                    │
                                                  └─> WP-14
WP-11 (independent — build early, it validates the stack before any API exists)
```

**Suggested sequencing for a time-boxed assignment:** WP-01 → WP-02 → WP-03 → WP-11 (you now have a provably working Milvus and a demoable Requirement 1 + 3) → WP-04/05 → WP-06/07/08 → WP-09 → WP-10 → WP-12 → WP-13 → WP-15 → WP-14 → WP-16.

Build WP-11 before the API. It is the cheapest possible proof that your infrastructure automation actually produced a functioning Milvus, and it de-risks everything downstream.

---

## 7. Definition of done (self-check before submitting)

- [ ] `git clone && cp .env.example .env && make up` works on a machine that has never run the project.
- [ ] `make destroy && make up` works — repeatability is explicitly graded.
- [ ] `make demo` prints ranked search results.
- [ ] Dashboard shows all six panels populated.
- [ ] `make chaos-milvus` → dashboard degrades visibly, API returns 200s, an event row appears; `make chaos-recover` → recovers with no manual restart.
- [ ] `make chaos-postgres` → API stays up, recovers on its own via `pool_pre_ping`.
- [ ] `docs/RELIABILITY.md` contains real captured output, not predictions.
- [ ] `alembic downgrade base && alembic upgrade head` round-trips.
- [ ] No secrets committed; `.env` is gitignored; `.env.example` has no real credentials.
- [ ] Every image tag is pinned; no `:latest` anywhere.
- [ ] You can explain, without notes: why the envelope returns 200 on dependency failure, why `deep_probe` exists alongside `/healthz`, why events are transition-only, and why migrations run as a separate one-shot service.

---

## 8. Highest-value differentiators (what separates a pass from a strong pass)

Most submissions will produce a stack, some CRUD, and a UI. These are the things reviewers notice:

1. **The degradation envelope.** Returning 200 with a structured `degraded_reason` instead of a 500 is a control-plane design decision, and it is the single clearest signal of production experience in this assignment.
2. **`deep_probe` vs `/healthz`.** Demonstrating that MinIO can be down while Milvus reports healthy — and that you built a check that catches it — is the reliability insight scenario C is fishing for.
3. **Transition-only events.** Gives you a real incident timeline to point at during the demo instead of scrolling logs.
4. **Version-tolerant metrics allowlist.** Shows you thought about what happens after a Milvus upgrade.
5. **`pool_pre_ping` self-healing.** Recovering from a Postgres restart without touching the API is a two-line change with a large demo payoff.
6. **A `RELIABILITY.md` with real timestamps.** Predicted behaviour is worthless; captured behaviour is evidence.