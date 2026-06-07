#!/bin/bash
# =============================================================================
# OAUTH / BROWSER STORM WATCHER
# =============================================================================
# Guards a nightly ingestion run against the regression that motivated the
# no-browser SDK preflight fix: a non-interactive Claude CLI starting an OAuth
# *browser* login flow (listener on localhost:18043, oauth/callback URLs) and
# multiplying into a "session storm".
#
# This watcher polls every few seconds while the run is in progress. If it sees
# either (a) a listener on port 18043, or (b) a process whose command line
# mentions oauth/callback or :18043, it:
#   1. records the offender to the event log (PID + command, callback CODE/STATE
#      redacted), and
#   2. SIGKILLs ONLY that offender's process group, so the storm cannot grow.
# It deliberately uses narrow patterns; it never runs `pkill claude` broadly
# (that would kill the user's interactive Claude sessions).
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

cleanup() { log "watcher stopping (detections=$DETECTIONS)"; exit 0; }
trap cleanup SIGTERM SIGINT

DETECTIONS=0
log "watcher started (poll=${POLL_SECONDS}s, port=18043). Clean = no output between start and stop."

while true; do
    [ -f "$STOP_FILE" ] && cleanup

    # (a) Listener on the OAuth callback port.
    LISTENERS=$(lsof -tiTCP:18043 -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$LISTENERS" ]; then
        DETECTIONS=$((DETECTIONS+1))
        for pid in $LISTENERS; do
            cmd=$(ps -p "$pid" -o command= 2>/dev/null | sed -E 's/(code|state)=[^ &]*/\1=REDACTED/g')
            log "DETECTED listener on 18043 pid=$pid cmd=${cmd:-unknown} -> killing process group"
            pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
            [ -n "$pgid" ] && kill -KILL -- "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
        done
    fi

    # (b) Any process advertising an oauth callback / port 18043 in its args.
    OFFENDERS=$(ps -axo pid,command 2>/dev/null | grep -E 'oauth/callback|localhost:18043|:18043/oauth' | grep -v 'grep' | grep -v 'oauth_storm_watcher' | awk '{print $1}' || true)
    if [ -n "$OFFENDERS" ]; then
        DETECTIONS=$((DETECTIONS+1))
        for pid in $OFFENDERS; do
            cmd=$(ps -p "$pid" -o command= 2>/dev/null | sed -E 's/(code|state)=[^ &]*/\1=REDACTED/g')
            log "DETECTED oauth/callback process pid=$pid cmd=${cmd:-unknown} -> killing process group"
            pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
            [ -n "$pgid" ] && kill -KILL -- "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
        done
    fi

    sleep "$POLL_SECONDS"
done
