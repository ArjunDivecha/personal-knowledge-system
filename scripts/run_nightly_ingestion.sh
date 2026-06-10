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

# Ingestion inference uses Claude Agent SDK subscription auth first. If the real
# SDK preflight fails and the project Anthropic key is configured, this wrapper
# switches to API fallback instead of skipping the overnight run.
export PKS_ALLOW_ANTHROPIC_API_FALLBACK="${PKS_ALLOW_ANTHROPIC_API_FALLBACK:-0}"
export PKS_SDK_MAX_TURNS="${PKS_SDK_MAX_TURNS:-4}"
export PKS_SDK_MAX_BUDGET_USD="${PKS_SDK_MAX_BUDGET_USD:-0.25}"
export PKS_API_FALLBACK_RESERVE_USD="${PKS_API_FALLBACK_RESERVE_USD:-0.25}"
export PKS_API_FALLBACK_RUN_MAX_BUDGET_USD="${PKS_API_FALLBACK_RUN_MAX_BUDGET_USD:-5.00}"
export PKS_API_FALLBACK_MAX_CALLS="${PKS_API_FALLBACK_MAX_CALLS:-200}"
export PKS_API_FALLBACK_BUDGET_FILE="${PKS_API_FALLBACK_BUDGET_FILE:-$INGESTION/checkpoints/api_fallback_budget_$DATE.json}"
export PKS_SDK_MODEL="${PKS_SDK_MODEL:-sonnet}"
export PKS_SDK_PREFLIGHT_ATTEMPTS="${PKS_SDK_PREFLIGHT_ATTEMPTS:-2}"
export PKS_AGENT_SESSION_DISTILL_RETRY_LIMIT="${PKS_AGENT_SESSION_DISTILL_RETRY_LIMIT:-2}"
export PKS_AGENT_SESSION_STATUS_FILE="${PKS_AGENT_SESSION_STATUS_FILE:-$AGENT_SESSION_STATUS}"
export DREAM_ALLOW_ANTHROPIC_API_FALLBACK="${DREAM_ALLOW_ANTHROPIC_API_FALLBACK:-0}"

# Hard no-browser guard for the whole unattended (launchd) run. Subscription
# auth must already be present; if it is not, the SDK preflight fails fast and we
# route to API fallback. Never open an interactive OAuth/login browser window.
export BROWSER="${BROWSER:-/usr/bin/false}"
export GIT_TERMINAL_PROMPT="${GIT_TERMINAL_PROMPT:-0}"
# 4.3 — CI=1 enables hardened timeouts and non-interactive modes run-wide.
# Previously only the preflight check exported this; now it covers all stages.
export CI="${CI:-1}"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# ---------------------------------------------------------------------------
# Fault isolation: each ingestion pipeline runs as an independent "stage". A
# stage failure is captured and logged but MUST NOT abort the rest of the night
# (the historical bug: one repo's bad README aborted GitHub, and `set -e` then
# skipped agent-sessions + dream). STAGE_STATUS records "name=rc" for every
# stage; the run summary and success marker report all of them. Failures are
# surfaced loudly (FAILED line + non-zero overall exit), never silently masked.
# ---------------------------------------------------------------------------
STAGE_STATUS=()

run_stage() {
    local name="$1"; shift
    log "--- $name starting ---"
    local rc=0
    set +e
    "$@" 2>&1 | tee -a "$LOG"
    rc=${PIPESTATUS[0]}
    set -e
    if [ "$rc" -ne 0 ]; then
        log "--- $name FAILED (exit $rc); continuing with remaining stages ---"
    else
        log "--- $name done ---"
    fi
    STAGE_STATUS+=("$name=$rc")
    return 0
}

log "=== Nightly ingestion started ==="

# Pre-flight: verify Agent SDK is importable
if ! ~/agent-sdk-venv/bin/python3 -c "from claude_agent_sdk import query" 2>/dev/null; then
    log "FATAL: claude-agent-sdk not importable."
    log "Fix: ~/agent-sdk-venv/bin/pip install --upgrade claude-agent-sdk"
    exit 1
fi
log "Agent SDK: OK"
log "LLM billing policy: Agent SDK primary; API fallback route=${PKS_ALLOW_ANTHROPIC_API_FALLBACK}; model=${PKS_SDK_MODEL}; per-call budget=${PKS_SDK_MAX_BUDGET_USD}; fallback reserve=${PKS_API_FALLBACK_RESERVE_USD}; fallback run budget=${PKS_API_FALLBACK_RUN_MAX_BUDGET_USD}; fallback call cap=${PKS_API_FALLBACK_MAX_CALLS}; fallback budget file=${PKS_API_FALLBACK_BUDGET_FILE}; Dream API fallback=${DREAM_ALLOW_ANTHROPIC_API_FALLBACK}"

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
if "$VENV/bin/python" "$REPO/scripts/check_claude_sdk_auth_noninteractive.py" >/dev/null 2>&1
then
    export PKS_ALLOW_ANTHROPIC_API_FALLBACK=0
    log "Claude Agent SDK inference preflight: OK (model=${PKS_SDK_MODEL}); using SDK billing route."
else
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        log "FATAL: Claude Agent SDK real inference preflight failed and ANTHROPIC_API_KEY is not set."
        log "API fallback cannot run without the project Anthropic key."
        exit 1
    fi
    export PKS_ALLOW_ANTHROPIC_API_FALLBACK=1
    log "WARNING: Claude Agent SDK real inference preflight failed; using Anthropic API fallback for this overnight run."
    log "API fallback controls: model=${PKS_SDK_MODEL}; per-call budget=${PKS_SDK_MAX_BUDGET_USD}; reserve=${PKS_API_FALLBACK_RESERVE_USD}; run budget=${PKS_API_FALLBACK_RUN_MAX_BUDGET_USD}; call cap=${PKS_API_FALLBACK_MAX_CALLS}; budget file=${PKS_API_FALLBACK_BUDGET_FILE}."
fi

# --------------------------------------------------------------------------
# Each pipeline is an isolated stage: a failure in one does not stop the others.
# Twitter / GitHub / Agent sessions are "hard" stages (a failure fails the run
# overall). Dream judge is "soft" — a stalled judge queue is not fatal to
# nightly ingestion (see overall-status calc below).
# --------------------------------------------------------------------------
run_stage "Twitter ingestion" "$VENV/bin/python" "$INGESTION/twitter/run.py" --require-redis-state

run_stage "GitHub ingestion" "$VENV/bin/python" "$INGESTION/github/run.py"

run_stage "Agent sessions ingestion" "$VENV/bin/python" "$INGESTION/agent_sessions/run.py"

# Dream judge (border-case ops queued by the Cloudflare Worker). Uses the
# `claude` CLI for subscription billing.
run_stage "Dream judge" "$VENV/bin/python" "$INGESTION/dream_judge/run.py"

# --------------------------------------------------------------------------
# Compute overall status. Hard stages (Twitter/GitHub/Agent sessions) failing
# fails the run; the soft Dream judge stage does not.
# --------------------------------------------------------------------------
HARD_FAILURES=()
SOFT_FAILURES=()
for entry in "${STAGE_STATUS[@]}"; do
    name="${entry%=*}"
    rc="${entry##*=}"
    if [ "$rc" -ne 0 ]; then
        if [ "$name" = "Dream judge" ]; then
            SOFT_FAILURES+=("$name(rc=$rc)")
        else
            HARD_FAILURES+=("$name(rc=$rc)")
        fi
    fi
done

OVERALL_OK="true"
if [ "${#HARD_FAILURES[@]}" -ne 0 ]; then
    OVERALL_OK="false"
    log "NIGHTLY RESULT: FAILED — hard stage failures: ${HARD_FAILURES[*]}"
elif [ "${#SOFT_FAILURES[@]}" -ne 0 ]; then
    log "NIGHTLY RESULT: OK with tolerated soft failures: ${SOFT_FAILURES[*]}"
else
    log "NIGHTLY RESULT: OK — all stages succeeded"
fi

# Build JSON for per-stage exit codes and the failed-stage list.
STAGES_JSON=$(printf '%s\n' "${STAGE_STATUS[@]}" | "$VENV/bin/python" -c '
import json, sys
stages = {}
for line in sys.stdin.read().splitlines():
    if not line:
        continue
    name, _, rc = line.rpartition("=")
    try:
        stages[name] = int(rc)
    except ValueError:
        stages[name] = rc
print(json.dumps(stages))
')
FAILED_STAGES_JSON=$(printf '%s\n' "${HARD_FAILURES[@]:-}" "${SOFT_FAILURES[@]:-}" | "$VENV/bin/python" -c '
import json, sys
items = [line for line in sys.stdin.read().splitlines() if line.strip()]
print(json.dumps(items))
')

# --------------------------------------------------------------------------
# Log rotation: keep 30 days
# --------------------------------------------------------------------------
find "$LOG_DIR" -name "*.log" -mtime +30 -delete
find "$INGESTION/checkpoints" -name "api_fallback_budget_*.json" -mtime +30 -delete
find "$INGESTION/checkpoints" -name "api_fallback_budget_*.json.lock" -mtime +30 -delete

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
  "agent_session_redis_write_failed": $AGENT_SESSION_REDIS_WRITE_FAILED_JSON,
  "ok": $OVERALL_OK,
  "stages": $STAGES_JSON,
  "failed_stages": $FAILED_STAGES_JSON
}
EOF

log "=== Nightly ingestion complete ==="

# Loud overall exit code: non-zero if any hard stage failed, so launchd, the
# health monitor, and any operator see the failure. The marker above always
# records exactly which stages failed, even on a non-zero exit.
if [ "$OVERALL_OK" != "true" ]; then
    exit 1
fi
