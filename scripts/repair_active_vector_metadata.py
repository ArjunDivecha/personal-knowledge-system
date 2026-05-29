#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path

from _memory_migration import (
    append_report,
    build_vector_metadata,
    ensure_runtime_dirs,
    load_entries,
    metadata_matches,
    normalize_entry_for_phase2,
    utc_now_iso,
)

import sys

DISTILLATION_ROOT = Path(__file__).resolve().parent.parent / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402
from upstash_vector.errors import UpstashError  # noqa: E402


def is_archived(entry: object) -> bool:
    metadata = getattr(entry, "metadata", None)
    return bool(getattr(metadata, "archived", False))


def _with_retry(fn, *, what: str, max_attempts: int = 6, base_delay: float = 2.0):
    """Run fn(), retrying transient Upstash 503s with capped exponential backoff.

    Upstash Vector intermittently returns "backend currently unavailable" (503)
    under sustained write load. Rather than crash the whole reconciliation, we
    wait out the down-stretch. Raises the last error only if all attempts fail.
    """
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch active vector metadata to match Redis-derived metadata")
    parser.add_argument("--entry-type", choices=["all", "knowledge", "project"], default="all")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-updates", type=int, default=2000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    ensure_runtime_dirs()
    redis_client = RedisClient()
    vector_client = VectorClient()

    entries = load_entries(redis_client, entry_type=args.entry_type)
    entries.sort(key=lambda entry: entry.id)
    active_entries = []
    for entry in entries:
        normalize_entry_for_phase2(entry)
        if not is_archived(entry):
            active_entries.append(entry)

    print(f"Loaded {len(entries)} entries; checking {len(active_entries)} active entries")
    if not args.apply:
        print("Dry run enabled: pass --apply to patch vector metadata")

    updated = 0
    missing_vectors: list[str] = []
    mismatches: list[dict] = []
    failed_updates: list[str] = []

    for start in range(0, len(active_entries), args.batch_size):
        batch = active_entries[start:start + args.batch_size]
        try:
            fetch_results = _with_retry(
                lambda: vector_client.fetch_entries(
                    [entry.id for entry in batch],
                    include_metadata=True,
                    batch_size=args.batch_size,
                ),
                what=f"fetch batch @{start}",
            )
        except UpstashError as err:
            print(f"  [skip] batch @{start} fetch failed after retries: {err}", flush=True)
            failed_updates.extend(entry.id for entry in batch)
            continue

        stop = False
        for entry, fetch_result in zip(batch, fetch_results):
            expected_metadata = build_vector_metadata(entry)
            actual_metadata = fetch_result.metadata if fetch_result else None
            if fetch_result is None:
                missing_vectors.append(entry.id)
                continue
            if metadata_matches(expected_metadata, actual_metadata):
                continue

            mismatches.append(
                {
                    "entry_id": entry.id,
                    "expected": expected_metadata,
                    "actual": actual_metadata,
                }
            )
            if args.apply:
                if updated >= args.max_updates:
                    print("Stopping: max updates reached")
                    stop = True
                    break
                try:
                    _with_retry(
                        lambda eid=entry.id, md=expected_metadata: vector_client.update_entry_metadata(eid, md),
                        what=f"update {entry.id}",
                    )
                    updated += 1
                except UpstashError as err:
                    print(f"  [skip] update {entry.id} failed after retries: {err}", flush=True)
                    failed_updates.append(entry.id)

        print(
            f"Checked {min(start + len(batch), len(active_entries))}/{len(active_entries)} active entries; "
            f"mismatches={len(mismatches)}, updated={updated}, "
            f"failed={len(failed_updates)}, missing_vectors={len(missing_vectors)}",
            flush=True,
        )
        if stop:
            break

    report = {
        "generated_at": utc_now_iso(),
        "apply": args.apply,
        "entry_type": args.entry_type,
        "active_entry_count": len(active_entries),
        "mismatch_count": len(mismatches),
        "updated_count": updated,
        "failed_update_count": len(failed_updates),
        "failed_updates": failed_updates[:200],
        "missing_vector_count": len(missing_vectors),
        "missing_vectors": missing_vectors,
        "mismatches_sample": mismatches[:25],
    }
    report_path = append_report(f"repair_active_vector_metadata_{datetime_safe_stamp()}.json", report)
    print(
        f"Done: updated={updated}, failed={len(failed_updates)}, "
        f"missing_vectors={len(missing_vectors)}. Re-run to mop up failures (idempotent).",
        flush=True,
    )
    print(f"Report written to {report_path}")
    if args.apply and (failed_updates or missing_vectors):
        return 1
    return 0


def datetime_safe_stamp() -> str:
    return utc_now_iso().replace(":", "").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
