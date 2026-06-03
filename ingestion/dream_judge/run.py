#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: dream_judge/run.py
=============================================================================

INPUT FILES:
- ingestion/.env: DREAM_OPERATOR_TOKEN (auth to Worker), DREAM_MCP_BASE_URL
  (default https://mcp.dancing-ganesh.com), DREAM_OPUS_MODEL (default
  claude-opus-4-6), ANTHROPIC_API_KEY (fallback)

OUTPUT FILES:
- Verdicts POSTed back to the Worker at /ops/dream/judge_verdict
- ingestion/logs/dream_judge.log: per-run log

VERSION: 1.0
LAST UPDATED: 2026-05-17
AUTHOR: Arjun Divecha

DESCRIPTION:
Reads pending border-case ops from the Cloudflare Worker's judge queue and
asks Opus to decide each one (apply or skip). Posts verdicts back so the
next Worker cycle can act on them.

Tries the `claude` CLI first (subscription credits, no API cost). Falls
back to the Anthropic API and emits a warning if the CLI isn't available
or fails (e.g., quota exceeded, not logged in).

Designed to run nightly via run_nightly_ingestion.sh, AFTER the other
ingestion steps and BEFORE the remote Worker cron fires its next cycle.

DEPENDENCIES:
- requests
- anthropic (only used as fallback)
- python-dotenv
- claude CLI on PATH (preferred; subscription billing)

USAGE:
    # Process all pending judge items
    python dream_judge/run.py

    # Dry-run: classify but don't POST verdicts back
    python dream_judge/run.py --dry-run

    # Limit number of items judged this run
    python dream_judge/run.py --limit 5

    # Force API fallback (skip claude CLI)
    python dream_judge/run.py --force-api
=============================================================================
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
INGESTION_DIR = HERE.parent
load_dotenv(INGESTION_DIR / ".env")

LOG_DIR = INGESTION_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "dream_judge.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
WORKER_BASE_URL = os.getenv("DREAM_MCP_BASE_URL", "https://mcp.dancing-ganesh.com").rstrip("/")
OPERATOR_TOKEN = os.getenv("DREAM_OPERATOR_TOKEN", "")
OPUS_MODEL = os.getenv("DREAM_OPUS_MODEL", "claude-opus-4-6")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "")

REQUEST_TIMEOUT = 60  # seconds
CLAUDE_CLI_TIMEOUT = 120  # seconds per judge call
CLAUDE_CLI_CANDIDATES = [
    Path.home() / ".nvm/versions/node/v24.12.0/bin/claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
]


def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OPERATOR_TOKEN}",
        "Content-Type": "application/json",
    }


# ----------------------------------------------------------------------------
# Queue I/O
# ----------------------------------------------------------------------------
def fetch_pending_items() -> list[dict[str, Any]]:
    """GET /ops/dream/judge_queue and return items without a verdict yet."""
    url = f"{WORKER_BASE_URL}/ops/dream/judge_queue"
    resp = requests.get(url, headers=auth_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    raw_items = body.get("items", []) or []
    # Only judge items that have no verdict yet AND have an attached item payload.
    pending = [
        i for i in raw_items
        if i.get("item") and not i.get("verdict")
    ]
    log.info(
        "Fetched %d pending items (%d unjudged) from Worker queue",
        len(raw_items),
        len(pending),
    )
    return pending


def post_verdict(op_id: str, verdict: str, reason: str, judge_model: str, judge_source: str) -> None:
    url = f"{WORKER_BASE_URL}/ops/dream/judge_verdict"
    payload = {
        "op_id": op_id,
        "verdict": verdict,
        "reason": reason,
        "judge_model": judge_model,
        "judge_source": judge_source,
    }
    resp = requests.post(url, headers=auth_headers(), data=json.dumps(payload), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    log.info("  → posted verdict %s for %s (source=%s)", verdict, op_id, judge_source)


# ----------------------------------------------------------------------------
# Prompt building
# ----------------------------------------------------------------------------
def build_prompt(item: dict[str, Any]) -> str:
    op_type = item.get("op_type", "?")
    rubric = item.get("rubric", "")
    payload = json.dumps(item.get("payload", {}), indent=2, ensure_ascii=False)
    return (
        "You are reviewing a border-case decision in a personal knowledge memory system.\n"
        "Your job is to decide ONE thing: APPLY the proposed action, or SKIP it.\n\n"
        f"Op type: {op_type}\n"
        f"Rubric: {rubric}\n\n"
        "Payload (the data you must decide on):\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "Reply with EXACTLY a single JSON object on one line, nothing else:\n"
        '{"verdict": "apply" | "skip", "reason": "1-2 sentences explaining your decision"}\n\n'
        "If anything is ambiguous, prefer SKIP. The cost of a wrong apply is higher than the cost of a wrong skip."
    )


def parse_verdict_response(text: str) -> tuple[str, str] | None:
    """Parse the JSON verdict from the model's response. Returns (verdict, reason) or None."""
    text = (text or "").strip()
    # If wrapped in code fences, strip them.
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip().rstrip("```").strip()
    # Find the first { and last } in case there's preamble.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    verdict = obj.get("verdict")
    reason = obj.get("reason", "")
    if verdict not in ("apply", "skip"):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return verdict, reason.strip()


# ----------------------------------------------------------------------------
# Judges
# ----------------------------------------------------------------------------
def resolve_claude_cli() -> str | None:
    if CLAUDE_CLI_PATH:
        candidate = Path(CLAUDE_CLI_PATH).expanduser()
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)

    found = shutil.which("claude")
    if found:
        return found

    for candidate in CLAUDE_CLI_CANDIDATES:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def judge_via_claude_cli(prompt: str, model: str) -> tuple[str, str, str] | None:
    """
    Try `claude --print --model <model> <prompt>`.
    Returns (verdict, reason, source) or None on failure.
    Source = "claude_cli" on success, "claude_cli_failed" if invocation fails.
    """
    claude_bin = resolve_claude_cli()
    if not claude_bin:
        log.warning("claude CLI not found; falling back to API")
        return None

    try:
        result = subprocess.run(
            [claude_bin, "--print", "--model", model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_CLI_TIMEOUT,
        )
    except FileNotFoundError:
        log.warning("claude CLI path disappeared; falling back to API")
        return None
    except subprocess.TimeoutExpired:
        log.warning("claude CLI timed out; falling back to API")
        return None
    except Exception as e:
        log.warning("claude CLI failed (%s); falling back to API", e)
        return None

    if result.returncode != 0:
        log.warning(
            "claude CLI returned exit %s; stderr=%s; falling back to API",
            result.returncode,
            (result.stderr or "")[:300],
        )
        return None

    parsed = parse_verdict_response(result.stdout)
    if parsed is None:
        log.warning(
            "claude CLI output was unparseable; falling back to API. stdout=%s",
            (result.stdout or "")[:300],
        )
        return None

    verdict, reason = parsed
    return verdict, reason, "claude_cli"


def judge_via_anthropic_api(prompt: str, model: str) -> tuple[str, str, str] | None:
    """
    Fallback: call Anthropic API directly. Returns (verdict, reason, source) or None.
    Emits a warning since this incurs API cost vs the subscription path.
    """
    if not ANTHROPIC_API_KEY:
        log.error("Anthropic fallback unavailable: ANTHROPIC_API_KEY not set")
        return None
    try:
        import anthropic
    except ImportError:
        log.error("Anthropic fallback unavailable: anthropic package not installed")
        return None

    log.warning("[FALLBACK] Calling Anthropic API for judge call (incurs cost)")
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for block in resp.content:
            if hasattr(block, "text"):
                text += block.text
        parsed = parse_verdict_response(text)
        if parsed is None:
            log.error("Anthropic API response was unparseable: %s", text[:300])
            return None
        verdict, reason = parsed
        return verdict, reason, "anthropic_api"
    except Exception as e:
        log.error("Anthropic API call failed: %s", e)
        return None


def judge_item(item: dict[str, Any], model: str, force_api: bool) -> tuple[str, str, str] | None:
    prompt = build_prompt(item)
    if not force_api:
        result = judge_via_claude_cli(prompt, model)
        if result is not None:
            return result
    return judge_via_anthropic_api(prompt, model)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Judge border-case Dream ops via Opus.")
    parser.add_argument("--dry-run", action="store_true", help="classify but do not POST verdicts")
    parser.add_argument("--limit", type=int, default=None, help="max items to judge this run")
    parser.add_argument("--force-api", action="store_true", help="skip claude CLI; go straight to API")
    args = parser.parse_args()

    if not OPERATOR_TOKEN:
        log.error("DREAM_OPERATOR_TOKEN not set; cannot authenticate to Worker.")
        return 2

    log.info(
        "Starting dream_judge run: worker=%s model=%s force_api=%s dry_run=%s claude_cli=%s",
        WORKER_BASE_URL,
        OPUS_MODEL,
        args.force_api,
        args.dry_run,
        "api_forced" if args.force_api else (resolve_claude_cli() or "not_found"),
    )

    try:
        items = fetch_pending_items()
    except Exception as e:
        log.error("Failed to fetch pending items: %s", e)
        return 1

    if args.limit:
        items = items[:args.limit]

    if not items:
        log.info("No pending judge items. Done.")
        return 0

    n_applied = 0
    n_skipped = 0
    n_failed = 0
    n_api_fallback = 0
    start = time.time()

    for queue_row in items:
        item = queue_row["item"]
        op_id = item.get("op_id") or queue_row.get("op_id")
        if not op_id:
            log.warning("Skipping malformed item: %s", json.dumps(queue_row)[:200])
            continue

        log.info("Judging %s (op_type=%s)", op_id, item.get("op_type"))
        judged = judge_item(item, OPUS_MODEL, args.force_api)
        if judged is None:
            log.error("  ✗ judge failed for %s", op_id)
            n_failed += 1
            continue

        verdict, reason, source = judged
        if source == "anthropic_api":
            n_api_fallback += 1
        log.info("  verdict=%s source=%s reason=%s", verdict, source, reason[:120])

        if verdict == "apply":
            n_applied += 1
        else:
            n_skipped += 1

        if args.dry_run:
            log.info("  [dry-run] not posting verdict")
            continue

        try:
            post_verdict(op_id, verdict, reason, OPUS_MODEL, source)
        except Exception as e:
            log.error("  ✗ failed to post verdict for %s: %s", op_id, e)
            n_failed += 1
            continue

        # Gentle rate limit between judge calls.
        time.sleep(0.5)

    elapsed = time.time() - start
    log.info(
        "Done: %d items processed in %.1fs (applied=%d, skipped=%d, failed=%d, api_fallback=%d)",
        len(items), elapsed, n_applied, n_skipped, n_failed, n_api_fallback,
    )
    if n_api_fallback > 0:
        log.warning(
            "Used Anthropic API fallback for %d/%d calls. Check that 'claude' CLI is on PATH and the subscription is logged in.",
            n_api_fallback, len(items),
        )
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
