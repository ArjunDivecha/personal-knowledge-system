#!/usr/bin/env python3
"""
=============================================================================
GMAIL INGESTION RUNNER
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Ingest knowledge from Gmail sent messages.
Extracts positions, expertise, and commitments from substantive emails.

INPUT FILES:
- Gmail mbox export file

OUTPUT FILES:
- Knowledge entries in Upstash Redis
- Embeddings in Upstash Vector
- Checkpoint files in checkpoints/

USAGE:
    python run.py                    # Default: since 2020
    python run.py --since 2022       # Custom start year
    python run.py --max 500          # Limit emails processed
    python run.py --dry-run          # Extract but don't save
=============================================================================
"""

import argparse
import json
import pickle
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Setup path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import validate_gmail_config, CHECKPOINT_DIR, GMAIL_SINCE_YEAR
from core.storage import StorageClient
from core.extractor import Extractor
from gmail.parser import MboxParser


def save_checkpoint(name: str, data: any):
    """Save checkpoint data to disk."""
    path = CHECKPOINT_DIR / f"gmail_{name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"  ✓ Checkpoint saved: {name}")


def load_checkpoint(name: str) -> any:
    """Load checkpoint data from disk."""
    path = CHECKPOINT_DIR / f"gmail_{name}.pkl"
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def run_gmail_ingestion(
    since_year: int = None,
    max_emails: int = None,
    batch_size: int = 50,
    dry_run: bool = False,
    resume: bool = True,
):
    """
    Run Gmail knowledge ingestion.
    
    Args:
        since_year: Only process emails from this year onwards
        max_emails: Maximum number of emails to process
        batch_size: Emails per extraction batch
        dry_run: Extract but don't save to storage
        resume: Resume from checkpoint if available
    """
    print("=" * 60)
    print("GMAIL KNOWLEDGE INGESTION")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")
    print()
    
    since_year = since_year or GMAIL_SINCE_YEAR
    
    # Validate configuration
    errors = validate_gmail_config()
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  ✗ {error}")
        return
    
    # Initialize clients
    parser = MboxParser()
    extractor = Extractor()
    storage = StorageClient() if not dry_run else None
    
    if not dry_run:
        ok, msg = storage.test_connection()
        print(f"Storage: {msg}")
        if not ok:
            print("  ✗ Cannot connect to storage, aborting")
            return
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 1: Count and analyze emails
    # -------------------------------------------------------------------------
    print("[1/4] ANALYZING MBOX FILE")
    print("-" * 40)
    print(f"File: {parser.mbox_path}")
    
    counts = parser.count_emails(since_year=since_year)
    print(f"Emails since {since_year}: {counts['total']}")
    print("By year:")
    for year, count in counts["by_year"].items():
        print(f"  {year}: {count}")
    
    # Check for already processed emails
    processed_ids = set()
    if resume and storage:
        processed_ids = set(storage.get_processed_sources("gmail"))
        print(f"\nAlready processed: {len(processed_ids)} emails")
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 2: Parse and filter emails
    # -------------------------------------------------------------------------
    print("[2/4] PARSING EMAILS")
    print("-" * 40)
    
    emails_to_process = []
    emails_skipped = 0
    
    for email_data in parser.parse_emails(since_year=since_year, max_emails=max_emails):
        if email_data["id"] in processed_ids:
            emails_skipped += 1
            continue
        
        emails_to_process.append(email_data)
        
        # Progress indicator
        if len(emails_to_process) % 100 == 0:
            print(f"  Parsed {len(emails_to_process)} emails...", flush=True)
    
    print(f"Total emails to process: {len(emails_to_process)}")
    print(f"Skipped (already processed): {emails_skipped}")
    
    if not emails_to_process:
        print("\nNo new emails to process")
        return
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 3: Extract knowledge in batches
    # -------------------------------------------------------------------------
    print("[3/4] EXTRACTING KNOWLEDGE")
    print("-" * 40)
    
    all_entries = []
    stats = {
        "emails_processed": 0,
        "entries_extracted": 0,
        "errors": 0,
        "by_year": defaultdict(int),
    }
    
    for i in range(0, len(emails_to_process), batch_size):
        batch = emails_to_process[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(emails_to_process) + batch_size - 1) // batch_size
        
        print(f"\nBatch {batch_num}/{total_batches} ({len(batch)} emails)")
        
        for j, email_data in enumerate(batch):
            try:
                # Extract knowledge from email
                entries = extractor.extract_from_email(
                    email_content=email_data["content"],
                    email_subject=email_data["subject"],
                    email_date=email_data["date"],
                    recipients=email_data["to"],
                )
                
                if entries:
                    all_entries.extend(entries)
                    stats["entries_extracted"] += len(entries)
                    
                    year = email_data["date_obj"].year
                    stats["by_year"][year] += len(entries)
                    
                    print(f"  [{j+1}/{len(batch)}] {email_data['date'][:10]} - {len(entries)} entries")
                
                stats["emails_processed"] += 1
                
                # Mark as processed
                if storage:
                    storage.mark_source_processed("gmail", email_data["id"], {
                        "date": email_data["date"],
                        "subject": email_data["subject"][:100],
                        "entries_count": len(entries),
                    })
                
            except Exception as e:
                print(f"  ✗ Error: {e}")
                stats["errors"] += 1
        
        # Checkpoint after each batch
        save_checkpoint("entries", all_entries)
        save_checkpoint("stats", dict(stats))
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 4: Save to storage
    # -------------------------------------------------------------------------
    print("[4/4] SAVING TO STORAGE")
    print("-" * 40)
    
    if dry_run:
        print("DRY RUN - Not saving to storage")
        print(f"Would save {len(all_entries)} entries")
        
        # Save to file for inspection
        output_path = CHECKPOINT_DIR / "gmail_dry_run.json"
        with open(output_path, "w") as f:
            json.dump(all_entries, f, indent=2)
        print(f"Saved to {output_path}")
    
    elif all_entries:
        print(f"Saving {len(all_entries)} entries...")
        
        # Batch save
        save_batch_size = 20
        for i in range(0, len(all_entries), save_batch_size):
            batch = all_entries[i:i + save_batch_size]
            storage.save_knowledge_entries_batch(batch)
            print(f"  Saved {min(i + save_batch_size, len(all_entries))}/{len(all_entries)}")
        
        # Update thin index
        print("Updating thin index...")
        storage.update_thin_index(all_entries)
        print("  ✓ Thin index updated")
    
    else:
        print("No entries to save")
    
    print()
    
    # -------------------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------------------
    print("=" * 40)
    print("SUMMARY")
    print("=" * 40)
    print(f"Emails processed:  {stats['emails_processed']}")
    print(f"Entries extracted: {stats['entries_extracted']}")
    print(f"Errors:            {stats['errors']}")
    
    print("\nEntries by year:")
    for year in sorted(stats["by_year"].keys()):
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest knowledge from Gmail sent messages"
    )
    parser.add_argument(
        "--since",
        type=int,
        default=GMAIL_SINCE_YEAR,
        help=f"Only process emails from this year onwards (default: {GMAIL_SINCE_YEAR})"
    )
    parser.add_argument(
        "--max",
        type=int,
        help="Maximum number of emails to process"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Emails per extraction batch (default: 50)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract but don't save to storage"
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't skip already processed emails"
    )
    
    args = parser.parse_args()
    
    run_gmail_ingestion(
        since_year=args.since,
        max_emails=args.max,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        resume=not args.no_resume,
    )

