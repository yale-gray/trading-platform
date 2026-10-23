#!/usr/bin/env bash
# Per-user LaunchAgent helper for the paper runner and the API.
#
#   tradebot-service.sh install     render plists into ~/Library/LaunchAgents and start them
#   tradebot-service.sh uninstall   stop both agents and remove their plists
#   tradebot-service.sh status      show launchd state for both agents
#
# User-level only: never sudo, never system folders. Simulated money only.
#
# Test hooks (not for normal use):
#   LAUNCH_AGENTS_DIR            target folder for plists (default ~/Library/LaunchAgents)
#   TRADEBOT_REPO_ROOT           repo root override (default: two levels above this script)
#   TRADEBOT_SERVICE_DRY_RUN=1   render files only; skip every launchctl call

set -euo pipefail

RUNNER_LABEL="com.tradebot.runner"
API_LABEL="com.tradebot.api"
API_URL="http://127.0.0.1:8000"

die() {
    echo "tradebot-service: $*" >&2
    exit 1
}

if [ "$(id -u)" -eq 0 ]; then
    die "refusing to run as root. Run as your normal user (no sudo)."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR"
REPO_ROOT="${TRADEBOT_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LAUNCH_AGENTS_DIR="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
DRY_RUN="${TRADEBOT_SERVICE_DRY_RUN:-0}"
UID_NUM="$(id -u)"
LOG_DIR="$REPO_ROOT/logs"

# Paths are substituted into sed and XML, so reject characters that would break either.
check_safe_path() {
    case "$1" in
        *[\|\&\\\<\>]*|*$'\n'*) die "path contains a character the plist renderer cannot handle: $1" ;;
    esac
}

render() {
    # render <template> <output>
    local template="$1" out="$2"
    sed -e "s|@UV@|$UV_BIN|g" \
        -e "s|@UV_DIR@|$UV_DIR|g" \
        -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
        "$template" > "$out"
    chmod 644 "$out"
}

plist_path() {
    echo "$LAUNCH_AGENTS_DIR/$1.plist"
}

# launchctl helpers: bootstrap with a fallback to the older load verb.
launch_bootstrap() {
    local label="$1" plist="$2"
    launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1 || true
    if ! launchctl bootstrap "gui/$UID_NUM" "$plist" 2>/dev/null; then
        launchctl load -w "$plist"
    fi
}

launch_remove() {
    local label="$1" plist="$2"
    if ! launchctl bootout "gui/$UID_NUM/$label" >/dev/null 2>&1; then
        if [ -f "$plist" ]; then
            launchctl unload -w "$plist" >/dev/null 2>&1 || true
        fi
    fi
}

cmd_install() {
    UV_BIN="$(command -v uv || true)"
    [ -n "$UV_BIN" ] || die "uv not found on PATH. Install uv first (https://docs.astral.sh/uv/)."
    case "$UV_BIN" in /*) ;; *) die "uv path is not absolute: $UV_BIN" ;; esac
    UV_DIR="$(dirname "$UV_BIN")"

    check_safe_path "$UV_BIN"
    check_safe_path "$UV_DIR"
    check_safe_path "$REPO_ROOT"
    [ -f "$REPO_ROOT/pyproject.toml" ] || die "repo root has no pyproject.toml: $REPO_ROOT"

    mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

    local label plist
    for label in "$RUNNER_LABEL" "$API_LABEL"; do
        plist="$(plist_path "$label")"
        render "$TEMPLATE_DIR/$label.plist.template" "$plist"
    done

    if [ "$DRY_RUN" != "1" ]; then
        for label in "$RUNNER_LABEL" "$API_LABEL"; do
            launch_bootstrap "$label" "$(plist_path "$label")"
        done
    fi

    echo "Installed LaunchAgents (start at login, restart on crash):"
    echo "  $(plist_path "$RUNNER_LABEL")  -> uv run tradebot runner start"
    echo "  $(plist_path "$API_LABEL")  -> uv run tradebot api serve"
    echo "Logs: $LOG_DIR/{runner,api}.{out,err}.log"
    if [ "$DRY_RUN" = "1" ]; then
        echo "(dry run: launchctl was not called)"
    fi
    echo "Open the app at $API_URL"
}

cmd_uninstall() {
    local label plist
    for label in "$RUNNER_LABEL" "$API_LABEL"; do
        plist="$(plist_path "$label")"
        if [ "$DRY_RUN" != "1" ]; then
            launch_remove "$label" "$plist"
        fi
        rm -f "$plist"
        echo "Removed $label"
    done
    echo "Logs were kept in $LOG_DIR"
}

cmd_status() {
    local label plist
    for label in "$RUNNER_LABEL" "$API_LABEL"; do
        plist="$(plist_path "$label")"
        echo "== $label"
        if [ ! -f "$plist" ]; then
            echo "  not installed"
            continue
        fi
        if [ "$DRY_RUN" = "1" ]; then
            echo "  installed (dry run: launchctl skipped)"
            continue
        fi
        if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
            launchctl print "gui/$UID_NUM/$label" \
                | grep -E '^[[:space:]]*(state|pid|last exit code) =' || true
        else
            echo "  installed but not loaded (run: install)"
        fi
    done
}

usage() {
    echo "usage: $(basename "$0") {install|uninstall|status}" >&2
    exit 2
}

[ "$#" -eq 1 ] || usage
case "$1" in
    install) cmd_install ;;
    uninstall) cmd_uninstall ;;
    status) cmd_status ;;
    *) usage ;;
esac
