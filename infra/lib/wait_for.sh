#!/usr/bin/env bash
# Blocking readiness helpers.
#
# Contract shared by every function here: emit one progress dot per second so a
# slow start looks like progress rather than a hang, and on timeout dump the
# last 50 log lines of the failing container to stderr before returning
# non-zero. A bare "timed out" with no logs forces the operator to go hunting;
# the logs are the whole point of the failure path.
#
# bash 3.2 compatible.

if [ -n "${CP_WAIT_FOR_SOURCED:-}" ]; then return 0; fi
CP_WAIT_FOR_SOURCED=1

CP_LOG_TAIL_LINES="${CP_LOG_TAIL_LINES:-50}"

# Dump recent container logs to stderr. Never fails: a missing container during
# error handling must not mask the original error.
dump_container_logs() {
    local container="$1"
    local lines="${2:-$CP_LOG_TAIL_LINES}"

    if [ -z "$container" ]; then
        return 0
    fi
    if ! docker inspect "$container" >/dev/null 2>&1; then
        warn "no container named '${container}' to pull logs from"
        return 0
    fi

    printf '\n%s--- last %s log lines: %s ---%s\n' \
        "${C_DIM}" "$lines" "$container" "${C_RESET}" >&2
    docker logs --tail "$lines" "$container" 2>&1 | sed 's/^/  /' >&2 || true
    printf '%s--- end %s ---%s\n\n' "${C_DIM}" "$container" "${C_RESET}" >&2
}

# Internal: one progress dot, flushed immediately.
_tick() {
    printf '.' >&2
}

_tick_done() {
    printf '\n' >&2
}

# wait_for_healthy <container> <timeout_s>
#
# Polls `docker inspect -f '{{.State.Health.Status}}'`. Containers without a
# HEALTHCHECK report an empty status; for those we fall back to "is it running",
# which keeps this usable for one-shot and unhealthchecked services rather than
# hanging forever on a status that will never arrive.
wait_for_healthy() {
    local container="$1"
    local timeout="${2:-120}"
    local elapsed=0
    local status running exit_code

    printf '%s  waiting for %s (timeout %ss) %s' \
        "${C_DIM}" "$container" "$timeout" "${C_RESET}" >&2

    while [ "$elapsed" -lt "$timeout" ]; do
        if ! docker inspect "$container" >/dev/null 2>&1; then
            _tick; sleep 1; elapsed=$((elapsed + 1)); continue
        fi

        status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
            "$container" 2>/dev/null || echo '')

        if [ -n "$status" ]; then
            case "$status" in
                healthy)
                    _tick_done
                    ok "${container} is healthy (${elapsed}s)"
                    return 0
                    ;;
                unhealthy)
                    _tick_done
                    err "${container} reported unhealthy after ${elapsed}s"
                    dump_container_logs "$container"
                    return 1
                    ;;
            esac
        else
            # No healthcheck defined. Accept a running container, or a one-shot
            # that already exited cleanly.
            running=$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || echo false)
            exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$container" 2>/dev/null || echo 1)
            if [ "$running" = "true" ]; then
                _tick_done
                ok "${container} is running (no healthcheck defined)"
                return 0
            fi
            if [ "$exit_code" = "0" ]; then
                _tick_done
                ok "${container} completed successfully"
                return 0
            fi
        fi

        _tick
        sleep 1
        elapsed=$((elapsed + 1))
    done

    _tick_done
    err "timed out after ${timeout}s waiting for ${container} to become healthy"
    dump_container_logs "$container"
    return 1
}

# wait_for_http <url> <timeout_s> [container_for_logs]
wait_for_http() {
    local url="$1"
    local timeout="${2:-60}"
    local container="${3:-}"
    local elapsed=0

    printf '%s  waiting for %s (timeout %ss) %s' \
        "${C_DIM}" "$url" "$timeout" "${C_RESET}" >&2

    while [ "$elapsed" -lt "$timeout" ]; do
        if curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; then
            _tick_done
            ok "${url} responded (${elapsed}s)"
            return 0
        fi
        _tick
        sleep 1
        elapsed=$((elapsed + 1))
    done

    _tick_done
    err "timed out after ${timeout}s waiting for ${url}"
    dump_container_logs "$container"
    return 1
}

# wait_for_tcp <host> <port> <timeout_s> [container_for_logs]
#
# Prefers nc; falls back to bash's /dev/tcp, which is not available in every
# build. Both paths are wrapped so a refused connection does not trip set -e.
wait_for_tcp() {
    local host="$1"
    local port="$2"
    local timeout="${3:-60}"
    local container="${4:-}"
    local elapsed=0

    printf '%s  waiting for %s:%s (timeout %ss) %s' \
        "${C_DIM}" "$host" "$port" "$timeout" "${C_RESET}" >&2

    while [ "$elapsed" -lt "$timeout" ]; do
        if _tcp_probe "$host" "$port"; then
            _tick_done
            ok "${host}:${port} accepting connections (${elapsed}s)"
            return 0
        fi
        _tick
        sleep 1
        elapsed=$((elapsed + 1))
    done

    _tick_done
    err "timed out after ${timeout}s waiting for ${host}:${port}"
    dump_container_logs "$container"
    return 1
}

_tcp_probe() {
    local host="$1" port="$2"
    if command -v nc >/dev/null 2>&1; then
        nc -z -G 2 "$host" "$port" >/dev/null 2>&1 && return 0
        # -G is macOS-only; retry with the portable form for GNU netcat.
        nc -z -w 2 "$host" "$port" >/dev/null 2>&1 && return 0
        return 1
    fi
    (exec 3<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1 || return 1
    return 0
}
