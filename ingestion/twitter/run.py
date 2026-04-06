#!/usr/bin/env python3
"""
=============================================================================
TWITTER INGESTION RUNNER
=============================================================================
Version: 1.0.0
Last Updated: April 2026

PURPOSE:
Ingest knowledge from a downloaded Twitter/X data archive.

Processes:
  1. Your original tweets — opinions, analyses, positions you posted
  2. Your replies — what you said in response to other people, bundled
     with the tweet you were replying to so the context is preserved

INPUT FILES:
- <TWITTER_ARCHIVE_PATH>/data/tweets.js  (or tweets.js at archive root)
  Downloaded from: twitter.com/settings/your_account → "Download an archive"

OUTPUT FILES:
- Knowledge entries written to Upstash Redis (key: knowledge:<id>)
- Embeddings written to Upstash Vector (id: <entry_id>)
- Thin index updated in Redis (key: index:current)
- Deduplication markers: ingested:twitter:<tweet_id>
- Checkpoint file: ingestion/checkpoints/twitter_entries.pkl
- Dry-run output: ingestion/checkpoints/twitter_dry_run.json

USAGE:
    # Default run (all years, from config)
    python run.py

    # Only process tweets since 2020
    python run.py --since 2020

    # Dry run — extract but do NOT write to storage
    python run.py --dry-run

    # Limit how many tweets to process (useful for testing)
    python run.py --max 200

    # Re-process tweets that were already ingested
    python run.py --no-resume

CONFIGURATION:
Set these in your ingestion/.env file (or environment):
    TWITTER_ARCHIVE_PATH=/path/to/unzipped/twitter-archive
    TWITTER_USERNAME=YourHandle          (without the @)
    TWITTER_SINCE_YEAR=2015
=============================================================================
"""

import argparse
import json
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Make sure parent package is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import (
    CHECKPOINT_DIR,
    TWITTER_ARCHIVE_PATH,
    TWITTER_MIN_TWEET_LENGTH,
    TWITTER_SINCE_YEAR,
    TWITTER_USERNAME,
    validate_twitter_config,
)
from core.storage import StorageClient
from twitter.parser import TwitterArchiveParser
from twitter.tweet_extractor import TweetExtractor


# ---------------------------------------------------------------------------
# Checkpoint helpers (mirrors gmail/run.py pattern)
# ---------------------------------------------------------------------------

def _save_checkpoint(name: str, data) -> None:
    path = CHECKPOINT_DIR / f"twitter_{name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"  ✓ Checkpoint saved: {name}")


def _load_checkpoint(name: str):
    path = CHECKPOINT_DIR / f"twitter_{name}.pkl"
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


# ---------------------------------------------------------------------------
# Main ingestion function
# ---------------------------------------------------------------------------

def run_twitter_ingestion(
    since_year: int = None,
    max_tweets: int = None,
    original_batch_size: int = 25,
    reply_batch_size: int = 12,
    dry_run: bool = False,
    resume: bool = True,
) -> list[dict]:
    """
    Run the full Twitter ingestion pipeline.

    Args:
        since_year:          Only process tweets from this year onwards.
        max_tweets:          Cap total tweets to process (None = no cap).
        original_batch_size: Tweets per LLM call for original tweets.
        reply_batch_size:    Reply threads per LLM call.
        dry_run:             If True, extract but do NOT write to storage.
        resume:              If True, skip tweets already in the dedup index.

    Returns:
        List of all knowledge entry dicts that were extracted.
    """
    print("=" * 60)
    print("TWITTER KNOWLEDGE INGESTION")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")
    print()

    since_year = since_year or TWITTER_SINCE_YEAR

    # ------------------------------------------------------------------
    # Config validation
    # ------------------------------------------------------------------
    errors = validate_twitter_config()
    if errors:
        print("Configuration errors:")
        for err in errors:
            print(f"  ✗ {err}")
        return []

    # ------------------------------------------------------------------
    # Init clients
    # ------------------------------------------------------------------
    parser = TwitterArchiveParser(TWITTER_ARCHIVE_PATH)
    extractor = TweetExtractor()
    storage = StorageClient() if not dry_run else None

    if not dry_run:
        ok, msg = storage.test_connection()
        print(f"Storage: {msg}")
        if not ok:
            print("  ✗ Cannot connect to storage, aborting")
            return []

    print()

    # ------------------------------------------------------------------
    # STEP 1: Count and analyse the archive
    # ------------------------------------------------------------------
    print("[1/4] ANALYSING TWITTER ARCHIVE")
    print("-" * 40)
    print(f"Archive: {parser.tweets_js_path}")
    print(f"Username: @{TWITTER_USERNAME or '(not set)'}")

    counts = parser.count_tweets(
        since_year=since_year,
        min_length=TWITTER_MIN_TWEET_LENGTH,
    )
    print(f"Tweets since {since_year}:  {counts['total']}")
    print(f"  Original:  {counts['original']}")
    print(f"  Replies:   {counts['replies']}")
    print("By year:")
    for year in sorted(counts["by_year"]):
        print(f"  {year}: {counts['by_year'][year]}")

    # Get already-processed tweet IDs for dedup
    processed_ids: set[str] = set()
    if resume and storage:
        processed_ids = set(storage.get_processed_sources("twitter"))
        print(f"\nAlready processed: {len(processed_ids)} tweets")

    print()

    # ------------------------------------------------------------------
    # STEP 2: Parse tweets into two lists (originals & replies)
    # ------------------------------------------------------------------
    print("[2/4] PARSING TWEETS")
    print("-" * 40)

    original_tweets: list[dict] = []
    reply_threads: list[dict] = []
    skipped = 0
    total_seen = 0

    for record in parser.parse_tweets(
        since_year=since_year,
        username=TWITTER_USERNAME,
        min_length=TWITTER_MIN_TWEET_LENGTH,
    ):
        total_seen += 1

        if record["id"] in processed_ids:
            skipped += 1
            continue

        if max_tweets and (len(original_tweets) + len(reply_threads)) >= max_tweets:
            break

        if record["is_reply"]:
            reply_threads.append({
                "reply": record,
                "parent_text": record["reply_to_text"],
                "parent_author": record["reply_to_author"],
            })
        else:
            original_tweets.append(record)

        if total_seen % 200 == 0:
            print(f"  Parsed {total_seen} tweets...", flush=True)

    print(f"Original tweets to process: {len(original_tweets)}")
    print(f"Reply threads to process:   {len(reply_threads)}")
    print(f"Skipped (already done):     {skipped}")

    if not original_tweets and not reply_threads:
        print("\nNo new tweets to process.")
        return []

    print()

    # ------------------------------------------------------------------
    # STEP 3: Extract knowledge with LLM
    # ------------------------------------------------------------------
    print("[3/4] EXTRACTING KNOWLEDGE")
    print("-" * 40)

    all_entries: list[dict] = []
    stats = {
        "original_processed": 0,
        "replies_processed": 0,
        "entries_extracted": 0,
        "errors": 0,
        "by_year": defaultdict(int),
    }

    # ---- 3a: Original tweets ----
    if original_tweets:
        print(f"\nOriginal tweets: {len(original_tweets)} tweets in batches of {original_batch_size}")
        for i in range(0, len(original_tweets), original_batch_size):
            batch = original_tweets[i : i + original_batch_size]
            batch_num = i // original_batch_size + 1
            total_batches = (len(original_tweets) + original_batch_size - 1) // original_batch_size

            print(f"  Batch {batch_num}/{total_batches} ({len(batch)} tweets)...", end=" ", flush=True)

            try:
                entries = extractor.extract_from_original_tweets(batch)
                all_entries.extend(entries)
                stats["entries_extracted"] += len(entries)
                stats["original_processed"] += len(batch)

                for record in batch:
                    year = record["date_obj"].year
                    stats["by_year"][year] += 1
                    if storage:
                        storage.mark_source_processed(
                            "twitter",
                            record["id"],
                            {
                                "date": record["created_at"],
                                "type": "original",
                                "entries_count": len(entries),
                            },
                        )

                print(f"→ {len(entries)} entries extracted")
            except Exception as e:
                print(f"→ ERROR: {e}")
                stats["errors"] += 1

            # Checkpoint periodically
            if batch_num % 5 == 0:
                _save_checkpoint("entries", all_entries)
                _save_checkpoint("stats", dict(stats))

    # ---- 3b: Reply threads ----
    if reply_threads:
        print(f"\nReply threads: {len(reply_threads)} replies in batches of {reply_batch_size}")
        for i in range(0, len(reply_threads), reply_batch_size):
            batch = reply_threads[i : i + reply_batch_size]
            batch_num = i // reply_batch_size + 1
            total_batches = (len(reply_threads) + reply_batch_size - 1) // reply_batch_size

            print(f"  Batch {batch_num}/{total_batches} ({len(batch)} replies)...", end=" ", flush=True)

            try:
                entries = extractor.extract_from_reply_threads(batch)
                all_entries.extend(entries)
                stats["entries_extracted"] += len(entries)
                stats["replies_processed"] += len(batch)

                for thread in batch:
                    record = thread["reply"]
                    year = record["date_obj"].year
                    stats["by_year"][year] += 1
                    if storage:
                        storage.mark_source_processed(
                            "twitter",
                            record["id"],
                            {
                                "date": record["created_at"],
                                "type": "reply",
                                "reply_to_id": record.get("reply_to_id"),
                                "entries_count": len(entries),
                            },
                        )

                print(f"→ {len(entries)} entries extracted")
            except Exception as e:
                print(f"→ ERROR: {e}")
                stats["errors"] += 1

            if batch_num % 5 == 0:
                _save_checkpoint("entries", all_entries)
                _save_checkpoint("stats", dict(stats))

    # Final checkpoint after all extraction
    _save_checkpoint("entries", all_entries)
    _save_checkpoint("stats", dict(stats))

    print()

    # ------------------------------------------------------------------
    # STEP 4: Save to storage
    # ------------------------------------------------------------------
    print("[4/4] SAVING TO STORAGE")
    print("-" * 40)

    if dry_run:
        print("DRY RUN — not saving to storage")
        print(f"Would save {len(all_entries)} entries")
        output_path = CHECKPOINT_DIR / "twitter_dry_run.json"
        with open(output_path, "w") as f:
            json.dump(all_entries, f, indent=2, default=str)
        print(f"Dry-run output written to: {output_path}")

    elif all_entries:
        save_batch = 20
        print(f"Saving {len(all_entries)} entries in batches of {save_batch}...")
        for i in range(0, len(all_entries), save_batch):
            batch = all_entries[i : i + save_batch]
            storage.save_knowledge_entries_batch(batch)
            print(f"  Saved {min(i + save_batch, len(all_entries))}/{len(all_entries)}")

        print("Updating thin index...")
        storage.update_thin_index(all_entries)
        print("  ✓ Thin index updated")

    else:
        print("No entries to save.")

    print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("=" * 40)
    print("SUMMARY")
    print("=" * 40)
    print(f"Original tweets processed:  {stats['original_processed']}")
    print(f"Reply threads processed:    {stats['replies_processed']}")
    print(f"Knowledge entries extracted:{stats['entries_extracted']}")
    print(f"Errors:                     {stats['errors']}")

    print("\nTweets processed by year:")
    for year in sorted(stats["by_year"]):
        print(f"  {year}: {stats['by_year'][year]}")

    if storage:
        storage_stats = storage.get_stats()
        print("\nStorage totals:")
        print(f"  Knowledge entries: {storage_stats['knowledge_entries']}")
        print(f"  Vectors:           {storage_stats['total_vectors']}")

    print()
    print(f"Completed: {datetime.now().isoformat()}")
    print("=" * 60)

    return all_entries


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        description="Ingest knowledge from a downloaded Twitter/X data archive"
    )
    arg_parser.add_argument(
        "--since",
        type=int,
        default=TWITTER_SINCE_YEAR,
        help=f"Only process tweets from this year onwards (default: {TWITTER_SINCE_YEAR})",
    )
    arg_parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Maximum number of tweets to process (default: all)",
    )
    arg_parser.add_argument(
        "--original-batch",
        type=int,
        default=25,
        help="Tweets per LLM call for original tweets (default: 25)",
    )
    arg_parser.add_argument(
        "--reply-batch",
        type=int,
        default=12,
        help="Reply threads per LLM call (default: 12)",
    )
    arg_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract but do NOT save to storage",
    )
    arg_parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Re-process tweets that have already been ingested",
    )

    args = arg_parser.parse_args()

    run_twitter_ingestion(
        since_year=args.since,
        max_tweets=args.max,
        original_batch_size=args.original_batch,
        reply_batch_size=args.reply_batch,
        dry_run=args.dry_run,
        resume=not args.no_resume,
    )
