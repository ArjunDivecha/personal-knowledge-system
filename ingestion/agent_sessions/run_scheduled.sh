#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INGESTION_DIR="$REPO_ROOT/ingestion"
PYTHON_BIN="$REPO_ROOT/distillation/venv/bin/python"
RUNNER="$INGESTION_DIR/agent_sessions/run.py"
DESIRED_UTC_HOUR=6

IGNORE_UTC_GUARD=0
FORCED_UTC_HOUR=""
PYTHON_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ignore-utc-guard)
      IGNORE_UTC_GUARD=1
      shift
      ;;
    --force-utc-hour)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --force-utc-hour" >&2
        exit 2
      fi
      FORCED_UTC_HOUR="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'EOF'
Usage: run_scheduled.sh [--ignore-utc-guard] [--force-utc-hour HOUR] [agent_sessions args...]

Runs the agent session ingestion through the repo virtualenv.

Options:
  --ignore-utc-guard     Run regardless of current UTC hour.
  --force-utc-hour HOUR  Override the observed UTC hour (test helper).
  -h, --help             Show this help.

Any remaining arguments are passed through to agent_sessions/run.py.
EOF
      exit 0
      ;;
    *)
      PYTHON_ARGS+=("$1")
      shift
      ;;
  esac
done

mkdir -p "$INGESTION_DIR/logs"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing virtualenv interpreter: $PYTHON_BIN" >&2
  exit 1
fi

if [[ ! -f "$RUNNER" ]]; then
  echo "Missing agent session runner: $RUNNER" >&2
  exit 1
fi

if [[ -n "$FORCED_UTC_HOUR" ]]; then
  CURRENT_UTC_HOUR="$FORCED_UTC_HOUR"
else
  CURRENT_UTC_HOUR="$(date -u +%H)"
fi

CURRENT_UTC_HOUR=$((10#$CURRENT_UTC_HOUR))

if [[ "$IGNORE_UTC_GUARD" -ne 1 && "$CURRENT_UTC_HOUR" -ne "$DESIRED_UTC_HOUR" ]]; then
  echo "$(date -u +%FT%TZ) Skip agent_sessions run: utc_hour=$CURRENT_UTC_HOUR desired=$DESIRED_UTC_HOUR"
  exit 0
fi

echo "$(date -u +%FT%TZ) Starting agent_sessions run"
cd "$INGESTION_DIR"
if [[ "${#PYTHON_ARGS[@]}" -gt 0 ]]; then
  exec "$PYTHON_BIN" "$RUNNER" "${PYTHON_ARGS[@]}"
fi

exec "$PYTHON_BIN" "$RUNNER"
