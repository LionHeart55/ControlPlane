#!/usr/bin/env bash
# TTY-aware colour and logging helpers.
#
# Sourced by deploy.sh. Degrades to plain text when NO_COLOR is set (see
# https://no-color.org) or when stdout is not a terminal, so piping output to a
# file or a CI log never embeds escape sequences.
#
# bash 3.2 compatible: no associative arrays, no ${var^^}.

# Guard against double-sourcing.
if [ -n "${CP_COLORS_SOURCED:-}" ]; then return 0; fi
CP_COLORS_SOURCED=1

if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    C_RESET=''
    C_BOLD=''
    C_DIM=''
    C_RED=''
    C_GREEN=''
    C_YELLOW=''
    C_BLUE=''
    C_CYAN=''
else
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_CYAN=$'\033[36m'
fi

# Status glyphs. ASCII only — a reviewer may be on a terminal without a
# Unicode-capable font, and a mojibake tick reads as a failure.
G_OK="[ ok ]"
G_WARN="[warn]"
G_FAIL="[fail]"
G_INFO="[info]"

# All diagnostics go to stderr so that command output stays pipeable.
info() { printf '%s %s\n' "${C_BLUE}${G_INFO}${C_RESET}" "$*" >&2; }
ok()   { printf '%s %s\n' "${C_GREEN}${G_OK}${C_RESET}" "$*" >&2; }
warn() { printf '%s %s\n' "${C_YELLOW}${G_WARN}${C_RESET}" "$*" >&2; }
err()  { printf '%s %s\n' "${C_RED}${G_FAIL}${C_RESET}" "$*" >&2; }

# A failure the user can act on: what broke, then how to fix it.
fail_with() {
    local msg="$1"
    shift
    err "$msg"
    local line
    for line in "$@"; do
        printf '       %s%s%s\n' "${C_DIM}" "$line" "${C_RESET}" >&2
    done
}

hr() {
    printf '%s%s%s\n' "${C_DIM}" \
        "----------------------------------------------------------------------" \
        "${C_RESET}" >&2
}

banner() {
    hr
    printf '%s%s%s\n' "${C_BOLD}${C_CYAN}" "  $*" "${C_RESET}" >&2
    hr
}

# Colour a health/state word for table output. Echoes to stdout because callers
# embed the result in printf format strings.
colorize_state() {
    local state="$1"
    case "$state" in
        healthy|running|ok|OK|up|Loaded|reachable)
            printf '%s%s%s' "${C_GREEN}" "$state" "${C_RESET}" ;;
        starting|restarting|provisioning|degraded|stale)
            printf '%s%s%s' "${C_YELLOW}" "$state" "${C_RESET}" ;;
        unhealthy|exited|dead|missing|unavailable|failed|down)
            printf '%s%s%s' "${C_RED}" "$state" "${C_RESET}" ;;
        *)
            printf '%s%s%s' "${C_DIM}" "$state" "${C_RESET}" ;;
    esac
}

# printf pads by byte count, which colour escapes inflate. Pad the plain text
# first, then wrap the padded string in colour.
pad_colorized() {
    local text="$1" width="$2"
    local padded
    padded=$(printf '%-*s' "$width" "$text")
    colorize_state "$padded"
}
