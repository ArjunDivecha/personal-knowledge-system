"""
=============================================================================
MODULE: ingestion/source_first/chat_export_scanner.py
=============================================================================

Integrates claude.ai chat exports into source-first memory as their own source
kind. Until 2026-09-04 claude.ai conversations were absent from production
entirely: the legacy distillation pipeline that read them wrote to the retired
ke_* store. This scanner reads the raw export directly and emits immutable
evidence records, exactly like the file and session scanners.

INPUT FILES (configured in shared/source_first_config.json -> "chat_exports"):
- <root>/<YYYY-MM-DD>/conversations-*.zip   (each zip holds one conversations.json part)
- <root>/<YYYY-MM-DD>/memories-*.zip        (memories/<account>.json)
- <root>/conversations.json, <root>/memories.json   (older loose-file exports)
  Default root: /Users/arjundivecha/Dropbox/Identity and Important Papers/Arjun Digital Identity/Anthropic
  The newest export directory (by name, then mtime) wins; parts are merged and
  de-duplicated by conversation uuid (latest updated_at kept).

OUTPUT: list[EvidenceRecord] + a diagnostics dict recorded in the manifest as
"chat_exports". No files are written.

Evidence contract:
- source_kind "claude_ai_chat", evidence_role "authoritative" (no attention lift,
  ordinary 0.65 floor), authority from config (0.6), project None, no retention
  cutoff — the whole archive since 2023 is searchable.
- source_path "chat://claude_ai/<conversation uuid>"; all chunks of one
  conversation share a source_id so get_deep returns the full conversation.
- memories export -> pinned curated_memory records at authority 1.0,
  source_path "chat-export://claude_ai/memories/<section>".
- Credentials are redacted (session_scanner.redact_session_text) before
  checksums, embeddings, or any remote write.

VERSION: 1.0  |  2026-09-04  |  Claude Code (Fable 5.1) for Arjun Divecha
=============================================================================
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import EvidenceRecord
from .scanner import chunk_text, normalize_text, source_id_for_path
from .session_scanner import SessionTurn, _without_retrieval_meta_turns, redact_session_text

UTC = timezone.utc
DEFAULT_ROOT = "/Users/arjundivecha/Dropbox/Identity and Important Papers/Arjun Digital Identity/Anthropic"


@dataclass(frozen=True)
class ChatTurn:
    role: str
    text: str
    timestamp: str


@dataclass(frozen=True)
class ChatConversation:
    uuid: str
    name: str
    created_at: str
    updated_at: str
    turns: list[ChatTurn]


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Locating and loading the export
# ---------------------------------------------------------------------------

def find_export_dir(root: Path) -> Path | None:
    """Newest dated export directory under root, else root itself if it holds
    a loose conversations.json, else None."""
    if not root.is_dir():
        return None
    candidates = [
        path for path in root.iterdir()
        if path.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", path.name)
        and (list(path.glob("conversations*.zip")) or (path / "conversations.json").exists())
    ]
    if candidates:
        return sorted(candidates, key=lambda path: (path.name, path.stat().st_mtime))[-1]
    if (root / "conversations.json").exists() or list(root.glob("conversations*.zip")):
        return root
    return None


def _json_members(path: Path, name_filter) -> Iterable[Any]:
    """Yield parsed JSON from a .json file or from matching members of a zip."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.endswith(".json") and name_filter(member):
                    yield json.loads(archive.read(member))
    elif path.suffix.lower() == ".json" and name_filter(path.name):
        yield json.loads(path.read_text(encoding="utf-8", errors="replace"))


def load_raw_conversations(export_dir: Path) -> list[dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = {}
    sources = sorted(export_dir.glob("conversations*.zip")) + sorted(export_dir.glob("conversations*.json"))
    for source in sources:
        for payload in _json_members(source, lambda name: Path(name).name.startswith("conversations")):
            items = payload if isinstance(payload, list) else (payload.get("conversations") if isinstance(payload, dict) else None)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("uuid"), str):
                    continue
                previous = raw.get(item["uuid"])
                if previous is None or str(item.get("updated_at") or "") >= str(previous.get("updated_at") or ""):
                    raw[item["uuid"]] = item
    return list(raw.values())


def load_raw_memories(export_dir: Path) -> dict[str, Any] | None:
    sources = sorted(export_dir.glob("memories*.zip")) + sorted(export_dir.glob("memories*.json"))
    for source in sources:
        for payload in _json_members(source, lambda name: "memories" in name):
            if isinstance(payload, dict):
                return payload
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload[0]
    return None


# ---------------------------------------------------------------------------
# Conversation parsing
# ---------------------------------------------------------------------------

def _message_text(message: dict[str, Any]) -> str:
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"].strip())
    return "\n".join(part for part in parts if part)


def _primary_path(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Follow parent_message_uuid links from the root, preferring the most
    recent child at each branch. Falls back to chronological order when the
    export carries no usable parent links."""
    by_id = {m["uuid"]: m for m in messages if isinstance(m.get("uuid"), str)}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    linked = False
    for message in by_id.values():
        parent = message.get("parent_message_uuid")
        if isinstance(parent, str) and parent in by_id:
            children.setdefault(parent, []).append(message)
            linked = True
        else:
            roots.append(message)
    if not linked or not roots:
        return sorted(messages, key=lambda m: str(m.get("created_at") or ""))
    path: list[dict[str, Any]] = []
    current = sorted(roots, key=lambda m: str(m.get("created_at") or ""))[0]
    seen: set[str] = set()
    while current and current["uuid"] not in seen:
        seen.add(current["uuid"])
        path.append(current)
        kids = children.get(current["uuid"]) or []
        current = sorted(kids, key=lambda m: str(m.get("created_at") or ""))[-1] if kids else None
    return path


def parse_conversation(raw: dict[str, Any], max_turn_chars: int) -> ChatConversation | None:
    messages = [m for m in (raw.get("chat_messages") or []) if isinstance(m, dict)]
    turns: list[ChatTurn] = []
    for message in _primary_path(messages):
        sender = message.get("sender")
        if sender not in ("human", "assistant"):
            continue
        text = _message_text(message)
        timestamp = _iso(message.get("created_at")) or _iso(raw.get("created_at")) or ""
        if text:
            turns.append(ChatTurn("user" if sender == "human" else "assistant", text[:max_turn_chars], timestamp))
    if not turns:
        return None
    created = _iso(raw.get("created_at")) or turns[0].timestamp
    updated = _iso(raw.get("updated_at")) or turns[-1].timestamp or created
    return ChatConversation(str(raw["uuid"]), str(raw.get("name") or "").strip(), created, updated, turns)


def conversation_text(conversation: ChatConversation, max_conversation_chars: int) -> str:
    """Turns in order from the start, bounded. The opening question and its
    answers carry the durable content of a chat, so truncation drops the tail."""
    parts: list[str] = []
    used = 0
    for turn in conversation.turns:
        value = f"[{turn.timestamp}] {turn.role}: {turn.text}".strip()
        if used + len(value) > max_conversation_chars:
            remaining = max_conversation_chars - used
            if remaining > 200:
                parts.append(value[:remaining])
            break
        parts.append(value)
        used += len(value) + 2
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def _memory_sections(memories: dict[str, Any]) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    value = memories.get("conversations_memory")
    if isinstance(value, str) and value.strip():
        sections.append(("conversations_memory", value.strip()))
    project_memories = memories.get("project_memories")
    if isinstance(project_memories, dict):
        for key, text in project_memories.items():
            body = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=1)
            if body.strip():
                sections.append((f"project_memories/{key}", body.strip()))
    elif isinstance(project_memories, list):
        for index, item in enumerate(project_memories):
            body = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, indent=1)
            if body.strip():
                sections.append((f"project_memories/{index}", body.strip()))
    memory_files = memories.get("memory_files")
    if isinstance(memory_files, dict):
        for key, text in memory_files.items():
            body = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=1)
            if body.strip():
                sections.append((f"memory_files/{key}", body.strip()))
    elif isinstance(memory_files, list):
        for index, item in enumerate(memory_files):
            if isinstance(item, dict):
                # The 2026-09 export shape is {"path": "/areas/13f-filings.md", "content": ..., "updated_at": ...}
                name = str(item.get("path") or item.get("name") or item.get("filename") or index).strip("/")
                body = item.get("content") or item.get("text") or json.dumps(item, ensure_ascii=False, indent=1)
            else:
                name, body = str(index), str(item)
            if str(body).strip():
                sections.append((f"memory_files/{name}", str(body).strip()))
    return sections


def scan_chat_exports(
    config: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    settings = config.get("chat_exports") or {}
    now = now or datetime.now(UTC)
    enabled = bool(settings.get("enabled", False))
    diagnostics: dict[str, Any] = {
        "enabled": enabled,
        "export_dir": None,
        "conversation_count": 0,
        "conversation_chunk_count": 0,
        "skipped_empty_conversations": 0,
        "memory_section_count": 0,
        "memory_chunk_count": 0,
        "redacted_match_count": 0,
        "oldest_conversation_at": None,
        "newest_conversation_at": None,
        "last_successful_scan_at": now.replace(microsecond=0).isoformat(),
    }
    if not enabled:
        return [], diagnostics

    root = Path(str(settings.get("root") or DEFAULT_ROOT)).expanduser()
    require_root = bool(settings.get("require_root", True))
    export_dir = find_export_dir(root)
    if export_dir is None:
        if require_root:
            raise RuntimeError(f"chat_export_root_unavailable:{root}")
        return [], diagnostics
    diagnostics["export_dir"] = str(export_dir)

    source_kind = str(settings.get("source_kind") or "claude_ai_chat")
    authority = float(settings.get("authority", 0.6))
    max_turn_chars = int(settings.get("max_turn_chars", 4000))
    max_conversation_chars = int(settings.get("max_conversation_chars", 60000))
    chunk_chars = int(config.get("chunk_chars", 3200))
    overlap_chars = int(config.get("chunk_overlap_chars", 240))
    records: list[EvidenceRecord] = []
    # Same boundary as the session scanner: retrieval-validation prompts and their
    # PASS/FAIL reports are operational traffic, not evidence about the subjects
    # used as probes. Without it a claude.ai chat that ran the PKS validation
    # checks re-admitted the "sourdough fermentation recipe" negative control
    # (candidate sf_20260905T0331Z failed neg_sourdough_recipe at 0.373).
    excluded_probe_queries = [
        str(query)
        for query in ((config.get("recent_sessions") or {}).get("excluded_retrieval_probe_queries") or [])
        if isinstance(query, str) and query.strip()
    ]
    diagnostics["excluded_retrieval_meta_turn_count"] = 0

    updated_values: list[str] = []
    for raw in load_raw_conversations(export_dir):
        conversation = parse_conversation(raw, max_turn_chars)
        if conversation is None:
            diagnostics["skipped_empty_conversations"] += 1
            continue
        kept_turns, excluded_turns = _without_retrieval_meta_turns(
            [SessionTurn(turn.role, turn.text, turn.timestamp) for turn in conversation.turns],
            excluded_probe_queries,
        )
        diagnostics["excluded_retrieval_meta_turn_count"] += excluded_turns
        if not kept_turns:
            diagnostics["skipped_empty_conversations"] += 1
            continue
        conversation = ChatConversation(
            conversation.uuid, conversation.name, conversation.created_at, conversation.updated_at,
            [ChatTurn(turn.role, turn.text, turn.timestamp) for turn in kept_turns],
        )
        redacted, redaction_count = redact_session_text(conversation_text(conversation, max_conversation_chars))
        diagnostics["redacted_match_count"] += redaction_count
        chunks = chunk_text(redacted, chunk_chars, overlap_chars)
        if not chunks:
            diagnostics["skipped_empty_conversations"] += 1
            continue
        diagnostics["conversation_count"] += 1
        updated_values.append(conversation.updated_at)
        logical_path = f"chat://claude_ai/{conversation.uuid}"
        day = conversation.updated_at[:10]
        title = f"Claude.ai chat — {conversation.name or 'untitled'} — {day}"[:240]
        source_id = source_id_for_path(logical_path)
        for index, chunk in enumerate(chunks):
            checksum = hashlib.sha256(chunk.encode()).hexdigest()
            identity = f"claude_ai:{conversation.uuid}:{index}:{checksum}"
            records.append(EvidenceRecord(
                id=f"ev_chat_{hashlib.sha256(identity.encode()).hexdigest()[:20]}",
                source_id=source_id,
                title=title,
                text=chunk,
                source_path=logical_path,
                source_kind=source_kind,
                project=None,
                source_modified_at=conversation.updated_at,
                content_checksum=checksum,
                chunk_index=index,
                chunk_count=len(chunks),
                authority=authority,
            ))
    diagnostics["conversation_chunk_count"] = len(records)
    if updated_values:
        diagnostics["oldest_conversation_at"] = min(updated_values)
        diagnostics["newest_conversation_at"] = max(updated_values)

    if bool(settings.get("pin_memories", True)):
        memories = load_raw_memories(export_dir)
        if memories:
            sections = _memory_sections(memories)
            diagnostics["memory_section_count"] = len(sections)
            memory_modified = _iso(memories.get("updated_at")) or diagnostics["newest_conversation_at"] or now.replace(microsecond=0).isoformat()
            for key, body in sections:
                redacted, redaction_count = redact_session_text(normalize_text(body))
                diagnostics["redacted_match_count"] += redaction_count
                chunks = chunk_text(redacted, chunk_chars, overlap_chars)
                logical_path = f"chat-export://claude_ai/memories/{key}"
                source_id = source_id_for_path(logical_path)
                for index, chunk in enumerate(chunks):
                    checksum = hashlib.sha256(chunk.encode()).hexdigest()
                    identity = f"claude_ai_memory:{key}:{index}:{checksum}"
                    records.append(EvidenceRecord(
                        id=f"ev_chatmem_{hashlib.sha256(identity.encode()).hexdigest()[:20]}",
                        source_id=source_id,
                        title=f"Claude.ai memory — {key}"[:240],
                        text=chunk,
                        source_path=logical_path,
                        source_kind="curated_memory",
                        project=None,
                        source_modified_at=memory_modified,
                        content_checksum=checksum,
                        chunk_index=index,
                        chunk_count=len(chunks),
                        authority=1.0,
                        pinned=True,
                    ))
                    diagnostics["memory_chunk_count"] += 1
    return records, diagnostics
