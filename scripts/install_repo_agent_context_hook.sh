#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_PATH="${REPO_ROOT}/scripts/pre-commit.repo-agent-context.sh"
EXPORTER_PATH="${REPO_ROOT}/scripts/export_repo_agent_context.py"
TARGET_REPO="${1:-$(pwd)}"

if ! git -C "${TARGET_REPO}" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "[agent-context] ${TARGET_REPO} is not a git repository" >&2
  exit 1
fi

TARGET_ROOT="$(git -C "${TARGET_REPO}" rev-parse --show-toplevel)"
HOOK_PATH="${TARGET_ROOT}/.git/hooks/pre-commit"

if [ ! -f "${TEMPLATE_PATH}" ]; then
  echo "[agent-context] Missing hook template at ${TEMPLATE_PATH}" >&2
  exit 1
fi

if [ -f "${HOOK_PATH}" ] && ! grep -q "repo-agent-context hook" "${HOOK_PATH}"; then
  BACKUP_PATH="${HOOK_PATH}.backup.$(date +%Y%m%d%H%M%S)"
  cp "${HOOK_PATH}" "${BACKUP_PATH}"
  echo "[agent-context] Backed up existing hook to ${BACKUP_PATH}"
fi

sed \
  -e "s|__TARGET_REPO__|${TARGET_ROOT}|g" \
  -e "s|__EXPORTER_PATH__|${EXPORTER_PATH}|g" \
  "${TEMPLATE_PATH}" > "${HOOK_PATH}"

chmod +x "${HOOK_PATH}"

echo "[agent-context] Installed hook into ${TARGET_ROOT}"
echo "[agent-context] Hook will export repo context via ${EXPORTER_PATH}"
