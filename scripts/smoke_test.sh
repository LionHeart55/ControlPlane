#!/usr/bin/env bash
# Walk every endpoint in the API contract against a live stack, asserting
# status codes and required fields.
#
# The interesting assertions are not "did it return 200" but the degradation
# contract: read endpoints must answer 200 with a well-formed envelope whatever
# state Milvus, MinIO and Docker are in. Run this during a chaos drill and it
# should still pass, except for the checks explicitly about healthy data.
#
# Usage: scripts/smoke_test.sh [BASE_URL]
set -Eeuo pipefail

BASE="${1:-${CP_BASE_URL:-http://localhost:8000}}"
API="${BASE}/api/v1"

PASS=0
FAIL=0
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; DIM=$'\033[2m'; NC=$'\033[0m'

command -v jq >/dev/null 2>&1 || { echo "${RED}jq is required${NC}" >&2; exit 2; }

ok()   { PASS=$((PASS+1)); printf '  %sok%s   %s\n' "$GREEN" "$NC" "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  %sFAIL%s %s\n' "$RED" "$NC" "$1"; [ -n "${2:-}" ] && printf '       %s%s%s\n' "$DIM" "$2" "$NC"; }
note() { printf '  %s--%s   %s\n' "$YELLOW" "$NC" "$1"; }
section() { printf '\n%s\n' "$1"; }

BODY_FILE=$(mktemp); trap 'rm -f "$BODY_FILE"' EXIT

# request METHOD PATH -> sets STATUS, body in $BODY_FILE
request() {
    local method="$1" url="$2"
    # curl exits non-zero on a connection failure AND prints nothing, so the
    # status is defaulted rather than left to concatenate into garbage.
    STATUS=$(curl -sS -X "$method" -o "$BODY_FILE" -w '%{http_code}' \
                  --max-time 20 "$url" 2>/dev/null) || STATUS="000"
}

# expect_status METHOD PATH EXPECTED LABEL
expect_status() {
    local method="$1" url="$2" want="$3" label="$4"
    request "$method" "$url"
    if [ "$STATUS" = "$want" ]; then
        ok "$label ($STATUS)"
    else
        bad "$label — expected $want, got $STATUS" "$(head -c 200 "$BODY_FILE")"
    fi
}

# expect_field LABEL JQ_FILTER  (run against the last response body)
expect_field() {
    local label="$1" filter="$2"
    if jq -e "$filter" "$BODY_FILE" >/dev/null 2>&1; then
        ok "$label"
    else
        bad "$label" "jq filter failed: $filter"
    fi
}

printf 'Smoke test against %s\n' "$BASE"

# --- system ---------------------------------------------------------------
section "system"
expect_status GET "${BASE}/healthz" 200 "GET /healthz"
expect_field  "  liveness body has status+service" '.status == "ok" and (.service|type=="string")'

request GET "${BASE}/readyz"
if [ "$STATUS" = "200" ]; then
    ok "GET /readyz (200, Postgres up)"
elif [ "$STATUS" = "503" ]; then
    ok "GET /readyz (503, Postgres down — correct)"
else
    bad "GET /readyz — expected 200 or 503, got $STATUS"
fi
expect_field "  readiness body has checks.postgres" '.checks.postgres.ok|type=="boolean"'

# --- clusters -------------------------------------------------------------
section "clusters"
request GET "${API}/clusters"
if [ "$STATUS" = "503" ]; then
    note "Postgres is down; skipping metadata routes (503 is the correct answer)"
    printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
    [ "$FAIL" -eq 0 ] || exit 1
    exit 0
fi
[ "$STATUS" = "200" ] && ok "GET /clusters (200)" || bad "GET /clusters — got $STATUS"
expect_field "  page has items/total/limit/offset" \
    '(.items|type=="array") and (.total|type=="number") and (.limit|type=="number") and (.offset|type=="number")'

CLUSTER_ID=$(jq -r '.items[0].id // empty' "$BODY_FILE")
if [ -z "$CLUSTER_ID" ]; then
    bad "no cluster registered — start the API so it bootstraps one, or POST /clusters"
    printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
    exit 1
fi
printf '  %susing cluster %s%s\n' "$DIM" "$CLUSTER_ID" "$NC"

expect_status GET "${API}/clusters?limit=1&offset=0" 200 "GET /clusters (paginated)"
expect_field  "  limit is honoured" '(.items|length) <= 1'
expect_status GET "${API}/clusters?status=running" 200 "GET /clusters?status="
expect_status GET "${API}/clusters/${CLUSTER_ID}" 200 "GET /clusters/{id}"
expect_field  "  cluster has endpoint_uri + last_health_status" \
    '(.endpoint_uri|type=="string") and (.last_health_status|type=="string")'

expect_status GET "${API}/clusters/00000000-0000-0000-0000-000000000000" 404 "GET /clusters/{unknown} is 404"
expect_field  "  404 uses the error envelope" '.error.code and .error.message'
expect_status GET "${API}/clusters/not-a-uuid" 422 "GET /clusters/{malformed} is 422"

expect_status POST "${API}/clusters" 422 "POST /clusters with no body is 422"

# --- the degradation envelope --------------------------------------------
# Every one of these MUST be 200 regardless of dependency state. That is the
# single most important rule in the contract.
section "degradation envelope (must be 200 even with dependencies down)"
for resource in health collections metrics components overview; do
    request GET "${API}/clusters/${CLUSTER_ID}/${resource}"
    if [ "$STATUS" = "200" ]; then
        ok "GET /${resource} (200)"
    else
        bad "GET /${resource} returned $STATUS — a dependency being down must never 5xx" \
            "$(head -c 300 "$BODY_FILE")"
    fi
    if [ "$resource" = "overview" ]; then
        expect_field "  overview has all six sections" \
            '.health and .collections and .metrics and .components and .logs and .events'
        expect_field "  every section carries its own status" \
            '[.health,.collections,.metrics,.components,.logs,.events]|all(.status|type=="string")'
        expect_field "  overview reports its budget and duration" \
            '(.budget_s|type=="number") and (.duration_ms|type=="number") and (.degraded|type=="boolean")'
        expect_field "  overview stayed within its 6s budget" '.duration_ms < 6000'
    else
        expect_field "  envelope has live_status/observed_at/stale" \
            '(.live_status|type=="string") and (.observed_at|type=="string") and (.stale|type=="boolean")'
        expect_field "  live_status is one of ok|stale|unavailable" \
            '.live_status as $s | ["ok","stale","unavailable"]|index($s) != null'
        expect_field "  unavailable implies a degraded_reason with a code" \
            'if .live_status == "unavailable" then (.degraded_reason.code|type=="string") else true end'
        expect_field "  stale implies stale:true" \
            'if .live_status == "stale" then .stale == true else true end'
        if [ "$resource" = "health" ]; then
            # /health reports an outage inside `live` rather than nulling it --
            # a probe that determines "Milvus is down" succeeded. But an
            # unhealthy cluster must always surface in degraded_reason too, so
            # a client has one field to check.
            expect_field "  unhealthy cluster always sets degraded_reason" \
                'if (.live != null and .live.status != "healthy") then (.degraded_reason.code|type=="string") else true end'
            expect_field "  live carries the rule that decided the status" \
                '.live == null or (.live.rule|type=="number")'
        else
            expect_field "  live is null exactly when unavailable" \
                'if .live_status == "unavailable" then .live == null else .live != null end'
        fi
    fi
done

# --- logs -----------------------------------------------------------------
section "logs"
expect_status GET "${API}/clusters/${CLUSTER_ID}/logs?component=milvus-standalone&lines=20" 200 \
    "GET /logs (allowlisted component)"
expect_field "  logs envelope names the component" '.live == null or (.live.component|type=="string")'
expect_status GET "${API}/clusters/${CLUSTER_ID}/logs?component=../../etc/passwd" 422 \
    "GET /logs rejects a path-traversal component name"
expect_status GET "${API}/clusters/${CLUSTER_ID}/logs?component=totally-unknown" 422 \
    "GET /logs rejects an unknown component"
expect_status GET "${API}/clusters/${CLUSTER_ID}/logs" 422 "GET /logs without ?component is 422"

# --- health history and forced check --------------------------------------
section "health"
expect_status GET "${API}/clusters/${CLUSTER_ID}/health-history?limit=5" 200 "GET /health-history"
expect_field  "  history page is well formed" '(.items|type=="array") and (.total|type=="number")'
expect_field  "  history rows carry status + checked_at" \
    '(.items|length) == 0 or ((.items[0].status|type=="string") and (.items[0].checked_at|type=="string"))'

expect_status POST "${API}/clusters/${CLUSTER_ID}/health-check" 200 "POST /health-check"
expect_field  "  forced check returns a live verdict with its rule" \
    '(.live.status|type=="string") and (.live.rule|type=="number")'

# --- events ---------------------------------------------------------------
section "events"
expect_status GET "${API}/events?limit=10" 200 "GET /events"
expect_field  "  events page is well formed" '(.items|type=="array") and (.total|type=="number")'
expect_field  "  event rows carry type/severity/created_at" \
    '(.items|length) == 0 or ((.items[0].event_type|type=="string") and (.items[0].severity|type=="string"))'
expect_status GET "${API}/events?event_type=health_transition&limit=5" 200 "GET /events?event_type="
expect_status GET "${API}/events?limit=99999" 422 "GET /events rejects limit above the cap"

# --- transition contract ---------------------------------------------------
# The health job samples every CP_HEALTH_INTERVAL_S but events are written only
# on change, so there must be far fewer events than samples.
request GET "${API}/clusters/${CLUSTER_ID}/health-history?limit=1"
CHECKS=$(jq -r '.total' "$BODY_FILE")
request GET "${API}/events?cluster_id=${CLUSTER_ID}&event_type=health_transition&limit=1"
TRANSITIONS=$(jq -r '.total' "$BODY_FILE")
if [ "$CHECKS" -gt 0 ] && [ "$TRANSITIONS" -le "$CHECKS" ]; then
    ok "transition contract: ${TRANSITIONS} events from ${CHECKS} checks"
else
    bad "transition contract: ${TRANSITIONS} events from ${CHECKS} checks"
fi

# --- demo output ------------------------------------------------------------
# `make demo` writes this. Checked only when present, so the API smoke test
# still runs standalone.
DEMO_JSON="${DEMO_JSON:-demo_results.json}"
if [ -f "$DEMO_JSON" ]; then
    section "demo output ($DEMO_JSON)"
    cp "$DEMO_JSON" "$BODY_FILE"
    expect_field "demo reported success" '.ok == true'
    expect_field "  all 11 stages recorded" '(.stages|length) == 11'
    expect_field "  every stage is timed" '[.stages[].seconds]|all(type=="number")'
    expect_field "  rows were actually inserted" '.insert.rows > 0'
    expect_field "  ranked results were returned" '(.search.results|length) > 0'
    expect_field "  results are ranked 1..n in order" \
        '[.search.results[].rank] == [range(1; (.search.results|length)+1)]'
    expect_field "  row_count matches the insert" '.stats.row_count >= .insert.rows'
    expect_field "  collection reached Loaded" '.load_state == "Loaded"'
else
    note "no $DEMO_JSON — run 'make demo' to include the ops-script checks"
fi

# --- OpenAPI ---------------------------------------------------------------
section "openapi"
expect_status GET "${BASE}/openapi.json" 200 "GET /openapi.json"
expect_field  "  every path has a description" \
    '[.paths[][] | select(type=="object") | .description] | all(. != null and . != "")'
expect_status GET "${BASE}/docs" 200 "GET /docs renders"

printf '\n%s%s passed%s, %s%s failed%s\n' "$GREEN" "$PASS" "$NC" \
    "$([ "$FAIL" -gt 0 ] && printf '%s' "$RED" || printf '%s' "$DIM")" "$FAIL" "$NC"
[ "$FAIL" -eq 0 ]
