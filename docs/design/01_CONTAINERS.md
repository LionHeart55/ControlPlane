# 01 — CONTAINER SPECIFICATION, INSTANCE BY INSTANCE

> Seven container instances across two Compose profiles. This document specifies each one completely, then gives a bring-up procedure that starts them **one at a time with verification between each**, and only afterwards assembles them into a single Compose file.

---

## 1.0 The instance inventory

| # | Instance name | Profile | Role | Depends on | Startup order |
|---|---|---|---|---|---|
| I-1 | `milvus-etcd` | `infra` | Milvus metadata store (collection schemas, channel checkpoints) | — | 1 |
| I-2 | `milvus-minio` | `infra` | S3-compatible object store: segments, indexes, **and the Woodpecker WAL** | — | 1 (parallel with I-1) |
| I-3 | `milvus-minio-init` | `infra` | One-shot: creates the bucket, exits 0 | I-2 healthy | 2 |
| I-4 | `milvus-standalone` | `infra` | Milvus 2.6 — proxy, mixcoord, streamnode, querynode, datanode in one process | I-1 + I-2 healthy | 3 |
| I-5 | `cp-postgres` | `infra` | Control-plane relational metadata | — | 1 (parallel) |
| I-6 | `cp-migrate` | `app` | One-shot: `alembic upgrade head`, exits 0 | I-5 healthy | 4 |
| I-7 | `cp-api` | `app` | FastAPI control plane | I-6 completed, I-5 healthy | 5 |
| I-8 | `cp-dashboard` | `app` | nginx serving the SPA + reverse proxy | I-7 started | 6 |

Eight definitions, six long-lived containers (I-3 and I-6 exit).

**Network:** one user-defined bridge, `cp-net`, subnet `172.28.0.0/16`. Explicit subnet so you can reason about `docker network inspect` output during the failure drills.

**Universal labels on every service** (the control plane discovers containers by these, never by hardcoded names):
```yaml
labels:
  com.milvus-cp.stack: "milvus-control-plane"
  com.milvus-cp.component: "<instance-name>"
  com.milvus-cp.tier: "infra" | "app"
```

---

## I-1 — `milvus-etcd`

### Purpose
Stores Milvus metadata: collection schemas, partition maps, segment metadata, channel checkpoints, and (in 2.6) the Woodpecker log metadata. Losing this volume means every collection definition is gone even if the MinIO data survives.

### Complete specification

| Property | Value |
|---|---|
| Image | `quay.io/coreos/etcd:${ETCD_VERSION}` (`v3.5.25`) |
| Container name | `milvus-etcd` |
| Command | `etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd` |
| Network alias | `etcd` **and** `milvus-etcd` (Milvus config points at `etcd:2379`; the label-based discovery uses the container name) |
| Published ports | **none** — deliberately internal. Debug with `docker exec`. |
| Volume | `${DOCKER_VOLUME_DIRECTORY}/etcd:/etcd` |
| Restart policy | `unless-stopped` |
| Memory limit | `1g`, reservation `256m` |

### Environment variables — all four, and why each

| Variable | Value | Reason |
|---|---|---|
| `ETCD_AUTO_COMPACTION_MODE` | `revision` | Without compaction etcd's DB grows unboundedly and eventually refuses writes, taking Milvus down with it |
| `ETCD_AUTO_COMPACTION_RETENTION` | `1000` | Keep 1000 revisions |
| `ETCD_QUOTA_BACKEND_BYTES` | `4294967296` | 4 GB backend quota; the 2 GB default is too low for Milvus |
| `ETCD_SNAPSHOT_COUNT` | `50000` | Fewer snapshot writes → less fsync pressure |

### Healthcheck
```yaml
healthcheck:
  test: ["CMD", "etcdctl", "endpoint", "health"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 15s
```

### Standalone verification (run this before adding any other container)
```bash
docker exec milvus-etcd etcdctl endpoint health
#   expected: 127.0.0.1:2379 is healthy: successfully committed proposal: took = 1.2ms

docker exec milvus-etcd etcdctl put smoke "ok"
docker exec milvus-etcd etcdctl get smoke
#   expected: smoke \n ok
docker exec milvus-etcd etcdctl del smoke

docker inspect -f '{{.State.Health.Status}}' milvus-etcd
#   expected: healthy
```

### Expected startup time
2–5 seconds to healthy.

### Known failure modes
| Symptom | Cause | Fix |
|---|---|---|
| `mvcc: database space exceeded` | compaction env vars missing | ensure all four are set; `etcdctl defrag` |
| Repeated leader elections, `took too long` warnings | slow disk fsync | you are on a bind mount over `/mnt/c` (WSL) or a network drive — move the repo to a native filesystem |
| Container healthy but Milvus can't connect | network alias `etcd` missing | check `docker network inspect milvus-cp_cp-net` shows the alias |

---

## I-2 — `milvus-minio`

### Purpose
Two jobs in Milvus 2.6, and this surprises people: object storage for segments/indexes **and** the storage backend for the Woodpecker write-ahead log. That means MinIO is on the write hot path, not just the persistence path. This is exactly why the MinIO failure drill (Doc 05, scenario C) behaves the way it does.

### Complete specification

| Property | Value |
|---|---|
| Image | `minio/minio:${MINIO_VERSION}` |
| Container name | `milvus-minio` |
| Command | `minio server /minio_data --console-address ":9001"` |
| Network aliases | `minio`, `milvus-minio` |
| Published ports | `${MINIO_API_HOST_PORT}:9000`, `${MINIO_CONSOLE_HOST_PORT}:9001` |
| Volume | `${DOCKER_VOLUME_DIRECTORY}/minio:/minio_data` |
| Restart policy | `unless-stopped` |
| Memory limit | `1g`, reservation `256m` |

### Environment variables

| Variable | Value |
|---|---|
| `MINIO_ROOT_USER` | `${MINIO_ROOT_USER}` (`minioadmin`) |
| `MINIO_ROOT_PASSWORD` | `${MINIO_ROOT_PASSWORD}` (`minioadmin`) |

The console port is published on purpose: during the failure drills, being able to open `http://localhost:9001` and *see* the Milvus buckets and their objects is a genuinely useful diagnostic, and it lets you show a reviewer that segment files are real.

### Healthcheck
```yaml
healthcheck:
  test: ["CMD", "mc", "ready", "local"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 10s
```

### Standalone verification
```bash
curl -sf http://localhost:9000/minio/health/live && echo "MINIO LIVE OK"
docker exec milvus-minio mc alias set local http://localhost:9000 minioadmin minioadmin
docker exec milvus-minio mc ls local
docker inspect -f '{{.State.Health.Status}}' milvus-minio     # healthy
```
Browser check: `http://localhost:9001`, log in `minioadmin` / `minioadmin`.

### Expected startup time
3–8 seconds.

### Known failure modes
| Symptom | Cause | Fix |
|---|---|---|
| Exits immediately, `Unable to initialize backend` | `./volumes/minio` owned by root from a previous run | `sudo rm -rf volumes/minio && mkdir -p volumes/minio` |
| Health never passes on old images | `mc ready` unsupported | use `["CMD","curl","-f","http://localhost:9000/minio/health/live"]` instead |
| Milvus logs "NoSuchBucket" | I-3 never ran | run the bucket init (below) |

---

## I-3 — `milvus-minio-init` (one-shot)

### Purpose
Milvus will create its bucket on first use in most configurations, but relying on that makes the first-run failure mode confusing and undebuggable. Create it explicitly so a missing bucket is impossible.

### Complete specification

| Property | Value |
|---|---|
| Image | `minio/mc:${MINIO_MC_VERSION}` |
| Container name | `milvus-minio-init` |
| Restart policy | `"no"` — it must exit and stay exited |
| `depends_on` | `minio: {condition: service_healthy}` |
| Entrypoint | a `/bin/sh -c` script (below) |

### Entrypoint script (exact semantics)
```sh
until mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"; do
  echo 'waiting for minio'; sleep 2;
done
mc mb --ignore-existing "local/${MINIO_BUCKET}"
mc anonymous set none "local/${MINIO_BUCKET}"
mc ls local
echo 'BUCKET_INIT_COMPLETE'
```
`--ignore-existing` is what makes this idempotent — `deploy.sh up` will run it on every invocation and it must exit 0 every time.

### Verification
```bash
docker logs milvus-minio-init | tail -3         # expect BUCKET_INIT_COMPLETE
docker inspect -f '{{.State.ExitCode}}' milvus-minio-init   # expect 0
docker exec milvus-minio mc ls local            # expect a line for milvus-bucket
```

---

## I-4 — `milvus-standalone`

### Purpose
The whole of Milvus 2.6 in one process: proxy (gRPC on 19530), MixCoord, StreamingNode, QueryNode, DataNode. Exposes a Prometheus endpoint, a `/healthz` probe, and a WebUI on 9091.

### Complete specification

| Property | Value |
|---|---|
| Image | `milvusdb/milvus:${MILVUS_VERSION}` (`v2.6.20`) |
| Container name | `milvus-standalone` |
| Command | `["milvus", "run", "standalone"]` |
| `security_opt` | `["seccomp:unconfined"]` — required; Milvus uses syscalls the default profile blocks, and without this you get opaque crashes |
| Network aliases | `standalone`, `milvus-standalone` |
| Published ports | `${MILVUS_HOST_PORT}:19530`, `${MILVUS_METRICS_HOST_PORT}:9091` |
| Volumes | `${DOCKER_VOLUME_DIRECTORY}/milvus:/var/lib/milvus`, and optionally `./infra/milvus/user.yaml:/milvus/configs/user.yaml:ro` |
| Restart policy | `unless-stopped` |
| Memory limit | `6g`, reservation `2g` |
| `depends_on` | `etcd: {condition: service_healthy}`, `minio: {condition: service_healthy}`, `minio-init: {condition: service_completed_successfully}` |

### Environment variables

| Variable | Value | Notes |
|---|---|---|
| `ETCD_ENDPOINTS` | `etcd:2379` | |
| `MINIO_ADDRESS` | `minio:9000` | |
| `MINIO_REGION` | `${MINIO_REGION}` (`us-east-1`) | omitting this causes S3 signature warnings in logs |
| `MINIO_ACCESS_KEY_ID` | `${MINIO_ROOT_USER}` | |
| `MINIO_SECRET_ACCESS_KEY` | `${MINIO_ROOT_PASSWORD}` | |
| `MINIO_BUCKET_NAME` | `${MINIO_BUCKET}` | |
| `LOG_LEVEL` | `info` | set to `debug` only when diagnosing |

### `infra/milvus/user.yaml` (mounted config override)
Keep it minimal and documented — the file exists so you can demonstrate config control, not to fight the defaults.
```yaml
# Overrides merged over Milvus's built-in milvus.yaml.
# Milvus 2.6 defaults mq.type to woodpecker with minio storage; we make it explicit.
mq:
  type: woodpecker
woodpecker:
  storage:
    type: minio
log:
  level: info
common:
  security:
    authorizationEnabled: false   # LIMITATION: no auth. Documented in README.
```

### Healthcheck — note the `start_period`
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
  interval: 15s
  timeout: 10s
  retries: 5
  start_period: 120s
```
**`start_period: 120s` is mandatory.** Milvus 2.6 takes 45–110 seconds on a cold start (first-ever boot with empty etcd is the slow case). With a shorter start period, Compose marks it unhealthy, `deploy.sh` aborts, and you spend an hour debugging a system that was working.

### Standalone verification
```bash
# 1. Shallow liveness
curl -sf http://localhost:9091/healthz && echo " HEALTHZ OK"

# 2. gRPC port actually accepting
nc -zv localhost 19530          # expect "succeeded" / "open"

# 3. Deep check — this is the one that matters
python3 -c "
from pymilvus import MilvusClient
c = MilvusClient(uri='http://localhost:19530')
print('version:', c.get_server_version())
print('collections:', c.list_collections())
"
# expect: version: v2.6.20   collections: []

# 4. Metrics endpoint is parseable
curl -s http://localhost:9091/metrics | head -20
curl -s http://localhost:9091/metrics | grep -c '^milvus_'   # expect > 50

# 5. WebUI renders
open http://localhost:9091/webui/
```

### Expected startup timeline (watch with `docker logs -f milvus-standalone`)
| t | Log marker |
|---|---|
| 0–5 s | `Welcome to Milvus` banner, config load |
| 5–15 s | etcd connection established, session registration |
| 15–35 s | MinIO/object-storage client init, Woodpecker WAL init |
| 35–70 s | RootCoord/MixCoord ready, channel assignment |
| 70–110 s | `Proxy successfully started`, `/healthz` starts returning 200 |

If you are still not healthy at **180 seconds**, it is broken — go to failure modes.

### Known failure modes
| Symptom | Diagnosis command | Cause / fix |
|---|---|---|
| Container restarts in a loop, exit 137 | `docker inspect milvus-standalone --format '{{.State.OOMKilled}}'` → `true` | Docker memory allocation too low. Raise to 10 GB (§0.1). |
| Logs stop after "connecting to etcd" | `docker exec milvus-etcd etcdctl endpoint health` | etcd unhealthy or alias wrong |
| `Access Denied` / `NoSuchBucket` in logs | `docker exec milvus-minio mc ls local` | I-3 didn't run, or credentials mismatch between MinIO and Milvus env |
| Healthy but `list_collections` hangs | `docker stats milvus-standalone` | under-provisioned CPU; give Docker 4 vCPU |
| Immediate exit, no useful log | `docker logs milvus-standalone 2>&1 \| head -50` | `security_opt: seccomp:unconfined` missing |
| Works, then fails after a laptop sleep | `docker restart milvus-standalone` | clock skew broke etcd leases; known local-dev annoyance, document it |

---

## I-5 — `cp-postgres`

### Purpose
Persists all control-plane metadata: the five tables in Doc 02. Nothing about Milvus's own operation depends on it — which is the point of failure drill D.

### Complete specification

| Property | Value |
|---|---|
| Image | `postgres:${POSTGRES_VERSION}` (`16-alpine`) |
| Container name | `cp-postgres` |
| Published port | `${POSTGRES_HOST_PORT}:5432` |
| Volumes | `${DOCKER_VOLUME_DIRECTORY}/postgres:/var/lib/postgresql/data`, `./infra/postgres/initdb.sql:/docker-entrypoint-initdb.d/00-init.sql:ro` |
| Restart policy | `unless-stopped` |
| Memory limit | `512m`, reservation `128m` |
| Command | `postgres -c log_statement=none -c max_connections=100 -c shared_buffers=128MB` |

### Environment variables
| Variable | Value |
|---|---|
| `POSTGRES_USER` | `${POSTGRES_USER}` |
| `POSTGRES_PASSWORD` | `${POSTGRES_PASSWORD}` |
| `POSTGRES_DB` | `${POSTGRES_DB}` |
| `PGDATA` | `/var/lib/postgresql/data/pgdata` |

`PGDATA` pointing at a **subdirectory** matters: mounting a bind mount directly as the data directory fails on some hosts because of the `lost+found` / permissions check.

### Healthcheck
```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
  interval: 5s
  timeout: 5s
  retries: 10
  start_period: 10s
```
Note the `$$` — Compose interpolates single `$`, so escaping is required to pass the variable through to the shell inside the container.

### `infra/postgres/initdb.sql` — exact contents
```sql
-- Runs ONCE, only when the data directory is empty.
-- Schema objects are owned by Alembic; this file only installs extensions.
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
ALTER DATABASE controlplane SET timezone TO 'UTC';
```
**Do not put CREATE TABLE statements here.** Two sources of truth for schema is the single most common way these assignments become unmaintainable, and a reviewer will ask. Tables come from Alembic (Doc 02) and only from Alembic.

### Standalone verification
```bash
docker exec cp-postgres pg_isready -U controlplane -d controlplane
psql "postgresql://controlplane:controlplane@localhost:5432/controlplane" -c '\dx'
#   expect pgcrypto and pg_stat_statements listed
psql "postgresql://controlplane:controlplane@localhost:5432/controlplane" -c "SELECT gen_random_uuid();"
psql "postgresql://controlplane:controlplane@localhost:5432/controlplane" -c "SHOW timezone;"   # UTC
```

### Expected startup time
3–8 seconds cold, 2 seconds warm.

### Known failure modes
| Symptom | Cause | Fix |
|---|---|---|
| `initdb: directory not empty` | reused volume from a different PG major version | `rm -rf volumes/postgres` |
| `role "controlplane" does not exist` | volume pre-exists from a run with different `POSTGRES_USER`; initdb never re-runs | `make destroy` and recreate |
| Host port conflict | local Postgres running | change `POSTGRES_HOST_PORT` to `55432` |

---

## I-6 — `cp-migrate` (one-shot)

### Purpose
Applies Alembic migrations exactly once per `up`, before the API starts. Separating this from the API entrypoint means multiple API replicas can never race on `alembic upgrade`.

### Complete specification

| Property | Value |
|---|---|
| Image | built from `control_plane/Dockerfile` (same image as I-7) |
| Container name | `cp-migrate` |
| Working dir | `/app` |
| Command | `["alembic", "upgrade", "head"]` |
| Restart policy | `"no"` |
| `depends_on` | `cp-postgres: {condition: service_healthy}` |
| Env | inherits the full `.env` |
| Profile | `app` |

### Verification
```bash
docker inspect -f '{{.State.ExitCode}}' cp-migrate      # expect 0
docker logs cp-migrate | grep "Running upgrade"
psql "$PG_URL" -c "SELECT version_num FROM alembic_version;"
psql "$PG_URL" -c "\dt"     # expect 5 tables + alembic_version
```

### Failure mode
If it exits non-zero, `cp-api` will never start (`service_completed_successfully` is unmet) and Compose reports it clearly. That is the desired behaviour — a control plane against a half-migrated schema is worse than no control plane.

---

## I-7 — `cp-api`

### Purpose
The control plane itself. Talks to four things: Postgres (asyncpg), Milvus gRPC (pymilvus), Milvus metrics (HTTP), and the Docker daemon (unix socket).

### Complete specification

| Property | Value |
|---|---|
| Build context | `./control_plane`, Dockerfile `Dockerfile` |
| Container name | `cp-api` |
| Command | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log` |
| Published port | `${CP_API_HOST_PORT}:8000` |
| Volumes | `${CP_DOCKER_SOCKET}:/var/run/docker.sock:ro` |
| Restart policy | `unless-stopped` |
| Memory limit | `512m` |
| `depends_on` | `cp-postgres: {condition: service_healthy}`, `cp-migrate: {condition: service_completed_successfully}` |
| Profile | `app` |

`--workers 1` is deliberate: the APScheduler jobs live in-process, and multiple workers would mean N concurrent health-check writers. If you ever scale this, the scheduler moves out to its own container — note that in `ARCHITECTURE.md`.

**Milvus is deliberately NOT in `depends_on`.** The control plane must start and be useful when Milvus is down. If you add that dependency you have broken Requirement 5 before you've written a line of it.

### Dockerfile — exact stage-by-stage spec

```
STAGE 1 "builder":
  FROM python:3.12-slim
  ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
  RUN apt-get update && apt-get install -y --no-install-recommends build-essential
  WORKDIR /app
  COPY pyproject.toml ./
  RUN pip install --prefix=/install .

STAGE 2 "runtime":
  FROM python:3.12-slim
  RUN apt-get update && apt-get install -y --no-install-recommends curl \
      && rm -rf /var/lib/apt/lists/*
  RUN useradd --create-home --uid 10001 appuser
  COPY --from=builder /install /usr/local
  WORKDIR /app
  COPY --chown=appuser:appuser . .
  USER appuser
  ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
  EXPOSE 8000
  HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
      CMD curl -fsS http://localhost:8000/healthz || exit 1
  CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1"]
```

### The Docker socket problem — read before you debug for an hour
The container runs as UID 10001. `/var/run/docker.sock` on the host is owned by `root:docker` with mode `660`. So `appuser` inside the container **cannot read it** unless it belongs to a group with the socket's GID.

Three options, in order of preference for this assignment:

1. **Pass the host's docker GID at build time** (recommended):
   - In `.env`: `DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)` on Linux, `0` on macOS (Docker Desktop's socket is owned by root:root and proxied).
   - In Compose: `group_add: ["${DOCKER_GID}"]`.
   - `deploy.sh` computes and writes `DOCKER_GID` into `.env` during preflight.
2. Run the container as root (`user: root`) — simplest, worst. Only if option 1 fails.
3. Use a `docker-socket-proxy` sidecar exposing a read-only subset over TCP — the correct production answer. Mention it in `ARCHITECTURE.md`; don't build it.

**Verification that the socket is actually readable from inside:**
```bash
docker exec cp-api python -c "import docker; print([c.name for c in docker.from_env().containers.list()])"
# expect a list including milvus-standalone. A PermissionError here means group_add is wrong.
```

### Health verification
```bash
curl -s localhost:8000/healthz | jq          # {"status":"ok",...}  — must work even with Milvus down
curl -s localhost:8000/readyz  | jq          # {"status":"ready","postgres":"ok"}
curl -s localhost:8000/docs -o /dev/null -w '%{http_code}\n'   # 200
curl -s localhost:8000/api/v1/clusters | jq
```

---

## I-8 — `cp-dashboard`

### Purpose
Serves the built SPA and reverse-proxies `/api/*` to `cp-api`, so the browser sees a single origin and CORS never enters the picture.

### Complete specification

| Property | Value |
|---|---|
| Build context | `./dashboard` |
| Container name | `cp-dashboard` |
| Published port | `${DASHBOARD_HOST_PORT}:80` |
| Volume | `./infra/nginx/dashboard.conf:/etc/nginx/conf.d/default.conf:ro` |
| Restart policy | `unless-stopped` |
| Memory limit | `128m` |
| `depends_on` | `cp-api: {condition: service_started}` |
| Build args | `VITE_API_BASE=${VITE_API_BASE}`, `VITE_POLL_INTERVAL_MS=${VITE_POLL_INTERVAL_MS}` |
| Profile | `app` |

### Dockerfile spec
```
STAGE 1: FROM node:20-alpine
  WORKDIR /app
  COPY package.json package-lock.json ./
  RUN npm ci
  COPY . .
  ARG VITE_API_BASE
  ARG VITE_POLL_INTERVAL_MS
  RUN npm run build          # emits /app/dist

STAGE 2: FROM nginx:${NGINX_VERSION}
  COPY --from=builder /app/dist /usr/share/nginx/html
  EXPOSE 80
  HEALTHCHECK CMD wget -q --spider http://localhost/ || exit 1
```

### `infra/nginx/dashboard.conf` — exact contents
```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    # SPA routing: unknown paths fall back to index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # API reverse proxy — single origin, no CORS
    location /api/ {
        proxy_pass http://cp-api:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Request-ID $request_id;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;      # > CP_OVERVIEW_BUDGET_S (6s), < browser patience
        proxy_next_upstream off;     # never silently retry a mutating call
    }

    # Proxy the control plane's own probes for convenience
    location = /healthz { proxy_pass http://cp-api:8000/healthz; }
    location = /readyz  { proxy_pass http://cp-api:8000/readyz; }

    # Never cache the SPA shell; cache hashed assets forever
    location = /index.html { add_header Cache-Control "no-store"; }
    location /assets/      { add_header Cache-Control "public, max-age=31536000, immutable"; }

    access_log /var/log/nginx/access.log;
    error_log  /var/log/nginx/error.log warn;
}
```

`proxy_read_timeout 15s` must exceed `CP_OVERVIEW_BUDGET_S`. If nginx times out before FastAPI finishes its fan-out, the dashboard shows a network error during a partial outage — the exact moment you most need it to work.

---

## 1.1 BRING-UP PROCEDURE — one instance at a time

Do this the first time you build the stack. It takes ~15 minutes and guarantees that when something breaks later you know it was the last thing you added. After it succeeds once, `deploy.sh up` does the whole thing in one command.

Create the network first:
```bash
docker network create --driver bridge --subnet 172.28.0.0/16 milvus-cp_cp-net
```

### Phase 1 — etcd alone
```bash
docker run -d --name milvus-etcd --network milvus-cp_cp-net --network-alias etcd \
  -v "$PWD/volumes/etcd:/etcd" \
  -e ETCD_AUTO_COMPACTION_MODE=revision \
  -e ETCD_AUTO_COMPACTION_RETENTION=1000 \
  -e ETCD_QUOTA_BACKEND_BYTES=4294967296 \
  -e ETCD_SNAPSHOT_COUNT=50000 \
  quay.io/coreos/etcd:v3.5.25 \
  etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
```
**Gate:** `docker exec milvus-etcd etcdctl endpoint health` reports healthy. Do not continue until it does.

### Phase 2 — MinIO alone
```bash
docker run -d --name milvus-minio --network milvus-cp_cp-net --network-alias minio \
  -p 9000:9000 -p 9001:9001 \
  -v "$PWD/volumes/minio:/minio_data" \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  minio/minio:RELEASE.2024-05-28T17-19-04Z \
  server /minio_data --console-address ":9001"
```
**Gate:** `curl -sf http://localhost:9000/minio/health/live` returns 200, and the console loads at `:9001`.

### Phase 3 — bucket
```bash
docker run --rm --network milvus-cp_cp-net \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  --entrypoint sh minio/mc:RELEASE.2024-06-12T14-34-03Z -c \
  'mc alias set local http://minio:9000 minioadmin minioadmin && mc mb --ignore-existing local/milvus-bucket && mc ls local'
```
**Gate:** output lists `milvus-bucket/`.

### Phase 4 — Milvus
```bash
docker run -d --name milvus-standalone --network milvus-cp_cp-net --network-alias standalone \
  -p 19530:19530 -p 9091:9091 \
  -v "$PWD/volumes/milvus:/var/lib/milvus" \
  --security-opt seccomp=unconfined \
  -e ETCD_ENDPOINTS=etcd:2379 \
  -e MINIO_ADDRESS=minio:9000 \
  -e MINIO_REGION=us-east-1 \
  -e MINIO_ACCESS_KEY_ID=minioadmin \
  -e MINIO_SECRET_ACCESS_KEY=minioadmin \
  -e MINIO_BUCKET_NAME=milvus-bucket \
  milvusdb/milvus:v2.6.20 milvus run standalone
```
Watch it: `docker logs -f milvus-standalone`.
**Gate (allow up to 120 s):** `curl -sf localhost:9091/healthz` returns 200 **and** the pymilvus deep check in I-4's verification prints a version and `[]`.

**This is the single most important gate in the whole build.** If Milvus is not reachable via pymilvus here, nothing downstream can work, and every subsequent symptom will be a misleading downstream artifact.

### Phase 5 — Postgres
```bash
docker run -d --name cp-postgres --network milvus-cp_cp-net \
  -p 5432:5432 \
  -v "$PWD/volumes/postgres:/var/lib/postgresql/data" \
  -v "$PWD/infra/postgres/initdb.sql:/docker-entrypoint-initdb.d/00-init.sql:ro" \
  -e POSTGRES_USER=controlplane -e POSTGRES_PASSWORD=controlplane \
  -e POSTGRES_DB=controlplane -e PGDATA=/var/lib/postgresql/data/pgdata \
  postgres:16-alpine
```
**Gate:** `psql "postgresql://controlplane:controlplane@localhost:5432/controlplane" -c '\dx'` lists `pgcrypto`.

### Phase 6 — prove the demo works before writing any application code
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install "pymilvus>=2.6,<2.7" numpy
python ops/milvus_demo.py --uri http://localhost:19530 --rows 5000 --keep
```
**Gate:** ranked search results print. You now have a provably working Milvus and Requirements 1 and 3 are demonstrable.

### Phase 7 — tear down the manual containers, switch to Compose
```bash
docker rm -f milvus-standalone milvus-minio milvus-etcd cp-postgres
docker network rm milvus-cp_cp-net
# volumes/ is intentionally kept — Compose will pick the data back up
docker compose --env-file .env -f infra/docker-compose.yml --profile infra up -d
docker compose -f infra/docker-compose.yml ps
```
**Gate:** the same verifications from Phases 1–6 all pass again, now under Compose. If Phase 6's collection still exists after the switch, your volume mounts are correct.

---

## 1.2 The assembled `docker-compose.yml` — construction rules

Assemble `infra/docker-compose.yml` from the eight specifications above. Rules for the file itself:

1. **No `version:` key.** It is obsolete in Compose v2 and emits a warning.
2. **Every image tag from an env var.** Zero literal tags, zero `:latest`.
3. **Anchors for repeated blocks.** Define `x-common-labels: &common-labels` and `x-restart: &restart-policy` at the top and merge them into services with `<<:`.
4. **Named `logging` config on every service:**
   ```yaml
   logging:
     driver: json-file
     options: { max-size: "10m", max-file: "3" }
   ```
   Without this, Milvus at debug level will fill your disk during a long session, and the log-tailing endpoint will get slow.
5. **Resource limits under `deploy.resources`** with both `limits` and `reservations`. Compose v2 honours these without swarm mode.
6. **Profiles:** `infra` on I-1…I-5, `app` on I-6…I-8. `--profile infra` gives you Milvus alone; `--profile infra --profile app` gives everything.
7. **`env_file: ../.env`** on services that need the full set, plus explicit `environment:` for anything renamed.
8. **Volume paths relative to the compose file's directory** — since the file lives in `infra/`, `${DOCKER_VOLUME_DIRECTORY}` must be `../volumes` OR you always invoke with `--project-directory .` from the repo root. **Pick the second**, and put it in the Makefile so it is never ambiguous:
   ```
   COMPOSE := docker compose --env-file .env --project-directory . -f infra/docker-compose.yml
   ```

### Verification of the assembled file
```bash
docker compose --env-file .env --project-directory . -f infra/docker-compose.yml config
```
This renders the fully-interpolated file. **Read the output.** Confirm: no `${...}` remains unresolved, every image has an explicit tag, every volume path resolves to an absolute path under your repo, and the network appears once.

```bash
docker compose ... config --services      # expect 8 lines
docker compose ... config --profiles      # expect: app, infra
```

---

## 1.3 Container-level acceptance checklist

- [ ] `docker compose ... --profile infra up -d` → 4 running + 1 exited(0) within 180 s
- [ ] `docker compose ... ps --format 'table {{.Name}}\t{{.Status}}'` shows `(healthy)` on etcd, minio, standalone, postgres
- [ ] `docker compose ... --profile infra --profile app up -d` → 6 running + 2 exited(0)
- [ ] `docker exec cp-api python -c "import docker; docker.from_env().ping()"` → no exception
- [ ] `curl -s localhost:8080` returns the SPA HTML
- [ ] `curl -s localhost:8080/api/v1/clusters` returns JSON (proves the nginx proxy path)
- [ ] `docker compose ... down` then `up -d` → collections created earlier still exist (volumes persist)
- [ ] `docker compose ... down -v` + `rm -rf volumes/` + `up -d` → clean stack, no collections
- [ ] `docker network inspect milvus-cp_cp-net -f '{{len .Containers}}'` → 6

---

## Next

Proceed to **02_DATABASE.md**.
