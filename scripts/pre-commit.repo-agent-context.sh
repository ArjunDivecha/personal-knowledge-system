#!/bin/bash

set -euo pipefail

# repo-agent-context hook
REPO_ROOT="__TARGET_REPO__"
EXPORTER="__EXPORTER_PATH__"

if [ ! -f "${EXPORTER}" ]; then
  echo "[agent-context] Exporter not found at ${EXPORTER}" >&2
  exit 1
fi

python3 "${EXPORTER}" --repo-dir "${REPO_ROOT}" --surface all --stage
