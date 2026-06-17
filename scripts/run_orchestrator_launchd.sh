#!/bin/bash
# =============================================================================
# SCRIPT NAME: scripts/run_orchestrator_launchd.sh
# =============================================================================
#
# DESCRIPTION:
# launchd-facing wrapper for the PKS nightly orchestrator. Phase 4 installs this
# as a SHADOW-VALIDATION sidecar (label com.arjun.pks-nightly-orchestrator.shadow)
# alongside — never replacing — the existing production schedules.
#
# The `supervise` action is the launchd entrypoint: it is idempotent and
# supervisory. On each ~30-min firing within the overnight window it maps to the
# correct night's run_date and run/resume/skips. Repeated firings are safe — a
# terminal ledger is a no-op, an incomplete one resumes the SAME dream_run_id,
# and outside the window it exits 0 without calling the orchestrator.
#
# SHADOW-ONLY GUARANTEES (every invocation):
#   PKS_ORCH_DREAM_CLIENT=http        -> the REAL async Dream Worker (not the
#                                        in-process Phase 1 simulator)
#   PKS_ORCH_ALLOW_MUTATION=0         -> no local ingestion mutation
#   PKS_NIGHTLY_SOURCE_OF_TRUTH=legacy-> old schedules remain user-facing truth
#   no --mode live anywhere; Worker live apply stays disabled (Phase 2 default)
#
# INPUT FILES:
# - knowledge-system/.env (runtime keys, sourced: DREAM_OPERATOR_TOKEN, UPSTASH_*)
# - scripts/nightly_orchestrator.py (the orchestrator CLI)
#
# OUTPUT FILES:
# - ingestion/logs/orchestrator/launchd_YYYY-MM-DD.log (this wrapper's log)
# - everything scripts/nightly_orchestrator.py writes (ledger, reports, Redis)
#
# VERSION: 2.0 (Phase 4) | LAST UPDATED: 2026-06-17 | AUTHOR: Claude Code for Arjun
#
# USAGE:
#   scripts/run_orchestrator_launchd.sh supervise   # launchd entrypoint
#   scripts/run_orchestrator_launchd.sh preflight | run | resume | report
# =============================================================================
set -uo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
VENV="$REPO/ingestion/.venv"
PY="$VENV/bin/python"
LOG_DIR="$REPO/ingestion/logs/orchestrator"
LOG="$LOG_DIR/launchd_$(date +%Y-%m-%d).log"

export PATH="/Users/arjundivecha/.nvm/versions/node/v24.12.0/bin:/Users/arjundivecha/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ── Phase 4 shadow-only defaults; hard guarantees are re-asserted after .env. ─
export PKS_ORCH_DREAM_CLIENT="${PKS_ORCH_DREAM_CLIENT:-http}"
export DREAM_MCP_BASE_URL="${DREAM_MCP_BASE_URL:-https://mcp.dancing-ganesh.com}"
export PKS_ORCH_ALLOW_MUTATION="${PKS_ORCH_ALLOW_MUTATION:-0}"
export PKS_NIGHTLY_SOURCE_OF_TRUTH="${PKS_NIGHTLY_SOURCE_OF_TRUTH:-legacy}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
# No-browser guards for unattended auth (preflight never opens a browser).
export CI="${CI:-1}"
export BROWSER="${BROWSER:-/usr/bin/false}"
export GIT_TERMINAL_PROMPT="${GIT_TERMINAL_PROMPT:-0}"

if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi
# Re-assert the Phase 4 env AFTER sourcing .env so the shadow guarantees win
# even if .env happens to set any of them.
export PKS_ORCH_DREAM_CLIENT="http"
export DREAM_MCP_BASE_URL="https://mcp.dancing-ganesh.com"
export PKS_ORCH_ALLOW_MUTATION="0"
export PKS_NIGHTLY_SOURCE_OF_TRUTH="legacy"

mkdir -p "$LOG_DIR"
ACTION="${1:-supervise}"; shift || true

case "$ACTION" in
  supervise) CMD=(supervise) ;;                      # launchd entrypoint
  run)       CMD=(run --mode shadow --date auto) ;;  # shadow only, never live
  resume)    CMD=(resume --date auto) ;;
  report)    CMD=(report --date auto) ;;
  preflight) CMD=(preflight) ;;
  *)         CMD=("$ACTION" "$@") ;;
esac

echo "[$(date '+%Y-%m-%d %H:%M:%S')] orchestrator ${CMD[*]} (DREAM_CLIENT=$PKS_ORCH_DREAM_CLIENT MUTATION=$PKS_ORCH_ALLOW_MUTATION SOT=$PKS_NIGHTLY_SOURCE_OF_TRUTH)" | tee -a "$LOG"
"$PY" "$REPO/scripts/nightly_orchestrator.py" "${CMD[@]}" 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
