#!/usr/bin/env python3
"""
=============================================================================
TWITTER INGESTION RUNNER
=============================================================================
Version: 1.1.0
Last Updated: April 2026

PURPOSE:
Ingest knowledge from your Twitter/X timeline using the Twitter API v2
(pay-per-use model, Bearer Token auth).

On the FIRST run this does a full backfill — paginating back through as many
tweets as the API returns (up to ~3,200, the hard Twitter timeline limit).
On every SUBSEQUENT run it only fetches tweets posted since the last run by
tracking the highest tweet ID seen in the state checkpoint.

What gets ingested:
  1. Your original tweets (standalone posts)
  2. Your replies, bundled with the tweet you replied to for context
  3. Your quote-tweets, bundled with the tweet you quoted

Retweets are automatically excluded.

INPUT:
  Twitter API v2 — credentials via TWITTER_BEARER_TOKEN env var

OUTPUT FILES:
  Upstash Redis   — knowledge:<id> entries
  Upstash Vector  — embeddings for semantic search
  Redis key       — index:current (thin index)
  Redis keys      — ingested:twitter:<tweet_id>  (dedup markers)
  Checkpoint file — ingestion/checkpoints/twitter_state.json  (last_seen_id)
  Checkpoint pkl  — ingestion/checkpoints/twitter_entries.pkl (all entries)
  Dry-run output  — ingestion/checkpoints/twitter_dry_run.json

USAGE:
    # First run (full backfill)
    python run.py

    # Dry run — fetch + extract but do NOT write to storage
    python run.py --dry-run

    # Only process up to N tweets (useful for testing)
    python run.py --max 100 --dry-run

    # Force a full re-fetch even if state exists
    python run.py --reset-state

CONFIGURATION (in ingestion/.env or environment):
    TWITTER_BEARER_TOKEN=<your Bearer Token from developer.twitter.com>
    TWITTER_USERNAME=arjundivecha

NIGHTLY AUTOMATION (macOS launchd):
    A companion plist is provided at ingestion/twitter/com.arjun.knowledge-twitter.plist
    It runs this script every night at 2:00 AM.

    To install:
        cp ingestion/twitter/com.arjun.knowledge-twitter.plist ~/Library/LaunchAgents/
        launchctl load ~/Library/LaunchAgents/com.arjun.knowledge-twitter.plist

    To test immediately:
        launchctl start com.arjun.knowledge-twitter

    Logs:
        ~/.knowledge_twitter_stdout.log
        ~/.knowledge_twitter_stderr.log
=============================================================================
"""

import argparse
import json
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import (
    CHECKPOINT_DIR,
    TWITTER_BEARER_TOKEN,
    TWITTER_MIN_TWEET_LENGTH,
    TWITTER_USERNAME,
    validate_twitter_config,
)
from core.storage import StorageClient
from twitter.api_client import TwitterAPIClient
from twitter.tweet_extractor import TweetExtractor


# ---------------------------------------------------------------------------
# State file: persists the last-seen tweet ID between runs
# ---------------------------------------------------------------------------
_STATE_FILE = CHECKPOINT_DIR / "twitter_state.json"
_PROGRESS_FILE = CHECKPOINT_DIR / "twitter_progress.pkl"
_PHASE_ORDER = {"original": 0, "reply": 1, "quote": 2, "save": 3}


def _load_state() -> dict:
    """Load run state (last_seen_id, run_count, etc.) from disk."""
    if _STATE_FILE.exists():
        with open(_STATE_FILE) as f:
            return json.load(f)
    return {"last_seen_id": None, "run_count": 0, "last_run_at": None}


def _save_state(state: dict) -> None:
    """Persist run state to disk."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _save_checkpoint(name: str, data) -> None:
    path = CHECKPOINT_DIR / f"twitter_{name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(data, f)


def _load_checkpoint(name: str):
    path = CHECKPOINT_DIR / f"twitter_{name}.pkl"
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def _save_progress(progress: dict) -> None:
    with open(_PROGRESS_FILE, "wb") as f:
        pickle.dump(progress, f)


def _load_progress() -> Optional[dict]:
    if _PROGRESS_FILE.exists():
        with open(_PROGRESS_FILE, "rb") as f:
            return pickle.load(f)
    return None


def _clear_progress() -> None:
    if _PROGRESS_FILE.exists():
        _PROGRESS_FILE.unlink()


def _progress_signature(
    since_id: Optional[str],
    max_tweets: Optional[int],
    original_batch_size: int,
    reply_batch_size: int,
    quote_batch_size: int,
) -> dict:
    """Build the config signature that makes a checkpoint safe to resume."""
    return {
        "since_id": since_id,
        "max_tweets": max_tweets,
        "original_batch_size": original_batch_size,
        "reply_batch_size": reply_batch_size,
        "quote_batch_size": quote_batch_size,
    }


# ---------------------------------------------------------------------------
# Main ingestion function
# ---------------------------------------------------------------------------

def run_twitter_ingestion(
    max_tweets: int = None,
    original_batch_size: int = 25,
    reply_batch_size: int = 12,
    quote_batch_size: int = 12,
    dry_run: bool = False,
    reset_state: bool = False,
) -> list[dict]:
    """
    Run the Twitter ingestion pipeline.

    Args:
        max_tweets:          Cap total tweets to process (None = no cap).
        original_batch_size: Tweets per LLM call for original tweets.
        reply_batch_size:    Reply threads per LLM call.
        quote_batch_size:    Quote-tweet threads per LLM call.
        dry_run:             If True, fetch + extract but do NOT write to storage.
        reset_state:         If True, ignore last_seen_id and do a full re-fetch.

    Returns:
        List of all knowledge entry dicts that were extracted this run.
    """
    print("=" * 60)
    print("TWITTER KNOWLEDGE INGESTION")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")
    print()

    # ------------------------------------------------------------------
    # Config check
    # ------------------------------------------------------------------
    errors = validate_twitter_config()
    if errors:
        print("Configuration errors:")
        for err in errors:
            print(f"  ✗ {err}")
        return []

    # ------------------------------------------------------------------
    # Load run state
    # ------------------------------------------------------------------
    state = _load_state()
    expected_signature = _progress_signature(
        state.get("last_seen_id"),
        max_tweets,
        original_batch_size,
        reply_batch_size,
        quote_batch_size,
    )
    progress = None
    if reset_state:
        state["last_seen_id"] = None
        _clear_progress()
        print("State reset — will perform full backfill.")
    else:
        candidate_progress = _load_progress()
        if (
            not dry_run
            and candidate_progress
            and candidate_progress.get("mode") == "live"
            and candidate_progress.get("signature") == expected_signature
        ):
            progress = candidate_progress
        elif candidate_progress and not dry_run:
            print("Ignoring stale extraction checkpoint (run settings do not match).")

    if progress:
        print("Found resumable extraction checkpoint.")
        print(
            f"Resuming from phase={progress['phase']} "
            f"batch={progress['next_batch_index'] + 1}"
        )
    elif not reset_state:
        if state["last_seen_id"]:
            print(
                f"Incremental run — fetching tweets newer than "
                f"tweet_id {state['last_seen_id']}"
            )
            print(f"(Last run: {state.get('last_run_at', 'unknown')})")
        else:
            print("First run — performing full backfill (up to ~3,200 tweets)")
    print()

    # ------------------------------------------------------------------
    # Init clients
    # ------------------------------------------------------------------
    api = TwitterAPIClient(
        bearer_token=TWITTER_BEARER_TOKEN,
        username=TWITTER_USERNAME,
    )
    extractor = TweetExtractor()
    storage = StorageClient() if not dry_run else None

    if not dry_run:
        ok, msg = storage.test_connection()
        print(f"Storage: {msg}")
        if not ok:
            print("  ✗ Cannot connect to storage, aborting.")
            return []
    print()

    # ------------------------------------------------------------------
    # Get already-processed tweet IDs (dedup within same session)
    # ------------------------------------------------------------------
    processed_ids: set[str] = set()
    if storage:
        processed_ids = set(storage.get_processed_sources("twitter"))
        print(f"Previously processed tweet IDs in Redis: {len(processed_ids)}")
        print()

    # ------------------------------------------------------------------
    # STEP 1: Fetch tweets from the API
    # ------------------------------------------------------------------
    original_tweets: list[dict]
    reply_threads: list[dict]
    quote_tweets: list[dict]
    total_seen: int
    skipped_dedup: int
    highest_id_seen: str
    stats: dict
    all_entries: list[dict]
    sources_to_mark: list[dict]
    resume_phase = "original"
    resume_batch_index = 0

    if progress:
        print("[1/4] FETCHING TWEETS FROM TWITTER API")
        print("-" * 40)
        print("Skipped fetch — using resumable checkpoint")
        original_tweets = progress["original_tweets"]
        reply_threads = progress["reply_threads"]
        quote_tweets = progress["quote_tweets"]
        total_seen = progress["total_seen"]
        skipped_dedup = progress["skipped_dedup"]
        highest_id_seen = progress["highest_id_seen"]
        stats = progress["stats"]
        all_entries = progress["all_entries"]
        sources_to_mark = progress.get("sources_to_mark", [])
        resume_phase = progress["phase"]
        resume_batch_index = progress["next_batch_index"]
    else:
        print("[1/4] FETCHING TWEETS FROM TWITTER API")
        print("-" * 40)
        print(f"Username: @{TWITTER_USERNAME}")

        original_tweets = []
        reply_threads = []
        quote_tweets = []
        total_seen = 0
        skipped_dedup = 0
        highest_id_seen = state.get("last_seen_id") or ""

        for record in api.iter_user_tweets(
            username=TWITTER_USERNAME,
            since_id=state["last_seen_id"],
            min_length=TWITTER_MIN_TWEET_LENGTH,
            max_tweets=max_tweets,
        ):
            total_seen += 1

            if not highest_id_seen or int(record["id"]) > int(highest_id_seen):
                highest_id_seen = record["id"]

            if record["id"] in processed_ids:
                skipped_dedup += 1
                continue

            if record["is_reply"]:
                reply_threads.append({
                    "reply": record,
                    "parent_text": record["reply_to_text"],
                    "parent_author": record["reply_to_author"],
                })
            elif record["is_quote"]:
                quote_tweets.append({
                    "tweet": record,
                    "quoted_text": record["quoted_text"],
                    "quoted_author": record["quoted_author"],
                })
            else:
                original_tweets.append(record)

        print(f"Fetched {total_seen} tweets total from API")
        print(f"  Original:    {len(original_tweets)}")
        print(f"  Replies:     {len(reply_threads)}")
        print(f"  Quote-tweets:{len(quote_tweets)}")
        print(f"  Skipped (already processed): {skipped_dedup}")
        print("X API usage this fetch:")
        print(f"  Timeline posts consumed: {api.usage['timeline_posts_consumed']}")
        print(f"  Lookup posts consumed:   {api.usage['lookup_posts_consumed']}")
        print(f"  Total posts consumed:    {api.usage['total_posts_consumed']}")
        print(f"  User lookups:            {api.usage['user_lookups']}")

        stats = {
            "original_processed": 0,
            "replies_processed": 0,
            "quotes_processed": 0,
            "entries_extracted": 0,
            "errors": 0,
            "by_year": defaultdict(int),
        }
        all_entries = []
        sources_to_mark = []
        if not dry_run:
            _save_progress({
                "mode": "live",
                "signature": expected_signature,
                "phase": "original",
                "next_batch_index": 0,
                "original_tweets": original_tweets,
                "reply_threads": reply_threads,
                "quote_tweets": quote_tweets,
                "total_seen": total_seen,
                "skipped_dedup": skipped_dedup,
                "highest_id_seen": highest_id_seen,
                "all_entries": all_entries,
                "sources_to_mark": sources_to_mark,
                "stats": stats,
            })

    if not original_tweets and not reply_threads and not quote_tweets:
        print("\nNo new tweets to process.")
        # Still update state so next run knows the right since_id
        if highest_id_seen:
            state["last_seen_id"] = highest_id_seen
            state["last_run_at"] = datetime.utcnow().isoformat()
            state["run_count"] = state.get("run_count", 0) + 1
            if not dry_run:
                _save_state(state)
        return []

    print()

    # ------------------------------------------------------------------
    # STEP 2: (already done inline above in iter_user_tweets)
    # ------------------------------------------------------------------
    print("[2/4] CLASSIFYING TWEETS")
    print("-" * 40)
    print("Classification complete (done during fetch)")
    print()

    # ------------------------------------------------------------------
    # STEP 3: Extract knowledge with LLM
    # ------------------------------------------------------------------
    print("[3/4] EXTRACTING KNOWLEDGE")
    print("-" * 40)

    def _queue_tweets_processed(records: list[dict], tweet_type: str, n_entries: int):
        """Queue tweet IDs to be marked processed after durable storage succeeds."""
        for rec in records:
            sources_to_mark.append(
                {
                    "source_id": rec["id"],
                    "metadata": {
                        "date": rec["created_at"],
                        "type": tweet_type,
                        "entries_count": n_entries,
                    },
                }
            )

    def _checkpoint_progress(phase: str, next_batch_index: int) -> None:
        if dry_run:
            return
        _save_progress({
            "mode": "live",
            "signature": expected_signature,
            "phase": phase,
            "next_batch_index": next_batch_index,
            "original_tweets": original_tweets,
            "reply_threads": reply_threads,
            "quote_tweets": quote_tweets,
            "total_seen": total_seen,
            "skipped_dedup": skipped_dedup,
            "highest_id_seen": highest_id_seen,
            "all_entries": all_entries,
            "sources_to_mark": sources_to_mark,
            "stats": stats,
        })

    def _phase_should_run(phase: str) -> bool:
        return _PHASE_ORDER[phase] >= _PHASE_ORDER[resume_phase]

    def _extract_with_retry(kind: str, batch_num: int, total_batches: int, extract_fn):
        """Retry transient/LLM parse failures; abort run if a batch cannot be extracted safely."""
        max_attempts = 3
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                entries = extract_fn()
            except Exception as e:
                last_error = str(e)
            else:
                if not extractor.last_error:
                    return entries
                last_error = extractor.last_error

            if attempt < max_attempts:
                print(
                    f"retry {attempt}/{max_attempts - 1} after error: {last_error}",
                    end=" … ",
                    flush=True,
                )
                time.sleep(min(10, attempt * 2))
                continue

            raise RuntimeError(
                f"{kind} batch {batch_num}/{total_batches} failed after "
                f"{max_attempts} attempts: {last_error}"
            )

    # ---- 3a: Original tweets ----
    if original_tweets and _phase_should_run("original"):
        n_batches = (len(original_tweets) + original_batch_size - 1) // original_batch_size
        print(f"Original tweets: {len(original_tweets)} in {n_batches} batches")
        start_idx = resume_batch_index if resume_phase == "original" else 0
        for i in range(start_idx * original_batch_size, len(original_tweets), original_batch_size):
            batch = original_tweets[i : i + original_batch_size]
            batch_num = i // original_batch_size + 1
            print(f"  Batch {batch_num}/{n_batches} ({len(batch)} tweets) … ", end="", flush=True)
            try:
                entries = _extract_with_retry(
                    "original",
                    batch_num,
                    n_batches,
                    lambda: extractor.extract_from_original_tweets(batch),
                )
                all_entries.extend(entries)
                stats["entries_extracted"] += len(entries)
                stats["original_processed"] += len(batch)
                for rec in batch:
                    stats["by_year"][rec["date_obj"].year] += 1
                _queue_tweets_processed(batch, "original", len(entries))
                print(f"{len(entries)} entries")
            except Exception as e:
                print(f"ERROR: {e}")
                stats["errors"] += 1
                _checkpoint_progress("original", batch_num - 1)
                raise

            _checkpoint_progress("original", batch_num)
            if batch_num % 5 == 0:
                _save_checkpoint("entries", all_entries)

    # ---- 3b: Reply threads ----
    if reply_threads and _phase_should_run("reply"):
        n_batches = (len(reply_threads) + reply_batch_size - 1) // reply_batch_size
        print(f"\nReply threads: {len(reply_threads)} in {n_batches} batches")
        start_idx = resume_batch_index if resume_phase == "reply" else 0
        for i in range(start_idx * reply_batch_size, len(reply_threads), reply_batch_size):
            batch = reply_threads[i : i + reply_batch_size]
            batch_num = i // reply_batch_size + 1
            print(f"  Batch {batch_num}/{n_batches} ({len(batch)} replies) … ", end="", flush=True)
            try:
                entries = _extract_with_retry(
                    "reply",
                    batch_num,
                    n_batches,
                    lambda: extractor.extract_from_reply_threads(batch),
                )
                all_entries.extend(entries)
                stats["entries_extracted"] += len(entries)
                stats["replies_processed"] += len(batch)
                for thread in batch:
                    rec = thread["reply"]
                    stats["by_year"][rec["date_obj"].year] += 1
                _queue_tweets_processed(
                    [t["reply"] for t in batch], "reply", len(entries)
                )
                print(f"{len(entries)} entries")
            except Exception as e:
                print(f"ERROR: {e}")
                stats["errors"] += 1
                _checkpoint_progress("reply", batch_num - 1)
                raise

            _checkpoint_progress("reply", batch_num)
            if batch_num % 5 == 0:
                _save_checkpoint("entries", all_entries)

    # ---- 3c: Quote-tweets ----
    if quote_tweets and _phase_should_run("quote"):
        n_batches = (len(quote_tweets) + quote_batch_size - 1) // quote_batch_size
        print(f"\nQuote-tweets: {len(quote_tweets)} in {n_batches} batches")
        start_idx = resume_batch_index if resume_phase == "quote" else 0
        for i in range(start_idx * quote_batch_size, len(quote_tweets), quote_batch_size):
            batch = quote_tweets[i : i + quote_batch_size]
            batch_num = i // quote_batch_size + 1
            print(f"  Batch {batch_num}/{n_batches} ({len(batch)} quotes) … ", end="", flush=True)
            try:
                entries = _extract_with_retry(
                    "quote",
                    batch_num,
                    n_batches,
                    lambda: extractor.extract_from_quote_tweets(batch),
                )
                all_entries.extend(entries)
                stats["entries_extracted"] += len(entries)
                stats["quotes_processed"] += len(batch)
                for q in batch:
                    rec = q["tweet"]
                    stats["by_year"][rec["date_obj"].year] += 1
                _queue_tweets_processed(
                    [q["tweet"] for q in batch], "quote", len(entries)
                )
                print(f"{len(entries)} entries")
            except Exception as e:
                print(f"ERROR: {e}")
                stats["errors"] += 1
                _checkpoint_progress("quote", batch_num - 1)
                raise

            _checkpoint_progress("quote", batch_num)
            if batch_num % 5 == 0:
                _save_checkpoint("entries", all_entries)

    # Final checkpoint
    _checkpoint_progress("save", 0)
    _save_checkpoint("entries", all_entries)
    _save_checkpoint("stats", dict(stats))
    print()

    # ------------------------------------------------------------------
    # STEP 4: Save to storage
    # ------------------------------------------------------------------
    print("[4/4] SAVING TO STORAGE")
    print("-" * 40)

    if dry_run:
        print("DRY RUN — not writing to storage")
        print(f"Would save {len(all_entries)} entries")
        out = CHECKPOINT_DIR / "twitter_dry_run.json"
        with open(out, "w") as f:
            json.dump(all_entries, f, indent=2, default=str)
        print(f"Dry-run output: {out}")

    elif all_entries:
        batch_sz = 20
        print(f"Saving {len(all_entries)} entries …")
        for i in range(0, len(all_entries), batch_sz):
            batch = all_entries[i : i + batch_sz]
            storage.save_knowledge_entries_batch(batch)
            print(f"  Saved {min(i + batch_sz, len(all_entries))}/{len(all_entries)}")

        print("Updating thin index …")
        storage.update_thin_index(all_entries)
        print("  ✓ Thin index updated")
        print(f"Marking {len(sources_to_mark)} tweet IDs processed …")
        for item in sources_to_mark:
            storage.mark_source_processed("twitter", item["source_id"], item["metadata"])
        print("  ✓ Dedup markers updated")

    else:
        print("No entries to save.")

    print()

    # ------------------------------------------------------------------
    # Persist state for next run
    # ------------------------------------------------------------------
    if highest_id_seen:
        state["last_seen_id"] = highest_id_seen
    state["last_run_at"] = datetime.utcnow().isoformat()
    state["run_count"] = state.get("run_count", 0) + 1
    if not dry_run:
        _save_state(state)
        print(f"State saved — next run will use since_id={highest_id_seen}")
        _clear_progress()
    else:
        print(
            f"(Dry run — state NOT updated. Next run would use since_id={highest_id_seen})"
        )
    print()

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("=" * 40)
    print("SUMMARY")
    print("=" * 40)
    print(f"Tweets fetched from API:        {total_seen}")
    print(f"  Original tweets processed:    {stats['original_processed']}")
    print(f"  Reply threads processed:      {stats['replies_processed']}")
    print(f"  Quote-tweets processed:       {stats['quotes_processed']}")
    print(f"Knowledge entries extracted:    {stats['entries_extracted']}")
    print(f"Extraction errors:              {stats['errors']}")
    print("X API usage this run:")
    print(f"  Timeline posts consumed:      {api.usage['timeline_posts_consumed']}")
    print(f"  Lookup posts consumed:        {api.usage['lookup_posts_consumed']}")
    print(f"  Total posts consumed:         {api.usage['total_posts_consumed']}")
    print(f"  User lookups:                 {api.usage['user_lookups']}")
    print("  Billing note: check the X Developer Console or /2/usage/tweets")

    if stats["by_year"]:
        print("\nTweets by year:")
        for year in sorted(stats["by_year"]):
            print(f"  {year}: {stats['by_year'][year]}")

    if storage:
        sstats = storage.get_stats()
        print("\nStorage totals:")
        print(f"  Knowledge entries: {sstats['knowledge_entries']}")
        print(f"  Vectors:           {sstats['total_vectors']}")

    print()
    print(f"Completed: {datetime.now().isoformat()}")
    print("=" * 60)

    return all_entries


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Ingest knowledge from your Twitter/X timeline via the API"
    )
    ap.add_argument(
        "--max",
        type=int,
        default=None,
        help="Maximum tweets to process this run (default: all)",
    )
    ap.add_argument(
        "--original-batch",
        type=int,
        default=25,
        help="Original tweets per LLM call (default: 25)",
    )
    ap.add_argument(
        "--reply-batch",
        type=int,
        default=12,
        help="Reply threads per LLM call (default: 12)",
    )
    ap.add_argument(
        "--quote-batch",
        type=int,
        default=12,
        help="Quote-tweets per LLM call (default: 12)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + extract but do NOT write to storage",
    )
    ap.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore saved since_id and do a full re-fetch",
    )

    args = ap.parse_args()

    run_twitter_ingestion(
        max_tweets=args.max,
        original_batch_size=args.original_batch,
        reply_batch_size=args.reply_batch,
        quote_batch_size=args.quote_batch,
        dry_run=args.dry_run,
        reset_state=args.reset_state,
    )
