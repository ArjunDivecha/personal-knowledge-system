"""
=============================================================================
SCRIPT NAME: repair_vector_drift.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/verify_memory_consistency_<UTCSTAMP>.json
  : a verify-memory-consistency report (pass its path as --report). Only the
  entries it flags as `missing_vector` or `vector_metadata_mismatch` are
  touched.
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/.env
  : Upstash Redis + Vector + OpenAI credentials.

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/vector_drift_repair_<UTCSTAMP>.json
  : run report, written incrementally.
- (network, PRODUCTION Upstash VECTOR index, WRITE — only with --apply) one
  upsert per repaired entry.

VERSION: 1.0
LAST UPDATED: 2026-07-14
AUTHOR: Claude (Opus 4.8) for Arjun Divecha

DESCRIPTION:
Repairs drift between Redis (the source of truth) and the Upstash Vector index.

Background, plainly: every memory lives in two places — Redis holds the real
entry, and the vector index holds an embedding plus a small copy of its
metadata (tier, state, archived, ...) used to filter searches. Those two can
fall out of step if a process dies midway through writing both.

That is exactly what happened on 2026-07-14: Dream runs crashed mid-apply after
exceeding Cloudflare's per-invocation subrequest limit, leaving
  * 14 entries ACTIVE in Redis but with NO vector at all — invisible to
    semantic search, which is real (if silent) retrieval loss; and
  * 89 entries whose vector metadata (e.g. injection_tier) had gone stale
    relative to Redis — degraded ranking/filtering.
The 2026-06-26 verify report shows 0 issues, so this drift is new.

THE FIX: for each flagged entry, re-derive its embedding from the CURRENT Redis
entry and upsert the vector with freshly-built metadata.

SAFETY PROPERTIES:
1. VECTOR-ONLY. This script NEVER writes to Redis. Redis is the source of
   truth and is already correct; only the derived vector copy is wrong. That
   makes the repair inherently safe — worst case it rewrites a vector with the
   same values.
2. RAW DICTS. The entry is read, and its metadata built, from raw parsed JSON.
   The typed KnowledgeEntry dataclass is never used: it does not declare
   several Worker-managed fields (revision, injection_quarantine, ...) and
   round-tripping through it would silently DROP them (the lesson recorded in
   the PKS-CONTRADICTION-LIFECYCLE-001 ledger).
3. SKIPS ARCHIVED. An archived entry is SUPPOSED to have no vector. If a
   flagged entry is archived in Redis, it is skipped, not resurrected.
4. DRY-RUN DEFAULT, double-gated apply.

DEPENDENCIES: upstash_redis, upstash_vector, openai (via ingestion/core/config)

USAGE:
  # Dry run — lists exactly what would be repaired, zero writes.
  distillation/venv/bin/python scripts/repair_vector_drift.py \
      --report scripts/reports/verify_memory_consistency_<STAMP>.json

  # Apply.
  distillation/venv/bin/python scripts/repair_vector_drift.py \
      --report ... --apply --i-reviewed-the-dry-run
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_DIR = REPO_ROOT / "ingestion"
REPORTS_DIR = REPO_ROOT / "scripts" / "reports"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

REPAIRABLE_KINDS = {"missing_vector", "vector_metadata_mismatch"}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def build_embedding_text(entry: dict[str, Any]) -> str:
    """Mirror StorageClient.save_knowledge_entry's default embedding text."""
    metadata = entry.get("metadata") or {}
    repo_hint = metadata.get("github_repo")
    base = f"{entry.get('domain', '')}: {entry.get('current_view', '') or ''}"
    return f"{repo_hint}: {base}" if repo_hint else base


def main() -> int:
    ap = argparse.ArgumentParser(description="Repair Redis->Vector drift flagged by verify-memory-consistency.")
    ap.add_argument("--report", required=True, help="Path to a verify_memory_consistency_*.json report")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-reviewed-the-dry-run", action="store_true")
    args = ap.parse_args()

    if args.apply and not args.i_reviewed_the_dry_run:
        raise SystemExit("❌ --apply requires --i-reviewed-the-dry-run")

    from core.storage import StorageClient  # noqa: E402

    report_in = json.loads(Path(args.report).read_text())
    flagged = [i for i in (report_in.get("issues") or []) if i.get("kind") in REPAIRABLE_KINDS]
    by_kind: dict[str, int] = {}
    for i in flagged:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1

    storage = StorageClient()
    redis = storage.redis
    vector = storage.vector

    mode = "APPLY" if args.apply else "DRY RUN"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S+0000")
    out_path = REPORTS_DIR / f"vector_drift_repair_{stamp}.json"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print(f"Vector-drift repair — {mode}")
    print(f"source report: {Path(args.report).name}")
    print(f"flagged: {len(flagged)}  {by_kind}")
    print("=" * 68)

    repaired: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    def flush() -> None:
        out_path.write_text(json.dumps({
            "mode": mode,
            "generated_at": utc_now(),
            "source_report": str(args.report),
            "flagged_count": len(flagged),
            "by_kind": by_kind,
            "repaired_count": len(repaired),
            "skipped_count": len(skipped),
            "repaired": repaired,
            "skipped": skipped,
        }, indent=1))

    for idx, issue in enumerate(flagged, 1):
        entry_id = issue.get("entry_id")
        if not entry_id:
            skipped.append({"id": None, "reason": "issue has no entry_id"})
            continue

        raw = redis.get(f"knowledge:{entry_id}")
        if raw is None:
            skipped.append({"id": entry_id, "reason": "not found in Redis (project entry or deleted)"})
            continue
        entry = json.loads(raw) if isinstance(raw, str) else raw
        metadata = entry.get("metadata") or {}

        # SAFETY 3 — archived entries are meant to have no vector.
        if metadata.get("archived"):
            skipped.append({"id": entry_id, "reason": "archived in Redis — correctly has no vector"})
            continue

        if not args.apply:
            repaired.append({
                "id": entry_id,
                "kind": issue["kind"],
                "domain": str(entry.get("domain", ""))[:70],
                "redis_injection_tier": metadata.get("injection_tier"),
                "redis_state": entry.get("state"),
            })
            continue

        text = build_embedding_text(entry)
        embedding = storage.generate_embedding(text)
        vector.upsert(vectors=[{
            "id": entry_id,
            "vector": embedding,
            "metadata": storage._build_vector_metadata(entry),  # noqa: SLF001 — reuse the canonical builder
        }])
        repaired.append({
            "id": entry_id,
            "kind": issue["kind"],
            "domain": str(entry.get("domain", ""))[:70],
            "repaired_at": utc_now(),
        })
        if idx % 20 == 0 or idx == len(flagged):
            print(f"  repaired {len(repaired)}/{len(flagged)}")
            flush()

    flush()
    print()
    if args.apply:
        print(f"✅ Repaired {len(repaired)} vectors; skipped {len(skipped)}.")
    else:
        print(f"DRY RUN — zero writes. Would repair {len(repaired)}; skip {len(skipped)}.")
        for s in skipped[:5]:
            print(f"   SKIP {s['id']}: {s['reason']}")
        print("\nTo apply: rerun with --apply --i-reviewed-the-dry-run")
    print(f"Report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
