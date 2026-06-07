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
SUCCESS_MARKER="$INGESTION/checkpoints/nightly_ingestion_success.json"
AGENT_SESSION_STATUS="$INGESTION/checkpoints/agent_sessions_last_run.json"

# launchd provides a minimal PATH. Include the user-managed tool locations so
# Claude CLI, node-based hooks, Homebrew tools, and local helpers are visible.
export PATH="/Users/arjundivecha/.nvm/versions/node/v24.12.0/bin:/Users/arjundivecha/.local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Prefer the repo-level consolidated env file. Individual Python modules still
# load ingestion/.env for backward compatibility, but this gives launchd one
# place for all local runtime keys.
if [ -f "$REPO/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +a
fi

# Ingestion inference uses Claude Agent SDK subscription auth by default.
# The SDK wrapper scrubs ANTHROPIC_API_KEY from its child process unless this
# opt-in is set to 1 for a deliberate pay-as-you-go fallback run.
export PKS_ALLOW_ANTHROPIC_API_FALLBACK="${PKS_ALLOW_ANTHROPIC_API_FALLBACK:-0}"
export PKS_SDK_MAX_TURNS="${PKS_SDK_MAX_TURNS:-4}"
export PKS_SDK_MAX_BUDGET_USD="${PKS_SDK_MAX_BUDGET_USD:-0.25}"
export PKS_SDK_MODEL="${PKS_SDK_MODEL:-sonnet}"
export PKS_SDK_PREFLIGHT_ATTEMPTS="${PKS_SDK_PREFLIGHT_ATTEMPTS:-2}"
export PKS_AGENT_SESSION_DISTILL_RETRY_LIMIT="${PKS_AGENT_SESSION_DISTILL_RETRY_LIMIT:-1}"
export PKS_AGENT_SESSION_STATUS_FILE="${PKS_AGENT_SESSION_STATUS_FILE:-$AGENT_SESSION_STATUS}"
export DREAM_ALLOW_ANTHROPIC_API_FALLBACK="${DREAM_ALLOW_ANTHROPIC_API_FALLBACK:-0}"

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
log "LLM billing policy: Agent SDK primary; API fallback opt-in=${PKS_ALLOW_ANTHROPIC_API_FALLBACK}; Dream API fallback opt-in=${DREAM_ALLOW_ANTHROPIC_API_FALLBACK}"

# Pre-flight: verify ingestion venv exists
if [ ! -f "$VENV/bin/python" ]; then
    log "FATAL: ingestion venv missing at $VENV"
    log "Fix: cd $INGESTION && python3 -m venv .venv && .venv/bin/pip install -r ../distillation/requirements.txt"
    exit 1
fi
log "Ingestion venv: OK"

# Pre-flight: verify Claude CLI is available for Dream judge subscription path.
if ! command -v claude >/dev/null 2>&1; then
    log "FATAL: claude CLI not on PATH; Dream judge would fall back to API."
    exit 1
fi
log "Claude CLI: $(command -v claude)"

# Pre-flight: run one real subscription-backed SDK inference before fetching
# external sources. `claude --version` and SDK import can pass even when the
# local OAuth/subscription context is unavailable to the process.
if ! "$VENV/bin/python" "$REPO/scripts/check_claude_sdk_auth.py" >/dev/null 2>&1
then
    log "FATAL: Claude Agent SDK real inference preflight failed."
    log "This overnight runner must execute on the local Mac context where Claude subscription auth works."
    exit 1
fi
log "Claude Agent SDK inference preflight: OK (model=${PKS_SDK_MODEL})"

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
"$VENV/bin/python" "$INGESTION/agent_sessions/run.py" 2>&1 | tee -a "$LOG"
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

mkdir -p "$(dirname "$SUCCESS_MARKER")"
AGENT_SESSION_REDIS_WRITE_FAILED_JSON="null"
if [ -f "$PKS_AGENT_SESSION_STATUS_FILE" ]; then
    AGENT_SESSION_REDIS_WRITE_FAILED_JSON=$("$VENV/bin/python" - "$PKS_AGENT_SESSION_STATUS_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        value = json.load(fh).get("redis_write_failed")
except Exception:
    value = None

if value is True:
    print("true")
elif value is False:
    print("false")
else:
    print("null")
PY
)
fi
cat > "$SUCCESS_MARKER" <<EOF
{
  "completed_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "log": "$LOG",
  "sdk_model": "$PKS_SDK_MODEL",
  "api_fallback": "$PKS_ALLOW_ANTHROPIC_API_FALLBACK",
  "dream_api_fallback": "$DREAM_ALLOW_ANTHROPIC_API_FALLBACK",
  "agent_session_status_file": "$PKS_AGENT_SESSION_STATUS_FILE",
  "agent_session_redis_write_failed": $AGENT_SESSION_REDIS_WRITE_FAILED_JSON
}
EOF

log "=== Nightly ingestion complete ==="
