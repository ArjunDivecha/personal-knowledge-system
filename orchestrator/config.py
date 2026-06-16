"""
=============================================================================
MODULE: orchestrator/config.py
=============================================================================

DESCRIPTION:
Central configuration for the PKS nightly orchestrator: paths, Redis key
templates, timing defaults, and environment accessors. Values come from the
spec's "Defaults" section and the Atomicity And Race Contract.

Loads the repo `.env` (same search order as ingestion/core/config.py) so the
orchestrator can run unattended under launchd. Importing this module performs
NO network I/O and is safe in tests.

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env
    (optional) consolidated runtime keys: UPSTASH_*, DREAM_OPERATOR_TOKEN,
    DREAM_MCP_BASE_URL, ANTHROPIC_API_KEY, etc.

OUTPUT FILES:
- none (constants/accessors only).
=============================================================================
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv always present in the venv
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mirror ingestion/core/config.py .env search order.
for _candidate in (REPO_ROOT / ".env",
                   REPO_ROOT / "ingestion" / ".env",
                   REPO_ROOT / "distillation" / ".env"):
    if load_dotenv is not None and _candidate.exists():
        load_dotenv(_candidate)
        break

# ── Paths (full absolute paths) ──────────────────────────────────────────────
LEDGER_DIR = REPO_ROOT / "ingestion" / "checkpoints" / "orchestrator_runs"
REPORTS_DIR = REPO_ROOT / "scripts" / "reports"

# ── Time zone / SLA clock times (Pacific), spec "Defaults" ───────────────────
PACIFIC = ZoneInfo("America/Los_Angeles")
NIGHTLY_TARGET_HHMM = (23, 0)      # 23:00 Pacific nightly target
SLA_HHMM = (8, 30)                 # 08:30 Pacific orchestrator SLA
CATCHUP_CUTOFF_HHMM = (8, 45)      # 08:45 Pacific local catch-up cutoff
DEADMAN_HHMM = (9, 0)              # 09:00 Pacific external dead-man check
DREAM_HARD_CUTOFF_HHMM = (7, 30)   # 07:30 Pacific hard Dream cutoff

# ── Lock / fence / heartbeat (Atomicity And Race Contract) ───────────────────
LOCK_TTL_SECONDS = 2 * 60 * 60        # 2 hours
HEARTBEAT_INTERVAL_SECONDS = 60       # refresh every 60s
STALE_THRESHOLD_SECONDS = 90 * 60     # 90 minutes since heartbeat_at

# ── Dream wait/poll ──────────────────────────────────────────────────────────
DREAM_POLL_INTERVAL_SECONDS = 30
DREAM_FIRST_WAIT_TIMEOUT_SECONDS = 45 * 60   # 45 minutes

# ── Redis key templates (spec "Remote ledger and heartbeat keys") ────────────
KEY_LOCK = "pks:orchestrator:lock:{run_date}"
KEY_FENCE = "pks:orchestrator:fence:{run_date}"          # monotonic fence counter
KEY_RUN = "pks:orchestrator:run:{run_date}"
KEY_LAST_STARTED = "pks:orchestrator:last_started"
KEY_LAST_HEARTBEAT = "pks:orchestrator:last_heartbeat"
KEY_LAST_COMPLETED = "pks:orchestrator:last_completed"
KEY_LAST_STATUS = "pks:orchestrator:last_status"
KEY_LAST_REPORT = "pks:orchestrator:last_report"

# Dream date lock (authoritative Cloudflare-side mutation fence; written in
# Phase 2, defined here for shared naming).
KEY_DREAM_DATE_LOCK = "dream:scheduled-governed:date-lock:{run_date}"

# ── Report paths ─────────────────────────────────────────────────────────────
REPORT_JSON = "pks-nightly-{run_date}.json"
REPORT_MD = "pks-nightly-{run_date}.md"

# ── Mutation gate (Phase 1: hard-off; nothing mutates) ───────────────────────
# Even when a future phase flips this, Phase 1 stage wrappers remain shadow.
MUTATIONS_ENABLED = os.environ.get("PKS_ORCH_ALLOW_MUTATION", "0") == "1"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def dream_base_url() -> str:
    return env("DREAM_MCP_BASE_URL", "https://mcp.dancing-ganesh.com").rstrip("/")


def owner_host() -> str:
    """Stable host id for the lock owner_host field (spec example: m4max-base)."""
    return env("PKS_ORCH_HOST", os.uname().nodename)
