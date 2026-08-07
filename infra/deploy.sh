#!/usr/bin/env bash
#
# deploy.sh — infrastructure automation entrypoint for the Milvus 2.6 control plane.
#
# Every subcommand is idempotent: running it twice is a no-op that still exits 0.
#
#   ./infra/deploy.sh --help
#
# set -E (errtrace) is included alongside -euo pipefail so the ERR trap fires
# inside functions too; without it a failure in a function body reports no line.
set -Eeuo pipefail

SCRIPT_NAME=$(basename "$0")
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd)
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"

# shellcheck source=lib/colors.sh
. "${SCRIPT_DIR}/lib/colors.sh"

on_err() {
    local exit_code=$?
    local line="$1"
    err "${SCRIPT_NAME}: failed at line ${line} (exit ${exit_code})"
    exit "$exit_code"
}
trap 'on_err $LINENO' ERR

# --- configuration -------------------------------------------------------
CP_WAIT_ETCD="${CP_WAIT_ETCD:-90}"
CP_WAIT_MINIO="${CP_WAIT_MINIO:-90}"
CP_WAIT_POSTGRES="${CP_WAIT_POSTGRES:-90}"
# Milvus needs ~90-120s on a cold first boot before /healthz answers; the
# compose healthcheck has start_period 120s, so this must exceed it.
CP_WAIT_MILVUS="${CP_WAIT_MILVUS:-300}"
CP_WAIT_API="${CP_WAIT_API:-90}"

# Services that exit 0 rather than staying up; never wait for them to be healthy.
CP_ONESHOT_SERVICES="cp-migrate"

# Load .env so the script sees the same values Compose does.
load_env() {
    if [ -f "${REPO_ROOT}/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        . "${REPO_ROOT}/.env"
        set +a
    fi
}

# Compose wrapper. --project-directory pins the project root so that relative
# bind mounts resolve to ./volumes and .env is actually read; without it Compose
# uses infra/ as the project directory, silently drops every ${VAR}, and writes
# volume data to infra/volumes/. See the header of docker-compose.yml.
dc() {
    docker compose \
        --env-file "${REPO_ROOT}/.env" \
        -f "${COMPOSE_FILE}" \
        --project-directory "${REPO_ROOT}" \
        "$@"
}

container_for_service() {
    case "$1" in
        etcd)         echo "milvus-etcd" ;;
        minio)        echo "milvus-minio" ;;
        standalone)   echo "milvus-standalone" ;;
        postgres)     echo "cp-postgres" ;;
        *)            echo "$1" ;;
    esac
}

timeout_for_service() {
    case "$1" in
        etcd)         echo "$CP_WAIT_ETCD" ;;
        minio)        echo "$CP_WAIT_MINIO" ;;
        standalone)   echo "$CP_WAIT_MILVUS" ;;
        postgres)     echo "$CP_WAIT_POSTGRES" ;;
        *)            echo "$CP_WAIT_API" ;;
    esac
}

is_oneshot() {
    local svc="$1" one
    for one in $CP_ONESHOT_SERVICES; do
        [ "$svc" = "$one" ] && return 0
    done
    return 1
}

profile_args() {
    case "${1:-infra}" in
        all)  printf '%s' "--profile infra --profile app" ;;
        *)    printf '%s' "--profile infra" ;;
    esac
}

# --- help ----------------------------------------------------------------
usage() {
    cat <<EOF
${C_BOLD}${SCRIPT_NAME}${C_RESET} — Milvus 2.6 control-plane stack automation

${C_BOLD}USAGE${C_RESET}
  ${SCRIPT_NAME} <command> [options]

${C_BOLD}COMMANDS${C_RESET}
  ${C_CYAN}preflight${C_RESET}                Verify docker >= 24, compose v2, ~8 GB RAM, 20 GB disk
                           and that every required host port is free.
  ${C_CYAN}up${C_RESET} [options]             Bring the stack up end to end: preflight, bootstrap .env,
                           compose up, wait for health, create the MinIO bucket,
                           run migrations, seed the cluster, print endpoints.
  ${C_CYAN}status${C_RESET}                   Per-service container health, live endpoint probes and
                           control-plane table row counts.
  ${C_CYAN}logs${C_RESET} [service] [-f]      Tail logs. With no service, all of them.
  ${C_CYAN}restart${C_RESET} <service>        Restart one service and wait for it to be healthy again.
  ${C_CYAN}down${C_RESET}                     Stop and remove containers. ${C_BOLD}Volumes are kept.${C_RESET}
  ${C_CYAN}destroy${C_RESET} [--yes]          Remove containers, volumes and ./volumes on disk.
                           ${C_BOLD}Destroys all data.${C_RESET} Prompts unless --yes is given.
  ${C_CYAN}reset${C_RESET}                    destroy --yes, then up. A clean rebuild from zero.

${C_BOLD}OPTIONS for 'up'${C_RESET}
  --mode standalone|distributed   Deployment topology. Default: standalone.
                                  'distributed' is NOT implemented in this
                                  submission — see docs/ARCHITECTURE.md.
  --profile infra|all             infra = etcd, minio, milvus, postgres (4 containers)
                                  all   = infra plus cp-migrate, cp-api, cp-dashboard
                                  Default: all.

${C_BOLD}ENVIRONMENT${C_RESET}
  NO_COLOR=1                      Disable coloured output (also auto-disabled
                                  when stdout is not a terminal).
  CP_WAIT_MILVUS=<seconds>        Override the Milvus readiness timeout (default 300).

${C_BOLD}EXAMPLES${C_RESET}
  ./infra/deploy.sh preflight
  ./infra/deploy.sh up --profile infra
  ./infra/deploy.sh status
  ./infra/deploy.sh logs milvus-standalone -f
  ./infra/deploy.sh destroy --yes && ./infra/deploy.sh up
EOF
}

# --- .env bootstrap ------------------------------------------------------
bootstrap_env() {
    if [ -f "${REPO_ROOT}/.env" ]; then
        info ".env present"
        return 0
    fi
    if [ ! -f "${REPO_ROOT}/.env.example" ]; then
        fail_with ".env and .env.example are both missing" \
            "Cannot continue without configuration. Restore .env.example from version control."
        return 1
    fi
    cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
    ok "created .env from .env.example"
}

# --- MinIO bucket --------------------------------------------------------
# Reuses the already-pinned minio/minio image, which bundles the mc client, so
# this adds no new image tag to pin and no extra pull. MC_HOST_<alias> carries
# the credentials, keeping them out of the argv of a shared container.
create_minio_bucket() {
    local net bucket
    net="${COMPOSE_PROJECT_NAME:-milvus-cp}-net"
    bucket="${MINIO_BUCKET:-milvus-bucket}"

    info "ensuring MinIO bucket '${bucket}' exists"
    if docker run --rm \
        --network "$net" \
        -e "MC_HOST_cp=http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
        --entrypoint sh \
        "minio/minio:${MINIO_VERSION}" \
        -c "mc mb --ignore-existing cp/${bucket} >/dev/null 2>&1 && mc ls cp" >/dev/null 2>&1
    then
        ok "bucket '${bucket}' ready"
    else
        warn "could not verify bucket '${bucket}' — Milvus creates it on demand, continuing"
    fi
}

# --- migrations ----------------------------------------------------------
# Three paths, most-preferred first. Migrations must never run from the API
# entrypoint: concurrent replicas would race on the alembic_version table.
run_migrations() {
    local versions_dir="${REPO_ROOT}/control_plane/migrations/versions"

    if dc config --services 2>/dev/null | grep -qx 'cp-migrate'; then
        info "running migrations via the cp-migrate one-shot service"
        dc run --rm cp-migrate
        ok "migrations applied"
        return 0
    fi

    if ! ls "${versions_dir}"/*.py >/dev/null 2>&1; then
        warn "skipping migrations — no revisions found in ${versions_dir}"
        return 0
    fi

    # Prefer the project virtualenv; the host interpreter may be the wrong
    # Python version and will not have alembic installed.
    local alembic_bin=""
    if [ -x "${REPO_ROOT}/control_plane/.venv/bin/alembic" ]; then
        alembic_bin="${REPO_ROOT}/control_plane/.venv/bin/alembic"
    elif command -v alembic >/dev/null 2>&1; then
        alembic_bin="$(command -v alembic)"
    fi

    if [ -z "$alembic_bin" ]; then
        warn "skipping migrations — alembic not found"
        printf '       %sCreate the environment first:  make venv%s\n' "${C_DIM}" "${C_RESET}" >&2
        return 0
    fi

    # Running on the host, so target the published port: the .env defaults
    # (cp-postgres:5432) only resolve inside cp-net.
    info "running migrations with ${alembic_bin}"
    (
        cd "${REPO_ROOT}/control_plane" || exit 1
        POSTGRES_HOST=localhost \
        POSTGRES_PORT="${POSTGRES_HOST_PORT:-${POSTGRES_PORT:-5432}}" \
        "$alembic_bin" upgrade head
    )
    ok "migrations applied"
}

run_seed() {
    local seed="${REPO_ROOT}/scripts/seed_cluster.sh"
    if [ ! -x "$seed" ]; then
        warn "skipping seed — ${seed} not executable"
        return 0
    fi
    info "seeding cluster registration"
    "$seed" || warn "seed script reported a problem; continuing"
}

# --- endpoint summary ----------------------------------------------------
print_endpoints() {
    local pg_port="${POSTGRES_HOST_PORT:-${POSTGRES_PORT:-5432}}"
    banner "Endpoints"
    printf '  %-26s %-34s %s\n' "SERVICE" "URL" "NOTES" >&2
    printf '  %-26s %-34s %s\n' "-------" "---" "-----" >&2
    printf '  %-26s %-34s %s\n' "Milvus gRPC"        "localhost:19530"              "pymilvus target" >&2
    printf '  %-26s %-34s %s\n' "Milvus health"      "http://localhost:9091/healthz" "shallow liveness" >&2
    printf '  %-26s %-34s %s\n' "Milvus WebUI"       "http://localhost:9091/webui/"  "diagnosis aid" >&2
    printf '  %-26s %-34s %s\n' "MinIO console"      "http://localhost:9001"         "${MINIO_ROOT_USER:-minioadmin}" >&2
    printf '  %-26s %-34s %s\n' "PostgreSQL"         "localhost:${pg_port}"          "${POSTGRES_DB:-controlplane}" >&2
    printf '  %-26s %-34s %s\n' "Control-plane API"  "http://localhost:${CP_API_PORT:-8000}/docs" "OpenAPI UI" >&2
    printf '  %-26s %-34s %s\n' "Dashboard"          "http://localhost:${DASHBOARD_PORT:-8080}"   "single page" >&2
    hr
}

# --- commands ------------------------------------------------------------
cmd_preflight() {
    load_env
    # Guarded so a failing check exits cleanly with its own message rather than
    # tripping the ERR trap, which would report a script bug that isn't one.
    if ! run_preflight "${1:-all}"; then
        exit 1
    fi
}

cmd_up() {
    local mode="standalone" profile="all" svc container timeout

    while [ $# -gt 0 ]; do
        case "$1" in
            --mode)     mode="${2:-}"; shift 2 ;;
            --mode=*)   mode="${1#*=}"; shift ;;
            --profile)  profile="${2:-}"; shift 2 ;;
            --profile=*) profile="${1#*=}"; shift ;;
            -h|--help)  usage; return 0 ;;
            *) fail_with "unknown option for 'up': $1" "Run '${SCRIPT_NAME} --help'."; exit 2 ;;
        esac
    done

    case "$mode" in
        standalone) ;;
        distributed)
            fail_with "--mode distributed is not implemented in this submission" \
                "Only standalone is built. A distributed topology would select a second" \
                "compose file (docker-compose.distributed.yml) with separate coordinator," \
                "proxy, query, data and index nodes." \
                "See docs/ARCHITECTURE.md, section 'Alternatives considered'."
            exit 2 ;;
        *) fail_with "unknown --mode '${mode}'" "Valid values: standalone, distributed."; exit 2 ;;
    esac

    case "$profile" in
        infra|all) ;;
        *) fail_with "unknown --profile '${profile}'" "Valid values: infra, all."; exit 2 ;;
    esac

    banner "Bringing up the stack (mode=${mode}, profile=${profile})"

    bootstrap_env
    load_env
    if ! run_preflight "$profile"; then
        exit 1
    fi

    info "starting containers"
    # A port can be retaken between preflight and the actual bind — a
    # launchd/systemd service with KeepAlive will restart within seconds of
    # being stopped. Translate the daemon's raw error into something actionable
    # rather than letting preflight's earlier "free" verdict look like a lie.
    local up_log up_rc=0
    up_log="${TMPDIR:-/tmp}/cp-up-$$.log"
    # shellcheck disable=SC2046
    dc $(profile_args "$profile") up -d 2>&1 | tee "$up_log" >&2 || up_rc=$?
    if [ "$up_rc" -ne 0 ] || grep -q 'address already in use' "$up_log" 2>/dev/null; then
        if grep -q 'address already in use' "$up_log" 2>/dev/null; then
            local taken
            taken=$(grep -o 'listen tcp[^:]*:[0-9]\{2,5\}' "$up_log" | grep -o '[0-9]\{2,5\}$' | head -1)
            fail_with "port ${taken:-?} was taken between preflight and startup" \
                "Something restarted and grabbed it — a launchd or systemd unit with" \
                "KeepAlive will come back seconds after being stopped." \
                "Identify it:  lsof -nP -iTCP:${taken:-PORT} -sTCP:LISTEN   (may need sudo)" \
                "Either disable that unit, or publish elsewhere via .env" \
                "(e.g. POSTGRES_HOST_PORT=5433)."
        fi
        rm -f "$up_log"
        exit 1
    fi
    rm -f "$up_log"

    banner "Waiting for health"
    # shellcheck disable=SC2046  # profile_args intentionally expands to several flags
    for svc in $(dc $(profile_args "$profile") config --services 2>/dev/null); do
        if is_oneshot "$svc"; then continue; fi
        container=$(container_for_service "$svc")
        timeout=$(timeout_for_service "$svc")
        wait_for_healthy "$container" "$timeout"
    done

    banner "Provisioning"
    create_minio_bucket
    run_migrations
    run_seed

    print_endpoints
    ok "stack is up"
}

cmd_status() {
    load_env
    local pg_port="${POSTGRES_HOST_PORT:-${POSTGRES_PORT:-5432}}"
    local name state health

    banner "Containers"
    printf '  %-22s %-12s %s\n' "CONTAINER" "STATE" "HEALTH" >&2
    printf '  %-22s %-12s %s\n' "---------" "-----" "------" >&2
    for name in milvus-etcd milvus-minio milvus-standalone cp-postgres cp-api cp-dashboard; do
        if ! docker inspect "$name" >/dev/null 2>&1; then
            # Absent is reported, never omitted — a missing container is the
            # outage the operator most needs to see.
            printf '  %-22s %s %s\n' "$name" "$(pad_colorized missing 12)" "$(colorize_state '-')" >&2
            continue
        fi
        state=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo unknown)
        health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$name" 2>/dev/null || echo unknown)
        printf '  %-22s %s %s\n' "$name" "$(pad_colorized "$state" 12)" "$(colorize_state "$health")" >&2
    done

    banner "Endpoint probes"
    printf '  %-26s %s\n' "PROBE" "RESULT" >&2
    printf '  %-26s %s\n' "-----" "------" >&2
    _probe_http "Milvus /healthz" "http://localhost:9091/healthz"
    _probe_http "Milvus /metrics" "http://localhost:9091/metrics"
    _probe_tcp  "Milvus gRPC" localhost 19530
    _probe_http "MinIO live"  "http://localhost:9000/minio/health/live"
    _probe_http "API /healthz" "http://localhost:${CP_API_PORT:-8000}/healthz"
    _probe_http "Dashboard"    "http://localhost:${DASHBOARD_PORT:-8080}/"
    _probe_psql "PostgreSQL SELECT 1"

    banner "Control-plane tables"
    _print_row_counts
    hr
}

_probe_http() {
    local label="$1" url="$2" code
    # curl already prints 000 on connection failure AND exits non-zero, so a
    # `|| echo 000` fallback would concatenate into "000000".
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null || true)
    [ -z "$code" ] && code="000"
    if [ "$code" = "200" ]; then
        printf '  %-26s %s\n' "$label" "$(colorize_state "ok") ${C_DIM}HTTP ${code}${C_RESET}" >&2
    else
        printf '  %-26s %s\n' "$label" "$(colorize_state "unavailable") ${C_DIM}HTTP ${code}${C_RESET}" >&2
    fi
}

_probe_tcp() {
    local label="$1" host="$2" port="$3"
    if _tcp_probe "$host" "$port"; then
        printf '  %-26s %s\n' "$label" "$(colorize_state "ok")" >&2
    else
        printf '  %-26s %s\n' "$label" "$(colorize_state "unavailable")" >&2
    fi
}

_probe_psql() {
    local label="$1" out
    if ! docker inspect cp-postgres >/dev/null 2>&1; then
        printf '  %-26s %s\n' "$label" "$(colorize_state "missing")" >&2
        return 0
    fi
    out=$(docker exec cp-postgres psql -U "${POSTGRES_USER:-controlplane}" \
        -d "${POSTGRES_DB:-controlplane}" -tAc 'SELECT 1' 2>/dev/null || echo '')
    if [ "$out" = "1" ]; then
        printf '  %-26s %s\n' "$label" "$(colorize_state "ok")" >&2
    else
        printf '  %-26s %s\n' "$label" "$(colorize_state "unavailable")" >&2
    fi
}

# Counts only tables that exist, so this stays useful before WP-04 has created
# any schema instead of erroring out.
_print_row_counts() {
    local tables existing t count
    tables="clusters health_checks component_status collection_snapshots events"

    if ! docker inspect cp-postgres >/dev/null 2>&1; then
        printf '  %s\n' "${C_DIM}cp-postgres not running${C_RESET}" >&2
        return 0
    fi

    existing=$(docker exec cp-postgres psql -U "${POSTGRES_USER:-controlplane}" \
        -d "${POSTGRES_DB:-controlplane}" -tAc \
        "SELECT table_name FROM information_schema.tables
          WHERE table_schema='public'" 2>/dev/null || echo '')

    if [ -z "$existing" ]; then
        printf '  %s\n' "${C_DIM}no tables yet — run migrations (WP-04)${C_RESET}" >&2
        return 0
    fi

    printf '  %-26s %s\n' "TABLE" "ROWS" >&2
    printf '  %-26s %s\n' "-----" "----" >&2
    for t in $tables; do
        if printf '%s\n' "$existing" | grep -qx "$t"; then
            count=$(docker exec cp-postgres psql -U "${POSTGRES_USER:-controlplane}" \
                -d "${POSTGRES_DB:-controlplane}" -tAc "SELECT count(*) FROM ${t}" 2>/dev/null || echo '?')
            printf '  %-26s %s\n' "$t" "$count" >&2
        else
            printf '  %-26s %s\n' "$t" "${C_DIM}absent${C_RESET}" >&2
        fi
    done
}

cmd_logs() {
    load_env
    if [ $# -eq 0 ]; then
        dc --profile infra --profile app logs --tail 200
        return 0
    fi
    dc --profile infra --profile app logs --tail 200 "$@"
}

cmd_restart() {
    load_env
    if [ $# -lt 1 ]; then
        fail_with "restart requires a service name" \
            "Services: etcd, minio, standalone, postgres, cp-api, cp-dashboard"
        exit 2
    fi
    local svc="$1" container timeout
    container=$(container_for_service "$svc")
    timeout=$(timeout_for_service "$svc")

    info "restarting ${svc} (${container})"
    dc --profile infra --profile app restart "$svc"
    wait_for_healthy "$container" "$timeout"
    ok "${svc} restarted"
}

cmd_down() {
    load_env
    banner "Stopping the stack (volumes kept)"
    dc --profile infra --profile app down --remove-orphans
    ok "containers removed; data in ./volumes is intact"
    info "to delete data as well: ${SCRIPT_NAME} destroy"
}

cmd_destroy() {
    load_env
    local assume_yes=0 reply
    while [ $# -gt 0 ]; do
        case "$1" in
            -y|--yes) assume_yes=1; shift ;;
            *) fail_with "unknown option for 'destroy': $1"; exit 2 ;;
        esac
    done

    if [ "$assume_yes" -eq 0 ]; then
        warn "This deletes ALL stack data:"
        printf '       %s- every container and compose volume%s\n' "${C_DIM}" "${C_RESET}" >&2
        printf '       %s- %s/volumes (etcd, minio, milvus, postgres)%s\n' \
            "${C_DIM}" "${REPO_ROOT}" "${C_RESET}" >&2
        printf '%sProceed? [y/N] %s' "${C_BOLD}" "${C_RESET}" >&2
        read -r reply || reply=""
        case "$reply" in
            y|Y|yes|YES) ;;
            *) info "aborted; nothing was deleted"; return 0 ;;
        esac
    fi

    banner "Destroying the stack"
    dc --profile infra --profile app down -v --remove-orphans
    _remove_volume_dir
    ok "stack destroyed"
}

# Postgres writes its data dir as uid 999, so a plain rm can fail on Linux.
# Fall back to deleting from inside a container that already has the mount.
_remove_volume_dir() {
    local vol="${REPO_ROOT}/volumes"

    if [ ! -d "$vol" ]; then
        info "no ./volumes directory to remove"
        return 0
    fi
    # Guard: never rm -rf a path we failed to compute.
    case "$vol" in
        /|""|"/volumes") err "refusing to delete suspicious path '${vol}'"; return 1 ;;
    esac

    info "removing ${vol}"
    if rm -rf "$vol" 2>/dev/null; then
        ok "removed ${vol}"
        return 0
    fi

    warn "host rm failed (root-owned files); retrying inside a container"
    docker run --rm --entrypoint sh \
        -v "${vol}:/target" \
        "postgres:${POSTGRES_VERSION:-16-alpine}" \
        -c 'rm -rf /target/* /target/.[!.]* 2>/dev/null || true' >/dev/null 2>&1 || true
    rmdir "$vol" 2>/dev/null || true

    if [ -d "$vol" ]; then
        warn "${vol} still exists — remove it manually: sudo rm -rf ${vol}"
    else
        ok "removed ${vol}"
    fi
}

cmd_reset() {
    banner "Reset: destroy then rebuild"
    cmd_destroy --yes
    cmd_up "$@"
}

# --- dispatch ------------------------------------------------------------
main() {
    if [ $# -eq 0 ]; then usage; exit 0; fi

    local cmd="$1"; shift
    case "$cmd" in
        -h|--help|help) usage ;;
        preflight)      cmd_preflight "$@" ;;
        up)             cmd_up "$@" ;;
        status)         cmd_status "$@" ;;
        logs)           cmd_logs "$@" ;;
        restart)        cmd_restart "$@" ;;
        down)           cmd_down "$@" ;;
        destroy)        cmd_destroy "$@" ;;
        reset)          cmd_reset "$@" ;;
        *)
            fail_with "unknown command '${cmd}'" "Run '${SCRIPT_NAME} --help' for the command table."
            exit 2 ;;
    esac
}

# Sourced after colors.sh so the wait helpers can use its logging functions.
# shellcheck source=lib/wait_for.sh
. "${SCRIPT_DIR}/lib/wait_for.sh"
# shellcheck source=lib/preflight.sh
. "${SCRIPT_DIR}/lib/preflight.sh"

main "$@"
