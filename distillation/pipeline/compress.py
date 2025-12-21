"""
=============================================================================
STAGE 5: COMPRESS - Archive and compress old entries
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Identify entries eligible for compression, archive full content locally,
and generate compressed views. Non-destructive - original is always preserved.

INPUT FILES:
- Entries from Upstash Redis

OUTPUT FILES:
- Archived entries in ARCHIVE_PATH
- Compressed entries in Upstash Redis

USAGE:
    from distillation.pipeline.compress import compress_eligible_entries
    results = compress_eligible_entries(redis_client)
=============================================================================
"""

import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from config import ARCHIVE_PATH, COMPRESS_AFTER_DAYS, COMPRESS_IF_ACCESS_COUNT_BELOW
from models import KnowledgeEntry, Evolution, Evidence
from storage.redis_client import RedisClient
from prompts.compression import build_compression_prompt
from utils.llm import call_claude_json


# -----------------------------------------------------------------------------
# COMPRESSION RESULT
# -----------------------------------------------------------------------------

@dataclass
class CompressionResult:
    """Result from compressing an entry."""
    entry_id: str
    action: str  # "compressed", "skipped", "error"
    reason: str
    archive_path: Optional[str] = None
    success: bool = True
    error: Optional[str] = None


# -----------------------------------------------------------------------------
# ELIGIBILITY CHECK
# -----------------------------------------------------------------------------

def is_eligible_for_compression(entry: KnowledgeEntry) -> tuple[bool, str]:
    """
    Check if an entry is eligible for compression.
    
    Criteria:
    - detail_level is "full" (not already compressed)
    - state is not "contested" (need resolution first)
    - updated_at > COMPRESS_AFTER_DAYS ago
    - access_count < COMPRESS_IF_ACCESS_COUNT_BELOW
    - Not linked to active project (future check)
    
    Returns:
        Tuple of (is_eligible, reason)
    """
    # Already compressed
    if entry.detail_level == "compressed":
        return False, "Already compressed"
    
    # Contested entries need human resolution
    if entry.state == "contested":
        return False, "Entry is contested - needs resolution"
    
    # Check age
    try:
        updated_at = datetime.fromisoformat(entry.metadata.updated_at.replace("Z", "+00:00"))
        age_threshold = datetime.utcnow() - timedelta(days=COMPRESS_AFTER_DAYS)
        
        if updated_at.replace(tzinfo=None) > age_threshold.replace(tzinfo=None):
            days_old = (datetime.utcnow() - updated_at.replace(tzinfo=None)).days
            return False, f"Too recent ({days_old} days old, threshold is {COMPRESS_AFTER_DAYS})"
    except:
        pass  # If we can't parse date, continue with other checks
    
    # Check access count
    if entry.metadata.access_count >= COMPRESS_IF_ACCESS_COUNT_BELOW:
        return False, f"Access count too high ({entry.metadata.access_count})"
    
    # Check evolution recency
    if entry.evolution:
        try:
            latest_evo = max(entry.evolution, key=lambda e: e.date)
            evo_date = datetime.fromisoformat(latest_evo.date.replace("Z", "+00:00"))
            evo_threshold = datetime.utcnow() - timedelta(days=60)
            
            if evo_date.replace(tzinfo=None) > evo_threshold.replace(tzinfo=None):
                return False, "Recent evolution (within 60 days)"
        except:
            pass
    
    return True, "Eligible for compression"


# -----------------------------------------------------------------------------
# ARCHIVING
# -----------------------------------------------------------------------------

def archive_entry(entry: KnowledgeEntry) -> str:
    """
    Archive the full entry to local storage.
    
    Returns:
        Path to the archived file
    """
    archive_dir = Path(ARCHIVE_PATH)
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    archive_file = archive_dir / f"{entry.id}.json"
    
    with open(archive_file, "w", encoding="utf-8") as f:
        json.dump(entry.to_dict(), f, indent=2)
    
    return str(archive_file)


# -----------------------------------------------------------------------------
# COMPRESSION
# -----------------------------------------------------------------------------

def summarize_evolution(evolution: list[Evolution]) -> list[Evolution]:
    """
    Summarize multiple evolutions into one.
    Preserves the trajectory without full details.
    """
    if not evolution:
        return []
    
    if len(evolution) == 1:
        return evolution
    
    # Multiple evolutions: create summary
    first = evolution[0]
    last = evolution[-1]
    
    summary = Evolution(
        delta=f"Evolved through {len(evolution)} stages: {first.from_view[:50]}... → {last.to_view[:50]}...",
        trigger="Multiple conversations",
        from_view=first.from_view,
        to_view=last.to_view,
        date=last.date,
        evidence=last.evidence,
    )
    
    return [summary]


def compress_entry(
    entry: KnowledgeEntry,
    archive_path: str,
) -> KnowledgeEntry:
    """
    Create a compressed view of an entry.
    Uses LLM to generate concise summary.
    
    Args:
        entry: The full entry to compress
        archive_path: Path where full content was archived
    
    Returns:
        Compressed entry
    """
    # Build compression prompt
    prompt = build_compression_prompt(entry)
    
    try:
        # Call LLM for compression
        compressed_data, _, _ = call_claude_json(prompt)
        
        # Update entry with compressed content
        entry.detail_level = "compressed"
        entry.full_content_ref = archive_path
        
        # Use compressed current_view
        if compressed_data.get("current_view"):
            entry.current_view = compressed_data["current_view"][:200]  # Max 2 sentences
        
        # Keep only top 3 insights
        if compressed_data.get("key_insights"):
            # Rebuild insights from compressed data, keeping evidence
            new_insights = []
            for i, ins_data in enumerate(compressed_data["key_insights"][:3]):
                if i < len(entry.key_insights):
                    # Use original evidence with compressed text
                    original = entry.key_insights[i]
                    original.insight = ins_data.get("insight", original.insight)[:150]
                    new_insights.append(original)
            entry.key_insights = new_insights if new_insights else entry.key_insights[:3]
        else:
            entry.key_insights = entry.key_insights[:3]
        
        # Keep only top 2 capabilities
        entry.knows_how_to = entry.knows_how_to[:2]
        
        # Drop open questions (can retrieve from archive)
        entry.open_questions = []
        
        # Summarize evolution
        entry.evolution = summarize_evolution(entry.evolution)
        
    except Exception as e:
        # Fallback: simple truncation without LLM
        entry.detail_level = "compressed"
        entry.full_content_ref = archive_path
        entry.current_view = entry.current_view[:200]
        entry.key_insights = entry.key_insights[:3]
        entry.knows_how_to = entry.knows_how_to[:2]
        entry.open_questions = []
        entry.evolution = summarize_evolution(entry.evolution)
    
    return entry


# -----------------------------------------------------------------------------
# MAIN COMPRESS FUNCTION
# -----------------------------------------------------------------------------

def compress_eligible_entries(
    redis_client: RedisClient,
) -> list[CompressionResult]:
    """
    Find and compress all eligible entries.
    
    Args:
        redis_client: Redis client for storage
    
    Returns:
        List of CompressionResult objects
    """
    results = []
    
    # Get all knowledge entries
    entries = redis_client.get_all_knowledge_entries()
    
    for entry in entries:
        try:
            # Check eligibility
            eligible, reason = is_eligible_for_compression(entry)
            
            if not eligible:
                results.append(CompressionResult(
                    entry_id=entry.id,
                    action="skipped",
                    reason=reason,
                ))
                continue
            
            # Archive full content
            archive_path = archive_entry(entry)
            
            # Compress
            compressed = compress_entry(entry, archive_path)
            
            # Save compressed version
            redis_client.save_knowledge_entry(compressed)
            
            results.append(CompressionResult(
                entry_id=entry.id,
                action="compressed",
                reason="Successfully compressed",
                archive_path=archive_path,
            ))
        
        except Exception as e:
            results.append(CompressionResult(
                entry_id=entry.id,
                action="error",
                reason=str(e),
                success=False,
                error=str(e),
            ))
    
    return results


# -----------------------------------------------------------------------------
# CLI FOR TESTING
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compress old entries")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compressed")
    args = parser.parse_args()
    
    print("=" * 60)
    print("STAGE 5: COMPRESS - Testing compression logic")
    print("=" * 60)
    print()
    print("This stage requires Upstash credentials and stored entries.")
    print("Run the full pipeline to test compression functionality.")
    print()
    print(f"Archive path: {ARCHIVE_PATH}")
    print(f"Compress after: {COMPRESS_AFTER_DAYS} days")
    print(f"Compress if access count below: {COMPRESS_IF_ACCESS_COUNT_BELOW}")
    print("=" * 60)


if __name__ == "__main__":
    main()

