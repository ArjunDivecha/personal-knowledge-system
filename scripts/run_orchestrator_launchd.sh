#!/bin/bash
# =============================================================================
# SCRIPT NAME: scripts/run_orchestrator_launchd.sh
# =============================================================================
#
# DESCRIPTION:
# launchd-facing wrapper for the PKS nightly orchestrator. Per the spec, the
# same binary handles all paths (scheduled run, on-load catch-up, periodic
# catch-up). Phase 1 default action is a SHADOW (non-mutating) run.
#
# *** NOT INSTALLED IN PHASE 1. *** No launchd plist points at this script yet
# (plist install is Phase 4). The existing nightly schedules remain the
# production source of truth until cutover (Phase 5). This file exists so the
# entrypoint and env are ready and reviewable.
#
# INPUT FILES:
# - knowledge-system/.env (runtime keys, sourced)
# - scripts/nightly_orchestrator.py (the orchestrator CLI)
#
# OUTPUT FILES:
# - ingestion/logs/orchestrator/launchd_YYYY-MM-DD.log (this wrapper's log)
# - everything scripts/nightly_orchestrator.py writes (ledger, reports, Redis)
#
# VERSION: 1.0 | LAST UPDATED: 2026-06-16 | AUTHOR: Claude Code for Arjun
#
# USAGE (manual, Phase 1):
#   scripts/run_orchestrator_launchd.sh run
#   scripts/run_orchestrator_launchd.sh resume
#   scripts/run_orchestrator_launchd.sh report
# =============================================================================
set -uo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
VENV="$REPO/ingestion/.venv"
PY="$VENV/bin/python"
LOG_DIR="$REPO/ingestion/logs/orchestrator"
LOG="$LOG_DIR/launchd_$(date +%Y-%m-%d).log"

export PATH="/Users/arjundivecha/.nvm/versions/node/v24.12.0/bin:/Users/arjundivecha/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# No-browser guards for any unattended auth check (preflight never opens a browser).
export CI="${CI:-1}"
export BROWSER="${BROWSER:-/usr/bin/false}"
export GIT_TERMINAL_PROMPT="${GIT_TERMINAL_PROMPT:-0}"

if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi

mkdir -p "$LOG_DIR"
ACTION="${1:-run}"; shift || true

case "$ACTION" in
  run)     CMD=(run --mode shadow --date auto) ;;   # Phase 1: shadow only
  resume)  CMD=(resume --date auto) ;;
  report)  CMD=(report --date auto) ;;
  preflight) CMD=(preflight) ;;
  *)       CMD=("$ACTION" "$@") ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] orchestrator ${CMD[*]}" | tee -a "$LOG"
"$PY" "$REPO/scripts/nightly_orchestrator.py" "${CMD[@]}" 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
