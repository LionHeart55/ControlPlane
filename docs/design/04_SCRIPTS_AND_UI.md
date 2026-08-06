# 04 — SCRIPTS AND DASHBOARD SPECIFICATION

> `deploy.sh` (Requirement 1), `milvus_demo.py` (Requirement 3), `chaos.sh` (Requirement 5), and the dashboard (Requirement 4). Function by function.

---

# PART A — `infra/deploy.sh`

## A.1 Structure

Single entrypoint plus three sourced libraries. Header on every file:
```bash
#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
```
Plus, in `deploy.sh` only:
```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"                      # every relative path is now unambiguous
trap 'echo "ERROR at ${BASH_SOURCE[0]}:${LINENO}: ${BASH_COMMAND}" >&2' ERR
```

Compose invocation, defined once and used everywhere:
```bash
COMPOSE=(docker compose --env-file .env --project-directory "${REPO_ROOT}" -f infra/docker-compose.yml)
```
Use `"${COMPOSE[@]}" ps` — an array, not a string, so quoting survives.

## A.2 `infra/lib/colors.sh`

```bash
supports_color()   # 1 if stdout is a TTY and NO_COLOR is unset
log_info()  msg    # blue  [INFO]
log_ok()    msg    # green [ OK ]
log_warn()  msg    # amber [WARN]
log_error() msg    # red   [FAIL]  -> stderr
log_step()  n total msg   # "[3/7] Starting Milvus…"
hr()               # horizontal rule
table_row() col1 col2 col3   # printf-aligned, %-24s %-12s %s
```
All output goes through these. No bare `echo` in the codebase — that is what lets `NO_COLOR=1` and CI logs stay readable.

## A.3 `infra/lib/preflight.sh`

One function per check; each returns 0/1 and prints its own diagnosis.

```bash
check_docker_installed()          # docker in PATH
check_docker_running()            # docker info succeeds
check_docker_version()            # server >= 24.0
check_compose_v2()                # docker compose version >= 2.20
check_memory()                    # docker info MemTotal >= 8e9; WARN at 8-10GB, FAIL below 8
check_cpus()                      # NCPU >= 4, WARN below
check_disk()                      # >= 25GB free at REPO_ROOT
check_ports()                     # each of the 7 ports; names the occupying process on failure
check_socket()                    # /var/run/docker.sock exists and is readable
check_env_file()                  # .env exists; create from .env.example if not, then WARN
check_required_env()              # every key in .env.example present in .env (set difference)
detect_docker_gid()               # writes DOCKER_GID into .env  (see Doc 01, I-7)
detect_platform()                 # sets MILVUS_PLATFORM=linux/amd64 on Apple Silicon if needed
run_preflight()                   # runs all; prints a summary table; returns 1 if any FAIL
```

`check_ports` must name the offender, not just report a conflict:
```
[FAIL] port 5432 in use by: postgres (pid 1234)
       Fix: stop it, or set POSTGRES_HOST_PORT=55432 in .env
```
A preflight that says "port in use" without saying what is using it wastes the user's time, which defeats the purpose of having one.

`check_required_env` catches the specific failure where someone pulls a newer `.env.example` and their existing `.env` is missing a key — the resulting `KeyError` deep inside pydantic is otherwise very confusing.

## A.4 `infra/lib/wait_for.sh`

```bash
wait_for_tcp     host port timeout_s
wait_for_http    url  timeout_s [expected_status=200]
wait_for_healthy container timeout_s          # docker inspect .State.Health.Status
wait_for_exit_ok container timeout_s          # one-shot containers; asserts ExitCode==0
wait_for_milvus  uri  timeout_s               # healthz AND a real pymilvus call
dump_failure_context container                # last 50 log lines + inspect State + resource usage
```

Rules for all `wait_for_*`:
- Poll every 1 s; print a dot; newline at the end.
- On timeout: call `dump_failure_context`, then `return 1`. **Never fail silently.** The single most valuable thing this script does is put the reason for the failure on screen at the moment it happens.
- Accept the timeout as an argument, never hardcode. Callers pass: etcd 60, minio 60, minio-init 60, milvus **240**, postgres 60, migrate 120, api 60, dashboard 30.

`wait_for_milvus` does two things because `/healthz` returning 200 does not guarantee gRPC is serving:
```bash
wait_for_http "${uri%:*}:9091/healthz" "$timeout" && \
docker run --rm --network "${NETWORK}" python:3.12-slim sh -c \
  "pip -q install 'pymilvus>=2.6,<2.7' && python -c \"
from pymilvus import MilvusClient; print(MilvusClient(uri='${uri}').get_server_version())\""
```
(Or use the host venv if present — check for `.venv/bin/python` first and prefer it, since the container pull is slow.)

## A.5 `deploy.sh` subcommands — exact behaviour

### `preflight`
`run_preflight`; exit 1 on any FAIL. Everything else calls this first unless `--skip-preflight`.

### `up [--profile infra|all] [--mode standalone|distributed] [--skip-preflight] [--no-seed]`

Nine steps, each announced with `log_step n 9`:

| Step | Action | Wait | On failure |
|---|---|---|---|
| 1 | `run_preflight` | — | exit 1 |
| 2 | `mkdir -p volumes/{etcd,minio,milvus,postgres}` | — | exit 1 |
| 3 | `"${COMPOSE[@]}" --profile infra up -d etcd minio postgres` | `wait_for_healthy` ×3 | dump + exit 1 |
| 4 | `"${COMPOSE[@]}" up -d minio-init` | `wait_for_exit_ok` 60 | dump + exit 1 |
| 5 | `"${COMPOSE[@]}" up -d standalone` | `wait_for_milvus` **240** | dump + exit 1 |
| 6 | if profile=all: `"${COMPOSE[@]}" --profile app up -d cp-migrate` | `wait_for_exit_ok` 120 | dump + exit 1 |
| 7 | if profile=all: `up -d cp-api` | `wait_for_http localhost:8000/healthz` 60 | dump + exit 1 |
| 8 | if profile=all and not `--no-seed`: `scripts/seed_cluster.sh` | — | WARN, continue |
| 9 | if profile=all: `up -d cp-dashboard` | `wait_for_http localhost:8080` 30 | WARN, continue |

Steps 3→4→5 are ordered deliberately rather than relying on `depends_on` alone, because it makes the failure attributable: if step 5 times out you know for certain etcd, MinIO and the bucket were already good.

Then print the summary block:
```
════════════════════════════════════════════════════
  Milvus Control Plane — READY
════════════════════════════════════════════════════
  Dashboard        http://localhost:8080
  API docs         http://localhost:8000/docs
  Milvus WebUI     http://localhost:9091/webui/
  MinIO console    http://localhost:9001   (minioadmin/minioadmin)
  Milvus gRPC      localhost:19530
  Postgres         localhost:5432          (controlplane/controlplane)

  Cluster ID       <uuid from .cluster_id>
  Elapsed          3m 42s

  Next:  make demo        # create a collection and search it
         make smoke       # exercise every endpoint
         make chaos-milvus
════════════════════════════════════════════════════
```

**Idempotency requirement:** running `up` twice must exit 0 both times and change nothing on the second run. Test this explicitly.

### `status`
Prints four sections: container table (name, state, health, restarts, uptime); endpoint probes (`/healthz`, gRPC TCP, MinIO live, `pg_isready`, API `/readyz`, dashboard) each OK/FAIL; database summary (`SELECT count(*)` per table); and the cluster row. Exit 0 if everything is up, 1 otherwise, so `make status` is usable in CI.

### `logs [service] [-f] [-n N]`
Wrapper over `"${COMPOSE[@]}" logs`. With no service, all. Validate the service name against `"${COMPOSE[@]}" config --services` and list valid names on a miss.

### `restart <service>`
`"${COMPOSE[@]}" restart <svc>` then the matching `wait_for_*`. For `standalone`, wait 240 s.

### `down`
`"${COMPOSE[@]}" --profile infra --profile app down --remove-orphans`. Containers and network removed; **volumes kept**. Print exactly what was and was not deleted — ambiguity here causes accidental data loss.

### `destroy [--yes]`
1. Unless `--yes`, prompt: `This deletes ALL data in ./volumes (Milvus collections and control-plane metadata). Type 'destroy' to confirm:` — require the literal word, not `y`.
2. `"${COMPOSE[@]}" --profile infra --profile app down -v --remove-orphans`
3. `rm -rf volumes/` then recreate the empty subdirectories.
4. `rm -f .cluster_id`
5. Report freed bytes (`du -sh` before).

### `reset`
`destroy --yes && up "$@"`.

### `--help`
The full subcommand table plus three worked examples. `deploy.sh` with no arguments prints help and exits 2.

## A.6 The `Makefile`

```make
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE := docker compose --env-file .env --project-directory . -f infra/docker-compose.yml
CLUSTER_ID = $(shell cat .cluster_id 2>/dev/null)

help:            ## Show this help
up:              ## Start the full stack (infra + app)
up-infra:        ## Start infrastructure only
down:            ## Stop containers, keep data
destroy:         ## Stop containers AND delete all data
reset:           ## destroy + up
status:          ## Show component and endpoint status
logs:            ## Tail all logs        (make logs SVC=standalone)
ps:              ## Container table
migrate:         ## Run alembic upgrade head
seed:            ## Register the local cluster
demo:            ## Run the Milvus operations script
demo-minilm:     ## Same, with real MiniLM embeddings
history:         ## Backfill 24h of demo history
smoke:           ## Exercise every API endpoint
test:            ## pytest unit tests
test-integration:## pytest integration tests (stack must be up)
chaos-milvus:    ## Stop Milvus and observe
chaos-minio:     ## Stop MinIO and observe
chaos-postgres:  ## Stop Postgres and observe
chaos-pause:     ## Pause Milvus (timeout vs refused)
chaos-recover:   ## Restore everything
fmt:             ## ruff format + eslint --fix
lint:            ## ruff check + mypy + tsc --noEmit
open:            ## Open the dashboard in a browser
```

`help` implemented with the standard awk one-liner over `##` comments, so the target list can never drift from reality.

Every target that needs a running stack starts with a guard:
```make
guard-stack:
	@$(COMPOSE) ps --status running --quiet standalone | grep -q . || \
	 { echo "Stack not running. Run 'make up' first."; exit 1; }
```

---

# PART B — `ops/milvus_demo.py` (Requirement 3)

## B.1 CLI

```
--uri                default http://localhost:19530
--collection         default demo_docs
--dim                default 384
--rows               default 5000
--batch              default 1000
--index              HNSW | IVF_FLAT | AUTOINDEX      default HNSW
--metric             COSINE | L2 | IP                  default COSINE
--embedder           random | minilm                   default random
--topk               default 5
--filter             optional Milvus boolean expression
--drop-existing      flag, default True
--keep               flag; skip cleanup so the dashboard has something to show
--json-out PATH      machine-readable summary
--seed               default 42
-v/--verbose         debug logging
```

## B.2 Functions — exact signatures

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace
def connect(uri: str, timeout_s: float = 10.0) -> MilvusClient
def build_schema(client: MilvusClient, dim: int) -> CollectionSchema
def create_collection(client, name: str, schema, drop_existing: bool) -> None
def generate_vectors(n: int, dim: int, seed: int) -> np.ndarray
def generate_minilm(corpus: list[str]) -> tuple[np.ndarray, list[str]]
def build_corpus(n: int, seed: int) -> list[tuple[str, str]]        # (text, category)
def insert_data(client, name, vectors, texts, categories, batch: int) -> int
def build_index(client, name, index_type, metric, dim) -> dict
def load_collection(client, name, timeout_s: float = 120.0) -> None
def run_search(client, name, query_vector, topk, index_type,
               metric, filter_expr: str | None) -> tuple[list[dict], float]
def display_results(results, latency_ms: float) -> None
def collection_stats(client, name) -> dict
def cleanup(client, name, keep: bool) -> None
def stage(n: int, total: int, title: str) -> ContextManager   # prints and times each stage
def main(argv=None) -> int
```

## B.3 Stage-by-stage behaviour

**Stage 1 — Connect.** `MilvusClient(uri=...)`, then `get_server_version()`. On failure print the URI, the exception, and the hint `Is Milvus running? Try: docker compose ps` and `return 3`.

**Stage 2 — Schema.**
```
create_schema(auto_id=True, enable_dynamic_field=True)
  id         INT64        is_primary=True
  vector     FLOAT_VECTOR dim=<dim>
  text       VARCHAR      max_length=512
  category   VARCHAR      max_length=64
  created_at INT64
```
`auto_id=True` keeps the insert payload simple; `enable_dynamic_field=True` demonstrates 2.6 schema flexibility and costs nothing.

**Stage 3 — Create collection.** `has_collection` → if present and `--drop-existing`, `drop_collection` and log it. Create **without** an index so index building is a visible separate stage — an index created implicitly is an index the reviewer never sees you build.

**Stage 4 — Embeddings.**
- `random`: `np.random.default_rng(seed).normal(size=(n, dim)).astype(np.float32)`, then **L2-normalize each row**. Without normalization, COSINE distances are meaningless and the demo shows nonsense. One line, easily forgotten.
- `minilm`: `SentenceTransformer("all-MiniLM-L6-v2")`, force `dim=384`, encode `build_corpus(n)` with `normalize_embeddings=True`. Print a warning that first run downloads ~90 MB.
- `build_corpus`: cycles ~40 template sentences across 5 categories (`tech`, `science`, `finance`, `health`, `sports`) with numeric variation, so filtered search returns something meaningful and MiniLM neighbours are visibly sensible.

**Stage 5 — Insert.** Batches of `--batch` as `list[dict]`; progress line per batch (`inserted 3000/5000 (60%) 4210 rows/s`); `client.flush(name)` at the end. Report total rows/s.

**Stage 6 — Index.**
```python
ip = MilvusClient.prepare_index_params()
ip.add_index(field_name="vector", index_name="vector_idx",
             index_type=args.index, metric_type=args.metric,
             params=INDEX_PARAMS[args.index])
ip.add_index(field_name="category", index_name="category_idx", index_type="INVERTED")
client.create_index(collection_name=name, index_params=ip, sync=True)
```
with
```python
INDEX_PARAMS = {
  "HNSW":     {"M": 16, "efConstruction": 200},
  "IVF_FLAT": {"nlist": 128},
  "AUTOINDEX": {},
}
```
Then `describe_index` and print `index_type`, `metric_type`, and the params **as Milvus reports them back** — not as you sent them. Milvus can substitute (notably AUTOINDEX), and showing the server's answer is the honest version. The scalar `INVERTED` index on `category` is what makes the filtered search in stage 8 actually use an index rather than a brute-force scan.

**Stage 7 — Load.** `load_collection`, then poll `get_load_state` every 0.5 s until `Loaded`, timeout 120 s. Print elapsed.

**Stage 8 — Search.**
```python
SEARCH_PARAMS = {"HNSW": {"ef": 64}, "IVF_FLAT": {"nprobe": 16}, "AUTOINDEX": {}}
```
Query vector = row 0 of the inserted set (so the top hit should be that exact row — a built-in sanity check; assert `results[0].distance` is ~1.0 for COSINE and warn loudly if not). `limit=topk`, `output_fields=["text","category"]`, `filter=args.filter` when given. Time it with `perf_counter`.

**Stage 9 — Display.**
```
 rank        id      score  category    text
 ────────────────────────────────────────────────────────────────────
    1  459283746     1.0000  tech        Distributed systems scale by …
    2  459283801     0.8734  tech        Vector databases index high-…
```
`text` truncated to 60 chars with an ellipsis. Print latency in ms below the table.

**Stage 10 — Stats.** `get_collection_stats` (row count) and `describe_collection` (field summary). Print both.

**Stage 11 — Cleanup.** Drop unless `--keep`. Print which.

## B.4 Exit codes and `--json-out`

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | bad arguments |
| 3 | could not connect to Milvus |
| 4 | a Milvus operation failed |
| 5 | search returned zero results (a real failure, not an empty success) |

`--json-out` writes:
```json
{"collection":"demo_docs","rows_inserted":5000,"dim":384,
 "index_type":"HNSW","metric_type":"COSINE",
 "insert_seconds":1.42,"index_seconds":3.81,"load_seconds":2.10,
 "search_latency_ms":4.7,"topk":5,"top_score":1.0,
 "server_version":"v2.6.20","results":[...],"status":"ok"}
```
`smoke_test.sh` consumes this rather than screen-scraping.

## B.5 Acceptance
- `python ops/milvus_demo.py --rows 5000` completes in under 60 s on a laptop and prints 5 ranked rows.
- Top hit score ≈ 1.0 for COSINE (the self-match check).
- `--filter 'category == "tech"'` returns only `tech` rows.
- `--embedder minilm` returns topically coherent neighbours.
- Running twice consecutively succeeds.
- With Milvus stopped: exits 3 with a readable message, no traceback dump.
- `--index IVF_FLAT` and `--index AUTOINDEX` both work.

---

# PART C — `scripts/chaos.sh`

## C.1 Subcommands

```
milvus-stop | milvus-start | milvus-pause | milvus-unpause | milvus-kill
minio-stop  | minio-start
postgres-stop | postgres-start
etcd-stop   | etcd-start
net-cut <service> | net-restore <service>
disk-fill   (optional: fill MinIO's volume to trigger write failure)
recover-all
status
observe <seconds>     # poll the API and print a status timeline
```

## C.2 Required behaviour

Every injection prints a banner:
```
═══ CHAOS: milvus-stop ═══  2026-08-06T14:32:10Z
  before: overall=healthy  milvus=reachable  components=4/4 running
```
performs the injection, waits `CP_HEALTH_INTERVAL_S * 2 + 5`, then:
```
  after:  overall=unavailable  milvus=MILVUS_UNREACHABLE  components=3/4 running
  detected_after: 17.3s
  events: 1 new (health_transition healthy->unavailable)
═══ END ═══
```
Before/after snapshots come from `curl -s "$API/api/v1/clusters/$CID/overview" | jq`. The **`detected_after`** figure is measured, not asserted — it goes straight into `docs/RELIABILITY.md` as the observed MTTD.

`observe N` polls `/overview` every 2 s for N seconds and prints a one-line timeline, which is what you screen-record for the submission:
```
14:32:10  healthy      milvus=ok    comp=4/4  lat=8ms
14:32:24  unavailable  milvus=DOWN  comp=3/4  lat=-
14:33:02  unavailable  milvus=DOWN  comp=3/4  lat=-
14:33:20  healthy      milvus=ok    comp=4/4  lat=11ms
```

`net-cut` uses `docker network disconnect milvus-cp_cp-net <container>` — this produces a **different failure signature** from `stop` (the container is up and healthy from Docker's view, but unreachable over the network), which is exactly the kind of distinction worth demonstrating.

`recover-all` restarts every stopped container, reconnects networks, unpauses, and waits for full health, then prints total recovery time.

---

# PART D — Dashboard

## D.1 Files

```
dashboard/src/
  main.tsx                   React root + QueryClientProvider
  App.tsx                    layout, cluster resolution
  api/client.ts              typed fetch wrapper
  api/types.ts               mirrors the backend Pydantic models
  hooks/useOverview.ts       polling query
  hooks/useLogs.ts           separate poll, component-scoped
  components/
    Header.tsx  StatusPill.tsx  ConnectionBanner.tsx
    MetadataCard.tsx  ComponentTable.tsx  CollectionsTable.tsx
    MetricsPanel.tsx  LogViewer.tsx  EventsStrip.tsx
    HealthSparkline.tsx  Panel.tsx  StaleBadge.tsx
  styles.css
```

`Panel.tsx` is the shared wrapper enforcing the four render states — build it first and use it for every panel:
```tsx
<Panel title="Collections" envelope={data.collections}>
  {(value) => <CollectionsTable rows={value} />}
</Panel>
```
It renders: a skeleton while loading; an error box with the `code` and `message` when `live_status === "unavailable"`; the children dimmed with a `<StaleBadge observedAt/>` when `stale`; an empty-state message when the value is an empty array; the children normally otherwise. Centralising this is what guarantees no panel can render a blank box — the failure mode that makes a dashboard actively misleading during an outage.

## D.2 Data flow

- **One** `useQuery(['overview', clusterId], …, {refetchInterval: 5000, retry: 1, keepPreviousData: true, staleTime: 2000})`.
- **One** `useQuery(['logs', clusterId, component], …, {refetchInterval: 5000, enabled: autoRefresh})`.
- Nothing else polls. Per-component queries would multiply request volume by six for no benefit.
- `keepPreviousData: true` means a transient API failure dims the UI instead of blanking it.
- On query error (the API itself unreachable, distinct from a dependency being down), `ConnectionBanner` shows "Control plane unreachable — retrying" and the last data stays visible, dimmed.

## D.3 Panel contents

| Panel | Fields |
|---|---|
| Header | cluster name, `StatusPill(overall_status)`, deployment type, Milvus version, "checked Ns ago", manual refresh button |
| ConnectionBanner | one row per entry in `degraded_dependencies`: dependency, code, message, `since` |
| MetadataCard | id, name, type, status, endpoint, metrics uri, object store, bucket, compose project, created, updated, last check |
| ComponentTable | name, state (colour-coded), health, restarts, uptime, image; `missing` rows in red italic |
| CollectionsTable | name, rows, dim, index type, metric, loaded, load %; empty state: "No collections — run `make demo`" |
| MetricsPanel | tile grid; `available: false` tiles greyed with "not exposed by v2.6.20"; histograms show p50/p95/p99 |
| HealthSparkline | last 60 checks from `health_history` as coloured bars (green/amber/red), hover shows timestamp + error code |
| LogViewer | component `<select>`, line-count select (50/200/500), auto-scroll toggle, monospace, ERROR/WARN tinting, "paused" chip when auto-scroll is off |
| EventsStrip | last 10 events: relative time, severity dot, type, message; expandable payload |

## D.4 Styling

One `styles.css`. CSS custom properties for the status palette so the colours are defined once:
```css
:root {
  --status-healthy:     #16a34a;
  --status-degraded:    #d97706;
  --status-unavailable: #dc2626;
  --status-unknown:     #6b7280;
  --stale-opacity: 0.55;
}
```
CSS Grid, two columns on desktop, one below 900 px. No component library, no Tailwind, no icon package. Visual polish is not graded; every dependency you add is surface you must be able to explain.

## D.5 Acceptance

- Healthy stack: all panels populate within 5 s of load, zero console errors.
- `make chaos-milvus`: within ~15 s the pill turns red, the banner names `MILVUS_UNREACHABLE`, the component table shows `exited`, collections/metrics dim with a stale badge, and **the metadata card stays fully live**. No white screen, no error boundary.
- `make chaos-postgres`: the banner reports the control plane is degraded; panels keep last-known data.
- `docker stop cp-api`: banner says "Control plane unreachable"; previous data stays visible, dimmed.
- `make chaos-recover`: everything returns to green without a page reload.
- Network tab shows exactly 2 requests per 5-second cycle.

---

## Claude Code prompts

**Prompt A:** "Write `infra/deploy.sh` and the three libraries in `infra/lib/` per [paste Part A]. Bash 3.2-compatible (macOS), `set -euo pipefail`, compose invoked as a bash array with `--project-directory` at the repo root. Implement every listed function. `up` executes the nine steps in the tabulated order with the stated per-step timeouts, and every wait timeout calls `dump_failure_context` before exiting non-zero. `destroy` requires the literal word 'destroy' unless `--yes`. Running `up` twice must be a no-op that exits 0. Then write the Makefile with the tabulated targets and the awk-based self-documenting `help`."

**Prompt B:** "Write `ops/milvus_demo.py` per [paste Part B]: the exact argparse interface, the listed function signatures, and the 11 timed stages. Use pymilvus 2.6's `MilvusClient` API (`create_schema`/`prepare_index_params`/`create_index`/`load_collection`/`search`), not the legacy `connections`+`Collection` API. Random vectors must be L2-normalized. Add an INVERTED scalar index on `category`. Print `describe_index` output as the server returns it. Assert the self-match top score and warn if it deviates. Implement exit codes 0/2/3/4/5 and `--json-out`."

**Prompt C:** "Write `scripts/chaos.sh` per [paste Part C], with before/after `/overview` snapshots, a measured `detected_after` figure, and an `observe N` timeline mode."

**Prompt D:** "Build the Vite + React 18 + TypeScript dashboard per [paste Part D]. Build `Panel.tsx` first — it enforces loading/error/stale/empty rendering for every panel via a render-prop taking the envelope. Exactly two polling queries. `keepPreviousData: true`. One plain CSS file with the status palette as custom properties. No component library."

---

## Next

Proceed to **05_RUNBOOK.md**.
