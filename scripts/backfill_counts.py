#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from _memory_migration import (
    CHECKPOINT_DIR,
    MIGRATION_FLAG_KEY,
    append_report,
    build_embedding_text,
    build_vector_metadata,
    chunked,
    ensure_runtime_dirs,
    load_checkpoint,
    load_entries,
    metadata_matches,
    normalize_entry_for_phase2,
    rebuild_thin_index,
    save_checkpoint,
    utc_now_iso,
)

import sys

DISTILLATION_ROOT = Path(__file__).resolve().parent.parent / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402
from utils.embedding import get_embeddings_batch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill mention counts, tiers, timestamps, and vector metadata")
    parser.add_argument("--entry-type", choices=["all", "knowledge", "project"], default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild-thin-index", action="store_true")
    parser.add_argument("--mark-complete", action="store_true")
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT_DIR / "backfill_counts.json")
    parser.add_argument("--max-entry-updates", type=int, default=10000)
    parser.add_argument("--max-vector-updates", type=int, default=10000)
    parser.add_argument("--max-embedding-tokens", type=int, default=250000)
    args = parser.parse_args()

    ensure_runtime_dirs()
    checkpoint = load_checkpoint(args.checkpoint, name="backfill_counts")
    redis_client = RedisClient()
    vector_client = VectorClient()

    entries = load_entries(redis_client, entry_type=args.entry_type)
    entries.sort(key=lambda entry: entry.id)
    if args.limit is not None:
        entries = entries[: args.limit]

    completed_ids = set(checkpoint["completed_ids"])
    pending_entries = [entry for entry in entries if entry.id not in completed_ids]

    stats = checkpoint.setdefault("stats", {})
    stats.setdefault("entries_seen", 0)
    stats.setdefault("entries_updated", 0)
    stats.setdefault("vector_metadata_updates", 0)
    stats.setdefault("vector_upserts", 0)
    stats.setdefault("embedding_tokens", 0)
    stats.setdefault("thin_index_rebuilt", 0)

    print(f"Loaded {len(entries)} entries, {len(pending_entries)} pending normalization")
    if args.dry_run:
        print("Dry run enabled: Redis, Vector, and migration flags will not be mutated")

    for batch in chunked(pending_entries, args.batch_size):
        if stats["entries_updated"] >= args.max_entry_updates:
            print("Stopping: max entry updates reached")
            break
        if stats["vector_metadata_updates"] + stats["vector_upserts"] >= args.max_vector_updates:
            print("Stopping: max vector updates reached")
            break
        if stats["embedding_tokens"] >= args.max_embedding_tokens:
            print("Stopping: max embedding token budget reached")
            break

        normalized_entries = [normalize_entry_for_phase2(entry) for entry in batch]
        stats["entries_seen"] += len(normalized_entries)

        fetch_results = vector_client.fetch_entries([entry.id for entry in normalized_entries], include_metadata=True)
        missing_vectors = []
        vector_upsert_entries = []

        for entry, fetch_result in zip(normalized_entries, fetch_results):
            expected_metadata = build_vector_metadata(entry)
            actual_metadata = fetch_result.metadata if fetch_result else None
            if not args.dry_run:
                if entry.type == "knowledge":
                    redis_client.save_knowledge_entry(entry)
                else:
                    redis_client.save_project_entry(entry)
            stats["entries_updated"] += 1

            if fetch_result is None:
                missing_vectors.append(entry)
                continue

            if not metadata_matches(expected_metadata, actual_metadata):
                if not args.dry_run:
                    vector_client.update_entry_metadata(entry.id, expected_metadata)
                stats["vector_metadata_updates"] += 1

            if not args.dry_run:
                checkpoint["completed_ids"].append(entry.id)

        if missing_vectors:
            texts = [build_embedding_text(entry) for entry in missing_vectors]
            if not args.dry_run:
                embeddings, tokens_used = get_embeddings_batch(texts)
            else:
                embeddings = [[] for _ in missing_vectors]
                tokens_used = 0
            stats["embedding_tokens"] += tokens_used

            for entry, embedding in zip(missing_vectors, embeddings):
                vector_upsert_entries.append(
                    {
                        "id": entry.id,
                        "vector": embedding,
                        "type": entry.type,
                        "domain": entry.domain if entry.type == "knowledge" else entry.name,
                        "state": entry.state if entry.type == "knowledge" else entry.status,
                        "updated_at": entry.metadata.updated_at,
                        "source_conversations": entry.metadata.source_conversations,
                        "classification_status": entry.metadata.classification_status,
                        "context_type": entry.metadata.context_type,
                        "injection_tier": entry.metadata.injection_tier,
                        "salience_score": entry.metadata.salience_score,
                        "archived": entry.metadata.archived,
                    }
                )
                if not args.dry_run:
                    checkpoint["completed_ids"].append(entry.id)

            if vector_upsert_entries:
                if not args.dry_run:
                    vector_client.upsert_entries_batch(vector_upsert_entries)
                stats["vector_upserts"] += len(vector_upsert_entries)

        if not args.dry_run:
            checkpoint["completed_ids"] = list(dict.fromkeys(checkpoint["completed_ids"]))
            save_checkpoint(args.checkpoint, checkpoint)
        print(
            f"Processed {stats['entries_updated']}/{len(entries)} entries, "
            f"vector_metadata_updates={stats['vector_metadata_updates']}, "
            f"vector_upserts={stats['vector_upserts']}, embedding_tokens={stats['embedding_tokens']}"
        )

    if args.rebuild_thin_index:
        knowledge_entries = redis_client.get_all_knowledge_entries()
        project_entries = redis_client.get_all_project_entries()
        for entry in knowledge_entries:
            normalize_entry_for_phase2(entry)
        for entry in project_entries:
            normalize_entry_for_phase2(entry)

        if not args.dry_run:
            thin_index = rebuild_thin_index(redis_client, knowledge_entries, project_entries)
        else:
            thin_index = {}
        stats["thin_index_rebuilt"] = 1
        checkpoint["stats"] = stats
        if not args.dry_run:
            save_checkpoint(args.checkpoint, checkpoint)
        print("Thin index rebuild completed")
    else:
        thin_index = None

    pending_count = redis_client.get_set_cardinality("classification:pending")
    remaining_unclassified_count = sum(
        1
        for entry in load_entries(redis_client, entry_type=args.entry_type)
        if (not entry.metadata) or (entry.metadata.classification_status == "pending") or (not entry.metadata.context_type)
    )
    if args.mark_complete:
        if pending_count != 0 or remaining_unclassified_count != 0:
            print(
                "Refusing to mark migration complete while unclassified entries remain: "
                f"classification:pending={pending_count}, remaining_unclassified={remaining_unclassified_count}"
            )
        elif not args.dry_run:
            redis_client.set(MIGRATION_FLAG_KEY, utc_now_iso())
            redis_client.delete("classification:pending")
            print(f"Marked migration complete via {MIGRATION_FLAG_KEY}")

    report_payload = {
        "generated_at": utc_now_iso(),
        "checkpoint": str(args.checkpoint),
        "dry_run": args.dry_run,
        "stats": checkpoint["stats"],
        "pending_classification_count": pending_count,
        "remaining_unclassified_count": remaining_unclassified_count,
        "marked_complete": bool(args.mark_complete and pending_count == 0 and remaining_unclassified_count == 0 and not args.dry_run),
        "thin_index": thin_index,
    }
    report_path = append_report(f"backfill_counts_{datetime_safe_stamp()}.json", report_payload)
    print(f"Report written to {report_path}")
    return 0


def datetime_safe_stamp() -> str:
    return utc_now_iso().replace(":", "").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
