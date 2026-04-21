#!/bin/bash

set -u

# repo-agent-context global hook
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
HOME_DIR="${HOME:-}"
GLOBAL_HOOK_DIR="${HOME_DIR}/.githooks"
PREVIOUS_HOOK="${GLOBAL_HOOK_DIR}/pre-commit.previous"
REPO_LOCAL_HOOK="${REPO_ROOT}/.githooks/pre-commit"
EXPORT_WRAPPER="${PKS_AGENT_CONTEXT_BIN:-${HOME_DIR}/.local/bin/pks-export-repo-context}"
status=0

run_hook_if_present() {
  local hook_path="$1"
  if [ -x "${hook_path}" ]; then
    "${hook_path}" || return $?
  fi
  return 0
}

if [ -n "${REPO_ROOT}" ] \
  && [ "${PKS_DISABLE_GLOBAL_AGENT_CONTEXT:-0}" != "1" ] \
  && [ ! -f "${REPO_ROOT}/.pks-disable-hook" ]; then
  if [ -x "${EXPORT_WRAPPER}" ]; then
    if ! "${EXPORT_WRAPPER}" --repo-dir "${REPO_ROOT}" --surface all --stage --require-github-origin; then
      echo "[agent-context] Export failed; continuing commit without repo context." >&2
    fi
  else
    echo "[agent-context] Export wrapper not found at ${EXPORT_WRAPPER}; continuing commit." >&2
  fi
fi

if ! run_hook_if_present "${PREVIOUS_HOOK}"; then
  status=$?
fi

if [ -n "${REPO_ROOT}" ] && ! run_hook_if_present "${REPO_LOCAL_HOOK}"; then
  status=$?
fi

exit "${status}"
