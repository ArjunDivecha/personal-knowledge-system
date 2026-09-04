#!/bin/bash
# =============================================================================
# SCRIPT NAME: kick_source_first_rebuild.sh
# =============================================================================
#
# DESCRIPTION:
# Cadence guard for the PKS "Source-First Memory Rebuild" GitHub Action. GitHub
# owns the build -> gate -> promote pipeline, but its cron (17 */2 * * *) is
# best-effort: measured 2026-08-24..09-04 it fired ~5.5x/day instead of 12x
# (median gap 4.2h, max 12.6h). launchd runs this script every 30 minutes
# (com.arjundivecha.pks-rebuild-kicker). It:
#   1. asks GitHub for the newest runs of the workflow;
#   2. if nothing is queued/in progress and the newest run started more than
#      KICK_AFTER_SECONDS ago (default 7200), dispatches `workflow_dispatch`
#      with publish=true (identical to a scheduled run);
#   3. reads the Worker /health endpoint and records the serving generation's
#      age; ok=false when it exceeds STALE_AFTER_SECONDS (default 21600 = 6h)
#      or the Worker reports non-green;
#   4. writes a marker JSON that Overseer evaluates (marker type, freshness
#      max_age_h 1.5).
# It never builds, stages, or promotes locally (AGENTS.md invariant).
#
# INPUT FILES:
# - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env
#     GITHUB_TOKEN (exported as GH_TOKEN so `gh` does not need the keychain
#     from a launchd context).
# - https://mcp.dancing-ganesh.com/health  (read-only)
# - GitHub API via `gh` for ArjunDivecha/personal-knowledge-system
#
# OUTPUT FILES:
# - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/source_first_kicker.json
#     Overseer marker: completed_at, ok, failed_stages, warn_stages, dispatched,
#     newest_run_age_min, serving_age_min, generation, health_status.
# - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/source_first_kicker.log
#     Append-only log, truncated when it passes ~5 MB.
#
# VERSION: 1.0  |  LAST UPDATED: 2026-09-04  |  AUTHOR: Claude Code (Fable 5.1) for Arjun
#
# USAGE:
#   bash scripts/kick_source_first_rebuild.sh            # normal (launchd)
#   DRY_RUN=1 bash scripts/kick_source_first_rebuild.sh  # evaluate, never dispatch
# =============================================================================

set -uo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
GH_REPO="ArjunDivecha/personal-knowledge-system"
WORKFLOW="source-first-rebuild.yml"
HEALTH_URL="https://mcp.dancing-ganesh.com/health"
MARKER="$REPO/ingestion/checkpoints/source_first_kicker.json"
LOG="$REPO/ingestion/logs/source_first_kicker.log"
KICK_AFTER_SECONDS="${KICK_AFTER_SECONDS:-7200}"
STALE_AFTER_SECONDS="${STALE_AFTER_SECONDS:-21600}"
DRY_RUN="${DRY_RUN:-0}"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export GIT_TERMINAL_PROMPT=0
export GH_PROMPT_DISABLED=1

mkdir -p "$(dirname "$MARKER")" "$(dirname "$LOG")"
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5000000 ]; then : > "$LOG"; fi
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }

if [ -f "$REPO/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +a
fi
if [ -z "${GH_TOKEN:-}" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
    export GH_TOKEN="$GITHUB_TOKEN"
fi

FAILED=()
WARN=()
DISPATCHED=false
NEWEST_AGE_MIN="null"
SERVING_AGE_MIN="null"
GENERATION="null"
HEALTH_STATUS="null"

# --- 1. GitHub run state -----------------------------------------------------
RUNS_JSON=$(gh run list -R "$GH_REPO" --workflow="$WORKFLOW" --limit 8 \
    --json status,createdAt,conclusion,event 2>>"$LOG") || RUNS_JSON=""
if [ -z "$RUNS_JSON" ]; then
    FAILED+=("github_run_list")
    log "ERROR: gh run list failed"
else
    read -r ACTIVE NEWEST_AGE_SEC <<<"$(python3 - "$RUNS_JSON" <<'PY'
import json, sys
from datetime import datetime, timezone
runs = json.loads(sys.argv[1])
active = any(r.get("status") in ("queued", "in_progress", "waiting", "pending", "requested") for r in runs)
newest = max((datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")) for r in runs), default=None)
age = int((datetime.now(timezone.utc) - newest).total_seconds()) if newest else 10**9
print("1" if active else "0", age)
PY
)"
    NEWEST_AGE_MIN=$((NEWEST_AGE_SEC / 60))
    if [ "$ACTIVE" = "1" ]; then
        log "run queued/in progress; newest started ${NEWEST_AGE_MIN}m ago; no dispatch"
    elif [ "$NEWEST_AGE_SEC" -gt "$KICK_AFTER_SECONDS" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            log "DRY_RUN: would dispatch (newest run ${NEWEST_AGE_MIN}m ago > $((KICK_AFTER_SECONDS / 60))m)"
        elif gh workflow run "$WORKFLOW" -R "$GH_REPO" --ref main -f publish=true >>"$LOG" 2>&1; then
            DISPATCHED=true
            WARN+=("dispatched_after_${NEWEST_AGE_MIN}m_gap")
            log "DISPATCHED workflow_dispatch: newest run was ${NEWEST_AGE_MIN}m ago"
        else
            FAILED+=("workflow_dispatch")
            log "ERROR: gh workflow run failed"
        fi
    else
        log "newest run ${NEWEST_AGE_MIN}m ago; within cadence; no dispatch"
    fi
fi

# --- 2. Serving freshness from the Worker -----------------------------------
HEALTH_JSON=$(curl -s -m 20 "$HEALTH_URL") || HEALTH_JSON=""
if [ -z "$HEALTH_JSON" ]; then
    FAILED+=("health_unreachable")
    log "ERROR: /health unreachable"
else
    read -r HEALTH_STATUS SERVING_AGE_SEC GENERATION <<<"$(python3 - "$HEALTH_JSON" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    sf = d.get("source_first") or {}
    age = (sf.get("freshness") or {}).get("age_seconds")
    print(sf.get("overall_status") or "unknown", age if isinstance(age, int) else -1, sf.get("generation") or "unknown")
except Exception:
    print("unparseable", -1, "unknown")
PY
)"
    if [ "$SERVING_AGE_SEC" -lt 0 ]; then
        FAILED+=("health_unparseable")
        log "ERROR: /health payload unparseable"
    else
        SERVING_AGE_MIN=$((SERVING_AGE_SEC / 60))
        if [ "$SERVING_AGE_SEC" -gt "$STALE_AFTER_SECONDS" ]; then
            FAILED+=("serving_generation_stale_${SERVING_AGE_MIN}m")
            log "STALE: serving generation $GENERATION is ${SERVING_AGE_MIN}m old (> $((STALE_AFTER_SECONDS / 60))m)"
        elif [ "$HEALTH_STATUS" != "green" ]; then
            FAILED+=("health_status_${HEALTH_STATUS}")
            log "NOT GREEN: /health overall_status=$HEALTH_STATUS"
        else
            log "serving $GENERATION is ${SERVING_AGE_MIN}m old; health green"
        fi
    fi
fi

# --- 3. Marker for Overseer --------------------------------------------------
OK=true; [ "${#FAILED[@]}" -ne 0 ] && OK=false
python3 - "$MARKER" "$OK" "$DISPATCHED" "$NEWEST_AGE_MIN" "$SERVING_AGE_MIN" "$GENERATION" "$HEALTH_STATUS" \
    "$(printf '%s\n' "${FAILED[@]:-}")" "$(printf '%s\n' "${WARN[@]:-}")" <<'PY'
import json, os, sys, tempfile
from datetime import datetime, timezone
path, ok, dispatched, newest, serving, gen, health, failed, warn = sys.argv[1:10]
def num(v):
    try: return int(v)
    except ValueError: return None
payload = {
    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "ok": ok == "true",
    "dispatched": dispatched == "true",
    "newest_run_age_min": num(newest),
    "serving_age_min": num(serving),
    "generation": gen,
    "health_status": health,
    "failed_stages": [x for x in failed.splitlines() if x.strip()],
    "warn_stages": [x for x in warn.splitlines() if x.strip()],
}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".kicker-", suffix=".json")
with os.fdopen(fd, "w") as fh:
    json.dump(payload, fh, indent=2)
os.replace(tmp, path)
PY
log "marker written ok=$OK dispatched=$DISPATCHED"
[ "$OK" = "true" ] && exit 0 || exit 1
