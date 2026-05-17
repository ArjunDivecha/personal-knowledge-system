#!/bin/bash
# =============================================================================
# NIGHTLY KNOWLEDGE INGESTION RUNNER
# =============================================================================
# Called by launchd via com.arjun.knowledge-ingestion.plist.
# Wraps all three ingestion pipelines sequentially.
# caffeinate -i is applied by the plist so the Mac stays awake for the full run.
#
# Logs to: ingestion/logs/nightly/YYYY-MM-DD.log
# =============================================================================

set -euo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
INGESTION="$REPO/ingestion"
VENV="$INGESTION/.venv"
LOG_DIR="$INGESTION/logs/nightly"
DATE=$(date +%Y-%m-%d)
LOG="$LOG_DIR/$DATE.log"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== Nightly ingestion started ==="

# Pre-flight: verify Agent SDK is importable
if ! ~/agent-sdk-venv/bin/python3 -c "from claude_agent_sdk import query" 2>/dev/null; then
    log "FATAL: claude-agent-sdk not importable."
    log "Fix: ~/agent-sdk-venv/bin/pip install --upgrade claude-agent-sdk"
    exit 1
fi
log "Agent SDK: OK"

# Pre-flight: verify ingestion venv exists
if [ ! -f "$VENV/bin/python" ]; then
    log "FATAL: ingestion venv missing at $VENV"
    log "Fix: cd $INGESTION && python3 -m venv .venv && .venv/bin/pip install -r ../distillation/requirements.txt"
    exit 1
fi
log "Ingestion venv: OK"

# --------------------------------------------------------------------------
# Twitter
# --------------------------------------------------------------------------
log "--- Twitter ingestion starting ---"
"$VENV/bin/python" "$INGESTION/twitter/run.py" --require-redis-state 2>&1 | tee -a "$LOG"
log "--- Twitter ingestion done ---"

# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------
log "--- GitHub ingestion starting ---"
"$VENV/bin/python" "$INGESTION/github/run.py" 2>&1 | tee -a "$LOG"
log "--- GitHub ingestion done ---"

# --------------------------------------------------------------------------
# Agent Sessions
# --------------------------------------------------------------------------
log "--- Agent sessions ingestion starting ---"
"$VENV/bin/python" "$INGESTION/agent_sessions/run.py" --require-redis-state 2>&1 | tee -a "$LOG"
log "--- Agent sessions ingestion done ---"

# --------------------------------------------------------------------------
# Dream judge (border-case ops queued by the Cloudflare Worker)
# Uses `claude` CLI for subscription billing; falls back to Anthropic API
# with a logged warning if the CLI is unavailable. Always exits 0 unless
# something catastrophic happens — a stalled judge queue is not fatal to
# nightly ingestion.
# --------------------------------------------------------------------------
log "--- Dream judge starting ---"
"$VENV/bin/python" "$INGESTION/dream_judge/run.py" 2>&1 | tee -a "$LOG" || log "Dream judge exited with non-zero status (see log)"
log "--- Dream judge done ---"

# --------------------------------------------------------------------------
# Log rotation: keep 30 days
# --------------------------------------------------------------------------
find "$LOG_DIR" -name "*.log" -mtime +30 -delete

log "=== Nightly ingestion complete ==="
