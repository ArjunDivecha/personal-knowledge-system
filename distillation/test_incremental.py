#!/usr/bin/env python3
"""
Test incremental processing by comparing conversation hashes.
"""

import json
import hashlib
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from config import CLAUDE_EXPORT_PATH, GPT_EXPORT_PATH
from pipeline.parse import parse_claude_export, parse_gpt_export


def conv_hash(conv):
    """Generate a hash for a conversation."""
    content = json.dumps({
        "id": conv.id,
        "title": conv.title,
        "message_count": conv.message_count,
        "updated_at": conv.updated_at,
        "messages": [{"id": m.message_id, "content": m.content[:100]} for m in conv.messages]
    }, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def load_stored_hashes():
    """Load stored hashes from a simple JSON file."""
    hash_file = Path(__file__).parent / "checkpoints" / "conversation_hashes.json"
    if hash_file.exists():
        with open(hash_file, "r") as f:
            return json.load(f)
    return {}


def save_hashes(hashes):
    """Save hashes to file."""
    hash_file = Path(__file__).parent / "checkpoints" / "conversation_hashes.json"
    hash_file.parent.mkdir(exist_ok=True)
    with open(hash_file, "w") as f:
        json.dump(hashes, f, indent=2)


def main():
    print("=" * 60)
    print("INCREMENTAL PROCESSING TEST")
    print("=" * 60)
    print(f"Time: {datetime.now().isoformat()}")
    print()

    # Load stored hashes
    stored_hashes = load_stored_hashes()
    print(f"Loaded {len(stored_hashes)} stored hashes")
    print()

    # Parse current exports
    print("Parsing current exports...")
    claude_convs = parse_claude_export(CLAUDE_EXPORT_PATH)
    gpt_convs = parse_gpt_export(GPT_EXPORT_PATH)
    all_convs = claude_convs + gpt_convs
    print(f"  Claude: {len(claude_convs)} conversations")
    print(f"  GPT: {len(gpt_convs)} conversations")
    print(f"  Total: {len(all_convs)} conversations")
    print()

    # Compute current hashes
    print("Computing current hashes...")
    current_hashes = {}
    for conv in all_convs:
        current_hashes[conv.id] = conv_hash(conv)
    print(f"  Computed {len(current_hashes)} hashes")
    print()

    # Compare
    print("Comparing with stored hashes...")
    new_ids = set(current_hashes.keys()) - set(stored_hashes.keys())
    deleted_ids = set(stored_hashes.keys()) - set(current_hashes.keys())
    changed_ids = [
        conv_id for conv_id, h in current_hashes.items()
        if conv_id in stored_hashes and h != stored_hashes[conv_id]
    ]
    unchanged_ids = [
        conv_id for conv_id, h in current_hashes.items()
        if conv_id in stored_hashes and h == stored_hashes[conv_id]
    ]

    print(f"  New conversations: {len(new_ids)}")
    print(f"  Updated conversations: {len(changed_ids)}")
    print(f"  Deleted conversations: {len(deleted_ids)}")
    print(f"  Unchanged conversations: {len(unchanged_ids)}")
    print()

    # Show samples
    if new_ids:
        print("Sample new conversations:")
        for conv_id in list(new_ids)[:3]:
            conv = next((c for c in all_convs if c.id == conv_id), None)
            if conv:
                print(f"  - {conv.title[:50]}... ({conv.message_count} messages)")
        print()

    if changed_ids:
        print("Sample updated conversations:")
        for conv_id in list(changed_ids)[:3]:
            conv = next((c for c in all_convs if c.id == conv_id), None)
            if conv:
                print(f"  - {conv.title[:50]}... ({conv.message_count} messages)")
        print()

    if deleted_ids:
        print("Sample deleted conversations:")
        for conv_id in list(deleted_ids)[:3]:
            print(f"  - {conv_id}")
        print()

    # Summary
    to_process = len(new_ids) + len(changed_ids)
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total conversations: {len(all_convs)}")
    print(f"Conversations to process: {to_process}")
    print(f"Conversations to skip: {len(unchanged_ids)}")
    print(f"Efficiency: {len(unchanged_ids) / len(all_convs) * 100:.1f}% can be skipped")
    print()

    # Option to save current hashes
    if len(stored_hashes) == 0:
        print("No stored hashes found. Saving current hashes as baseline...")
        save_hashes(current_hashes)
        print("  ✓ Saved to checkpoints/conversation_hashes.json")
        print()
        print("Next time you run this test, it will compare against these hashes.")
    else:
        print("To update stored hashes, delete checkpoints/conversation_hashes.json and run again.")

    print("=" * 60)


if __name__ == "__main__":
    main()
