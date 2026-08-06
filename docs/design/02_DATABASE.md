# 02 — DATABASE SPECIFICATION

> Exact schema, exact migration contents, exact seed and population scripts, exact verification queries. Postgres 16, one database, one schema (`public`), five tables, three enums.

---

## 2.0 Ownership rules

| Object | Created by | Never created by |
|---|---|---|
| Database `controlplane`, role `controlplane` | Postgres image entrypoint via `POSTGRES_*` env | anything else |
| Extensions `pgcrypto`, `pg_stat_statements` | `infra/postgres/initdb.sql` | Alembic |
| Enums, tables, indexes, constraints | **Alembic migrations only** | initdb.sql, ORM `create_all()`, hand SQL |
| Seed rows | `scripts/seed_cluster.sh` (idempotent upsert) | migrations |
| Demo/history rows | `scripts/populate_history.py` | anything automatic |

`Base.metadata.create_all()` must appear nowhere in the codebase, including tests. Tests run migrations. Two schema sources is the failure mode this rule prevents.

---

## 2.1 Enums — exact definitions

Three native Postgres enums. They are created **before** any table that references them and dropped **after** all of them.

```sql
CREATE TYPE deployment_type AS ENUM (
    'docker_standalone',
    'docker_distributed',
    'k8s_operator'
);

CREATE TYPE deployment_status AS ENUM (
    'pending',        -- registered, never yet probed
    'provisioning',   -- deploy.sh is bringing it up
    'running',        -- last health check healthy
    'degraded',       -- reachable but something is wrong
    'unavailable',    -- not reachable
    'stopped',        -- intentionally down (make down)
    'deleted'         -- soft-deleted
);

CREATE TYPE health_status AS ENUM (
    'healthy',
    'degraded',
    'unavailable',
    'unknown'         -- could not determine; NEVER conflate with healthy
);
```

**Design note to put in a comment:** `unknown` exists because a control plane that cannot reach its own probe path must say so rather than defaulting to `healthy`. Every place that writes a status must be able to produce `unknown`.

**SQLAlchemy mapping requirement:** declare these with `sqlalchemy.Enum(PyEnum, name="deployment_type", create_type=False)` and create/drop them explicitly in the migration. Letting SQLAlchemy manage enum creation produces ordering bugs on downgrade.

---

## 2.2 Table DDL — exact, column by column

### 2.2.1 `clusters`

```sql
CREATE TABLE clusters (
    id                      UUID              PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT              NOT NULL,
    deployment_type         deployment_type   NOT NULL,
    deployment_status       deployment_status NOT NULL DEFAULT 'pending',
    milvus_version          TEXT              NULL,
    endpoint_uri            TEXT              NOT NULL,
    metrics_uri             TEXT              NULL,
    object_store_endpoint   TEXT              NULL,
    object_store_bucket     TEXT              NULL,
    compose_project         TEXT              NULL,
    namespace               TEXT              NULL,
    expected_components     JSONB             NOT NULL DEFAULT '[]'::jsonb,
    labels                  JSONB             NOT NULL DEFAULT '{}'::jsonb,
    last_health_check_at    TIMESTAMPTZ       NULL,
    last_health_status      health_status     NOT NULL DEFAULT 'unknown',
    created_at              TIMESTAMPTZ       NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ       NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ       NULL,

    CONSTRAINT ck_clusters_name_format
        CHECK (name ~ '^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$'),
    CONSTRAINT ck_clusters_endpoint_scheme
        CHECK (endpoint_uri ~ '^(http|https|grpc)://'),
    CONSTRAINT ck_clusters_k8s_namespace
        CHECK (deployment_type <> 'k8s_operator' OR namespace IS NOT NULL)
);

CREATE UNIQUE INDEX uq_clusters_name_active
    ON clusters (name) WHERE deleted_at IS NULL;

CREATE INDEX ix_clusters_deployment_status ON clusters (deployment_status);
CREATE INDEX ix_clusters_last_health_check_at ON clusters (last_health_check_at DESC);

COMMENT ON COLUMN clusters.expected_components IS
    'Component names the health aggregator requires to be running; a missing one yields degraded.';
COMMENT ON CONSTRAINT ck_clusters_k8s_namespace ON clusters IS
    'A k8s_operator cluster is unaddressable without a namespace.';
```

Three things worth defending in a review:
- The **partial unique index** on `name WHERE deleted_at IS NULL` lets you soft-delete `prod-a` and later create a new `prod-a`. A plain `UNIQUE` would block that forever.
- `ck_clusters_name_format` mirrors DNS-label rules because the name is used in URLs and (in the k8s path) in resource names.
- `expected_components` is per-cluster rather than global config, because a distributed deployment has a different component set than standalone.

### 2.2.2 `health_checks`

```sql
CREATE TABLE health_checks (
    id                        BIGGENERATED,   -- see note: BIGSERIAL / GENERATED ALWAYS AS IDENTITY
    cluster_id                UUID          NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    checked_at                TIMESTAMPTZ   NOT NULL DEFAULT now(),
    status                    health_status NOT NULL,
    latency_ms                INTEGER       NULL,
    milvus_reachable          BOOLEAN       NOT NULL,
    milvus_deep_probe_ok      BOOLEAN       NULL,
    object_store_reachable    BOOLEAN       NULL,
    metadata_store_reachable  BOOLEAN       NULL,
    docker_reachable          BOOLEAN       NULL,
    server_version            TEXT          NULL,
    collection_count          INTEGER       NULL,
    error_code                TEXT          NULL,
    error_message             TEXT          NULL,
    raw                       JSONB         NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_health_latency_nonneg CHECK (latency_ms IS NULL OR latency_ms >= 0)
);
```
Use `id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY` (SQL-standard, preferred over `BIGSERIAL` on PG 16).

```sql
CREATE INDEX ix_health_checks_cluster_time ON health_checks (cluster_id, checked_at DESC);
CREATE INDEX ix_health_checks_checked_at ON health_checks (checked_at);
CREATE INDEX ix_health_checks_status_partial
    ON health_checks (cluster_id, checked_at DESC)
    WHERE status <> 'healthy';
```
The third index is the one that makes "show me the last incident" fast without scanning the 5,760 healthy rows/day the 15-second poll produces.

**Nullable booleans are intentional.** `object_store_reachable = NULL` means *not checked this round*, which is semantically different from `FALSE` (*checked and down*). Do not collapse them into a non-null default; the failure drills depend on the distinction.

### 2.2.3 `component_status`

```sql
CREATE TABLE component_status (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id      UUID        NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    component_name  TEXT        NOT NULL,
    kind            TEXT        NOT NULL DEFAULT 'container',
    runtime_id      TEXT        NULL,
    image           TEXT        NULL,
    state           TEXT        NOT NULL,
    health          TEXT        NULL,
    restart_count   INTEGER     NOT NULL DEFAULT 0,
    exit_code       INTEGER     NULL,
    started_at      TIMESTAMPTZ NULL,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw             JSONB       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_component_kind CHECK (kind IN ('container','pod')),
    CONSTRAINT ck_component_state CHECK (state IN
        ('created','running','restarting','exited','paused','dead','missing','unknown'))
);

CREATE INDEX ix_component_status_lookup
    ON component_status (cluster_id, component_name, observed_at DESC);
CREATE INDEX ix_component_status_observed_at ON component_status (observed_at);
```

`'missing'` is a first-class state, not an absence of rows. A container that has been `docker rm`'d must produce a row saying so — otherwise the dashboard shows nothing and the operator concludes everything is fine.

### 2.2.4 `collection_snapshots`

```sql
CREATE TABLE collection_snapshots (
    id               BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id       UUID        NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    collection_name  TEXT        NOT NULL,
    row_count        BIGINT      NULL,
    num_partitions   INTEGER     NULL,
    num_fields       INTEGER     NULL,
    dimension        INTEGER     NULL,
    vector_field     TEXT        NULL,
    index_type       TEXT        NULL,
    metric_type      TEXT        NULL,
    is_loaded        BOOLEAN     NULL,
    load_progress    INTEGER     NULL,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw              JSONB       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_collection_rowcount_nonneg CHECK (row_count IS NULL OR row_count >= 0),
    CONSTRAINT ck_collection_dim CHECK (dimension IS NULL OR dimension BETWEEN 1 AND 32768)
);

CREATE INDEX ix_collection_snapshots_lookup
    ON collection_snapshots (cluster_id, collection_name, observed_at DESC);
CREATE INDEX ix_collection_snapshots_observed_at ON collection_snapshots (observed_at);
```

### 2.2.5 `events`

```sql
CREATE TABLE events (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cluster_id  UUID        NULL REFERENCES clusters(id) ON DELETE CASCADE,
    event_type  TEXT        NOT NULL,
    severity    TEXT        NOT NULL DEFAULT 'info',
    message     TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT ck_events_severity CHECK (severity IN ('info','warning','error','critical')),
    CONSTRAINT ck_events_type CHECK (event_type IN (
        'cluster_registered','cluster_updated','cluster_deleted',
        'health_transition','component_state_change',
        'dependency_failure','dependency_recovered',
        'breaker_opened','breaker_closed',
        'migration_applied','scheduler_started','scheduler_error'
    ))
);

CREATE INDEX ix_events_created_at ON events (created_at DESC);
CREATE INDEX ix_events_cluster_created ON events (cluster_id, created_at DESC);
CREATE INDEX ix_events_type ON events (event_type, created_at DESC);
```

The `CHECK` on `event_type` is deliberate: an enum would require a migration to add a type, and a free-text column drifts into typo'd variants that break dashboard filters. A check constraint is the middle ground, and it fails loudly in tests when someone invents a new type without updating the list.

**Payload conventions — enforce these in code review:**

| `event_type` | Required `payload` keys |
|---|---|
| `health_transition` | `from`, `to`, `error_code`, `latency_ms` |
| `component_state_change` | `component`, `from`, `to`, `restart_count` |
| `dependency_failure` | `dependency`, `error_code`, `error_message` |
| `breaker_opened` | `dependency`, `consecutive_failures` |

---

## 2.3 The `updated_at` trigger

Application code will forget. The database will not.

```sql
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_clusters_updated_at
    BEFORE UPDATE ON clusters
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

---

## 2.4 Alembic configuration — exact spec

### 2.4.1 `control_plane/alembic.ini`
- `script_location = migrations`
- `prepend_sys_path = .`
- `file_template = %%(year)d%%(month).2d%%(day).2d_%%(hour).2d%%(minute).2d_%%(slug)s`
- **Do not set `sqlalchemy.url`.** Leave it empty; `env.py` supplies it from settings so there is one source of truth for the DSN.
- `[post_write_hooks]` running `ruff format` on generated files.

### 2.4.2 `control_plane/migrations/env.py` — required behaviour

| Requirement | Detail |
|---|---|
| URL source | `from app.config import get_settings; url = get_settings().postgres_dsn_sync` — note **sync** DSN (`postgresql+psycopg2://` or plain `postgresql://`) for Alembic; async is for the app |
| Target metadata | `from app.db.base import Base; target_metadata = Base.metadata` |
| Offline mode | `context.configure(url=url, target_metadata=..., literal_binds=True, compare_type=True)` |
| Online mode | create a **synchronous** `Engine` with `poolclass=NullPool` |
| `compare_type=True` | so autogenerate notices a `TEXT`→`VARCHAR(64)` change |
| `include_object` hook | skip anything in schema `pg_catalog`/`information_schema` |
| Enum handling | set `configure(..., include_schemas=False)` and rely on hand-written enum DDL — see below |

**Why a sync engine for Alembic:** async Alembic works but adds an event-loop wrapper for no benefit in a one-shot migration container. Two DSN properties on `Settings` (`postgres_dsn_async`, `postgres_dsn_sync`) built from the same components keeps them consistent.

### 2.4.3 Revision 0001 — `initial_schema`

**Filename:** `migrations/versions/20260806_0900_initial_schema.py`
**`revision = "0001_initial"`, `down_revision = None`**

`upgrade()` must execute in **exactly this order**:

1. `op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")` — defensive; initdb already did it, but tests may run against a bare DB.
2. Create the three enum types via `sa.Enum(...).create(op.get_bind(), checkfirst=True)` or raw `op.execute("CREATE TYPE …")`. **Pick raw SQL** — it is explicit and downgrade-symmetric.
3. `op.create_table("clusters", …)` with all columns, the three CHECK constraints, and `postgresql_using` where relevant.
4. `op.create_index` × 3 for clusters (including the **partial** unique index via `postgresql_where=sa.text("deleted_at IS NULL")`).
5. `op.create_table("health_checks", …)` + 3 indexes (the partial one via `postgresql_where`).
6. `op.create_table("component_status", …)` + 2 indexes.
7. `op.create_table("collection_snapshots", …)` + 2 indexes.
8. `op.create_table("events", …)` + 3 indexes.
9. `op.execute(<set_updated_at function DDL>)`.
10. `op.execute(<trigger DDL>)`.
11. `op.execute(<COMMENT ON … statements>)`.

`downgrade()` reverses in **exact inverse order**: drop trigger → drop function → drop tables in reverse dependency order (`events`, `collection_snapshots`, `component_status`, `health_checks`, `clusters`) → `DROP TYPE health_status`, `deployment_status`, `deployment_type`. Do not rely on `op.drop_table` cascading the enums; it will not, and the next `upgrade` will fail with "type already exists" — which is precisely the bug the round-trip test in §2.8 catches.

### 2.4.4 Revision 0002 — `seed_reference_data` (optional but recommended)

Insert nothing environment-specific. If you want a migration-level seed, restrict it to a single row in a `schema_meta` table recording the schema's semantic version and creation time. **Cluster rows are not migration data** — they are environment data, and they belong in `seed_cluster.sh`.

---

## 2.5 `scripts/seed_cluster.sh` — the registration script

### Purpose
After `alembic upgrade head`, register the local Milvus instance so the dashboard has something to show. Must be **idempotent** — `deploy.sh up` calls it every time.

### Two implementations; build the API one, keep the SQL one as a fallback

**Primary (via the API, once `cp-api` is up):**
```bash
#!/usr/bin/env bash
set -euo pipefail
API="${CP_API_URL:-http://localhost:8000}"
NAME="${CP_SEED_CLUSTER_NAME:-local-milvus-standalone}"

# Idempotent: look first
EXISTING=$(curl -sf "${API}/api/v1/clusters?name=${NAME}" | jq -r '.items[0].id // empty')
if [[ -n "$EXISTING" ]]; then
  echo "cluster ${NAME} already registered: ${EXISTING}"
  echo "$EXISTING" > .cluster_id
  exit 0
fi

ID=$(curl -sf -X POST "${API}/api/v1/clusters" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n \
    --arg name "$NAME" \
    --arg uri "${MILVUS_URI}" \
    --arg murl "${MILVUS_METRICS_URI}" \
    --arg os "http://milvus-minio:9000" \
    --arg bucket "${MINIO_BUCKET}" \
    --arg proj "${COMPOSE_PROJECT_NAME}" \
    '{
      name: $name,
      deployment_type: "docker_standalone",
      endpoint_uri: $uri,
      metrics_uri: $murl,
      object_store_endpoint: $os,
      object_store_bucket: $bucket,
      compose_project: $proj,
      expected_components: ["milvus-standalone","milvus-etcd","milvus-minio","cp-postgres"],
      labels: {environment:"local", owner:"assignment", managed_by:"deploy.sh"}
    }')" | jq -r '.id')

echo "$ID" > .cluster_id
echo "registered cluster ${NAME} -> ${ID}"
```

Writing the id to `.cluster_id` (gitignored) is what lets every other script and the smoke test address the cluster without hardcoding a UUID.

**Fallback (direct SQL, works with cp-api down):**
```sql
INSERT INTO clusters (
    name, deployment_type, deployment_status, endpoint_uri, metrics_uri,
    object_store_endpoint, object_store_bucket, compose_project,
    expected_components, labels
) VALUES (
    'local-milvus-standalone', 'docker_standalone', 'pending',
    'http://milvus-standalone:19530', 'http://milvus-standalone:9091',
    'http://milvus-minio:9000', 'milvus-bucket', 'milvus-cp',
    '["milvus-standalone","milvus-etcd","milvus-minio","cp-postgres"]'::jsonb,
    '{"environment":"local","owner":"assignment"}'::jsonb
)
ON CONFLICT (name) WHERE deleted_at IS NULL
DO UPDATE SET
    endpoint_uri = EXCLUDED.endpoint_uri,
    metrics_uri  = EXCLUDED.metrics_uri,
    updated_at   = now()
RETURNING id;
```
The `ON CONFLICT … WHERE` clause must match the partial unique index exactly, or Postgres will not use it as the arbiter.

---

## 2.6 `scripts/populate_history.py` — demo data generator

### Purpose
A freshly-started stack has one health check and zero events. The dashboard's history panel and the incident strip look broken because they are empty. This script backfills a realistic 24 hours so the UI can be demonstrated immediately, and so you can test retention and pagination against non-trivial volume.

**It is a demo aid, not part of the running system.** Say so in the README, and never call it from `deploy.sh up`.

### CLI
```
python scripts/populate_history.py \
  --dsn postgresql://controlplane:controlplane@localhost:5432/controlplane \
  --cluster-id "$(cat .cluster_id)" \
  --hours 24 \
  --interval-s 15 \
  --incidents 3 \
  --seed 42 \
  [--dry-run] [--purge-first]
```

### Generation algorithm — specify this precisely

1. Compute `n = hours * 3600 / interval_s` timestamps ending at `now()`, walking backwards.
2. Choose `--incidents` non-overlapping windows at random (seeded), each 3–12 minutes long, each assigned a type from: `milvus_down` (status `unavailable`, `milvus_reachable=false`, `error_code='MILVUS_UNREACHABLE'`), `minio_down` (status `degraded`, `milvus_reachable=true`, `milvus_deep_probe_ok=false`, `object_store_reachable=false`), `component_flap` (status `degraded`, one `component_status` row in state `restarting`).
3. Outside incident windows: status `healthy`, `latency_ms` sampled from a lognormal centred on 12 ms with occasional 3× spikes.
4. Insert `health_checks` rows in batches of 500 via `executemany`.
5. **Emit `events` only at window boundaries** — one `health_transition` on entry, one on exit. Assert at the end that `count(events) == incidents * 2 + 1`. This assertion is the regression test for the transition-only rule; if the generator can violate it, so can the real health job.
6. Generate `component_status` rows every `snapshot_interval` (60 s) and `collection_snapshots` for two synthetic collections with a row count that increases monotonically with a plateau during incidents.
7. Print a summary table: rows per table, incident windows with timestamps, wall-clock duration.

### Verification after running
```sql
SELECT status, count(*) FROM health_checks GROUP BY status ORDER BY 2 DESC;
SELECT event_type, severity, count(*) FROM events GROUP BY 1,2;
SELECT min(checked_at), max(checked_at), count(*) FROM health_checks;
-- the transition-only invariant:
SELECT count(*) FROM events WHERE event_type='health_transition';   -- expect incidents*2 (+1 initial)
```

---

## 2.7 Query catalogue — the exact SQL each repository method runs

Specify these so Claude Code writes the right query rather than a naive one.

| Repository method | Query shape | Note |
|---|---|---|
| `cluster_repo.get_by_id` | `SELECT * FROM clusters WHERE id=$1 AND deleted_at IS NULL` | soft-delete filter is mandatory in every read |
| `cluster_repo.list` | `… ORDER BY created_at DESC LIMIT $1 OFFSET $2` plus a `count(*) OVER ()` window for `total` | one round trip, not two |
| `cluster_repo.soft_delete` | `UPDATE clusters SET deleted_at=now(), deployment_status='deleted' WHERE id=$1` | never `DELETE` |
| `health_repo.latest` | `SELECT * FROM health_checks WHERE cluster_id=$1 ORDER BY checked_at DESC LIMIT 1` | uses `ix_health_checks_cluster_time` |
| `health_repo.history` | `… WHERE cluster_id=$1 AND checked_at > now() - $2::interval ORDER BY checked_at DESC LIMIT $3` | |
| `health_repo.insert_and_update_cluster` | **single transaction**: `INSERT INTO health_checks …` then `UPDATE clusters SET last_health_check_at, last_health_status, deployment_status, milvus_version WHERE id=$1` | must be atomic — a health row without the denormalised cluster update makes the list view lie |
| `component_repo.latest_per_component` | `SELECT DISTINCT ON (component_name) * FROM component_status WHERE cluster_id=$1 ORDER BY component_name, observed_at DESC` | `DISTINCT ON` is the right tool here; do not emulate it with a window function subquery |
| `collection_repo.latest_per_collection` | same `DISTINCT ON (collection_name)` shape | |
| `event_repo.list` | `SELECT * FROM events WHERE ($1::uuid IS NULL OR cluster_id=$1) ORDER BY created_at DESC LIMIT $2 OFFSET $3` | nullable filter without dynamic SQL |
| `retention.purge` | `DELETE FROM health_checks WHERE checked_at < now() - $1::interval` (and the other two tables), then `DELETE FROM events WHERE created_at < now() - ($1*4)::interval` | run each in its own transaction; batch with `LIMIT` + loop if you expect >1M rows |

**Concurrency requirement on `insert_and_update_cluster`:** wrap in `async with session.begin():`. With `--workers 1` there is one writer, but the forced-check endpoint (`POST /health-check`) can race the scheduler. Use `SELECT … FOR UPDATE` on the cluster row, or accept last-writer-wins and document it. Pick one; do not leave it unconsidered.

---

## 2.8 Database acceptance tests — must all pass

```bash
PG="postgresql://controlplane:controlplane@localhost:5432/controlplane"

# 1. Migration applies cleanly to an empty DB
docker compose ... run --rm cp-migrate
psql "$PG" -c "SELECT version_num FROM alembic_version;"    # 0001_initial

# 2. Exactly 5 tables + alembic_version
psql "$PG" -Atc "SELECT count(*) FROM information_schema.tables
                 WHERE table_schema='public'"                # 6

# 3. Three enums exist with the right cardinality
psql "$PG" -c "SELECT t.typname, count(e.enumlabel)
               FROM pg_type t JOIN pg_enum e ON e.enumtypid=t.oid
               GROUP BY 1 ORDER BY 1;"
# deployment_status|7   deployment_type|3   health_status|4

# 4. Round trip — THE critical test for enum ordering bugs
docker compose ... run --rm cp-migrate alembic downgrade base
psql "$PG" -Atc "SELECT count(*) FROM pg_type WHERE typname='health_status'"   # 0
docker compose ... run --rm cp-migrate alembic upgrade head                     # must succeed

# 5. Models and schema agree — autogenerate must produce an EMPTY diff
docker compose ... run --rm cp-migrate alembic revision --autogenerate -m probe
# inspect the generated file: upgrade() body must be `pass`. Then delete it.

# 6. Constraints actually fire
psql "$PG" -c "INSERT INTO clusters (name,deployment_type,endpoint_uri)
               VALUES ('BadName!','docker_standalone','http://x:1');"
#   expect: violates check constraint "ck_clusters_name_format"
psql "$PG" -c "INSERT INTO clusters (name,deployment_type,endpoint_uri)
               VALUES ('ok-name','k8s_operator','http://x:1');"
#   expect: violates check constraint "ck_clusters_k8s_namespace"
psql "$PG" -c "INSERT INTO events (event_type,message) VALUES ('made_up','x');"
#   expect: violates check constraint "ck_events_type"

# 7. Soft delete then re-create the same name succeeds
psql "$PG" -c "UPDATE clusters SET deleted_at=now() WHERE name='local-milvus-standalone';"
bash scripts/seed_cluster.sh     # must succeed, not conflict

# 8. Cascade works
psql "$PG" -c "DELETE FROM clusters WHERE deleted_at IS NOT NULL;"
psql "$PG" -Atc "SELECT count(*) FROM health_checks hc
                 LEFT JOIN clusters c ON c.id=hc.cluster_id WHERE c.id IS NULL;"   # 0

# 9. Trigger works
psql "$PG" -c "UPDATE clusters SET milvus_version='v2.6.20' WHERE name='local-milvus-standalone'
               RETURNING updated_at > created_at;"    # t

# 10. Index usage on the hot query
psql "$PG" -c "EXPLAIN ANALYZE SELECT * FROM health_checks
               WHERE cluster_id=(SELECT id FROM clusters LIMIT 1)
               ORDER BY checked_at DESC LIMIT 1;"
#   expect: Index Scan using ix_health_checks_cluster_time — NOT Seq Scan
```

Test 5 is the one that catches drift between `models.py` and the migration, which is the defect that silently breaks a fresh install while working fine on your machine.

---

## 2.9 Claude Code prompts for this document

**Prompt A — models:**
> "Implement `control_plane/app/db/models.py` using SQLAlchemy 2.0 declarative with `Mapped[]`/`mapped_column()` for exactly these five tables and three enums: [paste §2.1 and §2.2]. Use `sa.Enum(..., name=..., create_type=False)` for the enums. Include every CHECK constraint, every index (including the two partial indexes via `postgresql_where`), every default, and the `COMMENT ON` text as `comment=` kwargs. Also implement `app/db/base.py` (DeclarativeBase with a naming convention for constraints) and `app/db/session.py` (async engine with `pool_pre_ping=True`, `pool_size=5`, `max_overflow=5`, `pool_recycle=300`, plus an `async_session_factory` and a `get_session` dependency). Never call `create_all()` anywhere."

**Prompt B — migration:**
> "Configure Alembic per [paste §2.4.1 and §2.4.2] and hand-write migration `0001_initial` per [paste §2.4.3], executing the eleven upgrade steps in exactly the stated order and reversing them in exactly the inverse order in `downgrade()`. Use raw `op.execute` for the three `CREATE TYPE` / `DROP TYPE` statements, the trigger function, the trigger, and the comments. The downgrade must leave `pg_type` with none of the three enums."

**Prompt C — scripts:**
> "Write `scripts/seed_cluster.sh` per [paste §2.5], idempotent, writing the cluster UUID to `.cluster_id`, with both the API path and a commented SQL fallback. Then write `scripts/populate_history.py` implementing the seven-step generation algorithm in [paste §2.6] with the stated CLI, using psycopg batched `executemany`, a seeded RNG, and a final assertion that the number of `health_transition` events equals `incidents * 2 + 1`."

---

## Next

Proceed to **03_BACKEND.md**.
