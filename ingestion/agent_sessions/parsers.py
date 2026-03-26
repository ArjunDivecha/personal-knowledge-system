"""
=============================================================================
SCRIPT NAME: parsers.py
=============================================================================

INPUT FILES:
- ~/.claude/projects/**/*.jsonl: Claude Code session files (JSONL format)
- ~/.codex/sessions/**/*.jsonl: Codex CLI rollout files (JSONL format)

OUTPUT FILES:
- None (returns parsed turn data in memory)

VERSION: 1.0
LAST UPDATED: 2026-03-25
AUTHOR: Arjun Divecha

DESCRIPTION:
Parsers for Claude Code and Codex CLI session JSONL files.
Each parser reads from a byte offset (for incremental processing)
and returns a list of conversation turns plus the new offset.

Turns are capped at 800 chars each to keep distillation context manageable.

DEPENDENCIES:
- json (stdlib)
- pathlib (stdlib)

USAGE:
    from agent_sessions.parsers import parse_claude_code, parse_codex
    turns, new_offset = parse_claude_code(path, from_offset=0)
=============================================================================
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Max chars per turn content to send to distillation
TURN_CAP = 800


def parse_claude_code(path: Path, from_offset: int = 0) -> tuple[list[dict], int, dict]:
    """
    Parse new user/assistant turns from a Claude Code session JSONL.

    Claude Code JSONL events have these types:
      - "user": user messages (message.content can be str or list of blocks)
      - "assistant": assistant responses (same content format)
      - "queue-operation": enqueue/dequeue of prompts
      - "progress": tool execution progress

    We extract user and assistant turns only.

    Args:
        path: Path to the .jsonl session file
        from_offset: Byte offset to resume reading from

    Returns:
        (turns, new_offset, session_meta)
        - turns: list of {role, content, timestamp, session_id, source, project, cwd}
        - new_offset: byte position after last read line
        - session_meta: {cwd, project} extracted from events
    """
    turns = []
    new_offset = from_offset
    session_meta = {"cwd": None, "project": path.parent.name}

    try:
        with open(path, "rb") as f:
            f.seek(from_offset)
            for raw in f:
                new_offset = f.tell()
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    ev = json.loads(line)

                    # Extract cwd from any event that has it
                    if ev.get("cwd") and not session_meta["cwd"]:
                        session_meta["cwd"] = ev["cwd"]

                    ev_type = ev.get("type", "")
                    if ev_type not in ("user", "assistant"):
                        continue

                    msg = ev.get("message", {})
                    content = msg.get("content", "")

                    # Content can be a list of blocks (text, tool_use, etc.)
                    if isinstance(content, list):
                        content = "\n".join(
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )

                    if not content or not content.strip():
                        continue

                    turns.append({
                        "role": ev_type,
                        "content": content.strip()[:TURN_CAP],
                        "timestamp": ev.get("timestamp", ""),
                        "session_id": ev.get("sessionId", path.stem),
                        "source": "claude_code",
                        "project": session_meta["project"],
                        "cwd": ev.get("cwd", ""),
                    })

                except (json.JSONDecodeError, KeyError):
                    continue

    except Exception as e:
        log.warning(f"Parse error {path.name}: {e}")

    return turns, new_offset, session_meta


def parse_codex(path: Path, from_offset: int = 0) -> tuple[list[dict], int, dict]:
    """
    Parse new user/assistant turns from a Codex CLI rollout JSONL.

    Codex JSONL events have a wrapper: {timestamp, type, payload}.
    Relevant types:
      - "session_meta": session metadata (cwd, model, etc.)
      - "response_item": contains role + content blocks
      - "turn_context": turn metadata (cwd, model, etc.)

    Args:
        path: Path to the .jsonl rollout file
        from_offset: Byte offset to resume reading from

    Returns:
        (turns, new_offset, session_meta)
        - turns: list of {role, content, timestamp, session_id, source, project, cwd}
        - new_offset: byte position after last read line
        - session_meta: {cwd, project} extracted from events
    """
    turns = []
    new_offset = from_offset
    session_meta = {"cwd": None, "project": path.parent.name}

    try:
        with open(path, "rb") as f:
            f.seek(from_offset)
            for raw in f:
                new_offset = f.tell()
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    ev = json.loads(line)
                    payload = ev.get("payload", ev)  # Some events have payload wrapper
                    ev_type = ev.get("type", payload.get("type", ""))

                    # Extract cwd from session_meta or turn_context
                    if ev_type == "session_meta":
                        session_meta["cwd"] = payload.get("cwd", "")
                        session_meta["project"] = (
                            Path(payload.get("cwd", "")).name
                            if payload.get("cwd")
                            else path.parent.name
                        )
                        continue

                    if ev_type == "turn_context":
                        if payload.get("cwd") and not session_meta["cwd"]:
                            session_meta["cwd"] = payload["cwd"]
                        continue

                    if ev_type != "response_item":
                        continue

                    role = payload.get("role", "")
                    # Codex uses "developer" for user messages
                    if role == "developer":
                        role = "user"
                    elif role != "assistant":
                        continue

                    content = payload.get("content", "")
                    if isinstance(content, list):
                        parts = []
                        for p in content:
                            if isinstance(p, dict):
                                parts.append(
                                    p.get("text", "")
                                    or p.get("output_text", "")
                                    or p.get("input_text", "")
                                )
                        content = "\n".join(parts)

                    if not content or not content.strip():
                        continue

                    turns.append({
                        "role": role,
                        "content": content.strip()[:TURN_CAP],
                        "timestamp": ev.get("timestamp", ""),
                        "session_id": payload.get("id", path.stem),
                        "source": "codex_cli",
                        "project": session_meta["project"],
                        "cwd": session_meta.get("cwd", ""),
                    })

                except (json.JSONDecodeError, KeyError):
                    continue

    except Exception as e:
        log.warning(f"Parse error {path.name}: {e}")

    return turns, new_offset, session_meta
