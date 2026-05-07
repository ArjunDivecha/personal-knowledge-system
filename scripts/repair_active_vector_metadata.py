#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def is_archived(entry: object) -> bool:
    metadata = getattr(entry, "metadata", None)
    return bool(getattr(metadata, "archived", False))


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

    for start in range(0, len(active_entries), args.batch_size):
        batch = active_entries[start:start + args.batch_size]
        fetch_results = vector_client.fetch_entries(
            [entry.id for entry in batch],
            include_metadata=True,
            batch_size=args.batch_size,
        )

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
                    break
                vector_client.update_entry_metadata(entry.id, expected_metadata)
                updated += 1

        print(
            f"Checked {min(start + len(batch), len(active_entries))}/{len(active_entries)} active entries; "
            f"mismatches={len(mismatches)}, updated={updated}, missing_vectors={len(missing_vectors)}",
            flush=True,
        )
        if args.apply and updated >= args.max_updates:
            break

    report = {
        "generated_at": utc_now_iso(),
        "apply": args.apply,
        "entry_type": args.entry_type,
        "active_entry_count": len(active_entries),
        "mismatch_count": len(mismatches),
        "updated_count": updated,
        "missing_vector_count": len(missing_vectors),
        "missing_vectors": missing_vectors,
        "mismatches_sample": mismatches[:25],
    }
    report_path = append_report(f"repair_active_vector_metadata_{datetime_safe_stamp()}.json", report)
    print(f"Report written to {report_path}")
    if args.apply and missing_vectors:
        return 1
    return 0


def datetime_safe_stamp() -> str:
    return utc_now_iso().replace(":", "").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
