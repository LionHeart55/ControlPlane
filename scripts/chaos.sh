#!/usr/bin/env bash
# Failure injection for the reliability drills (Requirement 5).
#
# Every subcommand prints a timestamped banner immediately before and after the
# injection. That is the whole point: the timestamps are what let you line up
# `docker logs`, the events table and the dashboard against a single moment, and
# they are what the MTTR numbers in docs/RELIABILITY.md are measured from.
#
# All timestamps are UTC, because that is what the API, the containers and the
# events table all use. Mixing in local time is how a five-second recovery ends
# up looking like an hour.
#
# Nothing here is destructive to data. Containers are stopped, paused or
# disconnected; volumes are never touched. `recover-all` restores everything and
# is safe to run at any time, including when nothing is broken.
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../infra/lib/colors.sh
[ -f "${REPO_ROOT}/infra/lib/colors.sh" ] && . "${REPO_ROOT}/infra/lib/colors.sh"
: "${C_RESET:=}" "${C_BOLD:=}" "${C_DIM:=}" "${C_RED:=}" "${C_GREEN:=}" "${C_YELLOW:=}" "${C_CYAN:=}"

# shellcheck disable=SC1091
if [ -f "${REPO_ROOT}/.env" ]; then set -a; . "${REPO_ROOT}/.env"; set +a; fi

PROJECT="${COMPOSE_PROJECT_NAME:-milvus-cp}"
NETWORK="${PROJECT}-net"
API="http://localhost:${CP_API_PORT:-8000}"

MILVUS=milvus-standalone
MINIO=milvus-minio
ETCD=milvus-etcd
POSTGRES=cp-postgres
CP_API=cp-api
CP_DASHBOARD=cp-dashboard

ALL_CONTAINERS="$MILVUS $MINIO $ETCD $POSTGRES $CP_API $CP_DASHBOARD"

now()  { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
epoch(){ date -u '+%s'; }

banner() {
    printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────────────${C_RESET}" >&2
    printf '%s %s%s%s\n' "$(now)" "${C_BOLD}" "$*" "${C_RESET}" >&2
    printf '%s\n' "${C_DIM}────────────────────────────────────────────────────────────────${C_RESET}" >&2
}
info() { printf '%s [info] %s\n' "$(now)" "$*" >&2; }
ok()   { printf '%s [ %sok%s ] %s\n' "$(now)" "${C_GREEN}" "${C_RESET}" "$*" >&2; }
warn() { printf '%s [%swarn%s] %s\n' "$(now)" "${C_YELLOW}" "${C_RESET}" "$*" >&2; }
die()  { printf '%s [%sfail%s] %s\n' "$(now)" "${C_RED}" "${C_RESET}" "$*" >&2; exit 1; }

exists() { docker inspect "$1" >/dev/null 2>&1; }

state_of() { docker inspect --format '{{.State.Status}}' "$1" 2>/dev/null || echo 'absent'; }

require() {
    exists "$1" || die "container '$1' does not exist — is the stack up? (./infra/deploy.sh up --profile all)"
}

# Snapshot of what the control plane thinks, printed either side of an
# injection so the writeup can quote a before and an after.
probe_api() {
    local health
    health=$(curl -s --max-time 10 "${API}/api/v1/clusters" 2>/dev/null || echo '')
    local cid
    cid=$(printf '%s' "$health" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p' | head -1)
    if [ -z "$cid" ]; then
        printf '%s [api ] unreachable or no cluster registered\n' "$(now)" >&2
        return 0
    fi
    local body
    body=$(curl -s --max-time 15 "${API}/api/v1/clusters/${cid}/health" 2>/dev/null || echo '')
    local status code
    status=$(printf '%s' "$body" | sed -n 's/.*"live":{"status":"\([^"]*\)".*/\1/p' | head -1)
    code=$(printf '%s' "$body" | sed -n 's/.*"degraded_reason":{"code":"\([^"]*\)".*/\1/p' | head -1)
    printf '%s [api ] status=%s code=%s\n' "$(now)" "${status:-?}" "${code:-none}" >&2
}

# --- injections ----------------------------------------------------------
inject() {
    local label="$1" container="$2" verb="$3"
    require "$container"
    banner "INJECT ${label} — docker ${verb} ${container}"
    probe_api
    local t0 t1
    t0=$(epoch)
    docker "$verb" "$container" >/dev/null
    t1=$(epoch)
    ok "${verb} completed in $((t1 - t0))s; ${container} is now $(state_of "$container")"
    banner "INJECTED ${label} at $(now)"
    info "watch:  ${SCRIPT_NAME} status"
    info "        curl -s ${API}/api/v1/events | jq '.items[:3]'"
}

cmd_milvus_stop()   { inject "A: Milvus stopped"   "$MILVUS"   stop; }
cmd_milvus_pause()  { inject "B: Milvus paused"    "$MILVUS"   pause; }
cmd_minio_stop()    { inject "C: MinIO stopped"    "$MINIO"    stop; }
cmd_postgres_stop() { inject "D: Postgres stopped" "$POSTGRES" stop; }
cmd_etcd_stop()     { inject "E: etcd stopped"     "$ETCD"     stop; }

cmd_network_cut() {
    local target="${1:-$CP_API}"
    require "$target"
    banner "INJECT F: network partition — disconnect ${target} from ${NETWORK}"
    probe_api
    docker network disconnect "$NETWORK" "$target" >/dev/null 2>&1 \
        || warn "${target} was already disconnected from ${NETWORK}"
    ok "${target} removed from ${NETWORK}"
    banner "INJECTED F at $(now)"
    info "reconnect with: ${SCRIPT_NAME} network-heal ${target}"
}

cmd_network_heal() {
    local target="${1:-$CP_API}"
    require "$target"
    banner "RECOVER F: reconnect ${target} to ${NETWORK}"
    docker network connect --alias "$target" "$NETWORK" "$target" >/dev/null 2>&1 \
        || warn "${target} was already connected"
    # A container that lost its network keeps stale DNS and connection state,
    # so it is restarted rather than merely reattached. Reconnecting alone
    # leaves it unable to resolve its peers.
    docker restart "$target" >/dev/null
    ok "${target} reconnected and restarted"
    banner "RECOVERED F at $(now)"
}

# --- recovery ------------------------------------------------------------
cmd_recover_all() {
    banner "RECOVER ALL — unpause, reconnect, start"
    local t0 container status
    t0=$(epoch)

    for container in $ALL_CONTAINERS; do
        exists "$container" || { warn "${container} does not exist; skipping"; continue; }
        status=$(state_of "$container")
        case "$status" in
            paused)
                docker unpause "$container" >/dev/null && ok "unpaused ${container}" ;;
            exited|created|dead)
                docker start "$container" >/dev/null && ok "started ${container}" ;;
            running)
                : ;;
            *)
                warn "${container} is in state '${status}'" ;;
        esac
        # Reattach anything that was disconnected. Harmless when already joined.
        if ! docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' \
                "$container" 2>/dev/null | grep -q "$NETWORK"; then
            docker network connect --alias "$container" "$NETWORK" "$container" >/dev/null 2>&1 \
                && ok "reconnected ${container} to ${NETWORK}"
            docker restart "$container" >/dev/null 2>&1 || true
        fi
    done

    info "waiting for the control plane to answer again"
    local deadline=$(( $(epoch) + 300 ))
    while [ "$(epoch)" -lt "$deadline" ]; do
        if curl -sf --max-time 5 "${API}/readyz" >/dev/null 2>&1; then break; fi
        sleep 2
    done

    # Milvus is the slow one; it needs a minute or two after a cold start.
    info "waiting for Milvus to report healthy"
    deadline=$(( $(epoch) + 300 ))
    local live=''
    while [ "$(epoch)" -lt "$deadline" ]; do
        live=$(curl -s --max-time 10 "${API}/api/v1/clusters" 2>/dev/null | grep -o '"last_health_status":"[a-z]*"' | head -1)
        case "$live" in *healthy*) break ;; esac
        sleep 3
    done

    ok "recovery finished in $(( $(epoch) - t0 ))s"
    probe_api
    banner "RECOVERED ALL at $(now)"
}

# --- status --------------------------------------------------------------
cmd_status() {
    banner "STATUS at $(now)"
    printf '  %-20s %-10s %-12s %s\n' "CONTAINER" "STATE" "HEALTH" "NETWORKS" >&2
    printf '  %-20s %-10s %-12s %s\n' "---------" "-----" "------" "--------" >&2
    local container status health nets
    for container in $ALL_CONTAINERS; do
        if ! exists "$container"; then
            printf '  %-20s %-10s %-12s %s\n' "$container" "absent" "-" "-" >&2
            continue
        fi
        status=$(state_of "$container")
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$container" 2>/dev/null || echo '-')
        nets=$(docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$container" 2>/dev/null | tr -s ' ')
        printf '  %-20s %-10s %-12s %s\n' "$container" "$status" "$health" "${nets:-none}" >&2
    done
    printf '\n' >&2
    probe_api
}

usage() {
    cat <<EOF
${C_BOLD}${SCRIPT_NAME}${C_RESET} — failure injection for the reliability drills

${C_BOLD}USAGE${C_RESET}
  ${SCRIPT_NAME} <command> [args]

${C_BOLD}INJECTIONS${C_RESET}
  ${C_CYAN}milvus-stop${C_RESET}              A — stop Milvus. Connection refused.
  ${C_CYAN}milvus-pause${C_RESET}             B — SIGSTOP Milvus. Accepts TCP, never replies:
                             the hung-dependency case that proves timeouts work.
  ${C_CYAN}minio-stop${C_RESET}               C — stop the object store. Shallow health keeps
                             saying 200 for a while; this is the interesting one.
  ${C_CYAN}postgres-stop${C_RESET}            D — stop the control plane's own database.
  ${C_CYAN}etcd-stop${C_RESET}                E — stop Milvus's metadata store.
  ${C_CYAN}network-cut${C_RESET} [service]    F — disconnect a container from ${NETWORK}.
                             Default: ${CP_API}.

${C_BOLD}RECOVERY${C_RESET}
  ${C_CYAN}network-heal${C_RESET} [service]   Reconnect and restart one container.
  ${C_CYAN}recover-all${C_RESET}              Unpause, reconnect and start everything, then wait
                             until the control plane reports healthy again.

${C_BOLD}OBSERVATION${C_RESET}
  ${C_CYAN}status${C_RESET}                   Container state, health and networks, plus what the
                             control plane currently reports.

${C_BOLD}NOTES${C_RESET}
  Timestamps are UTC, matching the API, the containers and the events table.
  No volume is ever touched; ${C_CYAN}recover-all${C_RESET} is always safe to run.
EOF
}

case "${1:-}" in
    milvus-stop)    shift; cmd_milvus_stop "$@" ;;
    milvus-pause)   shift; cmd_milvus_pause "$@" ;;
    minio-stop)     shift; cmd_minio_stop "$@" ;;
    postgres-stop)  shift; cmd_postgres_stop "$@" ;;
    etcd-stop)      shift; cmd_etcd_stop "$@" ;;
    network-cut)    shift; cmd_network_cut "$@" ;;
    network-heal)   shift; cmd_network_heal "$@" ;;
    recover-all)    shift; cmd_recover_all "$@" ;;
    status)         shift; cmd_status "$@" ;;
    -h|--help|help|"") usage ;;
    *) die "unknown command '$1' — run '${SCRIPT_NAME} --help'" ;;
esac
