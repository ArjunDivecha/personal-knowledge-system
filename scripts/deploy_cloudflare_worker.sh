#!/bin/bash
set -euo pipefail

REPO="/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"
WORKER_DIR="$REPO/cloudflare-mcp/mcp-server"

if [ -f "$REPO/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO/.env"
    set +a
fi

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
    echo "FATAL: CLOUDFLARE_API_TOKEN is not set in $REPO/.env" >&2
    exit 1
fi

cd "$WORKER_DIR"
npx wrangler deploy --env=""
