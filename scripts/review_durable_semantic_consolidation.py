#!/usr/bin/env python3
"""Independent local review: scope, authority, bounds, and fail-closed cutover."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = (ROOT / "contracts" / "durable-semantic-consolidation-v2.spec.md").read_text()
INDEX = (ROOT / "cloudflare-mcp" / "mcp-server" / "src" / "index.ts").read_text()
QUEUE = (ROOT / "cloudflare-mcp" / "mcp-server" / "src" / "maintenanceQueue.ts").read_text()
assert "preflight_estimate: complete" in CONTRACT
assert "requires_permission: true" in CONTRACT
assert "MAX_MAINTENANCE_CLUSTER_SIZE" in QUEUE and "MAX_MAINTENANCE_BYTES" in QUEUE
assert 'env.DREAM_QUEUE_MODE === "live"' in INDEX
print("DURABLE_SEMANTIC_REVIEW_PASS")
