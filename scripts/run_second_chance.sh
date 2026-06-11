#!/bin/bash
# =============================================================================
# SCRIPT NAME: run_second_chance.sh
# =============================================================================
#
# DESCRIPTION:
# 02:00 "second chance" for the nightly knowledge ingestion. Called by launchd
# via com.arjun.knowledge-ingestion-2am.plist. If the 23:00 run succeeded,
# this exits immediately (the common case — zero cost). If the 23:00 run
# failed or never completed, it re-runs the full nightly ingestion. All
# ingestion pipelines are incremental and idempotent (Twitter dedup markers,
# GitHub per-repo baselines, agent-session byte offsets), so a rerun only
# redoes the work that actually failed.
#
# This converts "transient failure at 23:00 = dead night discovered next
# morning" into "transient failure at 23:00 = automatic recovery at 02:00,
# noted in the 07:00 NightWatch digest".
#
# INPUT FILES:
# - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/nightly_ingestion_success.json
#     Success marker from the 23:00 run (completed_at, ok).
#
# OUTPUT FILES:
# - /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/nightly/second_chance_YYYY-MM-DD.log
#     This script's own decision log.
# - Everything run_nightly_ingestion.sh writes (nightly log, success marker),
#   if a rerun is triggered.
#
# VERSION: 1.0  |  LAST UPDATED: 2026-06-10  |  AUTHOR: Claude Code for Arjun
#
# USAGE: invoked by launchd daily at 02:00; safe to run manually any time.
# =============================================================================

set -uo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
MARKER="$REPO/ingestion/checkpoints/nightly_ingestion_success.json"
NIGHTLY="$REPO/scripts/run_nightly_ingestion.sh"
LOG_DIR="$REPO/ingestion/logs/nightly"
LOG="$LOG_DIR/second_chance_$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== Second-chance check starting ==="

# Never start a second copy while the 23:00 run is still going.
if pgrep -f "run_nightly_ingestion.sh" >/dev/null 2>&1; then
    log "Nightly ingestion still running — not starting a second copy. Exiting."
    exit 0
fi

# Decide whether the 23:00 run completed successfully within the last 4 hours.
NEED_RERUN=$(/usr/bin/python3 - "$MARKER" <<'PY'
import json, sys
from datetime import datetime, timezone

try:
    with open(sys.argv[1]) as fh:
        marker = json.load(fh)
    completed = datetime.fromisoformat(marker["completed_at"].replace("Z", "+00:00"))
    age_h = (datetime.now(timezone.utc) - completed).total_seconds() / 3600
    ok = bool(marker.get("ok"))
    if ok and age_h <= 4:
        print("no")           # tonight's run succeeded
    elif not ok and age_h <= 4:
        print("yes:failed")   # tonight's run failed
    else:
        print("yes:missing")  # no run tonight (crashed before marker / never started)
except Exception as exc:
    print(f"yes:marker_error:{exc}")
PY
)

if [ "$NEED_RERUN" = "no" ]; then
    log "23:00 run succeeded — nothing to do."
    exit 0
fi

log "23:00 run needs recovery ($NEED_RERUN) — re-running nightly ingestion."
"$NIGHTLY"
RC=$?
log "Second-chance nightly ingestion finished with exit $RC."
exit $RC
