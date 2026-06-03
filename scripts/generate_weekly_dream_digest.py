#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: scripts/generate_weekly_dream_digest.py
=============================================================================

INPUT:
- Upstash Redis (via UPSTASH_REDIS_REST_URL / TOKEN from ingestion/.env)
- The Cloudflare Worker (for /ops/dream/tripwire_status if available)

OUTPUT:
- scripts/reports/dream-weekly-YYYY-WW.md (Markdown digest)
- Returns 0 on success, 1 on Redis errors

VERSION: 1.0
LAST UPDATED: 2026-05-17
AUTHOR: Arjun Divecha

DESCRIPTION:
Generates a human-readable weekly digest of Dream activity. Covers:
- Governed scheduled Dream attempts this week (selected / held / applied)
- Auto-applied L1+L2 operations this week (count + sample)
- Judge queue activity (Opus decisions: applied / skipped / pending)
- Tripwire status (kill flags active, recent destructive/retrieval signals)
- Rollback-eligible window (ops within past 7 days)
- Errors / skipped cycle runs

Designed to run weekly (e.g. Sundays) via cron OR on-demand.

DEPENDENCIES:
- requests
- upstash-redis
- python-dotenv

USAGE:
    # Generate for the current ISO week
    python scripts/generate_weekly_dream_digest.py

    # Generate for a specific week
    python scripts/generate_weekly_dream_digest.py --week 2026-W20

    # Write to a custom path
    python scripts/generate_weekly_dream_digest.py --out /tmp/test.md
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
INGESTION = REPO / "ingestion"
load_dotenv(INGESTION / ".env")

REPORTS_DIR = REPO / "scripts" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

UTC = timezone.utc


# ----------------------------------------------------------------------------
# Redis access (REST API directly via requests; avoids upstash-redis dep)
# ----------------------------------------------------------------------------
def redis_post(url: str, token: str, command: list[Any]) -> Any:
    """POST a single Redis command to the Upstash REST endpoint."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=command, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body and body["error"]:
        raise RuntimeError(f"Redis error: {body['error']}")
    return body.get("result")


def redis_scan_keys(url: str, token: str, pattern: str, max_keys: int = 5000) -> list[str]:
    """SCAN a key pattern to completion (or up to max_keys)."""
    keys: list[str] = []
    cursor = "0"
    while True:
        result = redis_post(url, token, ["SCAN", cursor, "MATCH", pattern, "COUNT", 200])
        if not isinstance(result, list) or len(result) < 2:
            break
        cursor = str(result[0])
        batch = result[1] or []
        keys.extend(batch)
        if cursor == "0" or len(keys) >= max_keys:
            break
    return keys[:max_keys]


def redis_get(url: str, token: str, key: str) -> Any:
    return redis_post(url, token, ["GET", key])


# ----------------------------------------------------------------------------
# Week math
# ----------------------------------------------------------------------------
def parse_iso_week(s: str) -> tuple[int, int]:
    """Parse 'YYYY-Www' into (year, week)."""
    if not s:
        raise ValueError("empty week string")
    parts = s.split("-W")
    if len(parts) != 2:
        raise ValueError(f"invalid ISO week format: {s}")
    return int(parts[0]), int(parts[1])


def current_iso_week() -> tuple[int, int]:
    iso = datetime.now(UTC).isocalendar()
    return iso.year, iso.week


def week_range(year: int, week: int) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the given ISO week (Mon 00:00 → next Mon 00:00)."""
    first_jan = datetime(year, 1, 4, tzinfo=UTC)  # Jan 4 is always in ISO week 1
    week1_monday = first_jan - timedelta(days=first_jan.isoweekday() - 1)
    start = week1_monday + timedelta(weeks=week - 1)
    return start, start + timedelta(days=7)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Handle trailing Z
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Data collection
# ----------------------------------------------------------------------------
def collect_runs(
    url: str, token: str, start: datetime, end: datetime
) -> dict[str, list[dict[str, Any]]]:
    """Collect dpr_* (proposal) and dr_* (cycle) records overlapping the week.

    Records live at `dream:run:<run_id>`. Auxiliary keys like
    `dream:run:<run_id>:events` and `dream:run:<run_id>:proposal` are
    side-tables and skipped here.
    """
    out: dict[str, list[dict[str, Any]]] = {"cycle": [], "proposal": []}
    keys = redis_scan_keys(url, token, "dream:run:*")
    for k in keys:
        # Skip side-tables.
        if k.endswith(":events") or k.endswith(":proposal"):
            continue
        raw = redis_get(url, token, k)
        if not raw:
            continue
        try:
            rec = raw if isinstance(raw, dict) else json.loads(raw)
        except json.JSONDecodeError:
            continue
        # Guard: only dict records have run metadata fields. Skip lists / strings.
        if not isinstance(rec, dict):
            continue
        ts = parse_ts(rec.get("run_at") or rec.get("created_at"))
        if not ts:
            continue
        if start <= ts < end:
            run_id = rec.get("run_id", "")
            bucket = "proposal" if isinstance(run_id, str) and run_id.startswith("dpr_") else "cycle"
            out[bucket].append(rec)
    out["cycle"].sort(key=lambda r: r.get("run_at") or r.get("created_at") or "")
    out["proposal"].sort(key=lambda r: r.get("created_at") or r.get("run_at") or "")
    return out


def collect_judge_history(
    url: str, token: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in redis_scan_keys(url, token, "dream:judge:history:*"):
        raw = redis_get(url, token, k)
        if not raw:
            continue
        try:
            rec = raw if isinstance(raw, dict) else json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = parse_ts(rec.get("settled_at"))
        if not ts:
            continue
        if start <= ts < end:
            out.append(rec)
    out.sort(key=lambda r: r.get("settled_at", ""))
    return out


def collect_pending_judge_items(url: str, token: str) -> list[dict[str, Any]]:
    pending_set = redis_post(url, token, ["SMEMBERS", "dream:judge:pending"]) or []
    items: list[dict[str, Any]] = []
    for op_id in pending_set:
        item = redis_get(url, token, f"dream:judge:item:{op_id}")
        verdict = redis_get(url, token, f"dream:judge:verdict:{op_id}")
        if item:
            try:
                parsed_item = item if isinstance(item, dict) else json.loads(item)
            except json.JSONDecodeError:
                continue
            items.append({
                "op_id": op_id,
                "item": parsed_item,
                "verdict": verdict if isinstance(verdict, dict) else (json.loads(verdict) if verdict else None),
            })
    return items


def fetch_tripwire_status(
    worker_url: str, operator_token: str
) -> dict[str, Any] | None:
    if not worker_url or not operator_token:
        return None
    try:
        resp = requests.get(
            f"{worker_url}/ops/dream/tripwire_status",
            headers={"Authorization": f"Bearer {operator_token}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


# ----------------------------------------------------------------------------
# Markdown rendering
# ----------------------------------------------------------------------------
def _count_governed_operation(run: dict[str, Any], operation_type: str, legacy_key: str) -> int:
    counts = run.get("counts", {}) or {}
    if not isinstance(counts, dict):
        return 0
    selected_counts = counts.get("selected_counts")
    if isinstance(selected_counts, dict):
        value = selected_counts.get(operation_type)
        if isinstance(value, int):
            return value
    value = counts.get(legacy_key)
    return value if isinstance(value, int) else 0


def _count_layer2_operation(run: dict[str, Any], legacy_key: str, phase_key: str) -> int:
    counts = run.get("counts", {}) or {}
    if isinstance(counts, dict):
        value = counts.get(legacy_key)
        if isinstance(value, int):
            return value
    phases = run.get("phases", {}) or {}
    if not isinstance(phases, dict):
        return 0
    layer2 = phases.get("layer2_quarantine_and_demote", {}) or {}
    if not isinstance(layer2, dict):
        return 0
    value = layer2.get(phase_key)
    return value if isinstance(value, int) else 0


def _run_operation_counts(run: dict[str, Any]) -> dict[str, int]:
    counts = run.get("counts", {}) or {}
    if not isinstance(counts, dict):
        counts = {}
    return {
        "merged": _count_governed_operation(run, "duplicate_merge", "merged_duplicates"),
        "archived": _count_governed_operation(run, "archive_entry", "archived"),
        "promoted": _count_governed_operation(run, "promote_context_type", "promoted"),
        "marked_contested": _count_governed_operation(run, "mark_contested", "entries_marked_contested"),
        "quarantined": _count_layer2_operation(run, "quarantined", "quarantined_count"),
        "demoted": _count_layer2_operation(run, "demoted", "demoted_count"),
        "selected": counts.get("selected_operation_count", 0) if isinstance(counts.get("selected_operation_count"), int) else 0,
        "held": counts.get("held_operation_count", 0) if isinstance(counts.get("held_operation_count"), int) else 0,
        "applied": counts.get("applied_count", 0) if isinstance(counts.get("applied_count"), int) else 0,
    }


def _format_type_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    return ", ".join(f"{key}: {value[key]}" for key in sorted(value))


def render_digest(
    year: int,
    week: int,
    start: datetime,
    end: datetime,
    cycle_runs: list[dict[str, Any]],
    proposal_runs: list[dict[str, Any]],
    judge_history: list[dict[str, Any]],
    pending_judge: list[dict[str, Any]],
    tripwire_status: dict[str, Any] | None,
) -> str:
    lines: list[str] = []
    lines.append(f"# Dream + Forgetting — Weekly Digest, {year}-W{week:02d}")
    lines.append("")
    lines.append(f"**Window:** {start.isoformat()} → {end.isoformat()}  ")
    lines.append(f"**Generated:** {datetime.now(UTC).isoformat()}")
    lines.append("")

    # ── Top-line summary ──
    n_cycles_completed = sum(1 for r in cycle_runs if r.get("status") in {"completed", "completed_with_holds"})
    n_cycles_held = sum(1 for r in cycle_runs if r.get("status") == "held")
    n_cycles_skipped = sum(1 for r in cycle_runs if (r.get("status") or "").startswith("skipped"))
    n_cycles_failed = sum(1 for r in cycle_runs if r.get("status") == "failed")
    n_proposals = len(proposal_runs)

    total_selected = 0
    total_held = 0
    total_applied = 0
    total_applied_l1 = 0
    total_archived = 0
    total_promoted = 0
    total_merged = 0
    total_marked_contested = 0
    total_quarantined = 0
    total_demoted = 0
    for r in cycle_runs:
        run_counts = _run_operation_counts(r)
        total_selected += run_counts["selected"]
        total_held += run_counts["held"]
        total_applied += run_counts["applied"]
        total_archived += run_counts["archived"]
        total_promoted += run_counts["promoted"]
        total_merged += run_counts["merged"]
        total_marked_contested += run_counts["marked_contested"]
        total_quarantined += run_counts["quarantined"]
        total_demoted += run_counts["demoted"]
    total_applied_l1 = total_archived + total_promoted + total_merged + total_marked_contested

    judge_applied = sum(1 for r in judge_history if r.get("outcome") == "applied")
    judge_skipped = sum(1 for r in judge_history if r.get("outcome") == "skipped")
    judge_stale = sum(1 for r in judge_history if r.get("outcome") == "stale")

    lines.append("## Top-line summary")
    lines.append("")
    lines.append("| Metric | This week |")
    lines.append("|---|---|")
    lines.append(f"| Cycle runs (completed / held / skipped / failed) | {n_cycles_completed} / {n_cycles_held} / {n_cycles_skipped} / {n_cycles_failed} |")
    lines.append(f"| Proposal-only runs | {n_proposals} |")
    lines.append(f"| Governed operations selected / held / applied | {total_selected} / {total_held} / {total_applied} |")
    lines.append(f"| Total L1 operations applied | {total_applied_l1} |")
    lines.append(f"| L1 duplicate merges applied | {total_merged} |")
    lines.append(f"| L1 archives applied | {total_archived} |")
    lines.append(f"| L1 promotions applied | {total_promoted} |")
    lines.append(f"| L1 contested marks applied | {total_marked_contested} |")
    lines.append(f"| L2 quarantines applied | {total_quarantined} |")
    lines.append(f"| L2 tier demotions applied | {total_demoted} |")
    lines.append(f"| Opus judge decisions: applied / skipped / stale | {judge_applied} / {judge_skipped} / {judge_stale} |")
    lines.append(f"| Pending judge items (awaiting Mac script) | {len(pending_judge)} |")
    lines.append("")

    # ── Tripwire status ──
    lines.append("## Tripwire status")
    lines.append("")
    if tripwire_status is None:
        lines.append("_Worker tripwire status not fetched (no operator token or worker URL configured)._")
    elif "error" in tripwire_status:
        lines.append(f"**Error fetching tripwire status:** {tripwire_status['error']}")
    else:
        modes = tripwire_status.get("modes", {})
        for name, info in modes.items():
            eff = (info or {}).get("effective", "?")
            tripped = (info or {}).get("tripped", False)
            tr = (info or {}).get("trip_record")
            lines.append(f"- **{name}** — effective: `{eff}`, tripped: `{tripped}`")
            if tr:
                lines.append(f"  - reason: {tr.get('reason', '?')}")
                lines.append(f"  - tripped_at: {tr.get('tripped_at', '?')}")
                lines.append(f"  - source: {tr.get('source_tripwire', '?')}")
        tw = tripwire_status.get("tripwires", {})
        dest = tw.get("destructive_action_volume", {})
        retr = tw.get("retrieval_hit_collapse", {})
        if dest:
            lines.append(f"- destructive_action_volume tripped: `{dest.get('tripped', False)}` (threshold {dest.get('threshold')}, breaches {dest.get('consecutive_breaches')})")
        if retr:
            lines.append(f"- retrieval_hit_collapse tripped: `{retr.get('tripped', False)}` (threshold ratio {retr.get('threshold_ratio')}, breaches {retr.get('consecutive_breaches')})")
    lines.append("")

    # ── Per-cycle detail ──
    lines.append("## Cycle runs (chronological)")
    lines.append("")
    if not cycle_runs:
        lines.append("_No cycle runs this week._")
    else:
        for r in cycle_runs:
            run_id = r.get("run_id", "?")
            status = r.get("status", "?")
            counts = r.get("counts", {}) or {}
            phases = r.get("phases", {}) or {}
            l2 = phases.get("layer2_quarantine_and_demote", {}) or {}
            jq = phases.get("judge_queue", {}) or {}
            run_counts = _run_operation_counts(r)
            lines.append(f"### {run_id} — `{status}`")
            lines.append("")
            lines.append(f"- run_at: {r.get('run_at')}, completed_at: {r.get('completed_at')}")
            if r.get("auto_apply_mode") == "governed":
                lines.append(f"- governed: selected {run_counts['selected']}, applied {run_counts['applied']}, held {run_counts['held']}")
                lines.append(f"- proposed_by_type: {_format_type_counts(counts.get('operation_counts'))}")
                lines.append(f"- selected_by_type: {_format_type_counts(counts.get('selected_counts'))}")
            lines.append(f"- merged: {run_counts['merged']}, archived: {run_counts['archived']}, promoted: {run_counts['promoted']}, marked_contested: {run_counts['marked_contested']}")
            lines.append(f"- quarantined: {l2.get('quarantined_count', 0)}, demoted: {l2.get('demoted_count', 0)}, streak_reset: {l2.get('streak_reset_count', 0)}, streak_increment: {l2.get('streak_increment_count', 0)} (cap_hit: {l2.get('cap_hit', False)})")
            lines.append(f"- judge_queue: enqueued {jq.get('enqueued_count', 0)}, verdicts applied {jq.get('verdicts_applied_count', 0)}, skipped {jq.get('verdicts_skipped_count', 0)} (mode: {jq.get('opus_mode', '?')})")
            lines.append("")

    # ── Judge decisions detail ──
    lines.append("## Opus judge decisions (every one this week)")
    lines.append("")
    if not judge_history:
        lines.append("_No judge decisions this week._")
    else:
        for h in judge_history:
            outcome = h.get("outcome", "?")
            op_id = h.get("op_id", "?")
            verdict = (h.get("verdict") or {})
            item = (h.get("item") or {})
            lines.append(f"### {op_id} — outcome: `{outcome}`")
            lines.append(f"- op_type: `{item.get('op_type', '?')}`")
            lines.append(f"- verdict: `{verdict.get('verdict', '?')}` (model: {verdict.get('judge_model', '?')}, source: {verdict.get('judge_source', '?')})")
            reason = verdict.get("reason", "")
            if reason:
                lines.append(f"- reason: {reason}")
            settled = h.get("settled_at", "?")
            lines.append(f"- settled_at: {settled}")
            lines.append("")

    # ── Pending judge items ──
    lines.append("## Pending judge items (awaiting next nightly run)")
    lines.append("")
    if not pending_judge:
        lines.append("_None pending._")
    else:
        for p in pending_judge:
            it = p.get("item", {}) or {}
            verdict = p.get("verdict")
            status = "judged-not-yet-applied" if verdict else "awaiting-judge"
            lines.append(f"- `{p.get('op_id')}` — op_type: `{it.get('op_type')}` — status: {status}")

    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Generate weekly Dream digest markdown.")
    parser.add_argument("--week", default=None, help="ISO week, e.g. 2026-W20 (default: current week)")
    parser.add_argument("--out", default=None, help="output file path (default: scripts/reports/dream-weekly-YYYY-WW.md)")
    args = parser.parse_args()

    url = os.getenv("UPSTASH_REDIS_REST_URL", "")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        print("ERROR: UPSTASH_REDIS_REST_URL / TOKEN not set in env.", file=sys.stderr)
        return 2

    if args.week:
        year, week = parse_iso_week(args.week)
    else:
        year, week = current_iso_week()
    start, end = week_range(year, week)

    print(f"Generating digest for {year}-W{week:02d} ({start.isoformat()} → {end.isoformat()})")

    try:
        runs = collect_runs(url, token, start, end)
        history = collect_judge_history(url, token, start, end)
        pending = collect_pending_judge_items(url, token)
    except Exception as e:
        print(f"ERROR fetching Redis data: {e}", file=sys.stderr)
        return 1

    worker_url = os.getenv("DREAM_MCP_BASE_URL", "https://mcp.dancing-ganesh.com").rstrip("/")
    operator_token = os.getenv("DREAM_OPERATOR_TOKEN", "")
    tripwire_status = fetch_tripwire_status(worker_url, operator_token)

    md = render_digest(
        year=year,
        week=week,
        start=start,
        end=end,
        cycle_runs=runs["cycle"],
        proposal_runs=runs["proposal"],
        judge_history=history,
        pending_judge=pending,
        tripwire_status=tripwire_status,
    )

    out_path = Path(args.out) if args.out else REPORTS_DIR / f"dream-weekly-{year}-W{week:02d}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
