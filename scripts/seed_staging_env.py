#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import openai
from upstash_redis import Redis
from upstash_vector import Index

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from config import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL  # noqa: E402
from models.entries import KnowledgeEntry, ProjectEntry  # noqa: E402
from pipeline.index import generate_thin_index  # noqa: E402
from utils.salience import compute_salience, resolve_stored_tier  # noqa: E402

from _memory_migration import (  # noqa: E402
    MIGRATION_FLAG_KEY,
    append_report,
    build_embedding_text,
    build_vector_metadata,
    normalize_entry_for_phase2,
	utc_now_iso,
)


def refresh_policy_metadata(entry: KnowledgeEntry | ProjectEntry) -> KnowledgeEntry | ProjectEntry:
    if entry.metadata:
        entry.metadata.injection_tier = resolve_stored_tier(entry)
        entry.metadata.salience_score = compute_salience(entry)
    return entry


def load_bundle(path: Path) -> tuple[list[KnowledgeEntry], list[ProjectEntry], dict[str, Any]]:
    payload = json.loads(path.read_text())
    metadata = dict(payload.get("metadata") or {})

    knowledge_entries = [
        refresh_policy_metadata(normalize_entry_for_phase2(KnowledgeEntry.from_dict(item.get("data", item))))
        for item in payload.get("knowledge_entries", [])
    ]
    project_entries = [
        refresh_policy_metadata(normalize_entry_for_phase2(ProjectEntry.from_dict(item.get("data", item))))
        for item in payload.get("project_entries", [])
    ]
    return knowledge_entries, project_entries, metadata


def get_required_env(key: str, *, allow_empty: bool = False) -> str:
    value = os.getenv(key, "")
    if not value and not allow_empty:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value


def connect_redis() -> Redis:
    return Redis(
        url=get_required_env("STAGING_UPSTASH_REDIS_REST_URL"),
        token=get_required_env("STAGING_UPSTASH_REDIS_REST_TOKEN"),
    )


def connect_vector() -> Index:
    return Index(
        url=get_required_env("STAGING_UPSTASH_VECTOR_REST_URL"),
        token=get_required_env("STAGING_UPSTASH_VECTOR_REST_TOKEN"),
    )


def connect_openai() -> openai.OpenAI:
    api_key = os.getenv("STAGING_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing STAGING_OPENAI_API_KEY or OPENAI_API_KEY")
    return openai.OpenAI(api_key=api_key)


def scan_keys(redis: Redis, pattern: str) -> list[str]:
    cursor = 0
    keys: list[str] = []
    while True:
        cursor, batch = redis.scan(cursor, match=pattern, count=200)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


def delete_matching(redis: Redis, pattern: str) -> int:
    keys = scan_keys(redis, pattern)
    if not keys:
        return 0
    redis.delete(*keys)
    return len(keys)


def sync_secondary_indexes(redis: Redis, entry: KnowledgeEntry | ProjectEntry) -> None:
    if isinstance(entry, KnowledgeEntry):
        redis.sadd(f"by_domain:{entry.domain.lower().replace(' ', '_')}", entry.id)
        redis.sadd(f"by_state:{entry.state}", entry.id)
    else:
        redis.sadd(f"by_name:{entry.name.lower().replace(' ', '_')}", entry.id)
        redis.sadd(f"by_status:{entry.status}", entry.id)


def write_entries_to_redis(redis: Redis, knowledge_entries: list[KnowledgeEntry], project_entries: list[ProjectEntry]) -> None:
    for entry in knowledge_entries:
        redis.set(f"knowledge:{entry.id}", json.dumps(entry.to_dict()))
        sync_secondary_indexes(redis, entry)
    for entry in project_entries:
        redis.set(f"project:{entry.id}", json.dumps(entry.to_dict()))
        sync_secondary_indexes(redis, entry)


def build_vector_rows(
    openai_client: openai.OpenAI,
    entries: list[KnowledgeEntry | ProjectEntry],
) -> tuple[list[dict[str, Any]], int]:
    texts = [build_embedding_text(entry) for entry in entries]
    total_tokens = 0
    vectors: list[list[float]] = []

    for start in range(0, len(texts), 100):
        batch = texts[start:start + 100]
        response = openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=batch,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        total_tokens += response.usage.total_tokens
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors.extend([item.embedding for item in ordered])

    rows: list[dict[str, Any]] = []
    for entry, vector in zip(entries, vectors):
        rows.append(
            {
                "id": entry.id,
                "vector": vector,
                "metadata": build_vector_metadata(entry),
            }
        )
    return rows, total_tokens


def reset_staging(redis: Redis, vector: Index) -> dict[str, int]:
    deleted = {
        "knowledge_keys": delete_matching(redis, "knowledge:*"),
        "project_keys": delete_matching(redis, "project:*"),
        "thin_index_keys": delete_matching(redis, "index:current"),
        "archived_keys": delete_matching(redis, "archived:*"),
        "dream_keys": delete_matching(redis, "dream:*"),
        "access_keys": delete_matching(redis, "entry_access:*"),
        "last_accessed_keys": delete_matching(redis, "entry_last_accessed:*"),
        "secondary_indexes": 0,
        "processed_keys": delete_matching(redis, "processed:*"),
        "error_keys": delete_matching(redis, "reconsolidation:errors:*"),
    }
    deleted["secondary_indexes"] += delete_matching(redis, "by_domain:*")
    deleted["secondary_indexes"] += delete_matching(redis, "by_state:*")
    deleted["secondary_indexes"] += delete_matching(redis, "by_name:*")
    deleted["secondary_indexes"] += delete_matching(redis, "by_status:*")
    redis.delete(MIGRATION_FLAG_KEY, "classification:pending")
    vector.reset()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the staging memory environment from a fixture bundle")
    parser.add_argument(
        "--bundle",
        type=Path,
        default=REPO_ROOT / "tests" / "fixtures" / "sample_memory_fixture.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--skip-vectors", action="store_true")
    parser.add_argument("--mark-backfill-complete", action="store_true")
    args = parser.parse_args()

    knowledge_entries, project_entries, bundle_metadata = load_bundle(args.bundle)
    all_entries: list[KnowledgeEntry | ProjectEntry] = [*knowledge_entries, *project_entries]
    thin_index = generate_thin_index(knowledge_entries, project_entries)
    should_mark_complete = args.mark_backfill_complete or bool(bundle_metadata.get("mark_backfill_complete", False))

    report: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "bundle": str(args.bundle),
        "dry_run": args.dry_run,
        "reset_requested": args.reset,
        "skip_vectors": args.skip_vectors,
        "knowledge_entries": len(knowledge_entries),
        "project_entries": len(project_entries),
        "thin_index_summary": thin_index.get_summary(),
        "mark_backfill_complete": should_mark_complete,
        "deleted": None,
        "embedding_tokens": 0,
    }

    if args.dry_run:
        report_path = append_report(f"seed_staging_env_{utc_now_iso().replace(':', '').replace('+00:00', 'Z')}.json", report)
        print(f"Bundle: {args.bundle}")
        print(f"Knowledge entries: {len(knowledge_entries)}")
        print(f"Project entries: {len(project_entries)}")
        print(f"Thin index: {thin_index.get_summary()}")
        print(f"Report written to {report_path}")
        return 0

    redis = connect_redis()
    vector = connect_vector()
    deleted = reset_staging(redis, vector) if args.reset else None

    write_entries_to_redis(redis, knowledge_entries, project_entries)
    redis.set("index:current", json.dumps(thin_index.to_dict()))

    if should_mark_complete:
        redis.set(MIGRATION_FLAG_KEY, utc_now_iso())
        redis.delete("classification:pending")

    if not args.skip_vectors:
        openai_client = connect_openai()
        vector_rows, token_count = build_vector_rows(openai_client, all_entries)
        report["embedding_tokens"] = token_count
        for start in range(0, len(vector_rows), 100):
            vector.upsert(vectors=vector_rows[start:start + 100])

    report["deleted"] = deleted
    report_path = append_report(f"seed_staging_env_{utc_now_iso().replace(':', '').replace('+00:00', 'Z')}.json", report)
    print(f"Seeded staging Redis/Vector from {args.bundle}")
    print(f"Knowledge entries: {len(knowledge_entries)}")
    print(f"Project entries: {len(project_entries)}")
    print(f"Thin index: {thin_index.get_summary()}")
    print(f"Report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
