#!/usr/bin/env python3
"""
Generate a detailed report of changes before running the full pipeline.
Shows what conversations are new/updated/deleted and previews what would be extracted.
"""

import json
import hashlib
import sys
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))

from config import CLAUDE_EXPORT_PATH, GPT_EXPORT_PATH
from pipeline.parse import parse_claude_export, parse_gpt_export
from pipeline.filter import filter_conversations


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


def main():
    print("=" * 80)
    print("KNOWLEDGE DISTILLATION - CHANGE REPORT")
    print("=" * 80)
    print(f"Generated: {datetime.now().isoformat()}")
    print()

    # Load stored hashes
    stored_hashes = load_stored_hashes()
    print(f"Stored hashes: {len(stored_hashes)} conversations")
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
    print("Analyzing changes...")
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

    # Get conversations to process
    to_process_ids = new_ids | set(changed_ids)
    to_process = [c for c in all_convs if c.id in to_process_ids]

    print("=" * 80)
    print("DETAILED REPORT")
    print("=" * 80)
    print()

    # Filter conversations to see what would be kept
    print("Filtering conversations by value...")
    filtered = filter_conversations(to_process)
    kept = [f for f in filtered if f.should_keep]
    skipped = [f for f in filtered if not f.should_keep]

    print(f"  Would keep: {len(kept)} conversations")
    print(f"  Would skip: {len(skipped)} conversations (low value)")
    print()

    # Breakdown by source
    claude_to_process = [c for c in to_process if c.source == "claude"]
    gpt_to_process = [c for c in to_process if c.source == "gpt"]

    claude_kept = [f for f in kept if f.conversation.source == "claude"]
    gpt_kept = [f for f in kept if f.conversation.source == "gpt"]

    print("=" * 80)
    print("NEW CONVERSATIONS")
    print("=" * 80)
    print()

    print(f"Claude: {len([c for c in claude_to_process if c.id in new_ids])} new")
    print(f"GPT: {len([c for c in gpt_to_process if c.id in new_ids])} new")
    print()

    if new_ids:
        print("Sample new conversations:")
        new_convs = [c for c in all_convs if c.id in new_ids][:10]
        for i, conv in enumerate(new_convs, 1):
            source_icon = "🤖" if conv.source == "claude" else "💬"
            print(f"  {i}. {source_icon} {conv.title[:60]}...")
            print(f"     {conv.message_count} messages • {conv.updated_at[:10]}")
            print()

    print("=" * 80)
    print("UPDATED CONVERSATIONS")
    print("=" * 80)
    print()

    print(f"Claude: {len([c for c in claude_to_process if c.id in changed_ids])} updated")
    print(f"GPT: {len([c for c in gpt_to_process if c.id in changed_ids])} updated")
    print()

    if changed_ids:
        print("Sample updated conversations:")
        updated_convs = [c for c in all_convs if c.id in changed_ids][:10]
        for i, conv in enumerate(updated_convs, 1):
            source_icon = "🤖" if conv.source == "claude" else "💬"
            print(f"  {i}. {source_icon} {conv.title[:60]}...")
            print(f"     {conv.message_count} messages • {conv.updated_at[:10]}")
            print()

    print("=" * 80)
    print("DELETED CONVERSATIONS")
    print("=" * 80)
    print()

    if deleted_ids:
        print(f"Total deleted: {len(deleted_ids)}")
        print("\nSample deleted conversation IDs:")
        for conv_id in list(deleted_ids)[:10]:
            print(f"  - {conv_id}")
        print()
    else:
        print("No deleted conversations")
        print()

    print("=" * 80)
    print("PROCESSING ESTIMATE")
    print("=" * 80)
    print()

    # Estimate processing
    total_to_process = len(kept)
    total_messages = sum(c.message_count for c in [f.conversation for f in kept])

    # Estimate tokens (rough: 100 chars per token for text)
    avg_message_length = 500  # chars
    estimated_tokens = total_messages * avg_message_length // 100

    print(f"Conversations to process: {total_to_process}")
    print(f"Total messages: {total_messages}")
    print(f"Estimated input tokens: ~{estimated_tokens:,}")
    print()

    # Estimate cost (Claude Sonnet: $3/1M input, $15/1M output)
    input_cost = estimated_tokens * 3 / 1_000_000
    output_cost = estimated_tokens * 0.5 * 15 / 1_000_000  # Assume 50% output
    total_cost = input_cost + output_cost

    print(f"Estimated cost:")
    print(f"  Input: ${input_cost:.2f}")
    print(f"  Output: ${output_cost:.2f}")
    print(f"  Total: ${total_cost:.2f}")
    print()

    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print()

    print(f"Total conversations in files: {len(all_convs)}")
    print(f"Conversations to process: {total_to_process}")
    print(f"Conversations to skip: {len(unchanged_ids)}")
    print(f"Efficiency: {len(unchanged_ids) / len(all_convs) * 100:.1f}% can be skipped")
    print()

    if total_to_process > 0:
        print(f"Estimated processing time: ~{total_to_process * 2} seconds")
        print(f"Estimated knowledge entries: ~{total_to_process * 3}")
        print()

    print("=" * 80)
    print()

    # Save report
    report_file = Path(__file__).parent / "checkpoints" / "change_report.json"
    report_data = {
        "generated_at": datetime.now().isoformat(),
        "total_conversations": len(all_convs),
        "new_conversations": len(new_ids),
        "updated_conversations": len(changed_ids),
        "deleted_conversations": len(deleted_ids),
        "unchanged_conversations": len(unchanged_ids),
        "to_process": total_to_process,
        "to_skip": len(unchanged_ids),
        "estimated_tokens": estimated_tokens,
        "estimated_cost_usd": round(total_cost, 2),
        "new_conversation_ids": list(new_ids)[:50],  # Limit to 50
        "updated_conversation_ids": changed_ids[:50],
        "deleted_conversation_ids": list(deleted_ids)[:50],
    }

    report_file.parent.mkdir(exist_ok=True)
    with open(report_file, "w") as f:
        json.dump(report_data, f, indent=2)

    print(f"Report saved to: {report_file}")
    print()


if __name__ == "__main__":
    main()
