# 05 — RUNBOOK: EXECUTION, DRILLS, TROUBLESHOOTING, SUBMISSION

---

## 5.1 The build sequence, in order, with time estimates

| # | Task | Doc | Est. | Gate before moving on |
|---|---|---|---|---|
| 1 | Host setup + preflight | 00 §0.3–0.6 | 30 min | all 12 preflight checks pass |
| 2 | Repo skeleton + `.env` | 00 §0.8–0.9 | 15 min | 30 directories exist |
| 3 | Manual bring-up, phases 1–5 | 01 §1.1 | 45 min | pymilvus reports `v2.6.20` and `[]` |
| 4 | `milvus_demo.py` | 04 Part B | 2 h | ranked results print |
| 5 | Assemble `docker-compose.yml` | 01 §1.2 | 1 h | `config` renders clean; `--profile infra up` healthy |
| 6 | `deploy.sh` + libs + Makefile | 04 Part A | 3 h | `make up-infra` and `make destroy` both work twice |
| 7 | Models + migration | 02 | 2 h | all 10 DB acceptance tests pass |
| 8 | Config, logging, schemas | 03 M-01/02/04 | 1.5 h | settings validate, JSON logs emit |
| 9 | Adapters | 03 M-05→M-09 | 4 h | adapter integration checks pass |
| 10 | Repos + services | 03 M-10→M-14 | 4 h | truth-table tests green |
| 11 | Jobs | 03 M-15 | 1.5 h | transition-only event test passes |
| 12 | Routers + main | 03 M-17→M-19 | 3 h | `/docs` complete; `make smoke` green |
| 13 | App tier in Compose | 01 I-6/7/8 | 1 h | `make up` → 8 services |
| 14 | Dashboard | 04 Part D | 4 h | all panels populate |
| 15 | Chaos + RELIABILITY.md | 04 Part C | 2.5 h | 6 drills executed with real output |
| 16 | Tests | — | 2 h | `make test` green |
| 17 | README + docs | — | 2 h | a stranger follows it successfully |

**Total ≈ 40 hours.** If time-boxed harder, cut in this order: WP-17 K8s path, `populate_history.py`, `HealthSparkline`, MiniLM embedder, `net-cut`/`disk-fill` drills. Never cut: the degradation envelope, `deep_probe`, transition-only events, or the reliability drills — those are the graded differentiators.

---

## 5.2 Cold start from zero — the exact command sequence

```bash
git clone <repo> && cd milvus-control-plane
cp .env.example .env
make up                      # ~4 min first run (image pulls), ~90 s after
```

Expected terminal output shape:
```
[1/9] Preflight checks
  [ OK ] docker 27.0.3
  [ OK ] compose v2.29.1
  [ OK ] memory 10.4 GB
  ...
[2/9] Creating volume directories
[3/9] Starting etcd, minio, postgres
  waiting for milvus-etcd ....... healthy (7s)
  waiting for milvus-minio ..... healthy (5s)
  waiting for cp-postgres ...... healthy (6s)
[4/9] Initializing MinIO bucket
  milvus-minio-init exited 0 (3s)
[5/9] Starting Milvus 2.6
  waiting for milvus-standalone ................................. healthy (78s)
  verifying gRPC: server version v2.6.20
[6/9] Applying database migrations
  cp-migrate exited 0 — 0001_initial
[7/9] Starting control-plane API
  waiting for http://localhost:8000/healthz ... 200 (4s)
[8/9] Registering cluster
  registered local-milvus-standalone -> 7f3c1a2e-...
[9/9] Starting dashboard
  waiting for http://localhost:8080 .. 200 (2s)
```

Then:
```bash
make status        # everything green
make demo          # collection + 5000 vectors + HNSW + search
make smoke         # every endpoint exercised
make open          # dashboard
```

## 5.3 The 90-second verification you run before any demo

```bash
CID=$(cat .cluster_id)

curl -sf localhost:8000/healthz | jq -e '.status=="ok"'
curl -sf localhost:8000/readyz  | jq -e '.status=="ready"'
curl -sf localhost:8000/api/v1/clusters | jq -e '.total>=1'
curl -sf "localhost:8000/api/v1/clusters/$CID/health" | jq -e '.live_status=="ok"'
curl -sf "localhost:8000/api/v1/clusters/$CID/collections" | jq '.live | length'
curl -sf "localhost:8000/api/v1/clusters/$CID/metrics" \
  | jq '[.live[] | select(.available)] | length'          # expect >= 6
curl -sf "localhost:8000/api/v1/clusters/$CID/components" \
  | jq -e '[.live[] | select(.state=="running")] | length == 4'
curl -sf "localhost:8000/api/v1/clusters/$CID/logs?component=milvus-standalone&lines=20" \
  | jq '.live | length'
curl -sf "localhost:8000/api/v1/clusters/$CID/overview" | jq '.overall_status'
curl -sf localhost:8000/api/v1/events | jq '.total'
curl -sf localhost:8080/api/v1/clusters | jq -e '.total>=1'   # proves the nginx proxy
```

Any `jq -e` failure exits non-zero, so this whole block can be `set -e`'d as `make smoke`.

---

## 5.4 Reliability drills — exact procedure for each

Run each drill in a terminal with `scripts/chaos.sh observe 120` running in a second terminal. Capture both. Paste the real output into `docs/RELIABILITY.md`.

### Drill A — Milvus stopped

```bash
scripts/chaos.sh milvus-stop
```

| Field | What to record |
|---|---|
| Injection | `docker stop milvus-standalone` |
| Expected | `/health` → **200** with `live_status: "unavailable"`, `code: MILVUS_UNREACHABLE`; metadata endpoints unaffected; `components` shows `exited`; exactly one `health_transition` event; breaker OPEN after 3 checks |
| Detection | dashboard pill red within ~2 health intervals; measured `detected_after` from `chaos.sh` |
| Diagnosis commands | `docker compose ps` → `standalone` exited; `docker logs --tail 50 milvus-standalone`; `nc -zv localhost 19530` → refused; `curl -s localhost:8000/api/v1/system/info \| jq .breakers` → milvus breaker `open` |
| Recovery | `scripts/chaos.sh milvus-start`; measure time to green |

**The specific thing to prove:** after recovery, the API returns healthy **without being restarted**. That is `_invalidate_client` (Doc 03, M-07) working. If it requires an API restart, that line is missing.

### Drill B — Milvus paused (hung, not dead)

```bash
scripts/chaos.sh milvus-pause
```
Expected: `MILVUS_TIMEOUT`, not `MILVUS_UNREACHABLE`, and `/overview` still returns inside `CP_OVERVIEW_BUDGET_S`. This is the proof that timeouts are enforced end-to-end — a hung dependency cannot hang the control plane. Record the actual `/overview` response time (`curl -w '%{time_total}'`).

### Drill C — MinIO stopped (the interesting one)

```bash
scripts/chaos.sh minio-stop
curl -s localhost:9091/healthz -o /dev/null -w '%{http_code}\n'    # likely still 200!
python ops/milvus_demo.py --collection chaos_test --rows 1000      # will fail
docker logs --tail 100 milvus-standalone | grep -iE 'minio|s3|object|bucket'
```

| Field | What to record |
|---|---|
| Expected | Milvus's own `/healthz` keeps returning 200 for a period; `ping()` still succeeds; `deep_probe` and any insert/flush fail; overall status → `degraded` with `MILVUS_DEEP_PROBE_FAILED`; Milvus logs show object-store connection errors |
| Detection | **The shallow probe does not catch this.** `deep_probe` does. Record the exact time gap between MinIO stopping and the status changing. |
| Diagnosis | `curl -sf localhost:9000/minio/health/live` → connection refused; `docker compose ps minio` → exited; Milvus logs grep above |
| Writeup point | This is the drill that justifies having two probe depths. Say so explicitly. |

Also note whether **reads on already-loaded collections keep working** while writes fail — record what you actually observe. This is the most interesting empirical finding in the whole assignment and it will differ depending on cache state.

### Drill D — Postgres stopped

```bash
scripts/chaos.sh postgres-stop
curl -s localhost:8000/healthz | jq            # 200 — control plane alive
curl -s localhost:8000/readyz  | jq            # 503, POSTGRES_UNAVAILABLE
curl -s localhost:8000/api/v1/clusters | jq    # 503 with Retry-After
curl -s "localhost:8000/api/v1/clusters/$CID/health" | jq   # still serves live Milvus data
docker logs --tail 20 cp-api | grep health_job_skipped
scripts/chaos.sh postgres-start
sleep 15
curl -s localhost:8000/readyz | jq             # 200 — NO API restart
```
The last two lines are the `pool_pre_ping` demonstration. Record the recovery time.

### Drill E — etcd stopped

```bash
scripts/chaos.sh etcd-stop
```
Document what you actually observe: whether Milvus stays up, whether `list_collections` works from cache, how long before it degrades, and what appears in the Milvus logs. Do not predict — this one behaves differently depending on how long Milvus has been running and what is cached.

### Drill F — network partition

```bash
scripts/chaos.sh net-cut milvus-standalone
```
Distinct signature from Drill A: the container is `running` and `healthy` from Docker's perspective, but unreachable over the network. The component table shows green while the health probe shows unavailable — record this, because it is a genuinely instructive disagreement between two observability sources, and explaining it well is worth more than any amount of UI polish.

### `docs/RELIABILITY.md` structure

One section per drill, each with these six subsections and **real pasted output**:
```
### Drill A — Milvus unavailable
**Injection**       <command + timestamp>
**Expected**        <what the design says should happen>
**Observed**        <what actually happened, with API response bodies>
**Detection**       <signal + measured MTTD>
**Diagnosis**       <the commands run, in order, with trimmed output>
**Recovery**        <command + measured MTTR + whether manual intervention was needed>
```

Close the document with:
1. **What the design got right** — the envelope, deep probe, transition events, `_invalidate_client`.
2. **What surprised you** — be honest; there will be something.
3. **Production hardening** — Prometheus + Alertmanager on both Milvus and the control plane; OpenTelemetry traces across the `/overview` fan-out; a **synthetic canary** that inserts and searches every 60 s (the only check that would have caught Drill C immediately); PgBouncer; MinIO distributed mode; a docker-socket-proxy instead of the raw socket; per-cluster credentials.

---

## 5.5 Troubleshooting index

| Symptom | First command | Likely cause | Fix |
|---|---|---|---|
| `make up` hangs at step 5 for >240 s | `docker logs --tail 100 milvus-standalone` | OOM, or etcd/MinIO not truly ready | raise Docker memory to 10 GB; check drill-C style object-store errors in the log |
| `milvus-standalone` exit code 137 | `docker inspect milvus-standalone --format '{{.State.OOMKilled}}'` | memory limit | raise Docker Desktop allocation; lower `--rows` |
| `cp-api` can't see containers | `docker exec cp-api python -c "import docker;docker.from_env().ping()"` | socket group mismatch | set `DOCKER_GID` in `.env` (Doc 01, I-7) |
| `cp-migrate` exits 1, `type already exists` | `psql -c "\dT"` | a previous downgrade left enums behind | the migration's `downgrade()` is missing `DROP TYPE` — Doc 02 §2.4.3 |
| `cp-api` 503 on everything | `docker compose ps cp-postgres` | Postgres down or credentials changed | if credentials changed, the volume has the old role — `make destroy` |
| Dashboard blank, console CORS error | browser network tab | you opened `localhost:5173` (Vite dev) instead of `:8080` | use `:8080`, which proxies `/api` |
| Metrics panel all greyed | `curl -s localhost:9091/metrics \| grep '^# TYPE' \| head -40` | allowlist names don't match this version | rebuild the allowlist from `discover()` (Doc 03, M-09) |
| Collections panel empty after a restart | `docker exec milvus-etcd etcdctl get --prefix "" --keys-only \| head` | you deleted `volumes/etcd` but kept `volumes/minio` (or vice versa) | they are coupled — `make destroy`, never hand-delete one |
| Health flaps healthy↔degraded every poll | `curl -s localhost:8000/api/v1/events?limit=20 \| jq` | a component with a slow healthcheck is momentarily not `running` | raise the healthcheck `start_period`, or require 2 consecutive observations before a transition |
| Everything worked, then broke after laptop sleep | `docker restart milvus-standalone` | clock skew invalidated etcd leases | known local-dev issue; document it |
| `search` returns garbage scores | check normalization | random vectors not L2-normalized with COSINE | Doc 04, Stage 4 |
| Postgres `role does not exist` | `ls volumes/postgres/pgdata` | volume predates a credentials change; initdb never re-runs | `make destroy` |

---

## 5.6 README structure — the twelve required sections

1. **What this is** — 3 sentences + the endpoint table.
2. **Architecture** — Mermaid diagram: browser → nginx → FastAPI → {Postgres, Milvus gRPC, Milvus :9091, Docker socket}; Milvus → {etcd, MinIO}. Annotate which arrows are allowed to fail without a 5xx.
3. **Prerequisites** — Doc 00 §0.6, verbatim.
4. **Quickstart** — 5 commands, executed verbatim on a clean machine before submission.
5. **Command reference** — every `deploy.sh` subcommand, every Make target.
6. **API reference** — the endpoint table, link to `/docs`, and three worked `curl` examples including **one showing the degraded envelope**.
7. **Milvus operations script** — full CLI and one real captured run.
8. **Technology choices** — the decision table with the *why* column, plus what was rejected: K8s/Operator (heavier setup, same demo surface), Go/Node backend (no first-class Milvus SDK), Grafana (would satisfy the dashboard requirement but hides the API-composition work being evaluated), Celery (unnecessary broker for three periodic jobs).
9. **Assumptions and known limitations** — specific and unflinching:
   - single-cluster in practice, though the schema and API are multi-cluster
   - no authn/authz on the control plane; Milvus auth disabled
   - default credentials throughout; not suitable for any shared network
   - Docker socket mounted read-only, still root-equivalent
   - standalone only; no HA, no replicas; `--workers 1` because the scheduler is in-process
   - metrics allowlist derived empirically from v2.6.20 and may drift
   - logs read from the Docker daemon, not a log aggregator; no full-text search
   - `distributed` mode not implemented
   - retention is time-based with no downsampling
   - no TLS anywhere
   - last-writer-wins between the scheduler and the forced `/health-check` endpoint
10. **Reliability** — summary of the six drills, link to `docs/RELIABILITY.md`.
11. **Teardown** — `make down` vs `make destroy`, and exactly what each deletes.
12. **AI usage** — link to `docs/AI_USAGE.md`.

## 5.7 `docs/AI_USAGE.md`

The assignment asks for this, and it is graded on honesty and specificity, not on minimizing AI use. Four sections:

**What AI drafted:** compose file scaffolding, repository CRUD boilerplate, Pydantic schemas, React panel components, the argparse skeleton, docstrings.

**What I designed and directed:** the degradation envelope and the rule that dependency failures return 200; the ordered `aggregate_status` rule set; the `ping` vs `deep_probe` split; transition-only events; the version-tolerant metrics allowlist; the one-shot migration container; `_invalidate_client` on transport failure; the drill matrix.

**What AI got wrong and I corrected** — this is the section a reviewer actually reads. Be concrete:
- suggested Pulsar/RocksMQ containers for a 2.6 standalone stack, which 2.6 no longer needs (Woodpecker is embedded with MinIO as WAL backend)
- proposed a health model based solely on `:9091/healthz`, which cannot detect the MinIO outage in Drill C
- returned HTTP 500 on dependency failure in the first router draft
- hallucinated Milvus metric names that do not exist in v2.6.20 — caught by scraping `/metrics` and diffing
- omitted `DROP TYPE` from the migration downgrade, breaking the round-trip
- reused a dead gRPC client after Milvus restarted, requiring an API restart to recover
- forgot L2 normalization on random vectors with COSINE, producing meaningless scores

**How I verified:** ran every command; the 10 DB acceptance tests; the truth-table unit tests; all six drills with captured output; a clean-machine run of the README quickstart.

---

## 5.8 Final submission checklist

**Functional**
- [ ] `git clone && cp .env.example .env && make up` works on a machine that has never run this
- [ ] `make destroy && make up` works — repeatability is explicitly graded
- [ ] `make up` twice in a row is a clean no-op
- [ ] `make demo` prints ranked search results
- [ ] `make smoke` green
- [ ] Dashboard shows all panels populated
- [ ] `/docs` renders every route with a healthy **and** a degraded example

**Reliability**
- [ ] All six drills executed, with real output in `docs/RELIABILITY.md`
- [ ] Milvus recovery needs no API restart
- [ ] Postgres recovery needs no API restart
- [ ] `/health` returns 200 with Milvus stopped (automated test, not just manual)
- [ ] Exactly one event per status transition, verified over 10 polls

**Data**
- [ ] `alembic downgrade base && alembic upgrade head` round-trips
- [ ] `alembic revision --autogenerate` produces an empty diff
- [ ] All 10 DB acceptance tests pass

**Hygiene**
- [ ] No secrets committed; `.env` gitignored; `.env.example` has no real credentials
- [ ] `volumes/` gitignored and absent from the archive
- [ ] Every image tag pinned; no `:latest`
- [ ] `make lint` clean
- [ ] `README.md` quickstart executed verbatim on a clean machine

**Explainability** — you can answer these without notes:
- [ ] Why does a dependency failure return 200 instead of 500?
- [ ] Why does `deep_probe` exist alongside `/healthz`?
- [ ] Why are events written only on transition?
- [ ] Why do migrations run in a separate one-shot container?
- [ ] Why is the gRPC client discarded on transport failure?
- [ ] Why is the metrics list an allowlist rather than everything?
- [ ] Why `--workers 1`?
- [ ] What happens if you delete `volumes/etcd` but keep `volumes/minio`?

---

## 5.9 Demo script — 10 minutes, in order

1. `make destroy --yes` then `make up` — narrate the nine steps while it runs. *(4 min)*
2. `make demo` — collection, 5 000 vectors, HNSW, filtered search, results table. *(1 min)*
3. Open the dashboard — walk the six panels. *(1 min)*
4. Open `/docs` — show the degraded example on `/health`. *(30 s)*
5. `scripts/chaos.sh observe 120` in one terminal, `make chaos-milvus` in another — watch the dashboard degrade live, show the API still returning 200, show the single event row appear. *(2 min)*
6. `make chaos-recover` — everything returns green with no restarts. *(1 min)*
7. Show `docs/RELIABILITY.md` Drill C and explain why `/healthz` alone would have missed it. *(30 s)*

Step 7 is the closing argument. Lead with the design decision, not the code.
