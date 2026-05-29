#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: retier_by_percentile.py
=============================================================================

PURPOSE:
PRD Phase 3 (R3.1/R3.3). Re-assign injection_tier across the active corpus by
salience percentile instead of the static context_type map that produced the
74% Tier-1 glut. Top tier_1_top_pct -> Tier 1, next tier_2_next_pct -> Tier 2,
remainder -> Tier 3; identity_floor_context_types never fall below Tier 2.

SAFETY:
- DRY-RUN BY DEFAULT. Prints the new tier distribution (M1/M2) vs current and
  writes a report; writes nothing to the store.
- --apply persists: updates each entry's metadata.injection_tier in Redis and
  patches the vector metadata. Before applying it writes a one-shot rollback
  snapshot of the prior tiers to scripts/reports/ so the change is reversible.
- Recompute uses the live (Phase 2) compute_salience.

USAGE:
    python scripts/retier_by_percentile.py            # dry-run (default)
    python scripts/retier_by_percentile.py --apply     # persist + rollback file
    python scripts/retier_by_percentile.py --limit 100

VERSION: 1.0
LAST UPDATED: 2026-05-29
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from _memory_migration import append_report, ensure_runtime_dirs, utc_now_iso  # noqa: E402
from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402
from utils.salience import assign_tiers_by_percentile, compute_salience, load_memory_policy  # noqa: E402


def is_archived(entry) -> bool:
    meta = getattr(entry, "metadata", None)
    return bool(getattr(meta, "archived", False))


def current_tier(entry) -> int | None:
    meta = getattr(entry, "metadata", None)
    t = getattr(meta, "injection_tier", None)
    return t if isinstance(t, int) else None


def dist(counter: Counter, total: int) -> dict:
    return {f"tier_{t}": {"count": counter.get(t, 0), "share": round(counter.get(t, 0) / max(1, total), 4)} for t in (1, 2, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-tier active entries by salience percentile (PRD Phase 3).")
    parser.add_argument("--apply", action="store_true", help="persist new tiers (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="cap entries changed (for staged apply)")
    args = parser.parse_args()

    ensure_runtime_dirs()
    policy = load_memory_policy()
    redis = RedisClient()
    vector = VectorClient()

    knowledge = [e for e in redis.get_all_knowledge_entries() if not is_archived(e)]
    projects = [e for e in redis.get_all_project_entries() if not is_archived(e)]
    active = knowledge + projects
    total = len(active)

    salience_by_id = {str(e.id): compute_salience(e) for e in active}
    ctype_by_id = {str(e.id): getattr(getattr(e, "metadata", None), "context_type", None) for e in active}
    new_tiers = assign_tiers_by_percentile(salience_by_id, ctype_by_id, policy)

    cur_counter: Counter = Counter()
    new_counter: Counter = Counter()
    changes = []
    floor_types = set((policy.get("tier_percentiles", {}) or {}).get("identity_floor_context_types", []) or [])
    floor_violations = 0
    by_id = {str(e.id): e for e in active}

    for eid, e in by_id.items():
        cur = current_tier(e)
        new = new_tiers.get(eid)
        if cur in (1, 2, 3):
            cur_counter[cur] += 1
        new_counter[new] += 1
        if ctype_by_id.get(eid) in floor_types and new > 2:
            floor_violations += 1
        if cur != new:
            changes.append((eid, cur, new))

    print(f"[re-tier] active entries: {total}")
    print(f"[re-tier] CURRENT  tier1={cur_counter.get(1,0)} ({cur_counter.get(1,0)/max(1,total):.1%})  "
          f"tier2={cur_counter.get(2,0)} ({cur_counter.get(2,0)/max(1,total):.1%})  tier3={cur_counter.get(3,0)}")
    print(f"[re-tier] PROPOSED tier1={new_counter.get(1,0)} ({new_counter.get(1,0)/max(1,total):.1%})  "
          f"tier2={new_counter.get(2,0)} ({new_counter.get(2,0)/max(1,total):.1%})  tier3={new_counter.get(3,0)}")
    print(f"[re-tier] entries changing tier: {len(changes)}")
    print(f"[re-tier] identity-floor violations (should be 0): {floor_violations}")

    applied = 0
    rollback_path = None
    if args.apply:
        to_change = changes[: args.limit] if args.limit else changes
        # Rollback snapshot first.
        rollback = {"generated_at": utc_now_iso(), "tiers_before": {eid: cur for eid, cur, _ in to_change}}
        ts = utc_now_iso().replace(":", "").replace("-", "")
        rollback_path = append_report(f"retier_rollback_{ts}.json", rollback)
        print(f"[re-tier] rollback snapshot -> {rollback_path}")
        for eid, _cur, new in to_change:
            e = by_id[eid]
            e.metadata.injection_tier = new
            if eid.startswith("pe_"):
                redis.save_project_entry(e)
            else:
                redis.save_knowledge_entry(e)
            try:
                vector.update_entry_metadata(eid, {"injection_tier": new})
            except Exception as exc:  # noqa: BLE001
                print(f"  [warn] vector patch failed for {eid}: {exc}")
            applied += 1
        print(f"[re-tier] APPLIED new tier to {applied} entries.")
    else:
        print("[re-tier] DRY-RUN — no changes written. Re-run with --apply to persist.")

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "mode": "apply" if args.apply else "dry_run",
        "active_entries": total,
        "current_distribution": dist(cur_counter, total),
        "proposed_distribution": dist(new_counter, total),
        "entries_changing": len(changes),
        "identity_floor_violations": floor_violations,
        "applied": applied,
        "rollback_path": str(rollback_path) if rollback_path else None,
        "sample_changes": [{"id": e, "from": c, "to": n} for e, c, n in changes[:50]],
    }
    ts = report["generated_at"].replace(":", "").replace("-", "")
    path = append_report(f"retier_by_percentile_{ts}.json", report)
    print(f"[re-tier] report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
