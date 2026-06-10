#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: purge_stale_vectors.py
=============================================================================

INPUT FILES:
- (none) reads live Upstash Redis (canonical entries) + Upstash Vector index.

OUTPUT FILES:
- scripts/reports/purge_stale_vectors_<ts>.json : run report (counts + samples)
- scripts/reports/purge_stale_vectors_manifest_<ts>.json : (apply only) the
  full list of deleted vector IDs, for audit / re-embed reference.

VERSION: 1.0
LAST UPDATED: 2026-06-01
AUTHOR: PKS remediation

DESCRIPTION:
The Upstash Vector index should hold ONE vector per ACTIVE knowledge/project
entry. Over time it accumulated vectors that should not be searchable:
  - archived: the entry is archived in Redis (the archive path is supposed to
    delete its vector, but a bulk re-embed / partial archive left it behind);
  - orphan: there is no live Redis entry for the vector ID at all (entry was
    hard-removed but its vector survived).
Both pollute every search's topK budget and can leak into results on any read
path with a weak archived filter.

This script makes the vector index = {active, non-archived Redis entries}:
it builds the set of vector IDs that SHOULD exist from Redis, scans every
vector via /range, and deletes any vector whose ID is not in that set.

Canonical source of truth is Redis. Deleting a vector is reversible in
practice: restoring an archived entry re-embeds it (see restore path), so a
purged archived/orphan vector is re-created on demand if the entry comes back.

DEPENDENCIES:
- distillation/storage (RedisClient, VectorClient), upstash_vector

USAGE:
  # dry-run (default): report what WOULD be deleted, no writes
  python scripts/purge_stale_vectors.py
  # apply: delete stale vectors, write a manifest first
  python scripts/purge_stale_vectors.py --apply

NOTES:
- 503-resilient: transient "backend currently unavailable" errors are retried
  with capped backoff; a batch that still fails is recorded and skipped, not
  fatal. Re-run to mop up (idempotent).
- A safety rail aborts an --apply run if the stale fraction exceeds
  --max-delete-fraction (default 0.95) of the index, guarding against a Redis
  read failure that would otherwise look like "everything is orphan".
=============================================================================
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

DISTILLATION_ROOT = Path(__file__).resolve().parent.parent / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from _memory_migration import append_report, ensure_runtime_dirs, utc_now_iso  # noqa: E402

from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402
from upstash_vector.errors import UpstashError  # noqa: E402


def _is_archived(entry: object) -> bool:
    metadata = getattr(entry, "metadata", None)
    return bool(getattr(metadata, "archived", False))


def _with_retry(fn, *, what: str, max_attempts: int = 6, base_delay: float = 2.0):
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except UpstashError as err:
            last_err = err
            if "unavailable" not in str(err).lower() or attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), 32.0)
            print(f"  [retry] {what}: {err} — attempt {attempt}/{max_attempts}, sleeping {delay:.0f}s", flush=True)
            time.sleep(delay)
    if last_err is not None:
        raise last_err


def build_active_id_set(redis: RedisClient) -> set[str]:
    """IDs that SHOULD have a vector: active (non-archived) knowledge + project entries."""
    active: set[str] = set()
    for entry in redis.get_all_knowledge_entries():
        if not _is_archived(entry):
            active.add(entry.id)
    for entry in redis.get_all_project_entries():
        if not _is_archived(entry):
            active.add(entry.id)
    return active


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge archived/orphan vectors so the index = active Redis entries")
    parser.add_argument("--apply", action="store_true", help="delete stale vectors (default: dry-run)")
    parser.add_argument("--range-batch", type=int, default=1000, help="vectors per /range page")
    parser.add_argument("--delete-batch", type=int, default=200, help="vector IDs per delete call")
    parser.add_argument("--max-delete-fraction", type=float, default=0.95,
                        help="abort --apply if stale fraction exceeds this (Redis-read safety rail)")
    args = parser.parse_args()

    ensure_runtime_dirs()
    redis = RedisClient()
    vector = VectorClient()

    print("[purge] building active-id set from Redis (canonical)…", flush=True)
    active_ids = build_active_id_set(redis)
    print(f"[purge] active (non-archived) Redis entries: {len(active_ids)}", flush=True)

    index = vector.index
    cursor = "0"
    total_vectors = 0
    archived_ids: list[str] = []   # vector present, Redis entry archived
    orphan_ids: list[str] = []     # vector present, no Redis entry at all
    # We classify stale = not in active set; split archived vs orphan using Redis presence.
    # Cache Redis presence by checking the entry blob lazily via mget-friendly batches.
    stale_ids: list[str] = []

    print("[purge] scanning vector index via /range…", flush=True)
    while True:
        page = _with_retry(
            lambda: index.range(cursor=cursor, limit=args.range_batch, include_metadata=True),
            what=f"range @{total_vectors}",
        )
        vectors = page.vectors or []
        for v in vectors:
            total_vectors += 1
            vid = str(v.id)
            if vid in active_ids:
                continue
            stale_ids.append(vid)
            md = v.metadata or {}
            if md.get("archived") is True:
                archived_ids.append(vid)
            else:
                orphan_ids.append(vid)
        cursor = page.next_cursor
        if not cursor or cursor == "0":
            break

    stale = len(stale_ids)
    frac = stale / total_vectors if total_vectors else 0.0
    print(f"[purge] vectors scanned: {total_vectors}", flush=True)
    print(f"[purge]   keep (active): {total_vectors - stale}", flush=True)
    print(f"[purge]   stale total : {stale} ({frac*100:.1f}%)", flush=True)
    print(f"[purge]     archived  : {len(archived_ids)}", flush=True)
    print(f"[purge]     orphan    : {len(orphan_ids)}", flush=True)

    report = {
        "generated_at": utc_now_iso(),
        "apply": args.apply,
        "active_redis_entries": len(active_ids),
        "vectors_scanned": total_vectors,
        "keep_count": total_vectors - stale,
        "stale_count": stale,
        "stale_fraction": round(frac, 4),
        "archived_count": len(archived_ids),
        "orphan_count": len(orphan_ids),
        "archived_sample": archived_ids[:25],
        "orphan_sample": orphan_ids[:25],
    }

    deleted = 0
    failed_batches = 0
    if not args.apply:
        print("[purge] DRY-RUN — no deletes. Re-run with --apply to purge.", flush=True)
    else:
        if frac > args.max_delete_fraction:
            report["aborted"] = f"stale_fraction {frac:.3f} > max_delete_fraction {args.max_delete_fraction}"
            append_report(f"purge_stale_vectors_{_stamp()}.json", report)
            print(f"[purge] ABORTED: stale fraction {frac:.1%} exceeds safety rail "
                  f"{args.max_delete_fraction:.0%}. Refusing to delete — check Redis read.", flush=True)
            return 1
        # Write the manifest of IDs to be deleted BEFORE deleting (audit/restore reference).
        manifest_path = append_report(
            f"purge_stale_vectors_manifest_{_stamp()}.json",
            {"generated_at": utc_now_iso(), "archived_ids": archived_ids, "orphan_ids": orphan_ids},
        )
        print(f"[purge] manifest written: {manifest_path}", flush=True)

        for start in range(0, stale, args.delete_batch):
            batch = stale_ids[start:start + args.delete_batch]
            # 4.2 — Re-check Redis immediately before each delete: the snapshot can
            # be stale if new entries were ingested during the /range scan.  Any ID
            # that now exists in Redis is no longer "stale" and must be skipped.
            recheck_keys = []
            for vid in batch:
                if vid.startswith("pe_"):
                    recheck_keys.append(f"project:{vid}")
                else:
                    recheck_keys.append(f"knowledge:{vid}")
            try:
                live_check = redis.client.mget(*recheck_keys) if recheck_keys else []
            except Exception:
                live_check = [None] * len(recheck_keys)
            safe_batch = [vid for vid, val in zip(batch, live_check) if val is None]
            skipped_alive = len(batch) - len(safe_batch)
            if skipped_alive:
                print(f"  [recheck] {skipped_alive} IDs appeared in Redis since snapshot — skipping", flush=True)
            if not safe_batch:
                continue
            try:
                _with_retry(lambda b=safe_batch: index.delete(ids=b), what=f"delete @{start}")
                deleted += len(safe_batch)
            except UpstashError as err:
                failed_batches += 1
                print(f"  [skip] delete batch @{start} failed after retries: {err}", flush=True)
            print(f"  deleted {deleted}/{stale} (failed batches={failed_batches})", flush=True)

        report["deleted_count"] = deleted
        report["failed_batches"] = failed_batches
        print(f"[purge] APPLIED: deleted {deleted}/{stale} stale vectors "
              f"(failed batches={failed_batches}). Re-run to mop up (idempotent).", flush=True)

    report_path = append_report(f"purge_stale_vectors_{_stamp()}.json", report)
    print(f"[purge] report → {report_path}", flush=True)
    if args.apply and failed_batches:
        return 1
    return 0


def _stamp() -> str:
    return utc_now_iso().replace(":", "").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
