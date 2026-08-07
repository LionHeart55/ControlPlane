#!/usr/bin/env bash
# Host preflight checks.
#
# Every check reports pass/fail on its own line and, on failure, says what to do
# about it. Checks do not short-circuit: one run should surface every problem,
# because discovering a port clash only after fixing a memory limit wastes a
# five-minute stack boot.
#
# bash 3.2 compatible.

if [ -n "${CP_PREFLIGHT_SOURCED:-}" ]; then return 0; fi
CP_PREFLIGHT_SOURCED=1

CP_PREFLIGHT_FAILURES=0

# Docker Desktop reports slightly less than the configured allocation because
# the VM reserves some for itself: an "8 GB" setting shows ~7.65 GiB. Threshold
# is therefore 8 GB decimal, not 8 GiB, or a correctly-configured host fails.
CP_MIN_DOCKER_MEM_BYTES=8000000000
CP_MIN_DISK_KB=$((20 * 1024 * 1024))
CP_MIN_DOCKER_MAJOR=24

_pf_pass() { ok "$*"; }
_pf_fail() {
    CP_PREFLIGHT_FAILURES=$((CP_PREFLIGHT_FAILURES + 1))
    fail_with "$@"
}

# --- docker engine -------------------------------------------------------
check_docker_version() {
    local version major
    if ! command -v docker >/dev/null 2>&1; then
        _pf_fail "docker not found on PATH" \
            "Install Docker Desktop >= ${CP_MIN_DOCKER_MAJOR}: https://docs.docker.com/get-docker/"
        return 0
    fi
    if ! docker info >/dev/null 2>&1; then
        _pf_fail "docker daemon is not reachable" \
            "Start Docker Desktop (macOS/Windows) or: sudo systemctl start docker" \
            "Verify with: docker info"
        return 0
    fi
    version=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '0.0.0')
    major=${version%%.*}
    case "$major" in ''|*[!0-9]*) major=0 ;; esac
    if [ "$major" -lt "$CP_MIN_DOCKER_MAJOR" ]; then
        _pf_fail "docker ${version} is older than the required ${CP_MIN_DOCKER_MAJOR}.x" \
            "Upgrade Docker Desktop, or the compose 'condition: service_healthy' syntax may misbehave."
        return 0
    fi
    _pf_pass "docker ${version} (>= ${CP_MIN_DOCKER_MAJOR}.x)"
}

check_compose_v2() {
    local version major
    if ! docker compose version >/dev/null 2>&1; then
        _pf_fail "'docker compose' (v2) is not available" \
            "This project needs Compose v2, not the legacy docker-compose v1 binary." \
            "Docker Desktop ships it; on Linux install the docker-compose-plugin package."
        return 0
    fi
    version=$(docker compose version --short 2>/dev/null || echo '0')
    major=${version%%.*}
    case "$major" in ''|*[!0-9]*) major=0 ;; esac
    if [ "$major" -lt 2 ]; then
        _pf_fail "docker compose ${version} is not v2" \
            "Install the Compose v2 plugin; profiles and service_completed_successfully need it."
        return 0
    fi
    _pf_pass "docker compose v${version}"
}

# --- resources -----------------------------------------------------------
check_memory() {
    local mem_bytes mem_gib
    mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
    case "$mem_bytes" in ''|*[!0-9]*) mem_bytes=0 ;; esac
    mem_gib=$(awk -v b="$mem_bytes" 'BEGIN{printf "%.2f", b/1073741824}')

    if [ "$mem_bytes" -lt "$CP_MIN_DOCKER_MEM_BYTES" ]; then
        _pf_fail "Docker has only ${mem_gib} GiB of RAM available (need ~8 GB)" \
            "Milvus standalone alone wants ~4 GB; the full stack will OOM below this." \
            "Docker Desktop > Settings > Resources > Memory, then Apply & Restart."
        return 0
    fi
    _pf_pass "Docker memory ${mem_gib} GiB"
}

check_disk() {
    local avail_kb avail_gb
    avail_kb=$(df -Pk "$REPO_ROOT" 2>/dev/null | awk 'NR==2 {print $4}')
    case "$avail_kb" in ''|*[!0-9]*) avail_kb=0 ;; esac
    avail_gb=$(awk -v k="$avail_kb" 'BEGIN{printf "%.1f", k/1048576}')

    if [ "$avail_kb" -lt "$CP_MIN_DISK_KB" ]; then
        _pf_fail "only ${avail_gb} GB free on the volume holding ${REPO_ROOT} (need 20 GB)" \
            "Images total ~2 GB; the rest is Milvus segments, WAL and Postgres data." \
            "Free space, or point DOCKER_VOLUME_DIRECTORY at a larger volume."
        return 0
    fi
    _pf_pass "disk ${avail_gb} GB free"
}

# --- ports ---------------------------------------------------------------
# A port held by one of OUR containers is not a conflict — otherwise the second
# `up` would fail preflight and idempotency would be broken.
#
# Do NOT match against `docker ps --format '{{.Ports}}'`: Docker collapses
# contiguous bindings into ranges, so MinIO renders as "9000-9001->9000-9001/tcp"
# and a grep for ":9000->" silently misses it. Inspecting HostPort yields one
# exact port per binding with no ranges to parse.
port_owned_by_stack() {
    local port="$1" ids
    ids=$(docker ps --filter "label=com.docker.compose.project=${COMPOSE_PROJECT_NAME:-milvus-cp}" \
        -q 2>/dev/null || echo '')
    [ -z "$ids" ] && return 1
    # shellcheck disable=SC2086
    docker inspect \
        --format '{{range $p, $conf := .NetworkSettings.Ports}}{{range $conf}}{{println .HostPort}}{{end}}{{end}}' \
        $ids 2>/dev/null | grep -qx "$port"
}

# lsof cannot see listeners owned by other users without root, so netstat is
# consulted as well. Either one reporting a listener counts as in-use.
port_in_use() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then return 0; fi
    fi
    if netstat -an 2>/dev/null | grep -qE "[.:]${port}[[:space:]]+.*LISTEN"; then return 0; fi
    return 1
}

# check_port <port> <label> <strict>
# strict=0 downgrades a conflict to a warning, used for app-tier ports when only
# the infra profile is being started.
check_port() {
    local port="$1" label="$2" strict="${3:-1}"

    if ! port_in_use "$port"; then
        _pf_pass "port ${port} free (${label})"
        return 0
    fi
    if port_owned_by_stack "$port"; then
        _pf_pass "port ${port} held by this stack (${label}) — re-run, not a conflict"
        return 0
    fi
    if [ "$strict" -eq 0 ]; then
        warn "port ${port} in use (${label}) — not needed by this profile, continuing"
        return 0
    fi
    _pf_fail "port ${port} is already in use (${label})" \
        "Find the owner:  lsof -nP -iTCP:${port} -sTCP:LISTEN   (may need sudo)" \
        "                 netstat -an | grep ${port}" \
        "Then stop that service, or override the host port in .env."
}

check_ports() {
    local profile="${1:-infra}"
    local app_strict=0
    [ "$profile" = "all" ] && app_strict=1

    local pg_port="${POSTGRES_HOST_PORT:-${POSTGRES_PORT:-5432}}"

    check_port 19530 "Milvus gRPC" 1
    check_port 9091  "Milvus metrics/health" 1
    check_port 9000  "MinIO API" 1
    check_port 9001  "MinIO console" 1
    check_port "$pg_port" "PostgreSQL" 1
    check_port "${CP_API_PORT:-8000}" "control-plane API" "$app_strict"
    check_port "${DASHBOARD_PORT:-8080}" "dashboard" "$app_strict"
}

# --- docker socket -------------------------------------------------------
# Advisory only: the control plane is designed to stay useful without Docker
# access, reporting DOCKER_UNAVAILABLE rather than failing. But the components
# panel and the WP-15 drills need it, so a broken socket is worth flagging early.
check_docker_socket() {
    local sock="${DOCKER_SOCKET:-/var/run/docker.sock}"
    if [ -S "$sock" ]; then
        _pf_pass "docker socket present at ${sock}"
        return 0
    fi
    warn "docker socket not found at ${sock}"
    printf '       %sThe API container reads container state through this socket.%s\n' \
        "${C_DIM}" "${C_RESET}" >&2
    printf '       %smacOS: Docker Desktop > Settings > Advanced >%s\n' "${C_DIM}" "${C_RESET}" >&2
    printf '       %s       "Allow the default Docker socket to be used".%s\n' "${C_DIM}" "${C_RESET}" >&2
    printf '       %sLinux: add your user to the docker group, or set DOCKER_SOCKET in .env.%s\n' \
        "${C_DIM}" "${C_RESET}" >&2
}

# --- entrypoint ----------------------------------------------------------
run_preflight() {
    local profile="${1:-infra}"
    CP_PREFLIGHT_FAILURES=0

    banner "Preflight (profile: ${profile})"
    check_docker_version
    check_compose_v2
    check_memory
    check_disk
    check_ports "$profile"
    check_docker_socket

    if [ "$CP_PREFLIGHT_FAILURES" -gt 0 ]; then
        hr
        err "${CP_PREFLIGHT_FAILURES} preflight check(s) failed — fix the above and re-run"
        return 1
    fi
    hr
    ok "preflight passed"
    return 0
}
