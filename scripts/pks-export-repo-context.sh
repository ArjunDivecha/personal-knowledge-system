#!/bin/bash

set -euo pipefail

INSTALL_ROOT="${PKS_AGENT_CONTEXT_HOME:-$HOME/.local/share/pks-repo-agent-context}"
EXPORTER_PATH="${INSTALL_ROOT}/export_repo_agent_context.py"

resolve_python() {
  local candidate=""
  if [ -n "${PKS_HOOK_PYTHON:-}" ]; then
    candidate="${PKS_HOOK_PYTHON}"
    if [[ "${candidate}" == */* ]]; then
      [ -x "${candidate}" ] && { printf '%s\n' "${candidate}"; return 0; }
    elif command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  fi

  for candidate in python3.14 /opt/homebrew/bin/python3 python3; do
    if [[ "${candidate}" == */* ]]; then
      [ -x "${candidate}" ] && { printf '%s\n' "${candidate}"; return 0; }
    elif command -v "${candidate}" >/dev/null 2>&1; then
      command -v "${candidate}"
      return 0
    fi
  done

  return 1
}

if [ ! -f "${EXPORTER_PATH}" ]; then
  echo "[agent-context] Exporter not found at ${EXPORTER_PATH}" >&2
  exit 1
fi

PYTHON_BIN="$(resolve_python)" || {
  echo "[agent-context] No compatible python interpreter found" >&2
  exit 1
}

exec "${PYTHON_BIN}" "${EXPORTER_PATH}" "$@"
