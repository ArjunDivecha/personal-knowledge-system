#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
from pathlib import Path

from _memory_migration import (
    MIGRATION_FLAG_KEY,
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Redis, vector metadata, and thin index consistency")
    parser.add_argument("--entry-type", choices=["all", "knowledge", "project"], default="all")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    ensure_runtime_dirs()
    redis_client = RedisClient()
    vector_client = VectorClient()

    entries = load_entries(redis_client, entry_type=args.entry_type)
    entries.sort(key=lambda entry: entry.id)
    for entry in entries:
        normalize_entry_for_phase2(entry)

    if not args.full and len(entries) > args.sample_size:
        rng = random.Random(args.seed)
        entries = sorted(rng.sample(entries, args.sample_size), key=lambda entry: entry.id)

    fetch_results = vector_client.fetch_entries([entry.id for entry in entries], include_metadata=True)

    issues = []
    redis_topic_count = len(redis_client.get_all_knowledge_entries())
    redis_project_count = len(redis_client.get_all_project_entries())
    thin_index = redis_client.get_thin_index()

    for entry, fetch_result in zip(entries, fetch_results):
        metadata = entry.metadata
        if metadata is None:
            issues.append({"entry_id": entry.id, "kind": "missing_metadata"})
            continue
        if metadata.schema_version < 2:
            issues.append({"entry_id": entry.id, "kind": "stale_schema_version", "schema_version": metadata.schema_version})
        if fetch_result is None:
            issues.append({"entry_id": entry.id, "kind": "missing_vector"})
            continue
        expected_metadata = build_vector_metadata(entry)
        if not metadata_matches(expected_metadata, fetch_result.metadata):
            issues.append(
                {
                    "entry_id": entry.id,
                    "kind": "vector_metadata_mismatch",
                    "expected": expected_metadata,
                    "actual": fetch_result.metadata,
                }
            )

    if thin_index is None:
        issues.append({"kind": "missing_thin_index"})
    else:
        thin_index_topic_total = getattr(thin_index, "total_topic_count", thin_index.topic_count)
        thin_index_project_total = getattr(thin_index, "total_project_count", thin_index.project_count)
        if thin_index_topic_total != redis_topic_count:
            issues.append(
                {
                    "kind": "thin_index_topic_count_mismatch",
                    "expected": redis_topic_count,
                    "actual": thin_index_topic_total,
                }
            )
        if thin_index_project_total != redis_project_count:
            issues.append(
                {
                    "kind": "thin_index_project_count_mismatch",
                    "expected": redis_project_count,
                    "actual": thin_index_project_total,
                }
            )

    report = {
        "generated_at": utc_now_iso(),
        "entry_count_checked": len(entries),
        "issues": issues,
        "redis_topic_count": redis_topic_count,
        "redis_project_count": redis_project_count,
        "thin_index_topic_count": thin_index.topic_count if thin_index else None,
        "thin_index_project_count": thin_index.project_count if thin_index else None,
        "thin_index_total_topic_count": getattr(thin_index, "total_topic_count", None) if thin_index else None,
        "thin_index_total_project_count": getattr(thin_index, "total_project_count", None) if thin_index else None,
        "pending_classification_count": redis_client.get_set_cardinality("classification:pending"),
        "backfill_complete_flag": redis_client.get(MIGRATION_FLAG_KEY),
    }
    report_path = append_report(f"verify_memory_consistency_{datetime_safe_stamp()}.json", report)
    print(f"Checked {len(entries)} entries; issues={len(issues)}")
    print(f"Report written to {report_path}")

    if issues and args.strict:
        return 1
    return 0


def datetime_safe_stamp() -> str:
    return utc_now_iso().replace(":", "").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
