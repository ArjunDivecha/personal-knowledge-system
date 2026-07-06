#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: dream_judge/run.py
=============================================================================

INPUT FILES:
- ingestion/.env: DREAM_OPERATOR_TOKEN (auth to Worker), DREAM_MCP_BASE_URL
  (default https://mcp.dancing-ganesh.com), DREAM_OPUS_MODEL (default
  claude-opus-4-8), ANTHROPIC_API_KEY (only when fallback is explicit)

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

Tries the `claude` CLI first (subscription credits, no API cost). Anthropic API
fallback is fail-closed unless explicitly enabled with
DREAM_ALLOW_ANTHROPIC_API_FALLBACK=1 or --allow-api-fallback.

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

    # Force explicit API fallback (skip claude CLI)
    DREAM_ALLOW_ANTHROPIC_API_FALLBACK=1 python dream_judge/run.py --force-api
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
OPUS_MODEL = os.getenv("DREAM_OPUS_MODEL", "claude-opus-4-8")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_CLI_PATH = os.getenv("CLAUDE_CLI_PATH", "")

REQUEST_TIMEOUT = 60  # seconds
CLAUDE_CLI_TIMEOUT = 120  # seconds per judge call
CLAUDE_CLI_CANDIDATES = [
    Path.home() / ".nvm/versions/node/v24.12.0/bin/claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def api_fallback_enabled(flag: bool = False) -> bool:
    return flag or _truthy(os.getenv("DREAM_ALLOW_ANTHROPIC_API_FALLBACK"))


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


def post_verdict(
    op_id: str,
    verdict: str,
    reason: str,
    judge_model: str,
    judge_source: str,
    synthesis: dict[str, Any] | None = None,
) -> None:
    url = f"{WORKER_BASE_URL}/ops/dream/judge_verdict"
    payload = {
        "op_id": op_id,
        "verdict": verdict,
        "reason": reason,
        "judge_model": judge_model,
        "judge_source": judge_source,
    }
    if synthesis is not None:
        payload["synthesis"] = synthesis
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
    if op_type == "insight_synthesis":
        # Content-bearing verdict: an apply must CARRY the synthesized insight.
        # See docs/pks-dream-insight-synthesis-prd-2026-07-02.md.
        return (
            "You are reviewing a cluster of related entries in a personal knowledge memory system.\n"
            "Your job is to decide ONE thing: is there a durable cross-cutting insight here (APPLY), or not (SKIP)?\n\n"
            f"Op type: {op_type}\n"
            f"Rubric: {rubric}\n\n"
            "Payload (the cluster you must decide on):\n"
            "```json\n"
            f"{payload}\n"
            "```\n\n"
            "Reply with EXACTLY a single JSON object on one line, nothing else.\n"
            "To skip:\n"
            '{"verdict": "skip", "reason": "1-2 sentences explaining your decision"}\n'
            "To apply, include the synthesized insight:\n"
            '{"verdict": "apply", "reason": "1-2 sentences", "synthesis": {"insight_text": "the insight, max 500 chars", '
            '"placement": "append" | "create", "anchor_entry_id": "<a member id, required for append>", '
            '"domain": "<short domain string, required for create>"}}\n\n'
            "Use placement 'append' with anchor_entry_id when the insight refines one member entry; "
            "use placement 'create' with a domain when it genuinely spans entries.\n"
            "If anything is ambiguous, prefer SKIP. A wrong new memory is worse than a missed insight."
        )
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


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response (fences, preamble tolerated)."""
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
    if not isinstance(obj, dict):
        return None
    return obj


def parse_verdict_response(text: str) -> tuple[str, str] | None:
    """Parse the JSON verdict from the model's response. Returns (verdict, reason) or None."""
    obj = extract_json_object(text)
    if obj is None:
        return None
    verdict = obj.get("verdict")
    reason = obj.get("reason", "")
    if verdict not in ("apply", "skip"):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return verdict, reason.strip()


MAX_INSIGHT_CHARS = 500


def validate_synthesis(
    synthesis: Any,
    target_entry_ids: list[str],
    max_chars: int = MAX_INSIGHT_CHARS,
) -> str | None:
    """Mirror of the Worker-side validator. Returns a rejection reason or None when valid."""
    if not isinstance(synthesis, dict):
        return "missing_synthesis_block"
    text = synthesis.get("insight_text")
    if not isinstance(text, str) or not text.strip():
        return "empty_insight_text"
    if len(text.strip()) > max_chars:
        return "insight_text_too_long"
    placement = synthesis.get("placement")
    if placement == "append":
        anchor = synthesis.get("anchor_entry_id")
        if not isinstance(anchor, str) or not anchor:
            return "missing_anchor_entry_id"
        if anchor not in target_entry_ids:
            return "anchor_outside_cluster"
        return None
    if placement == "create":
        domain = synthesis.get("domain")
        if not isinstance(domain, str) or not domain.strip():
            return "missing_domain"
        return None
    return "invalid_placement"


def parse_insight_verdict_response(
    text: str,
    target_entry_ids: list[str],
) -> tuple[str, str, dict[str, Any] | None] | None:
    """
    Parse a content-bearing insight_synthesis verdict.
    Returns (verdict, reason, synthesis_or_none) or None on parse/validation
    failure — failure leaves the item pending so it is retried next night
    rather than posting a half-valid verdict.
    """
    obj = extract_json_object(text)
    if obj is None:
        return None
    verdict = obj.get("verdict")
    reason = obj.get("reason", "")
    if verdict not in ("apply", "skip"):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    if verdict == "skip":
        return verdict, reason.strip(), None
    synthesis = obj.get("synthesis")
    rejection = validate_synthesis(synthesis, target_entry_ids)
    if rejection is not None:
        log.warning("insight verdict rejected (%s); leaving item pending", rejection)
        return None
    # Normalize: keep only the fields the Worker accepts.
    normalized: dict[str, Any] = {
        "insight_text": synthesis["insight_text"].strip(),
        "placement": synthesis["placement"],
    }
    if synthesis["placement"] == "append":
        normalized["anchor_entry_id"] = synthesis["anchor_entry_id"]
    else:
        normalized["domain"] = synthesis["domain"].strip()
    return verdict, reason.strip(), normalized


def parse_response_for_item(
    text: str,
    item: dict[str, Any],
) -> tuple[str, str, dict[str, Any] | None] | None:
    """Dispatch parsing by op_type. Returns (verdict, reason, synthesis|None) or None."""
    if item.get("op_type") == "insight_synthesis":
        target_ids = item.get("target_entry_ids") or []
        return parse_insight_verdict_response(text, list(target_ids))
    parsed = parse_verdict_response(text)
    if parsed is None:
        return None
    verdict, reason = parsed
    return verdict, reason, None


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


def judge_via_claude_cli(prompt: str, model: str, item: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None, str] | None:
    """
    Try `claude --print --model <model> <prompt>`.
    Returns (verdict, reason, synthesis_or_none, source) or None on failure.
    Source = "claude_cli" on success, "claude_cli_failed" if invocation fails.
    """
    claude_bin = resolve_claude_cli()
    if not claude_bin:
        log.warning("claude CLI not found")
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
        log.warning("claude CLI path disappeared")
        return None
    except subprocess.TimeoutExpired:
        log.warning("claude CLI timed out")
        return None
    except Exception as e:
        log.warning("claude CLI failed (%s)", e)
        return None

    if result.returncode != 0:
        log.warning(
            "claude CLI returned exit %s; stderr=%s",
            result.returncode,
            (result.stderr or "")[:300],
        )
        return None

    parsed = parse_response_for_item(result.stdout, item)
    if parsed is None:
        log.warning(
            "claude CLI output was unparseable. stdout=%s",
            (result.stdout or "")[:300],
        )
        return None

    verdict, reason, synthesis = parsed
    return verdict, reason, synthesis, "claude_cli"


def judge_via_anthropic_api(prompt: str, model: str, item: dict[str, Any]) -> tuple[str, str, dict[str, Any] | None, str] | None:
    """
    Fallback: call Anthropic API directly. Returns (verdict, reason, synthesis_or_none, source) or None.
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
        parsed = parse_response_for_item(text, item)
        if parsed is None:
            log.error("Anthropic API response was unparseable: %s", text[:300])
            return None
        verdict, reason, synthesis = parsed
        return verdict, reason, synthesis, "anthropic_api"
    except Exception as e:
        log.error("Anthropic API call failed: %s", e)
        return None


def judge_item(
    item: dict[str, Any],
    model: str,
    force_api: bool,
    allow_api_fallback: bool,
) -> tuple[str, str, dict[str, Any] | None, str] | None:
    prompt = build_prompt(item)
    if not force_api:
        result = judge_via_claude_cli(prompt, model, item)
        if result is not None:
            return result
    if not allow_api_fallback:
        log.error("Anthropic API fallback disabled; set DREAM_ALLOW_ANTHROPIC_API_FALLBACK=1 or pass --allow-api-fallback for a deliberate API-billed judge run")
        return None
    return judge_via_anthropic_api(prompt, model, item)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Judge border-case Dream ops via Opus.")
    parser.add_argument("--dry-run", action="store_true", help="classify but do not POST verdicts")
    parser.add_argument("--limit", type=int, default=None, help="max items to judge this run")
    parser.add_argument("--force-api", action="store_true", help="skip claude CLI; go straight to API")
    parser.add_argument("--allow-api-fallback", action="store_true", help="allow deliberate Anthropic API fallback if the claude CLI path fails")
    args = parser.parse_args()
    allow_api_fallback = api_fallback_enabled(args.allow_api_fallback)

    if not OPERATOR_TOKEN:
        log.error("DREAM_OPERATOR_TOKEN not set; cannot authenticate to Worker.")
        return 2

    log.info(
        "Starting dream_judge run: worker=%s model=%s force_api=%s allow_api_fallback=%s dry_run=%s claude_cli=%s",
        WORKER_BASE_URL,
        OPUS_MODEL,
        args.force_api,
        allow_api_fallback,
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
        judged = judge_item(item, OPUS_MODEL, args.force_api, allow_api_fallback)
        if judged is None:
            log.error("  ✗ judge failed for %s", op_id)
            n_failed += 1
            continue

        verdict, reason, synthesis, source = judged
        if source == "anthropic_api":
            n_api_fallback += 1
        log.info("  verdict=%s source=%s reason=%s", verdict, source, reason[:120])
        if synthesis is not None:
            log.info("  synthesis placement=%s text=%s", synthesis.get("placement"), str(synthesis.get("insight_text"))[:120])

        if verdict == "apply":
            n_applied += 1
        else:
            n_skipped += 1

        if args.dry_run:
            log.info("  [dry-run] not posting verdict")
            continue

        try:
            post_verdict(op_id, verdict, reason, OPUS_MODEL, source, synthesis=synthesis)
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
