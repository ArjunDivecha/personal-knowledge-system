#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: salience_recompute_preview.py
=============================================================================

PURPOSE:
PRD Phase 2 dry-run. Recomputes salience for all active entries using the
UPDATED compute_salience (with the continuous evidence-richness lever) and
reports the new distribution vs the stored one — specifically M3
(max single-value share). READ-ONLY: writes nothing; proves the lever
un-flattens salience before any backfill apply.

INPUT:  Upstash Redis via distillation storage client (read-only).
OUTPUT: scripts/reports/salience_recompute_preview_<ISO8601>.json + stdout.

USAGE:
    python scripts/salience_recompute_preview.py

VERSION: 1.0
LAST UPDATED: 2026-05-29
=============================================================================
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from _memory_migration import append_report, ensure_runtime_dirs, utc_now_iso  # noqa: E402
from storage.redis_client import RedisClient  # noqa: E402
from utils.salience import compute_salience  # noqa: E402


def is_archived(entry) -> bool:
    meta = getattr(entry, "metadata", None)
    return bool(getattr(meta, "archived", False))


def stored_salience(entry) -> float | None:
    meta = getattr(entry, "metadata", None)
    s = getattr(meta, "salience_score", None)
    return round(float(s), 4) if isinstance(s, (int, float)) else None


def share_table(values: list[float], top_n: int = 15):
    total = max(1, len(values))
    counts = Counter(values)
    top = [{"value": v, "count": c, "share": round(c / total, 4)} for v, c in counts.most_common(top_n)]
    max_share = max((t["share"] for t in top), default=0.0)
    return max_share, top


def main() -> int:
    ensure_runtime_dirs()
    redis = RedisClient()
    knowledge = [e for e in redis.get_all_knowledge_entries() if not is_archived(e)]
    projects = [e for e in redis.get_all_project_entries() if not is_archived(e)]
    active = knowledge + projects

    stored_vals = [s for s in (stored_salience(e) for e in active) if s is not None]
    new_vals = [compute_salience(e) for e in active]

    stored_max, stored_top = share_table(stored_vals)
    new_max, new_top = share_table(new_vals)

    # How many entries' salience changed, and by how much.
    changed = 0
    deltas = []
    for e in active:
        old = stored_salience(e)
        new = compute_salience(e)
        if old is None:
            continue
        if abs(new - old) > 1e-9:
            changed += 1
            deltas.append(new - old)
    avg_delta = round(sum(deltas) / len(deltas), 5) if deltas else 0.0

    print(f"[salience-preview] active entries: {len(active)}")
    print(f"[salience-preview] M3 max single-value share  stored={stored_max:.1%}  new={new_max:.1%}")
    print(f"[salience-preview] entries changed: {changed}/{len(active)}  avg_delta={avg_delta:+.4f}")
    print(f"[salience-preview] stored top value: {stored_top[0] if stored_top else 'n/a'}")
    print(f"[salience-preview] new top value:    {new_top[0] if new_top else 'n/a'}")
    print(f"[salience-preview] distinct salience values: stored={len(set(stored_vals))}  new={len(set(new_vals))}")

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "mode": "dry_run_preview",
        "active_entries": len(active),
        "m3_stored": {"max_single_value_share": round(stored_max, 4), "distinct_values": len(set(stored_vals)), "top_values": stored_top},
        "m3_recomputed": {"max_single_value_share": round(new_max, 4), "distinct_values": len(set(new_vals)), "top_values": new_top},
        "entries_changed": changed,
        "avg_delta": avg_delta,
    }
    ts = report["generated_at"].replace(":", "").replace("-", "")
    path = append_report(f"salience_recompute_preview_{ts}.json", report)
    print(f"[salience-preview] report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
