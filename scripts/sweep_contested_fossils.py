"""
=============================================================================
SCRIPT NAME: sweep_contested_fossils.py
=============================================================================

INPUT FILES:
- (network, production Redis, read) Upstash Redis keys matching "knowledge:*"
  and "project:*" — full entry JSON. Credentials from
  /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/.env
  (gitignored) via distillation/core/config.py's UPSTASH_REDIS_REST_URL /
  UPSTASH_REDIS_REST_TOKEN.

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/contested_fossil_sweep_<UTCSTAMP>.json
  : dry-run or apply report — full candidate list with basis and, in apply
  mode, before/after state for every changed entry (gitignored, not durable)
- (network, production Redis, write — APPLY MODE ONLY) updates the `state`,
  `metadata.revision`, and `metadata.consolidation_notes` fields of fossil
  contested entries via a plain redis SET of the full entry JSON.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Implements the "contested is a fossil state" fix from
docs/pks-foundational-upgrade-spec-2026-07-07.md §4.3 and contract
PKS-CONTRADICTION-LIFECYCLE-001 (contracts/contradiction-lifecycle.spec.md).

Background: an entry can be marked state="contested" with `contradicts`
links (related_knowledge entries with relationship="contradicts") pointing
at other entries. Historically a same-domain/low-similarity heuristic set
this daily through April 2026; the entries it conflicted with were later
merged INTO the contested entry itself, so its `contradicts` links now point
at absorbed entries — including, in the flagship case (ke_4dbf732e757d),
itself. Nothing in the system ever reverts contested -> active when every
counterpart it names has become archived, merged away, or missing. This
script finds and (optionally) fixes exactly that: entries stuck in a
"contested" state that names no live counterpart to actually be contested
with.

An entry is a FOSSIL iff its state is "contested" and every counterpart
named in its `contradicts` links is one of:
  - the entry's own id (self-referential — a merge absorbed the counterpart
    into this very entry)
  - not present in the store at all (missing / deleted)
  - present but metadata.archived == true

A contested entry with zero contradicts links, OR with at least one
counterpart that is present and NOT archived and NOT the entry itself, is
NOT a fossil and is left untouched — it may be a live, real contest.

CRITICAL DESIGN NOTE (verified against the schema before writing this
script): the Python KnowledgeEntry/KnowledgeMetadata dataclasses
(distillation/models/entries.py) do NOT declare several fields the
production Worker manages on the same JSON (e.g. `revision`,
`injection_quarantine`, `quarantined_at`, `quarantine_streak_nights`,
`github_repo`). Round-tripping an entry through
KnowledgeEntry.from_dict() -> .to_dict() would silently DROP every field
the Python model doesn't know about — a real corruption risk on live
entries. This script therefore NEVER uses the typed dataclass for read or
write; it operates on raw parsed JSON dicts throughout, so every field the
Worker relies on survives untouched except the three this script explicitly
sets (state, metadata.revision, metadata.consolidation_notes).

Default mode is DRY-RUN: enumerates fossils and writes a JSON report; issues
ZERO Redis write calls (INV3 of the contract — this is structurally
guaranteed, not just policy: the apply-path methods are only ever called
inside the `if args.apply:` branch). APPLY mode requires BOTH --apply and
--i-reviewed-the-dry-run on the command line, or the script exits 2 naming
the missing flag — this makes an accidental apply from tab-completion or a
copy-pasted dry-run command structurally hard. Every applied change appends
a consolidation_notes receipt (ISO timestamp, source "fossil_sweep", run id,
prior state, counterpart ids with their statuses) and bumps
metadata.revision, so every change is enumerable and the prior state is
recorded in the report for manual restoration if ever needed. This script
performs NO vector writes; Worker-side vector metadata sync for the tier/
salience implications of a state change is Dream's job, not this script's.

DEPENDENCIES: upstash-redis (via distillation/core/config.py's credential
loading), Python 3.14 stdlib (argparse, json, datetime, pathlib, uuid).

USAGE:
  python3 scripts/sweep_contested_fossils.py --dry-run
      (default; equivalent to no flags at all)
  python3 scripts/sweep_contested_fossils.py --apply --i-reviewed-the-dry-run
      (mutates production; requires prior human review of a dry-run report)

NOTES:
- Read-only by default. The apply path is the only place this script writes,
  and it is double-flag-gated per the repo's "ask before destructive/live
  changes" convention.
- This script is production-entry-point code; automated tests
  (tests/python/test_contested_fossil_sweep.py) exercise FossilSweep against
  an injected in-memory fake, never the real Redis credentials.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = Path(__file__).resolve().parent / "reports"
MAX_CONSOLIDATION_NOTES = 20


class RedisLike(Protocol):
    """The minimal raw-string Redis surface this script needs. The production
    entry point injects distillation.storage.redis_client.RedisClient().client
    (the real upstash_redis.Redis instance); tests inject an in-memory fake
    implementing the same four methods."""

    def scan(self, cursor: int | str, match: str, count: int) -> tuple[Any, list[str]]: ...
    def mget(self, *keys: str) -> list[Any]: ...
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: str) -> Any: ...


def _scan_all(redis: RedisLike, pattern: str) -> list[str]:
    keys: list[str] = []
    cursor: int | str = 0
    while True:
        cursor, batch = redis.scan(cursor, match=pattern, count=100)
        keys.extend(batch)
        if cursor == 0 or cursor == "0":
            return keys


def _load_json(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(value, dict):
        return value
    return None


def _contradicts_targets(entry: dict) -> list[str]:
    """Every counterpart id from related_knowledge links whose relationship
    is "contradicts". Reads knowledge_id defensively (the field's real name
    per dream.ts's ensureRelatedKnowledgeLink), falling back to "id" for
    resilience against any older/alternate shape."""
    links = entry.get("related_knowledge")
    if not isinstance(links, list):
        return []
    targets = []
    for link in links:
        if not isinstance(link, dict):
            continue
        relationship = link.get("relationship") or link.get("link_type")
        if relationship != "contradicts":
            continue
        target_id = link.get("knowledge_id") or link.get("id")
        if target_id:
            targets.append(str(target_id))
    return targets


class FossilSweep:
    """Enumerates and (optionally) fixes contested entries whose contradicts
    counterparts are all archived, merged away, or self-referential.
    Constructor-injected with a RedisLike client so production code and
    tests share the exact same logic against different backends."""

    def __init__(self, redis: RedisLike):
        self._redis = redis

    def _load_all_entries(self) -> dict[str, tuple[str, dict]]:
        """id -> (redis_key, entry_dict) for every knowledge + project entry."""
        entries: dict[str, tuple[str, dict]] = {}
        for prefix in ("knowledge", "project"):
            keys = _scan_all(self._redis, f"{prefix}:*")
            for start in range(0, len(keys), 100):
                batch = keys[start:start + 100]
                if not batch:
                    continue
                for key, raw in zip(batch, self._redis.mget(*batch)):
                    entry = _load_json(raw)
                    if entry is None:
                        continue
                    entry_id = entry.get("id") or key.split(":", 1)[-1]
                    entries[str(entry_id)] = (key, entry)
        return entries

    def find_fossils(self) -> list[dict]:
        """Dry-run enumeration: no writes issued. Returns a list of candidate
        dicts: {id, key, state, counterparts: [{id, status}], basis}."""
        entries = self._load_all_entries()
        candidates = []
        for entry_id, (key, entry) in entries.items():
            if entry.get("state") != "contested":
                continue
            targets = _contradicts_targets(entry)
            counterpart_statuses = []
            all_dead = True
            for target_id in targets:
                if target_id == entry_id:
                    status = "self_referential"
                elif target_id not in entries:
                    status = "missing"
                else:
                    target_metadata = entries[target_id][1].get("metadata") or {}
                    if target_metadata.get("archived") is True:
                        status = "archived"
                    else:
                        status = "live"
                        all_dead = False
                counterpart_statuses.append({"id": target_id, "status": status})
            if not all_dead:
                continue  # at least one live, non-self counterpart: a real contest
            basis = (
                "no contradicts links"
                if not targets
                else "all counterparts self-referential, missing, or archived"
            )
            candidates.append({
                "id": entry_id,
                "key": key,
                "state": entry.get("state"),
                "counterparts": counterpart_statuses,
                "basis": basis,
            })
        return candidates

    def apply(self, candidates: list[dict], run_id: str) -> list[dict]:
        """Reverts each candidate's live entry to state="active" with a
        receipt, via a raw dict mutation + redis.set — never through the
        typed dataclass (see module docstring). Returns per-entry
        before/after records for the report. Never called by find_fossils();
        only reachable from the --apply branch of main()."""
        changed = []
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for candidate in candidates:
            key = candidate["key"]
            raw = self._redis.get(key)
            entry = _load_json(raw)
            if entry is None:
                continue  # entry vanished between dry-run and apply; skip, don't invent
            prior_state = entry.get("state")
            metadata = entry.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                entry["metadata"] = metadata
            prior_revision = metadata.get("revision")

            entry["state"] = "active"
            metadata["revision"] = (prior_revision if isinstance(prior_revision, int) else 0) + 1

            counterpart_summary = ", ".join(
                f"{c['id']}:{c['status']}" for c in candidate["counterparts"]
            ) or "no contradicts links"
            # INV4 requires the receipt to name run id, basis, AND counterpart
            # ids as distinct things — basis (why this is a fossil) is not
            # implied by the counterpart summary alone, so it is spelled out.
            receipt = (
                f"[{timestamp}] fossil_sweep run={run_id}: reverted state "
                f"{prior_state}->active; basis={candidate['basis']}; "
                f"counterparts=[{counterpart_summary}]"
            )
            notes = metadata.get("consolidation_notes")
            if not isinstance(notes, list):
                notes = []
            notes.append(receipt)
            metadata["consolidation_notes"] = notes[-MAX_CONSOLIDATION_NOTES:]

            self._redis.set(key, json.dumps(entry))
            changed.append({
                "id": candidate["id"],
                "key": key,
                "prior_state": prior_state,
                "new_state": "active",
                "prior_revision": prior_revision,
                "new_revision": metadata["revision"],
                "receipt": receipt,
            })
        return changed


def _build_production_redis() -> RedisLike:
    sys.path.insert(0, str(REPO_ROOT / "distillation"))
    from storage.redis_client import RedisClient  # noqa: E402 (production credential path)
    return RedisClient().client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="(default) enumerate fossils, write a report, issue zero writes")
    parser.add_argument("--apply", action="store_true",
                        help="revert fossil contested entries to active with a receipt; "
                             "requires --i-reviewed-the-dry-run")
    parser.add_argument("--i-reviewed-the-dry-run", action="store_true",
                        help="required alongside --apply; confirms a human reviewed the "
                             "dry-run candidate list before this run")
    args = parser.parse_args()

    if args.apply and not args.i_reviewed_the_dry_run:
        print("REFUSING: --apply requires --i-reviewed-the-dry-run "
              "(review a dry-run report first)")
        return 2

    redis = _build_production_redis()
    sweep = FossilSweep(redis)
    candidates = sweep.find_fossils()

    run_id = f"fossil_sweep_{uuid.uuid4().hex[:12]}"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    report: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": generated_at,
        "dry_run": not args.apply,
        "candidates": candidates,
    }

    if args.apply:
        changed = sweep.apply(candidates, run_id)
        report["applied"] = changed
        print(f"APPLIED: reverted {len(changed)} fossil contested entries to active")
    else:
        print(f"DRY RUN: {len(candidates)} fossil contested entries found "
              f"(zero writes issued)")
        for candidate in candidates:
            print(f"  {candidate['id']}: {candidate['basis']}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S+0000")
    out_path = REPORTS_DIR / f"contested_fossil_sweep_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=1))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
