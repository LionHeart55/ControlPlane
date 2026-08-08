# AI usage

**Tool:** Claude (Anthropic), used through Claude Code as an agentic pair —
reading files, running commands against the live stack, and iterating on
failures rather than only emitting text.

This document is specific rather than diplomatic, because the useful question
is not "was AI used" but "where did its judgement need checking, and how would
you know". The section worth reading is
[What it got wrong](#what-it-got-wrong-and-i-caught).

## Summary

| | |
|---|---|
| **Drafted by AI, reviewed by me** | Compose file, boilerplate CRUD and repositories, Pydantic schemas, React panels, bash scaffolding, test bodies once the cases were chosen |
| **Designed by me, AI implemented** | The degradation envelope, the ordered health rules, deep-probe rationale, the metrics allowlist, the transition contract, the two-phase health job, sub-budget strategy for `/overview` |
| **AI got wrong, I caught** | Pulsar for 2.6, four hallucinated metric names, a `/healthz`-only health model, 500 on dependency failure, and eight more below |
| **Verification** | Every work package checked against the live stack before the next began; 234 unit + 15 integration + 17 render tests + 72 smoke assertions; six chaos drills with captured output |

## What AI drafted

Mechanical or well-trodden work, where the cost of review is far below the cost
of typing:

- **`infra/docker-compose.yml`** — first draft from the upstream Milvus 2.6
  compose file. I then diffed it against upstream myself and made five
  corrections (below).
- **Repositories and CRUD.** Five thin repository classes are the same code
  five times; AI wrote them from the schema and I checked the query shapes —
  in particular that `latest_per_component` uses `DISTINCT ON` matching the
  index rather than a correlated subquery.
- **Pydantic schemas and OpenAPI examples.** Tedious, and mechanically checkable
  — an audit script asserts every one of the 17 operations has a
  `response_model`, a description, a tag and a reachable example.
- **React panels.** Given the panel list and the envelope contract, the tables
  and tiles are conventional. I specified the three render states and the
  staleness treatment; AI wrote the JSX and CSS.
- **Bash scaffolding.** Colour helpers, arg parsing, the usage block.
- **Test bodies.** I chose what to assert and why; AI wrote the arrange/act.
- **Prose drafts** of these documents, from my notes and the captured output.

## What I designed

Decisions where the interesting content is the reasoning, and where AI's first
instinct was usually the conventional-but-wrong answer:

- **The degradation envelope.** That a dependency outage returns 200 with
  `live: null` + a stable code, that PostgreSQL is the only 503, that
  `observed_at` is when data was *true* rather than when it was served, and
  that cached data is flagged stale even inside the fresh TTL. AI's default was
  a 500.
- **The ordered health rules**, including the two judgement calls that are not
  obvious from the list: why a dead Docker socket is `degraded` rather than
  `unknown`, and why `BREAKER_OPEN` must map to `unavailable` — the latter
  because mapping it to `unknown` silently breaks the transition contract
  mid-outage.
- **`deep_probe`, and its honest limitation.** I specified it, and I wrote into
  the docstring — before any drill ran — that it would *not* catch a MinIO
  outage because those calls are answered from etcd metadata. Scenario C later
  confirmed it exactly. That prediction is what justified building the direct
  store probes.
- **The transition contract.** Events on state change only, previous state read
  from the database under a row lock rather than from memory.
- **The metrics allowlist**, and the decision to author it from a live
  `discover()` run *with traffic* rather than from documentation.
- **The two-phase health job** — probe outside any transaction, persist inside a
  short locked one — so a 5 s gRPC timeout never pins a row lock.
- **Per-branch sub-budgets for `/overview`**, and the choice of `asyncio.wait`
  over `gather` + `wait_for` to make "partial results always returned" actually
  true.
- **What to drill, and that the write-up must be from captured output.**

## What it got wrong, and I caught

In rough order of how much damage each would have done.

### 1. Pulsar for Milvus 2.6

The first compose draft included a Pulsar container and `MQ_TYPE: pulsar` —
correct for Milvus 2.3, wrong for 2.6, which uses Woodpecker, an embedded WAL
backed by the object store. **Caught by reading the upstream v2.6.20 compose
file** rather than trusting the draft: it has exactly etcd, MinIO and
standalone. Three unnecessary containers avoided, and `MQ_TYPE: woodpecker`
added, which the draft had omitted entirely.

Later confirmed from the other direction: with MinIO stopped, Milvus logs
`["write message to woodpecker failed"] [error="failed to create lock object
files/wp/15/19/write.lock"]` — the WAL is unmistakably in object storage.

### 2. Four hallucinated or stale metric names

The starting allowlist contained four names that do not exist on 2.6.20. Each
would have produced a permanently blank tile that looked like a data problem:

| Suggested | Reality |
|---|---|
| `milvus_storage_op_count` | renamed → `internal_storage_op_count` |
| `milvus_storage_request_latency` | renamed → `internal_storage_request_latency` |
| `milvus_querynode_num_entities` | does not exist; `milvus_querynode_entity_num` does |
| `process_cpu_seconds_total` | parsed as `process_cpu_seconds` |

**Caught by running `discover()` against a live instance — twice.** Once idle
(254 families) and once after real workload (397). The second run is what made
it correct: on an idle server there are *zero* `milvus_proxy_*` metrics, so an
allowlist authored against an idle instance would have silently dropped half of
itself.

The fourth is the subtlest and is not AI's fault so much as a real trap:
`prometheus_client` **strips `_total`** from counter family names, so a name
copied correctly out of the raw exposition text still fails to match after
parsing. That is why `MetricSpec.aliases` exists.

### 3. A shallow `/healthz`-only health model

The proposed health check was `GET :9091/healthz` → 200 means healthy. That is
the single most misleading check available: during the drills it returned **200
with MinIO completely stopped**, and **200 for the twenty seconds before Milvus
exited** after etcd was killed. Replaced with a deep gRPC probe, component
reconciliation, direct store probes and six ordered rules.

### 4. 500 on dependency failure

Early route drafts let a Milvus timeout propagate into a 500. That inverts the
purpose of the tool — the moment you most need the control plane is when
something it monitors is broken. Replaced with the envelope, centralised in one
function so it cannot be forgotten per route, and pinned by a destructive
integration test that stops Milvus for real and asserts all five read endpoints
still return 200.

### 5. `_InactiveRpcError.code` treated as an attribute

The first error classifier read `exc.code` on gRPC errors. On
`_InactiveRpcError` that is a bound **method**, so it silently yields a method
object that compares equal to nothing and every timeout fell through to a
generic `RPC_ERROR`. Caught by introspecting the actual exception objects
before writing the classifier. Two neighbouring traps came out of the same
session: `DescribeCollectionException` carries `code=0` — the *success* value —
so it must be caught by type, and `get_load_state` returns
`{'state': NotExist}` without raising at all.

### 6. Timeouts that were documented but not implemented

The adapter's docstring claimed the inner gRPC deadline was "the defence that
actually works" while **no `timeout=` was passed to any of the eleven calls** —
only the outer `asyncio.wait_for` existed, which cannot cancel a thread blocked
in C. Caught by ruff's `ASYNC109`, not by reading. The docstring was describing
an intention.

### 7. Compose reads no `.env` with a bare `-f`

`docker compose -f infra/docker-compose.yml` resolves relative bind mounts and
locates `.env` against the *compose file's* directory, so every `${VAR}` fell
back to its default, every image tag rendered blank, and volumes would have
been written to `infra/volumes/`. Caught by running `docker compose config` and
reading the rendered output rather than assuming. Fixed with
`--env-file .env --project-directory .` everywhere, documented in the compose
header.

### 8. Two tests that passed while asserting nothing

- `caplog` sees nothing from structlog until `configure_logging()` has run,
  which it has not in a unit test — so two "logs a WARNING" assertions would
  have passed against code that logged nothing at all. Switched to
  `structlog.testing.capture_logs`.
- Asserting `job.max_instances == 1` before the scheduler starts raises
  `AttributeError`, because APScheduler merges job defaults only when a job is
  really added. Checking the `JOB_DEFAULTS` constant instead would have been
  asserting on a constant, not on behaviour. The test now starts the scheduler.

### 9. My own test expectations were wrong about `histogram_quantile`

Two quantile tests failed and **the code was right**. I had expected a p50
falling in the first bucket to return that bucket's upper bound; Prometheus
interpolates from **0**, so with 80 of 100 observations ≤ 1.0 the median is
0.625, not 1.0. Returning 1.0 would systematically overstate every latency
concentrated in the fastest bucket — which, for a healthy service, is most of
them. Cross-checked against numpy on lognormal, uniform and bimodal
distributions before changing the expectations.

### 10. A `pipefail` + `grep -q` race in the deploy script

`dc config --services | grep -qx 'cp-migrate'` failed *nondeterministically*:
`grep -q` exits on first match, closing the pipe, so compose takes a SIGPIPE
and `set -o pipefail` fails the whole pipeline even though the match succeeded.
Migrations silently fell back to a host interpreter — which works on my machine
and fails on a clean one. It reproduced under `bash -x` and not otherwise,
which is the worst possible signature. The same pattern was in the port
preflight, where it would produce phantom port conflicts. Caught by noticing
the *wrong log line* during a routine run, not by any test.

### 11. `socket.gaierror` escaping the database error handlers

When a container stops, Docker removes its DNS record, so from inside the
compose network a database failure is name resolution, not a refused
connection — and a `gaierror` raised inside asyncpg's connect is not a DBAPI
error, so SQLAlchemy never wraps it. Every metadata route returned 500 instead
of 503. It had *passed* an earlier drill because the API was running on the
host then, where the name always resolves. **Only drilling the real topology
found it.**

### 12. nginx caching a stale upstream IP

`proxy_pass http://cp-api:8000` resolves once at config load. I had considered
this and judged it safe because `docker restart` preserves the IP; that was
incomplete, since `compose up -d` *recreates* and changes it. The dashboard
502s silently until nginx itself restarts. Found because a chaos drill was
already broken *before* its injection.

### 13. Docker socket permissions, and a platform ceiling

Two environment realities neither of us predicted:

- The API runs as uid 10001 and the socket is mode 0660 root-owned, so the
  container needs the socket's **group** — and the right GID is
  platform-dependent (0 under Docker Desktop, the host `docker` group on
  Linux). Reading the host socket's gid on macOS gives the wrong answer
  entirely, since it is a symlink into `$HOME`.
- **PyTorch ships no macOS x86_64 wheels from v2.4 onward.** The documented
  `pip install sentence-transformers` therefore resolves to a *broken* install
  on an Intel Mac. Found the working pinned combination and recorded it in
  `ops/requirements.txt`.

## How I verified everything

The through-line is that nothing was accepted because it looked right.

**Introspect before writing.** The upstream compose file, pymilvus's exception
classes, the Docker SDK's `logs()` signature, MinIO's response to an anonymous
`HEAD`, etcd's `/health` body — all checked against the running system *before*
the code that depends on them was written. Most of the thirteen items above
were caught this way rather than at test time.

**Live acceptance per work package.** Each package was verified against the real
stack before the next began, which is how the containerised-only bugs (11, 12,
13) surfaced at all.

**Measure, do not assume.** The MinIO probe signs with SigV4 because I measured
that an anonymous `HEAD` returns **403 for both an existing and a missing
bucket** — an unsigned probe cannot tell them apart. `/overview` concurrency is
claimed on the basis that total wall time (969 ms) ≈ the slowest branch
(961 ms), not the sum (2 542 ms).

**Cross-check against an independent implementation.** Bucket-quantile
estimates were checked against numpy on three distributions; metric values were
checked against known state (`querynode_entity_num` = 5 000 = the rows just
inserted; `milvus_num_node` = 4 = standalone's four roles).

**Tests that would fail if the claim were false.** 234 unit tests (no
infrastructure), 15 integration, 17 dashboard render tests, 72 smoke assertions,
and a destructive test that stops Milvus for real and asserts the envelope
holds. The metrics fixture is a *captured* scrape, not a hand-written one,
because a hand-written fixture is written to match whatever the parser already
does.

**Drills, with the write-up from captured output only.**
[RELIABILITY.md](RELIABILITY.md) contains real timestamps, real error codes and
real log lines. Where observation contradicted expectation — scenario C's deep
probe passing, scenario E's Milvus dying rather than degrading — **the
observation is what is recorded**, with the expectation noted as wrong.

**The quickstart executed verbatim from `make destroy`.** 29 s to a healthy
seven-container stack, 9.0 s demo, dashboard 200 with all six panels, 72/72
smoke.

## An honest assessment

AI made this faster by a large factor, and it was most valuable on exactly the
work that is tedious and mechanically checkable: schemas, repositories, table
markup, the fifth near-identical adapter method.

It was least reliable on **domain specifics that changed recently** (Milvus 2.6
dropping Pulsar; metric renames) and on **judgement about failure**, where its
defaults were consistently the conventional ones: return 500, check
`/healthz`, trust that a documented timeout is an implemented timeout. Those
defaults are not stupid — they are what most codebases do — which is precisely
why they need a human deciding rather than accepting.

The pattern that caught the most bugs was not review; it was **running things
against reality early**. Nine of the thirteen items above were found by
executing something — a compose render, a live scrape, a drill — rather than by
reading code. The two most dangerous bugs (the `pipefail` race and the
`gaierror` handler gap) were both invisible to every test and both worked
perfectly on the development machine.
