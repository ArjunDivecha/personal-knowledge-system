#!/bin/bash
# =============================================================================
# OAUTH / BROWSER STORM WATCHER  (DETECT-AND-ALERT ONLY)
# =============================================================================
# Watches a nightly ingestion run for the regression that motivated the
# no-browser SDK preflight fix: a non-interactive Claude CLI starting an OAuth
# *browser* login flow (listener on localhost:18043, oauth/callback URLs) and
# multiplying into a "session storm".
#
# DESIGN NOTE — why this only DETECTS and never kills:
# The storm is now prevented at the source by the no-browser preflight
# (scripts/check_claude_sdk_auth_noninteractive.py), so this watcher is a
# secondary tripwire, not the primary defense. An EARLIER version of this script
# actively SIGKILLed process groups on port 18043. That was unsafe: port 18043 is
# also used legitimately by the `mcp-remote` knowledge-Worker client AND by the
# no-browser preflight's own sandboxed probe, and group-kills took down the
# legitimate clients and even the nightly run itself. Killing is therefore
# wrong here. This watcher only OBSERVES and logs loudly; the health monitor's
# `verify` step fails the run if any oauth/18043 reference shows up in the run
# log. If you ever need to stop a real storm, do it by hand with a narrow,
# PID-specific kill after inspecting the logged offenders.
#
# INPUT FILES:
# - None.
# OUTPUT FILES:
# - Event log written to the path given by $1 (default:
#   /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/nightly/oauth_storm_watch.log)
#
# USAGE:
#   bash scripts/oauth_storm_watcher.sh [EVENT_LOG_PATH] [POLL_SECONDS]
#   (intended to run in the background alongside run_nightly_ingestion.sh)
#
# STOP: the watcher exits cleanly when it receives SIGTERM/SIGINT, or when a
# sentinel file named "<EVENT_LOG_PATH>.stop" appears.
# =============================================================================

set -uo pipefail

EVENT_LOG="${1:-/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/nightly/oauth_storm_watch.log}"
POLL_SECONDS="${2:-3}"
STOP_FILE="${EVENT_LOG}.stop"

mkdir -p "$(dirname "$EVENT_LOG")"
rm -f "$STOP_FILE"

ts() { date '+%Y-%m-%dT%H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$EVENT_LOG"; }

cleanup() { log "watcher stopping (alerts=$ALERTS)"; exit 0; }
trap cleanup SIGTERM SIGINT

# Commands that legitimately touch this OAuth infra and are NOT the storm:
#   - mcp-remote                      the interactive knowledge-Worker client
#   - check_claude_sdk_auth*          the no-browser preflight + its own probe
#   - oauth_storm_watcher             this script
# We baseline the PIDs already listening on 18043 at startup, and never flag the
# allowlisted commands. Anything else on 18043 / oauth-callback is a real alert.
SAFE_CMD_REGEX='mcp-remote|check_claude_sdk_auth|oauth_storm_watcher'

is_safe_cmd() { printf '%s' "$1" | grep -Eq "$SAFE_CMD_REGEX"; }

BASELINE_18043=" $(lsof -tiTCP:18043 -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')"
log "watcher started (poll=${POLL_SECONDS}s, port=18043, DETECT-ONLY). Baseline listeners: ${BASELINE_18043:-none}. Clean = no ALERT lines between start and stop."

ALERTS=0

alert() {
    # $1 = pid, $2 = reason. Detect-and-log only; this watcher NEVER kills.
    local pid="$1" reason="$2"
    case "$BASELINE_18043" in
        *" $pid "*) return 0 ;;  # pre-existing legitimate listener
    esac
    local cmd
    cmd=$(ps -p "$pid" -o command= 2>/dev/null | sed -E 's/(code|state)=[^ &]*/\1=REDACTED/g')
    [ -z "$cmd" ] && return 0
    if is_safe_cmd "$cmd"; then
        return 0  # legitimate OAuth-infra user, not the storm
    fi
    ALERTS=$((ALERTS+1))
    log "ALERT $reason pid=$pid cmd=$cmd (DETECT-ONLY: not killed; inspect manually if unexpected)"
}

while true; do
    [ -f "$STOP_FILE" ] && cleanup

    # (a) NEW (non-baseline, non-allowlisted) listener on the OAuth callback port.
    for pid in $(lsof -tiTCP:18043 -sTCP:LISTEN 2>/dev/null || true); do
        alert "$pid" "new listener on 18043"
    done

    # (b) A browser/oauth-callback process that is not part of the known infra.
    for pid in $(ps -axo pid,command 2>/dev/null \
        | grep -E 'oauth/callback|:18043/oauth' \
        | grep -vE 'grep|oauth_storm_watcher|mcp-remote|check_claude_sdk_auth' \
        | awk '{print $1}' || true); do
        alert "$pid" "oauth-callback process"
    done

    sleep "$POLL_SECONDS"
done
