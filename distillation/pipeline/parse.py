"""
=============================================================================
STAGE 1: PARSE - Convert exports to normalized format
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Parse Claude and GPT conversation exports into a common normalized format.
Handles branch resolution for conversation trees.

INPUT FILES:
- Claude: /Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important 
  Papers/Arjun Digital Identity/Anthropic/conversations.json
- GPT: /Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important 
  Papers/Arjun Digital Identity/ChatGPT/conversations.json

OUTPUT FILES:
- NormalizedConversation objects (in memory, passed to next stage)

USAGE:
    from distillation.pipeline.parse import parse_all_exports
    conversations = parse_all_exports()
    
    # Or test mode:
    python -m distillation.pipeline.parse --test
=============================================================================
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional
import argparse

from config import CLAUDE_EXPORT_PATH, GPT_EXPORT_PATH
from models import (
    NormalizedConversation,
    NormalizedMessage,
    ParseMetadata,
    CodeBlock,
)


# -----------------------------------------------------------------------------
# HELPER FUNCTIONS
# -----------------------------------------------------------------------------

def extract_code_blocks(content: str) -> list[CodeBlock]:
    """
    Extract code blocks from message content.
    Looks for ``` delimited blocks with optional language hints.
    """
    blocks = []
    
    # Pattern: ```language\ncode\n```
    pattern = r"```(\w*)\n(.*?)```"
    matches = re.findall(pattern, content, re.DOTALL)
    
    for language, code in matches:
        blocks.append(CodeBlock(
            language=language if language else None,
            content=code.strip(),
        ))
    
    return blocks


def detect_content_type(content: str) -> str:
    """
    Detect if content is text, code, or mixed.
    """
    has_code = "```" in content
    has_significant_text = len(re.sub(r"```.*?```", "", content, flags=re.DOTALL).strip()) > 100
    
    if has_code and has_significant_text:
        return "mixed"
    elif has_code:
        return "code"
    else:
        return "text"


# -----------------------------------------------------------------------------
# CLAUDE PARSER
# -----------------------------------------------------------------------------

def parse_claude_export(export_path: Path) -> list[NormalizedConversation]:
    """
    Parse Claude export file into normalized conversations.
    
    Claude exports have a tree structure where messages have parent_message_uuid.
    We resolve branches by selecting the path to the most recent leaf.
    
    Args:
        export_path: Path to conversations.json or directory containing it
    
    Returns:
        List of NormalizedConversation objects
    """
    # Find the conversations.json file
    if export_path.is_dir():
        json_file = export_path / "conversations.json"
        if not json_file.exists():
            # Look for latest export
            exports = list(export_path.glob("*.json"))
            if exports:
                json_file = max(exports, key=lambda p: p.stat().st_mtime)
            else:
                return []
    else:
        json_file = export_path
    
    if not json_file.exists():
        return []
    
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # Claude export structure: list of conversations
    conversations_data = data if isinstance(data, list) else data.get("conversations", data)
    if not isinstance(conversations_data, list):
        conversations_data = [conversations_data]
    
    normalized = []
    
    for conv in conversations_data:
        try:
            parsed = _parse_claude_conversation(conv)
            if parsed and parsed.messages:
                normalized.append(parsed)
        except Exception as e:
            print(f"Error parsing Claude conversation {conv.get('uuid', 'unknown')}: {e}")
            continue
    
    return normalized


def _parse_claude_conversation(conv: dict) -> Optional[NormalizedConversation]:
    """Parse a single Claude conversation."""
    messages_data = conv.get("chat_messages", [])
    if not messages_data:
        return None
    
    # Build message graph
    message_map = {}
    children_map = {}
    roots = []
    
    for msg in messages_data:
        msg_id = msg.get("uuid", "")
        if not msg_id:
            continue
        
        message_map[msg_id] = msg
        parent_id = msg.get("parent_message_uuid")
        
        if parent_id:
            if parent_id not in children_map:
                children_map[parent_id] = []
            children_map[parent_id].append(msg_id)
        else:
            roots.append(msg_id)
    
    # Count branches
    branches_found = sum(1 for kids in children_map.values() if len(kids) > 1)
    
    # Select primary path (latest leaf)
    if not roots:
        # Try to find a root by finding messages with no valid parent
        all_ids = set(message_map.keys())
        for msg_id, msg in message_map.items():
            parent_id = msg.get("parent_message_uuid")
            if not parent_id or parent_id not in all_ids:
                roots.append(msg_id)
    
    if not roots:
        return None
    
    # Traverse from first root, selecting primary path
    selected_path = _select_primary_path_claude(roots[0], message_map, children_map)
    
    # Convert to normalized messages
    messages = []
    for msg_id in selected_path:
        msg = message_map.get(msg_id)
        if not msg:
            continue
        
        sender = msg.get("sender", "")
        if sender not in ("human", "assistant"):
            continue
        
        role = "user" if sender == "human" else "assistant"
        content = msg.get("text", "")
        
        # Parse timestamp
        created_at = msg.get("created_at", "")
        if not created_at:
            created_at = datetime.utcnow().isoformat()
        
        messages.append(NormalizedMessage(
            message_id=msg_id,
            role=role,
            created_at=created_at,
            content=content,
            content_type=detect_content_type(content),
            code_blocks=extract_code_blocks(content),
        ))
    
    if not messages:
        return None
    
    # Get conversation metadata
    conv_id = conv.get("uuid", conv.get("id", ""))
    title = conv.get("name", conv.get("title", "Untitled"))
    created_at = conv.get("created_at", messages[0].created_at if messages else "")
    updated_at = conv.get("updated_at", messages[-1].created_at if messages else "")
    
    return NormalizedConversation(
        id=conv_id,
        source="claude",
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
        parse_metadata=ParseMetadata(
            total_nodes=len(message_map),
            branches_found=branches_found,
            selected_path=selected_path,
            alternate_branches_kept=0,
            parser_version="1.0.0",
        ),
    )


def _select_primary_path_claude(
    root_id: str,
    message_map: dict,
    children_map: dict,
) -> list[str]:
    """
    Traverse from root selecting the primary path (latest leaf).
    """
    path = [root_id]
    current = root_id
    
    while current in children_map and children_map[current]:
        child_ids = children_map[current]
        
        if len(child_ids) == 1:
            # No branch
            path.append(child_ids[0])
            current = child_ids[0]
        else:
            # Multiple branches - select the one with latest leaf
            best_child = None
            best_time = None
            
            for child_id in child_ids:
                leaf_time = _get_latest_leaf_time(child_id, message_map, children_map)
                if best_time is None or (leaf_time and leaf_time > best_time):
                    best_time = leaf_time
                    best_child = child_id
            
            if best_child:
                path.append(best_child)
                current = best_child
            else:
                break
    
    return path


def _get_latest_leaf_time(
    node_id: str,
    message_map: dict,
    children_map: dict,
) -> Optional[str]:
    """Get the timestamp of the latest leaf in a subtree."""
    if node_id not in children_map or not children_map[node_id]:
        # This is a leaf
        msg = message_map.get(node_id, {})
        return msg.get("created_at", "")
    
    # Find latest among children
    latest = None
    for child_id in children_map[node_id]:
        child_time = _get_latest_leaf_time(child_id, message_map, children_map)
        if child_time and (latest is None or child_time > latest):
            latest = child_time
    
    return latest


# -----------------------------------------------------------------------------
# GPT PARSER
# -----------------------------------------------------------------------------

def parse_gpt_export(export_path: Path) -> list[NormalizedConversation]:
    """
    Parse GPT export file into normalized conversations.
    
    GPT exports use a mapping object with parent/children references (DAG).
    
    Args:
        export_path: Path to conversations.json or directory containing it
    
    Returns:
        List of NormalizedConversation objects
    """
    # Find the conversations.json file
    if export_path.is_dir():
        json_file = export_path / "conversations.json"
        if not json_file.exists():
            # Look for latest export
            exports = list(export_path.glob("*.json"))
            if exports:
                json_file = max(exports, key=lambda p: p.stat().st_mtime)
            else:
                return []
    else:
        json_file = export_path
    
    if not json_file.exists():
        return []
    
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    # GPT export is a list of conversations
    if not isinstance(data, list):
        data = [data]
    
    normalized = []
    
    for conv in data:
        try:
            parsed = _parse_gpt_conversation(conv)
            if parsed and parsed.messages:
                normalized.append(parsed)
        except Exception as e:
            print(f"Error parsing GPT conversation {conv.get('title', 'unknown')}: {e}")
            continue
    
    return normalized


def _parse_gpt_conversation(conv: dict) -> Optional[NormalizedConversation]:
    """Parse a single GPT conversation."""
    mapping = conv.get("mapping", {})
    if not mapping:
        return None
    
    # Find root node (has null parent)
    root_id = None
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            root_id = node_id
            break
    
    if not root_id:
        return None
    
    # Count branches
    branches_found = sum(
        1 for node in mapping.values()
        if len(node.get("children", [])) > 1
    )
    
    # Traverse to build path
    selected_path = _select_primary_path_gpt(root_id, mapping)
    
    # Convert to normalized messages (skip system messages)
    messages = []
    for node_id in selected_path:
        node = mapping.get(node_id, {})
        msg = node.get("message")
        
        if not msg:
            continue
        
        author = msg.get("author", {}).get("role", "")
        if author not in ("user", "assistant"):
            continue
        
        # Get content
        content_obj = msg.get("content", {})
        parts = content_obj.get("parts", [])
        content = "\n".join(str(p) for p in parts if isinstance(p, str))
        
        if not content:
            continue
        
        # Parse timestamp
        create_time = msg.get("create_time")
        if create_time:
            created_at = datetime.fromtimestamp(create_time).isoformat()
        else:
            created_at = datetime.utcnow().isoformat()
        
        messages.append(NormalizedMessage(
            message_id=node_id,
            role=author,
            created_at=created_at,
            content=content,
            content_type=detect_content_type(content),
            code_blocks=extract_code_blocks(content),
        ))
    
    if not messages:
        return None
    
    # Get conversation metadata
    title = conv.get("title", "Untitled")
    create_time = conv.get("create_time", 0)
    update_time = conv.get("update_time", 0)
    
    conv_id = f"gpt_{int(create_time)}" if create_time else f"gpt_{hash(title)}"
    created_at = datetime.fromtimestamp(create_time).isoformat() if create_time else messages[0].created_at
    updated_at = datetime.fromtimestamp(update_time).isoformat() if update_time else messages[-1].created_at
    
    return NormalizedConversation(
        id=conv_id,
        source="gpt",
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
        parse_metadata=ParseMetadata(
            total_nodes=len(mapping),
            branches_found=branches_found,
            selected_path=selected_path,
            alternate_branches_kept=0,
            parser_version="1.0.0",
        ),
    )


def _select_primary_path_gpt(root_id: str, mapping: dict) -> list[str]:
    """Traverse GPT mapping selecting primary path."""
    path = [root_id]
    current = root_id
    
    while True:
        node = mapping.get(current, {})
        children = node.get("children", [])
        
        if not children:
            break
        
        if len(children) == 1:
            path.append(children[0])
            current = children[0]
        else:
            # Multiple children - select one with latest leaf
            best_child = None
            best_time = None
            
            for child_id in children:
                leaf_time = _get_latest_leaf_time_gpt(child_id, mapping)
                if best_time is None or (leaf_time and leaf_time > best_time):
                    best_time = leaf_time
                    best_child = child_id
            
            if best_child:
                path.append(best_child)
                current = best_child
            else:
                break
    
    return path


def _get_latest_leaf_time_gpt(node_id: str, mapping: dict) -> Optional[float]:
    """Get timestamp of latest leaf in GPT subtree."""
    node = mapping.get(node_id, {})
    children = node.get("children", [])
    
    if not children:
        # This is a leaf
        msg = node.get("message", {})
        return msg.get("create_time")
    
    # Find latest among children
    latest = None
    for child_id in children:
        child_time = _get_latest_leaf_time_gpt(child_id, mapping)
        if child_time and (latest is None or child_time > latest):
            latest = child_time
    
    return latest


# -----------------------------------------------------------------------------
# MAIN PARSE FUNCTION
# -----------------------------------------------------------------------------

def parse_all_exports() -> list[NormalizedConversation]:
    """
    Parse all available exports from Claude and GPT.
    
    Returns:
        Combined list of normalized conversations from all sources
    """
    all_conversations = []
    
    # Parse Claude exports
    if CLAUDE_EXPORT_PATH.exists():
        claude_convs = parse_claude_export(CLAUDE_EXPORT_PATH)
        all_conversations.extend(claude_convs)
        print(f"Parsed {len(claude_convs)} Claude conversations")
    else:
        print(f"Claude export path not found: {CLAUDE_EXPORT_PATH}")
    
    # Parse GPT exports
    if GPT_EXPORT_PATH.exists():
        gpt_convs = parse_gpt_export(GPT_EXPORT_PATH)
        all_conversations.extend(gpt_convs)
        print(f"Parsed {len(gpt_convs)} GPT conversations")
    else:
        print(f"GPT export path not found: {GPT_EXPORT_PATH}")
    
    return all_conversations


# -----------------------------------------------------------------------------
# CLI FOR TESTING
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Parse conversation exports")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    parser.add_argument("--source", choices=["claude", "gpt", "all"], default="all",
                        help="Which source to parse")
    args = parser.parse_args()
    
    print("=" * 60)
    print("STAGE 1: PARSE - Testing conversation parsing")
    print("=" * 60)
    print()
    
    conversations = []
    
    if args.source in ("claude", "all"):
        print(f"Claude export path: {CLAUDE_EXPORT_PATH}")
        if CLAUDE_EXPORT_PATH.exists():
            claude_convs = parse_claude_export(CLAUDE_EXPORT_PATH)
            conversations.extend(claude_convs)
            print(f"  ✓ Parsed {len(claude_convs)} Claude conversations")
            
            if claude_convs:
                sample = claude_convs[0]
                print(f"  Sample: {sample.title[:50]}... ({sample.message_count} messages)")
        else:
            print(f"  ✗ Path not found")
    
    print()
    
    if args.source in ("gpt", "all"):
        print(f"GPT export path: {GPT_EXPORT_PATH}")
        if GPT_EXPORT_PATH.exists():
            gpt_convs = parse_gpt_export(GPT_EXPORT_PATH)
            conversations.extend(gpt_convs)
            print(f"  ✓ Parsed {len(gpt_convs)} GPT conversations")
            
            if gpt_convs:
                sample = gpt_convs[0]
                print(f"  Sample: {sample.title[:50]}... ({sample.message_count} messages)")
        else:
            print(f"  ✗ Path not found")
    
    print()
    print("=" * 60)
    print(f"Total: {len(conversations)} conversations")
    
    if conversations:
        total_messages = sum(c.message_count for c in conversations)
        with_code = sum(1 for c in conversations if c.has_code)
        total_branches = sum(c.parse_metadata.branches_found for c in conversations if c.parse_metadata)
        
        print(f"Total messages: {total_messages}")
        print(f"Conversations with code: {with_code}")
        print(f"Total branches resolved: {total_branches}")
        print("✓ Parse test passed!")
    else:
        print("✗ No conversations parsed")
    
    print("=" * 60)


if __name__ == "__main__":
    main()

