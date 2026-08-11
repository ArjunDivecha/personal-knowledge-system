from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import EvidenceRecord, ProjectRecord
from .scanner import chunk_text, normalize_text, source_id_for_path


UTC = timezone.utc
REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class SessionTurn:
    role: str
    text: str
    timestamp: str


@dataclass(frozen=True)
class ParsedSession:
    surface: str
    session_id: str
    cwd: str
    turns: list[SessionTurn]
    started_at: str
    ended_at: str


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:sk|pk|rk|ghp|github_pat|xox[baprs]|AKIA)[-_A-Za-z0-9]{12,}\b"),
    re.compile(r"(?i)\b(?:https?|redis)://[^\s/:]+:[^\s/@]+@"),
    re.compile(r"(?i)\bop://[^\s'\"`]+"),
    re.compile(
        r"(?im)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|CREDENTIAL|AUTH)[A-Z0-9_]*)"
        r"(\s*(?:=|:){1,2}\s*)([^\s,;]+|\"[^\"]*\"|'[^']*')"
    ),
)

# Retrieval validation prompts and their PASS/FAIL reports are operational
# traffic, not evidence about the subjects used as probes.  Without this
# boundary, a negative-control phrase can be copied into a recent session and
# then retrieve the transcript that issued the test.  Keep this deterministic
# and deliberately narrow: ordinary PKS design discussion is retained, while
# turns containing several characteristic validation-report markers are not.
_RETRIEVAL_META_MARKERS = (
    "personal knowledge validation",
    "run these checks",
    "checks pass",
    "fails outright",
    "pass/fail",
    "raw supporting fields",
    "self-referential match",
    "test script",
    "live acceptance",
    "negative control",
    "negative-control",
    "abstention",
    "abstain_reason",
    "minimum_final_score",
    "working_context_bonus",
    "content_checksum",
    "complete_source",
    "get_deep",
)


def _is_retrieval_meta_turn(turn: SessionTurn) -> bool:
    normalized = turn.text.casefold()
    marker_count = sum(marker in normalized for marker in _RETRIEVAL_META_MARKERS)
    return marker_count >= 3


def _without_retrieval_meta_turns(
    turns: list[SessionTurn],
    excluded_probe_queries: list[str],
) -> tuple[list[SessionTurn], int]:
    normalized_probes = [
        re.sub(r"\s+", " ", query.casefold()).strip()
        for query in excluded_probe_queries
        if query.strip()
    ]
    retained: list[SessionTurn] = []
    for turn in turns:
        normalized_turn = re.sub(r"\s+", " ", turn.text.casefold())
        if _is_retrieval_meta_turn(turn):
            continue
        if any(probe in normalized_turn for probe in normalized_probes):
            continue
        retained.append(turn)
    return retained, len(turns) - len(retained)


def redact_session_text(text: str) -> tuple[str, int]:
    """Deterministically remove common credentials before any persistence."""
    cleaned = text
    matches = 0
    for index, pattern in enumerate(_SECRET_PATTERNS):
        if index == len(_SECRET_PATTERNS) - 1:
            def replace_assignment(match: re.Match[str]) -> str:
                nonlocal matches
                matches += 1
                return f"{match.group(1)}{match.group(2)}{REDACTED}"

            cleaned = pattern.sub(replace_assignment, cleaned)
        else:
            cleaned, count = pattern.subn(REDACTED, cleaned)
            matches += count
    return normalize_text(cleaned), matches


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


def _text_blocks(content: Any, allowed_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            continue
        text = block.get("text") or block.get("input_text") or block.get("output_text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def parse_claude_session(path: Path, max_turn_chars: int) -> tuple[ParsedSession | None, int]:
    turns: list[SessionTurn] = []
    cwd = ""
    session_id = path.stem
    invalid_lines = 0
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                invalid_lines += 1
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("cwd"), str) and event["cwd"]:
                cwd = event["cwd"]
            if isinstance(event.get("sessionId"), str) and event["sessionId"]:
                session_id = event["sessionId"]
            role = event.get("type")
            if role not in {"user", "assistant"}:
                continue
            message = event.get("message")
            if not isinstance(message, dict) or message.get("role") not in {role, None}:
                continue
            text = _text_blocks(message.get("content"), {"text"})
            timestamp = _iso(event.get("timestamp"))
            if text.strip() and timestamp:
                turns.append(SessionTurn(str(role), text.strip()[:max_turn_chars], timestamp))
    if not turns or not cwd:
        return None, invalid_lines
    return ParsedSession("claude_code", session_id, cwd, turns, turns[0].timestamp, turns[-1].timestamp), invalid_lines


def parse_codex_session(path: Path, max_turn_chars: int) -> tuple[ParsedSession | None, int]:
    turns: list[SessionTurn] = []
    cwd = ""
    session_id = path.stem
    invalid_lines = 0
    with path.open("rb") as handle:
        for raw in handle:
            if not raw.endswith(b"\n"):
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                invalid_lines += 1
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if event.get("type") in {"session_meta", "turn_context"}:
                if isinstance(payload.get("cwd"), str) and payload["cwd"]:
                    cwd = payload["cwd"]
                identity = payload.get("id") or payload.get("session_id")
                if isinstance(identity, str) and identity:
                    session_id = identity
                continue
            if event.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            # Developer messages are deliberately excluded rather than relabeled.
            if role not in {"user", "assistant"}:
                continue
            allowed = {"input_text"} if role == "user" else {"output_text"}
            text = _text_blocks(payload.get("content"), allowed)
            timestamp = _iso(event.get("timestamp"))
            if text.strip() and timestamp:
                turns.append(SessionTurn(str(role), text.strip()[:max_turn_chars], timestamp))
    if not turns or not cwd:
        return None, invalid_lines
    return ParsedSession("codex", session_id, cwd, turns, turns[0].timestamp, turns[-1].timestamp), invalid_lines


def _project_for_cwd(cwd: str, projects: Iterable[ProjectRecord]) -> ProjectRecord | None:
    try:
        resolved = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    candidates: list[tuple[int, ProjectRecord]] = []
    for project in projects:
        root = Path(project.path).expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        candidates.append((len(root.parts), project))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _bounded_turn_text(turns: list[SessionTurn], max_session_chars: int) -> str:
    selected: list[str] = []
    used = 0
    for turn in reversed(turns):
        value = f"[{turn.timestamp}] {turn.role}: {turn.text}".strip()
        remaining = max_session_chars - used
        if remaining <= 0:
            break
        selected.append(value[-remaining:])
        used += min(len(value), remaining)
    return "\n\n".join(reversed(selected))


def scan_recent_sessions(
    config: dict[str, Any],
    projects: list[ProjectRecord],
    *,
    now: datetime | None = None,
) -> tuple[list[EvidenceRecord], dict[str, Any]]:
    settings = config.get("recent_sessions") or {}
    now = now or datetime.now(UTC)
    enabled = bool(settings.get("enabled", False))
    retention_days = int(settings.get("retention_days", 30))
    diagnostics: dict[str, Any] = {
        "enabled": enabled,
        "retention_days": retention_days,
        "attention_half_life_days": float(settings.get("attention_half_life_days", 3)),
        "attention_weight": float(settings.get("attention_weight", 0.08)),
        "unmapped_session_count": 0,
        "malformed_session_count": 0,
        "redacted_match_count": 0,
        "excluded_retrieval_meta_turn_count": 0,
        "last_successful_scan_at": now.replace(microsecond=0).isoformat(),
    }
    for surface in ("claude_code", "codex"):
        diagnostics[f"{surface}_discovered_files"] = 0
        diagnostics[f"{surface}_sessions"] = 0
        diagnostics[f"{surface}_chunks"] = 0
        diagnostics[f"{surface}_newest_observed_at"] = None
        diagnostics[f"{surface}_oldest_observed_at"] = None
        diagnostics[f"{surface}_excluded_old_sessions"] = 0
    if not enabled:
        return [], diagnostics

    cutoff = now - timedelta(days=retention_days)
    max_sessions = int(settings.get("max_sessions_per_surface", 100))
    max_total_chunks = int(settings.get("max_total_chunks", 250))
    max_turn_chars = int(settings.get("max_turn_chars", 1200))
    max_session_chars = int(settings.get("max_session_chars", 24000))
    chunk_chars = int(config.get("chunk_chars", 3200))
    overlap_chars = int(config.get("chunk_overlap_chars", 240))
    require_roots = bool(settings.get("require_source_roots", True))
    excluded_probe_queries = [
        str(query)
        for query in settings.get("excluded_retrieval_probe_queries") or []
        if isinstance(query, str) and query.strip()
    ]
    parsed: list[tuple[ParsedSession, ProjectRecord]] = []

    for raw_surface in settings.get("surfaces") or []:
        surface = str(raw_surface.get("name") or "")
        if surface not in {"claude_code", "codex"}:
            raise ValueError(f"unsupported_session_surface:{surface}")
        root = Path(str(raw_surface.get("path") or "")).expanduser()
        if not root.is_dir():
            if require_roots:
                raise RuntimeError(f"required_session_root_unavailable:{surface}")
            continue
        files = sorted(root.rglob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        eligible_files = [path for path in files if datetime.fromtimestamp(path.stat().st_mtime, UTC) >= cutoff]
        diagnostics[f"{surface}_discovered_files"] = len(eligible_files)
        diagnostics[f"{surface}_excluded_old_sessions"] = len(files) - len(eligible_files)
        parser = parse_claude_session if surface == "claude_code" else parse_codex_session
        malformed_files = 0
        surface_sessions: list[tuple[ParsedSession, ProjectRecord]] = []
        for path in eligible_files:
            try:
                session, invalid_lines = parser(path, max_turn_chars)
            except (OSError, ValueError):
                session, invalid_lines = None, 1
            if invalid_lines > 0:
                malformed_files += 1
            if session is None:
                continue
            ended = datetime.fromisoformat(session.ended_at)
            if ended < cutoff:
                diagnostics[f"{surface}_excluded_old_sessions"] += 1
                continue
            project = _project_for_cwd(session.cwd, projects)
            if project is None:
                diagnostics["unmapped_session_count"] += 1
                continue
            surface_sessions.append((session, project))
        diagnostics["malformed_session_count"] += malformed_files
        if eligible_files and malformed_files / len(eligible_files) > 0.05:
            raise RuntimeError(f"session_malformed_rate_exceeded:{surface}:{malformed_files}/{len(eligible_files)}")
        parsed.extend(surface_sessions[:max_sessions])

    parsed.sort(key=lambda item: item[0].ended_at, reverse=True)
    records: list[EvidenceRecord] = []
    for session, project in parsed:
        evidence_turns, excluded_meta_turns = _without_retrieval_meta_turns(
            session.turns,
            excluded_probe_queries,
        )
        diagnostics["excluded_retrieval_meta_turn_count"] += excluded_meta_turns
        redacted, redaction_count = redact_session_text(_bounded_turn_text(evidence_turns, max_session_chars))
        diagnostics["redacted_match_count"] += redaction_count
        chunks = chunk_text(redacted, chunk_chars, overlap_chars)
        if not chunks:
            continue
        surface = session.surface
        logical_path = f"session://{surface}/{session.session_id}"
        title_time = datetime.fromisoformat(session.ended_at).strftime("%Y-%m-%d %H:%M")
        title = f"{project.name} — {surface} session — {title_time}"
        source_id = source_id_for_path(logical_path)
        for chunk_index, chunk in enumerate(chunks):
            if len(records) >= max_total_chunks:
                break
            checksum = hashlib.sha256(chunk.encode()).hexdigest()
            identity = f"{surface}:{session.session_id}:{chunk_index}:{checksum}"
            records.append(EvidenceRecord(
                id=f"ev_session_{hashlib.sha256(identity.encode()).hexdigest()[:20]}",
                source_id=source_id,
                title=title,
                text=chunk,
                source_path=logical_path,
                source_kind=f"{surface}_session",
                project=project.name,
                source_modified_at=session.ended_at,
                content_checksum=checksum,
                chunk_index=chunk_index,
                chunk_count=len(chunks),
                authority=0.7,
                evidence_role="working_context",
                session_surface=surface,
                session_id=session.session_id,
                session_started_at=session.started_at,
                session_ended_at=session.ended_at,
                attention_observed_at=session.ended_at,
            ))
        if len(records) >= max_total_chunks:
            break

    for surface in ("claude_code", "codex"):
        surface_records = [record for record in records if record.session_surface == surface]
        session_ids = {record.session_id for record in surface_records}
        observed = sorted(record.attention_observed_at for record in surface_records if record.attention_observed_at)
        diagnostics[f"{surface}_sessions"] = len(session_ids)
        diagnostics[f"{surface}_chunks"] = len(surface_records)
        diagnostics[f"{surface}_newest_observed_at"] = observed[-1] if observed else None
        diagnostics[f"{surface}_oldest_observed_at"] = observed[0] if observed else None
    return records, diagnostics
