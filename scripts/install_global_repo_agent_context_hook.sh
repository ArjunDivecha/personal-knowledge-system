#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

INSTALL_ROOT="${PKS_AGENT_CONTEXT_HOME:-$HOME/.local/share/pks-repo-agent-context}"
BIN_DIR="${HOME}/.local/bin"
HOOKS_DIR="${HOME}/.githooks"
GLOBAL_HOOK_PATH="${HOOKS_DIR}/pre-commit"
PREVIOUS_HOOK_PATH="${HOOKS_DIR}/pre-commit.previous"
EXPORTER_SOURCE="${REPO_ROOT}/scripts/export_repo_agent_context.py"
WRAPPER_SOURCE="${REPO_ROOT}/scripts/pks-export-repo-context.sh"
HOOK_SOURCE="${REPO_ROOT}/scripts/pre-commit.global.repo-agent-context.sh"
EXPORTER_TARGET="${INSTALL_ROOT}/export_repo_agent_context.py"
WRAPPER_TARGET="${BIN_DIR}/pks-export-repo-context"
MARKER="repo-agent-context global hook"

for path in "${EXPORTER_SOURCE}" "${WRAPPER_SOURCE}" "${HOOK_SOURCE}"; do
  if [ ! -f "${path}" ]; then
    echo "[agent-context] Missing required source file: ${path}" >&2
    exit 1
  fi
done

mkdir -p "${INSTALL_ROOT}" "${BIN_DIR}" "${HOOKS_DIR}"

CURRENT_HOOKS_PATH="$(git config --global --get core.hooksPath || true)"
if [ -n "${CURRENT_HOOKS_PATH}" ]; then
  CURRENT_HOOKS_PATH="${CURRENT_HOOKS_PATH/#\~/${HOME}}"
fi

EXISTING_GLOBAL_HOOK=""
if [ -n "${CURRENT_HOOKS_PATH}" ] && [ "${CURRENT_HOOKS_PATH}" != "${HOOKS_DIR}" ] && [ -f "${CURRENT_HOOKS_PATH}/pre-commit" ]; then
  EXISTING_GLOBAL_HOOK="${CURRENT_HOOKS_PATH}/pre-commit"
elif [ -f "${GLOBAL_HOOK_PATH}" ]; then
  EXISTING_GLOBAL_HOOK="${GLOBAL_HOOK_PATH}"
fi

if [ -n "${EXISTING_GLOBAL_HOOK}" ] && ! grep -q "${MARKER}" "${EXISTING_GLOBAL_HOOK}"; then
  if [ ! -f "${PREVIOUS_HOOK_PATH}" ]; then
    cp "${EXISTING_GLOBAL_HOOK}" "${PREVIOUS_HOOK_PATH}"
    chmod +x "${PREVIOUS_HOOK_PATH}"
    echo "[agent-context] Preserved existing global pre-commit hook at ${PREVIOUS_HOOK_PATH}"
  else
    BACKUP_PATH="${HOOKS_DIR}/pre-commit.backup.$(date +%Y%m%d%H%M%S)"
    cp "${EXISTING_GLOBAL_HOOK}" "${BACKUP_PATH}"
    echo "[agent-context] Existing global hook already preserved; copied extra backup to ${BACKUP_PATH}"
  fi
fi

cp "${EXPORTER_SOURCE}" "${EXPORTER_TARGET}"
cp "${WRAPPER_SOURCE}" "${WRAPPER_TARGET}"
cp "${HOOK_SOURCE}" "${GLOBAL_HOOK_PATH}"

chmod +x "${EXPORTER_TARGET}" "${WRAPPER_TARGET}" "${GLOBAL_HOOK_PATH}"

git config --global core.hooksPath "${HOOKS_DIR}"

echo "[agent-context] Installed exporter to ${EXPORTER_TARGET}"
echo "[agent-context] Installed wrapper to ${WRAPPER_TARGET}"
echo "[agent-context] Installed global pre-commit hook to ${GLOBAL_HOOK_PATH}"
echo "[agent-context] Set git core.hooksPath to ${HOOKS_DIR}"
echo "[agent-context] Repo-specific opt-out: touch <repo>/.pks-disable-hook"
echo "[agent-context] Repo-specific chaining: create <repo>/.githooks/pre-commit"
