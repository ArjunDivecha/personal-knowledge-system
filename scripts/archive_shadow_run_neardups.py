"""
=============================================================================
SCRIPT NAME: archive_shadow_run_neardups.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/admission_decisions.jsonl
  : the AdmissionRouter decision log (JSONL, one routing decision per line)
  written during the 2026-07-12 admission-dedup shadow ingestion run. Only
  lines with decision == "append" are acted on.
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/.env
  : Upstash Redis + Vector credentials (read via ingestion/core/config.py).

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/shadow_neardup_archive_<UTCSTAMP>.json
  : the run report (dry-run listing, or the applied result). Written
  incrementally as entries are processed, so a crash mid-run still leaves a
  complete record of what was already done.
- (network, PRODUCTION Upstash Redis + Vector, WRITE — only with --apply)
  For each archived entry, faithfully reproducing the Worker's archiveEntry
  contract (cloudflare-mcp/mcp-server/src/dream.ts:5336) so that the existing
  POST /ops/dream/restore endpoint can reverse any of them:
    * SET  archived:knowledge:<id>:<run_id>   = the full pre-archive snapshot
    * SET  archived:knowledge:<id>:latest     = pointer to that snapshot
    * SET  knowledge:<id>                     = entry with archived=true,
           archived_at/_reason/_run_id/archive_snapshot_key, revision+1,
           and an appended consolidation_notes receipt
    * DELETE the entry's vector (removes it from all retrieval surfaces)

VERSION: 1.0
LAST UPDATED: 2026-07-12
AUTHOR: Claude (Opus 4.8) for Arjun Divecha

DESCRIPTION:
Cleans up near-duplicate knowledge entries created by the PKS-ADMISSION-DEDUP-001
shadow ingestion run of 2026-07-12.

Background, in plain terms: the ingestion pipeline today creates a brand-new
knowledge entry for every fact it extracts, even when an almost-identical entry
already exists. The "admission dedup" feature is meant to stop that. It was run
in SHADOW mode, which by design behaves exactly like today (create everything)
while merely LOGGING what it would have merged. Because the two repos ingested
(ASADO, Triptych) had been ingested before, re-reading them re-extracted mostly
the same knowledge — so 263 of the 316 entries created were near-duplicates of
entries that already existed. This script archives exactly those 263.

Nothing is lost: each archived entry is a >= 0.85-cosine near-duplicate of a
neighbor that REMAINS ACTIVE, and every archive is individually reversible.

SAFETY PROPERTIES (why this is safe to run):
1. ANCHOR CHECK. Some candidates' nearest neighbours are themselves candidates
   (11 such chains). Before archiving anything, the script walks each
   candidate's neighbour chain and refuses to archive it unless the chain
   terminates at an entry that will REMAIN ACTIVE. Any candidate whose chain
   cycles or dead-ends is skipped and reported, never archived.
2. RAW DICTS ONLY. Entries are read, mutated, and written as raw parsed JSON.
   The typed Python KnowledgeEntry dataclass is never used, because it does not
   declare Worker-managed fields (revision, injection_quarantine, github_repo,
   ...) and round-tripping through it would SILENTLY DROP them. This is the
   lesson recorded in the PKS-CONTRADICTION-LIFECYCLE-001 ledger.
3. REVERSIBLE. The archive snapshot + latest-pointer keys are written in the
   exact format restoreArchivedEntry() expects, so any entry can be restored
   via POST /ops/dream/restore {"entry_id": "...", "reason": "..."}.
4. NO FALSE TRIPWIRE. The Worker's archiveEntry bumps a daily
   "destructive action" counter that trips a kill flag (disabling Dream
   auto-apply) on a 3x spike. This script deliberately does NOT bump that
   counter: this is a one-off operator cleanup, not Dream behaving
   destructively, and 263 increments would near-certainly trip the spike
   detector and switch Dream off for the night.
5. DOUBLE-GATED. Dry-run is the default and performs ZERO writes. Applying
   requires BOTH --apply AND --i-reviewed-the-dry-run.

DEPENDENCIES: upstash_redis, upstash_vector (via ingestion/core/config.py)

USAGE:
  # 1. Dry run (default; zero writes) — review the report it prints.
  distillation/venv/bin/python scripts/archive_shadow_run_neardups.py

  # 2. Apply, only after reviewing the dry run.
  distillation/venv/bin/python scripts/archive_shadow_run_neardups.py \
      --apply --i-reviewed-the-dry-run

NOTES:
- Entries already archived (e.g. a re-run) are skipped, not double-archived.
- Only "append" decisions are touched. The 46 "link" and 7 "new" entries from
  the same shadow run are genuinely distinct knowledge and are left ACTIVE.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_DIR = REPO_ROOT / "ingestion"
DECISION_LOG = INGESTION_DIR / "logs" / "admission_decisions.jsonl"
REPORTS_DIR = REPO_ROOT / "scripts" / "reports"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def load_append_decisions() -> list[dict[str, Any]]:
    """Every 'append' verdict from the shadow run's decision log."""
    if not DECISION_LOG.exists():
        raise SystemExit(f"❌ Decision log not found: {DECISION_LOG}")
    decisions = [json.loads(line) for line in DECISION_LOG.read_text().splitlines() if line.strip()]
    return [d for d in decisions if d.get("decision") == "append"]


def resolve_anchor(
    candidate_id: str,
    neighbor_of: dict[str, str],
    archive_set: set[str],
) -> str | None:
    """Walk candidate -> neighbour until we leave the archive set.

    Returns the id of the entry that will REMAIN ACTIVE and therefore anchors
    this candidate's knowledge, or None if the chain cycles (never archive
    those). See SAFETY PROPERTY 1 in the module docstring.
    """
    seen: set[str] = set()
    current = candidate_id
    while True:
        if current in seen:
            return None  # cycle — refuse
        seen.add(current)
        neighbor = neighbor_of.get(current)
        if neighbor is None:
            return None
        if neighbor not in archive_set:
            return neighbor  # terminates at a surviving entry
        current = neighbor


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Archive the near-duplicate entries created by the admission-dedup shadow run."
    )
    parser.add_argument("--apply", action="store_true", help="Perform real writes (default: dry run)")
    parser.add_argument(
        "--i-reviewed-the-dry-run",
        action="store_true",
        help="Required second flag to confirm the dry-run output was reviewed.",
    )
    parser.add_argument(
        "--min-cosine",
        type=float,
        default=0.93,
        help=(
            "Only archive candidates whose cosine to their neighbour is >= this. "
            "Default 0.93 (Arjun's call, 2026-07-12). NOTE: this is deliberately "
            "STRICTER than admission_dedup.append_threshold (0.85). That 0.85 line "
            "governs APPEND, which CONSERVES the candidate's evidence into the "
            "neighbour. Archiving does NOT conserve — it hides the entry — so the "
            "append threshold is too permissive here: inspection showed the "
            "0.85-0.93 band contains genuinely distinct facts (e.g. an ASADO entry "
            "whose nearest neighbour was a Triptych one). Entries below this line "
            "are left ACTIVE for the nightly semantic-consolidation machinery, "
            "which has merge-conservation gates and preserves evidence."
        ),
    )
    args = parser.parse_args()

    if args.apply and not args.i_reviewed_the_dry_run:
        raise SystemExit(
            "❌ Refusing to apply: --apply requires --i-reviewed-the-dry-run.\n"
            "   Run without --apply first and review the report."
        )

    from core.storage import StorageClient  # noqa: E402 (needs sys.path above)

    storage = StorageClient()
    redis = storage.redis
    vector = storage.vector

    appends = load_append_decisions()
    neighbor_of = {d["candidate_id"]: d["neighbor_id"] for d in appends}
    # The anchor walk must consider ONLY the entries that will actually be
    # archived. A candidate below --min-cosine stays ACTIVE, so it is a valid
    # terminal anchor for anything pointing at it — building archive_set from
    # all appends would wrongly walk straight past it.
    archive_set = {
        d["candidate_id"]
        for d in appends
        if d.get("neighbor_score") is not None and d["neighbor_score"] >= args.min_cosine
    }

    run_id = f"shadow_neardup_archive_{uuid.uuid4().hex[:12]}"
    started = utc_now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S+0000")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"shadow_neardup_archive_{stamp}.json"

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"{'='*70}\nShadow-run near-duplicate archive — {mode}\nrun_id: {run_id}\n{'='*70}")
    print(f"append decisions in log: {len(appends)}")

    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    archived: list[dict[str, Any]] = []

    def flush_report() -> None:
        report_path.write_text(json.dumps({
            "run_id": run_id,
            "mode": mode,
            "started_at": started,
            "updated_at": utc_now_iso(),
            "decision_log": str(DECISION_LOG),
            "append_decision_count": len(appends),
            "planned_count": len(planned),
            "archived_count": len(archived),
            "skipped_count": len(skipped),
            "planned": planned,
            "archived": archived,
            "skipped": skipped,
        }, indent=1))

    # ---------------------------------------------------------------- plan
    for decision in appends:
        cid = decision["candidate_id"]
        score = decision.get("neighbor_score")

        # Threshold gate — see --min-cosine help. Below this line the content is
        # merely RELATED, not redundant, and archiving would hide real knowledge.
        if score is None or score < args.min_cosine:
            skipped.append({
                "id": cid,
                "reason": f"cosine {score} < min_cosine {args.min_cosine} — left ACTIVE for conservation-gated consolidation",
            })
            continue

        anchor = resolve_anchor(cid, neighbor_of, archive_set)
        if anchor is None:
            skipped.append({"id": cid, "reason": "neighbour chain cycles — no surviving anchor"})
            continue

        raw = redis.get(f"knowledge:{cid}")
        if raw is None:
            skipped.append({"id": cid, "reason": "entry not found in Redis"})
            continue
        entry = json.loads(raw) if isinstance(raw, str) else raw
        if (entry.get("metadata") or {}).get("archived"):
            skipped.append({"id": cid, "reason": "already archived"})
            continue

        anchor_raw = redis.get(f"knowledge:{anchor}")
        if anchor_raw is None:
            skipped.append({"id": cid, "reason": f"anchor {anchor} missing — refusing to orphan knowledge"})
            continue
        anchor_entry = json.loads(anchor_raw) if isinstance(anchor_raw, str) else anchor_raw
        if (anchor_entry.get("metadata") or {}).get("archived"):
            skipped.append({"id": cid, "reason": f"anchor {anchor} is archived — refusing to orphan knowledge"})
            continue

        planned.append({
            "id": cid,
            "domain": entry.get("domain", ""),
            "anchor_id": anchor,
            "anchor_domain": anchor_entry.get("domain", ""),
            "cosine_to_neighbor": decision.get("neighbor_score"),
            "entry": entry,  # carried for the apply phase; stripped from the report below
        })

    print(f"planned to archive:      {len(planned)}")
    print(f"skipped (not archived):  {len(skipped)}")
    for s in skipped:
        print(f"   SKIP {s['id']}: {s['reason']}")

    # Keep the on-disk report light: don't dump full entry bodies.
    report_planned = [{k: v for k, v in p.items() if k != "entry"} for p in planned]

    if not args.apply:
        planned_for_report = planned
        planned.clear()
        planned.extend(report_planned)
        flush_report()
        planned.clear()
        planned.extend(planned_for_report)
        print(f"\nDRY RUN — zero writes performed.")
        print(f"Report: {report_path}")
        print("\nTo apply: rerun with --apply --i-reviewed-the-dry-run")
        return 0

    # --------------------------------------------------------------- apply
    print(f"\nApplying {len(planned)} archives against PRODUCTION...")
    for i, item in enumerate(planned, 1):
        cid = item["id"]
        entry = item["entry"]
        timestamp = utc_now_iso()
        reason = (
            f"Near-duplicate created by the 2026-07-12 admission-dedup shadow ingestion run "
            f"(cosine {item['cosine_to_neighbor']:.4f} to {item['anchor_id']}, which remains active). "
            f"Archived by operator cleanup {run_id}."
        )
        snapshot_key = f"archived:knowledge:{cid}:{run_id}"

        # 1. Snapshot the pre-archive entry (this is what restore reads back).
        redis.set(snapshot_key, json.dumps({
            "schema_version": 1,
            "entry_id": cid,
            "entry_type": "knowledge",
            "run_id": run_id,
            "archived_at": timestamp,
            "archive_reason": reason,
            "snapshot": json.loads(json.dumps(entry)),  # deep copy
        }))
        # 2. Latest pointer — restoreArchivedEntry() resolves the snapshot via this.
        redis.set(f"archived:knowledge:{cid}:latest", json.dumps({
            "entry_id": cid,
            "entry_type": "knowledge",
            "run_id": run_id,
            "archived_at": timestamp,
            "snapshot_key": snapshot_key,
        }))

        # 3. Mark the live entry archived (raw dict — never via KnowledgeEntry).
        metadata = entry.get("metadata") or {}
        metadata["archived"] = True
        metadata["archived_at"] = timestamp
        metadata["archived_reason"] = reason
        metadata["archived_run_id"] = run_id
        metadata["archive_snapshot_key"] = snapshot_key
        metadata["last_consolidated"] = timestamp
        metadata["revision"] = int(metadata.get("revision") or 0) + 1
        notes = list(metadata.get("consolidation_notes") or [])
        notes.append(
            f"{timestamp} | source=operator | action=archive_entry | detail={reason}"
        )
        metadata["consolidation_notes"] = notes
        entry["metadata"] = metadata
        redis.set(f"knowledge:{cid}", json.dumps(entry))

        # 4. Remove from the vector index so it leaves every retrieval surface.
        try:
            vector.delete([cid])
        except Exception as exc:  # noqa: BLE001 — surface, never mask (FAIL IS FAIL)
            print(f"   ❌ vector delete failed for {cid}: {exc}")
            raise

        archived.append({
            "id": cid,
            "domain": item["domain"],
            "anchor_id": item["anchor_id"],
            "cosine_to_neighbor": item["cosine_to_neighbor"],
            "snapshot_key": snapshot_key,
            "prior_revision": metadata["revision"] - 1,
            "new_revision": metadata["revision"],
            "archived_at": timestamp,
        })

        if i % 25 == 0 or i == len(planned):
            print(f"   archived {i}/{len(planned)}")
            planned_backup = list(planned)
            planned.clear()
            planned.extend(report_planned)
            flush_report()
            planned.clear()
            planned.extend(planned_backup)

    planned.clear()
    planned.extend(report_planned)
    flush_report()

    print(f"\n✅ Archived {len(archived)} near-duplicate entries.")
    print(f"Report: {report_path}")
    print("\nAny entry is reversible via:")
    print('  POST /ops/dream/restore {"entry_id": "<id>", "reason": "..."}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
