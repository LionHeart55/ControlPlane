#!/usr/bin/env bash
# Ensure exactly one cluster is registered with the control plane.
#
# The API bootstraps a cluster from .env on startup when the table is empty, so
# in the normal case this only has to confirm that happened. It registers one
# explicitly if not -- for example when someone deleted the row, or when the API
# started before PostgreSQL was reachable and skipped its own bootstrap.
#
# Idempotent: registering a duplicate name returns 409, which is treated as
# success rather than an error.
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[ -f "${REPO_ROOT}/.env" ] && set -a && . "${REPO_ROOT}/.env" && set +a

API="http://localhost:${CP_API_PORT:-8000}"
NAME="${CP_CLUSTER_NAME:-local-standalone}"
# Inside the compose network, not localhost: this URI is what cp-api will use.
ENDPOINT="${MILVUS_URI:-http://milvus-standalone:19530}"
METRICS="${MILVUS_METRICS_URI:-http://milvus-standalone:9091}"
OBJECT_STORE="${MINIO_ENDPOINT:-milvus-minio:9000}"
PROJECT="${COMPOSE_PROJECT_NAME:-milvus-cp}"
WAIT_S="${CP_SEED_WAIT_S:-30}"

log() { printf '%s\n' "$*" >&2; }

# The API may still be starting when deploy.sh gets here.
deadline=$(( $(date +%s) + WAIT_S ))
until curl -sf -o /dev/null "${API}/healthz" 2>/dev/null; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "error: control-plane API did not answer at ${API}/healthz within ${WAIT_S}s"
        exit 1
    fi
    sleep 1
done

# Readiness, not just liveness: registering needs PostgreSQL.
if ! curl -sf -o /dev/null "${API}/readyz" 2>/dev/null; then
    log "error: API is up but not ready — PostgreSQL is unreachable"
    log "  check:  docker logs cp-postgres"
    exit 1
fi

existing=$(curl -sf "${API}/api/v1/clusters?limit=1" 2>/dev/null || echo '')
if printf '%s' "$existing" | grep -q '"id"'; then
    name=$(printf '%s' "$existing" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p' | head -1)
    log "cluster already registered: ${name:-unknown}"

    # A cluster registered while the API ran on the HOST carries host URIs
    # (localhost:19530). Those do not resolve from inside the cp-api container,
    # so every probe fails with MILVUS_UNREACHABLE and the dashboard shows a
    # permanently dead cluster for a reason that looks nothing like a config
    # mistake. The endpoint is a stored per-cluster value by design — it has to
    # be, for multi-cluster — so this warns rather than silently rewriting it.
    registered=$(printf '%s' "$existing" | sed -n 's/.*"endpoint_uri":"\([^"]*\)".*/\1/p' | head -1)
    if [ -n "$registered" ] && [ "$registered" != "$ENDPOINT" ]; then
        log ""
        log "WARNING: the registered endpoint does not match this deployment."
        log "  registered: ${registered}"
        log "  expected:   ${ENDPOINT}"
        log "  cp-api runs inside the compose network and cannot reach a host"
        log "  address. Repoint it with:"
        log "    curl -X PATCH ${API}/api/v1/clusters/<id> \\"
        log "         -H 'Content-Type: application/json' \\"
        log "         -d '{\"endpoint_uri\":\"${ENDPOINT}\",\"metrics_uri\":\"${METRICS}\"}'"
        log ""
    fi
    exit 0
fi

log "no cluster registered; registering ${NAME}"
body=$(cat <<JSON
{
  "name": "${NAME}",
  "endpoint_uri": "${ENDPOINT}",
  "deployment_type": "docker_standalone",
  "metrics_uri": "${METRICS}",
  "object_store_endpoint": "${OBJECT_STORE}",
  "compose_project": "${PROJECT}",
  "labels": {"source": "seed_cluster.sh"}
}
JSON
)

status=$(curl -s -o /tmp/cp-seed-$$.json -w '%{http_code}' \
    -X POST "${API}/api/v1/clusters" \
    -H 'Content-Type: application/json' \
    -d "$body" 2>/dev/null || echo '000')

case "$status" in
    201) log "registered ${NAME}" ;;
    # A concurrent bootstrap won the race. Same end state, so not an error.
    409) log "cluster ${NAME} already exists (409) — nothing to do" ;;
    *)
        log "error: registration failed with HTTP ${status}"
        [ -f "/tmp/cp-seed-$$.json" ] && head -c 400 "/tmp/cp-seed-$$.json" >&2 && echo >&2
        rm -f "/tmp/cp-seed-$$.json"
        exit 1
        ;;
esac
rm -f "/tmp/cp-seed-$$.json"
