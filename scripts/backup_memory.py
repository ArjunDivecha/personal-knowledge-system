#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: backup_memory.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/.env
  (Upstash Redis + Vector + OpenAI credentials, loaded by StorageClient)

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/backups/
  YYYY-MM-DDTHH-MM-SS/
    redis_knowledge.json    — all knowledge:* entries (raw JSON values)
    redis_projects.json     — all project:* entries
    redis_index.json        — all index:* entries (thin index etc.)
    redis_processed.json    — all processed:* source dedup markers
    redis_classification.json — classification:pending set and any by_domain/by_state sets
    vector_metadata.json    — all vector IDs + metadata (not the embedding vectors themselves;
                              embeddings are regenerated from Redis data on restore)
    manifest.json           — counts, timestamps, backup format version

VERSION: 1.0
LAST UPDATED: 2026-06-09
AUTHOR: Claude (Opus 4.8) — Phase 0.1 of pks-fix-prd-2026-06-09

DESCRIPTION:
Safety-net backup for the Personal Knowledge System before any Dream
activation run. Snapshots all durable state: Redis entries (knowledge,
projects, index, processed-source markers, classification tracking) and
Upstash Vector metadata (IDs + metadata fields; actual embedding floats are
NOT stored as they can be regenerated).

This script does NOT restore data. A RESTORE guide is printed to stdout and
appended to manifest.json. Run this immediately before any operation that
could permanently delete or archive memory entries (first live Dream
forgetting run, large re-tiering, purge operations).

The --validate flag runs INSTEAD of writing a new backup: it reads an
existing backup directory and checks that entry counts match what is
currently in Redis, flagging any drift.

USAGE:
    # Create a new backup
    python scripts/backup_memory.py

    # Validate an existing backup against current Redis state
    python scripts/backup_memory.py --validate /path/to/backup/dir

    # Dry-run: print what would be backed up without writing files
    python scripts/backup_memory.py --dry-run

NOTES:
- Safe to run anytime; read-only against production storage (except writes
  to local backup directory).
- Embedding vectors are intentionally omitted: the backup is useful for
  identifying and restoring *which* entries existed, not for pixel-perfect
  embedding reconstruction. A restore workflow rebuilds embeddings from the
  Redis JSON via the normal save_knowledge_entry path.
- The processed-source markers are critical: they control which emails,
  tweets, and GitHub repos are re-processed on the next ingest run.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
INGESTION_ROOT = REPO_ROOT / "ingestion"
BACKUP_DIR = REPO_ROOT / "backups"

sys.path.insert(0, str(INGESTION_ROOT))

from core.config import (
    UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN,
    UPSTASH_VECTOR_REST_URL,
    UPSTASH_VECTOR_REST_TOKEN,
)
from upstash_redis import Redis
from upstash_vector import Index

BACKUP_FORMAT_VERSION = 1

# Redis key patterns to snapshot (order matters for restore)
REDIS_PATTERNS = [
    ("knowledge", "knowledge:*"),
    ("projects", "project:*"),
    ("index", "index:*"),
    ("processed", "processed:*"),
    ("classification", "classification:*"),
    ("by_domain", "by_domain:*"),
    ("by_state", "by_state:*"),
]

# Maximum vectors to export in a single range page (Upstash limit)
VECTOR_PAGE_SIZE = 100


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _scan_keys(redis: Redis, pattern: str) -> list[str]:
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = redis.scan(cursor, match=pattern, count=200)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


def _dump_string_keys(redis: Redis, keys: list[str]) -> dict[str, Any]:
    """MGET all keys that hold string values; return id → parsed JSON (or raw string)."""
    result: dict[str, Any] = {}
    batch_size = 100
    for start in range(0, len(keys), batch_size):
        batch = keys[start:start + batch_size]
        values = redis.mget(*batch)
        for key, raw in zip(batch, values):
            if raw is None:
                continue
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[key] = raw
    return result


def _dump_set_keys(redis: Redis, keys: list[str]) -> dict[str, list[str]]:
    """SMEMBERS all keys that hold sets; return key → sorted member list."""
    result: dict[str, list[str]] = {}
    for key in keys:
        try:
            members = redis.smembers(key)
            result[key] = sorted(str(m) for m in (members or []))
        except Exception:
            result[key] = []
    return result


# ── Vector helpers ────────────────────────────────────────────────────────────

def _export_vector_metadata(vector: Index) -> list[dict]:
    """
    Page through all vectors using the Upstash Vector range API and collect
    IDs + metadata (no embedding floats).
    """
    records: list[dict] = []
    cursor = ""
    while True:
        page = vector.range(cursor=cursor, limit=VECTOR_PAGE_SIZE, include_metadata=True)
        for v in (page.vectors or []):
            records.append({
                "id": v.id,
                "metadata": v.metadata or {},
            })
        cursor = page.next_cursor
        if not cursor:
            break
    return records


# ── Core backup ───────────────────────────────────────────────────────────────

def run_backup(dry_run: bool = False) -> Path | None:
    """
    Snapshot all PKS state to a timestamped backup directory.
    Returns the backup path (or None if dry_run).
    """
    redis = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    vector = Index(url=UPSTASH_VECTOR_REST_URL, token=UPSTASH_VECTOR_REST_TOKEN)

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H-%M-%S")
    backup_path = BACKUP_DIR / ts

    print(f"PKS Memory Backup — {now.isoformat()}")
    print(f"{'[DRY RUN] ' if dry_run else ''}Output: {backup_path}")
    print()

    if not dry_run:
        backup_path.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": now.isoformat(),
        "dry_run": dry_run,
        "counts": {},
        "files": {},
        "restore_guide": _restore_guide(backup_path),
    }

    # ── Redis snapshots ───────────────────────────────────────────────────────
    for label, pattern in REDIS_PATTERNS:
        print(f"  Redis {pattern} ...", end=" ", flush=True)
        keys = _scan_keys(redis, pattern)
        print(f"{len(keys)} keys")

        if not keys:
            manifest["counts"][label] = 0
            continue

        # Sets (by_domain, by_state, classification:pending) vs strings
        if label in ("by_domain", "by_state", "classification"):
            data = _dump_set_keys(redis, keys)
        else:
            data = _dump_string_keys(redis, keys)

        manifest["counts"][label] = len(data)
        fname = f"redis_{label}.json"
        manifest["files"][label] = fname

        if not dry_run:
            out_path = backup_path / fname
            out_path.write_text(json.dumps(data, indent=2, default=str))
            print(f"    → wrote {fname} ({out_path.stat().st_size:,} bytes)")

    # ── Vector metadata export ────────────────────────────────────────────────
    print("  Vector metadata ...", end=" ", flush=True)
    vectors = _export_vector_metadata(vector)
    print(f"{len(vectors)} vectors")

    manifest["counts"]["vectors"] = len(vectors)
    manifest["files"]["vector_metadata"] = "vector_metadata.json"

    if not dry_run:
        out_path = backup_path / "vector_metadata.json"
        out_path.write_text(json.dumps(vectors, indent=2, default=str))
        print(f"    → wrote vector_metadata.json ({out_path.stat().st_size:,} bytes)")

    # ── Manifest ──────────────────────────────────────────────────────────────
    if not dry_run:
        (backup_path / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print()
    print("Counts:")
    for k, v in manifest["counts"].items():
        print(f"  {k:30s} {v:>6}")
    print()

    if dry_run:
        print("[DRY RUN] No files written.")
        return None

    print(f"Backup complete: {backup_path}")
    print()
    _print_restore_guide(backup_path)
    return backup_path


# ── Validate ──────────────────────────────────────────────────────────────────

def run_validate(backup_dir: Path) -> bool:
    """
    Compare an existing backup against current Redis/Vector state.
    Returns True if counts match within tolerance, False otherwise.
    """
    redis = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    vector = Index(url=UPSTASH_VECTOR_REST_URL, token=UPSTASH_VECTOR_REST_TOKEN)

    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: No manifest.json in {backup_dir}")
        return False

    manifest = json.loads(manifest_path.read_text())
    saved_counts = manifest.get("counts", {})

    print(f"Validating backup: {backup_dir}")
    print(f"Backup created:    {manifest.get('created_at')}")
    print()

    ok = True
    for label, pattern in REDIS_PATTERNS:
        keys = _scan_keys(redis, pattern)
        live = len(keys)
        saved = saved_counts.get(label, "?")
        drift = (live - saved) if isinstance(saved, int) else "?"
        status = "OK" if drift == "?" or abs(drift) <= 5 else "DRIFT"
        if status == "DRIFT":
            ok = False
        print(f"  {label:30s} saved={saved:>6}  live={live:>6}  drift={drift:>+5}  {status}")

    print()
    vi = vector.info()
    live_vec = vi.vector_count
    saved_vec = saved_counts.get("vectors", "?")
    drift = (live_vec - saved_vec) if isinstance(saved_vec, int) else "?"
    status = "OK" if drift == "?" or abs(drift) <= 10 else "DRIFT"
    if status == "DRIFT":
        ok = False
    print(f"  {'vectors':30s} saved={saved_vec:>6}  live={live_vec:>6}  drift={drift:>+5}  {status}")
    print()
    print(f"Validation: {'PASS' if ok else 'FAIL — significant drift detected'}")
    return ok


# ── Restore guide ─────────────────────────────────────────────────────────────

def _restore_guide(backup_path: Path) -> str:
    return f"""
To restore from this backup:

1. Ensure you have the ingestion .env credentials loaded.
2. Run the restore script (not yet implemented — see manual steps below):

   Manual restore:
   a. For each file in {backup_path}/redis_*.json:
      - Load the JSON dict (key → value pairs).
      - For string keys (knowledge, projects, index, processed):
        Use `redis.set(key, json.dumps(value))` for each entry.
      - For set keys (by_domain, by_state, classification):
        Use `redis.sadd(key, *members)` for each key.
   b. Regenerate vector embeddings by running:
        python ingestion/core/storage.py --reindex-from-redis
      (or trigger the nightly index rebuild via the distillation pipeline).
   c. Validate counts with:
        python scripts/backup_memory.py --validate {backup_path}

IMPORTANT: Do not restore over a live system without first taking a SECOND
snapshot of the current state. Restore is irreversible at the Redis level.
"""


def _print_restore_guide(backup_path: Path) -> None:
    print("=" * 60)
    print("RESTORE GUIDE")
    print("=" * 60)
    print(_restore_guide(backup_path))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backup PKS Redis + Vector state before Dream activation or destructive ops"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be backed up without writing any files",
    )
    parser.add_argument(
        "--validate",
        metavar="BACKUP_DIR",
        help="Validate an existing backup directory against current Redis state",
    )
    args = parser.parse_args()

    if args.validate:
        ok = run_validate(Path(args.validate))
        sys.exit(0 if ok else 1)
    else:
        run_backup(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
