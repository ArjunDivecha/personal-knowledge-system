#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: backfill_mention_count.py
=============================================================================

PURPOSE:
PRD Phase 1 R1.7. One-time backfill that recomputes `mention_count` from the
number of distinct `source_conversations` for entries that already span
multiple sources but were never merged (so their mention_count is stale at 1
or null). Restores the recurrence signal that un-flattens salience (P2/P3).

This is intended to run AFTER the first semantic-dedup pass (which unions
source_conversations during merges). It can also be run standalone to fix
historically under-counted entries.

SAFETY:
- DRY-RUN BY DEFAULT. Prints what would change; writes nothing.
- Requires --apply to persist. --apply updates the Redis entry JSON and
  patches the vector metadata mention_count to match.
- Per the remediation rollout, run --dry-run and review before --apply.

INPUT:  Upstash Redis + Vector via distillation storage clients.
OUTPUT: scripts/reports/backfill_mention_count_<ISO8601>.json (always).

USAGE:
    python scripts/backfill_mention_count.py             # dry-run (default)
    python scripts/backfill_mention_count.py --apply     # persist changes
    python scripts/backfill_mention_count.py --limit 50  # cap entries touched

VERSION: 1.0
LAST UPDATED: 2026-05-29
=============================================================================
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from _memory_migration import append_report, ensure_runtime_dirs, utc_now_iso  # noqa: E402
from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402


def distinct_sources(entry) -> int:
    meta = getattr(entry, "metadata", None)
    sources = getattr(meta, "source_conversations", None) or []
    return len({s for s in sources if s})


def current_mention(entry) -> int:
    meta = getattr(entry, "metadata", None)
    mc = getattr(meta, "mention_count", None)
    return int(mc) if isinstance(mc, int) else 1


def is_archived(entry) -> bool:
    meta = getattr(entry, "metadata", None)
    return bool(getattr(meta, "archived", False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill mention_count from distinct source_conversations (PRD R1.7).")
    parser.add_argument("--apply", action="store_true", help="persist changes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="max entries to update")
    parser.add_argument("--include-archived", action="store_true", help="also backfill archived entries")
    args = parser.parse_args()

    ensure_runtime_dirs()
    redis = RedisClient()
    vector = VectorClient()

    knowledge = redis.get_all_knowledge_entries()
    candidates = []
    for entry in knowledge:
        if not args.include_archived and is_archived(entry):
            continue
        sources = distinct_sources(entry)
        mc = current_mention(entry)
        if sources > mc:
            candidates.append((entry, mc, sources))

    candidates.sort(key=lambda t: t[2] - t[1], reverse=True)
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"[backfill] knowledge entries scanned: {len(knowledge)}")
    print(f"[backfill] entries with under-counted mention_count: {len(candidates)}")
    for entry, mc, sources in candidates[:25]:
        label = getattr(entry, "domain", None) or entry.id
        print(f"  {entry.id}  '{str(label)[:48]}'  mention_count {mc} -> {sources}")
    if len(candidates) > 25:
        print(f"  ... and {len(candidates) - 25} more")

    applied = 0
    if args.apply:
        for entry, _mc, sources in candidates:
            entry.metadata.mention_count = sources
            redis.save_knowledge_entry(entry)
            try:
                vector.update_entry_metadata(entry.id, {"mention_count": sources})
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] vector metadata patch failed for {entry.id}: {exc}")
            applied += 1
        print(f"[backfill] APPLIED mention_count updates to {applied} entries.")
    else:
        print("[backfill] DRY-RUN — no changes written. Re-run with --apply to persist.")

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "mode": "apply" if args.apply else "dry_run",
        "knowledge_scanned": len(knowledge),
        "under_counted": len(candidates),
        "applied": applied,
        "samples": [
            {"id": e.id, "from": mc, "to": s, "domain": getattr(e, "domain", None)}
            for e, mc, s in candidates[:100]
        ],
    }
    ts = report["generated_at"].replace(":", "").replace("-", "")
    path = append_report(f"backfill_mention_count_{ts}.json", report)
    print(f"[backfill] report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
