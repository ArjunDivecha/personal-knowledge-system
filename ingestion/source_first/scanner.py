from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import EvidenceRecord, ProjectRecord


UTC = timezone.utc


def strip_generated_boilerplate(text: str) -> str:
    """Remove workstation-wide agent plumbing copied into every project file.

    This section describes messaging mechanics, not the project. Keeping one
    identical copy per repo would recreate the exact duplication problem the
    source-first index is meant to avoid.
    """
    return re.sub(
        r"(?:^|\n)## Cross-session messaging\n.*?(?=\n## |\Z)",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )


@dataclass(frozen=True)
class SourceFile:
    path: Path
    source_kind: str
    project: str | None
    authority: float
    pinned: bool = False


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).replace(microsecond=0).isoformat()


def project_for_path(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    return relative.parts[0] if len(relative.parts) > 1 else root.name


def source_id_for_path(path: str | Path) -> str:
    """Stable logical ID used to retrieve every chunk from one source."""
    return f"src_{hashlib.sha256(str(path).encode()).hexdigest()[:20]}"


def is_authoritative(path: Path, root: Path, config: dict[str, Any]) -> bool:
    name_upper = path.name.upper()
    if path.name in set(config.get("authoritative_names") or []):
        return True
    if path.suffix.lower() == ".md" and any(
        token.upper() in name_upper for token in config.get("authoritative_name_contains") or []
    ):
        return True
    relative = path.relative_to(root).as_posix()
    return any(fnmatch.fnmatch(relative, pattern) for pattern in config.get("include_relative_globs") or [])


def iter_source_files(config: dict[str, Any], *, now: datetime | None = None) -> list[SourceFile]:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=int(config.get("recent_days", 365)))
    max_bytes = int(config.get("max_file_bytes", 750000))
    excluded = set(config.get("exclude_directories") or [])
    authority = config.get("source_authority") or {}
    discovered: dict[str, SourceFile] = {}

    for item in config.get("explicit_files") or []:
        path = Path(str(item["path"])).expanduser()
        if path.is_file() and path.stat().st_size <= max_bytes:
            kind = str(item.get("source_kind") or "curated_memory")
            discovered[str(path.resolve())] = SourceFile(
                path=path.resolve(),
                source_kind=kind,
                project=None,
                authority=float(authority.get(kind, 1.0)),
                pinned=bool(item.get("pinned", False)),
            )

    for root_item in config.get("roots") or []:
        root = Path(str(root_item["path"])).expanduser()
        if not root.is_dir():
            continue
        kind = str(root_item.get("source_kind") or "working_project")
        max_depth = int(root_item.get("max_depth", 4))
        # Per-root globs (relative to the root) that count as authoritative in
        # addition to the global name rules. Added 2026-09-04 so a folder whose
        # every markdown file is a verdict (A Complete/Investment Learnings) is
        # indexed whole instead of only the README-shaped files inside it.
        root_globs = [str(g) for g in (root_item.get("include_globs") or [])]
        exclude_git_worktrees = bool(config.get("exclude_git_worktrees", True))
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            depth = len(current_path.relative_to(root).parts)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in excluded
                and not name.startswith(".")
                and not (
                    exclude_git_worktrees
                    and depth == 0
                    and (current_path / name / ".git").is_file()
                )
            ]
            if depth >= max_depth:
                dirnames[:] = []
            for filename in filenames:
                path = current_path / filename
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size <= 0 or stat.st_size > max_bytes:
                    continue
                if datetime.fromtimestamp(stat.st_mtime, UTC) < cutoff:
                    continue
                if not is_authoritative(path, root, config) and not any(
                    fnmatch.fnmatch(path.relative_to(root).as_posix(), pattern) for pattern in root_globs
                ):
                    continue
                resolved = path.resolve()
                discovered[str(resolved)] = SourceFile(
                    path=resolved,
                    source_kind=kind,
                    project=project_for_path(path, root),
                    authority=float(authority.get(kind, 0.8)),
                )

    return sorted(discovered.values(), key=lambda item: str(item.path))


def normalize_text(text: str) -> str:
    cleaned = text.replace("\x00", "").replace("\r\n", "\n")
    return strip_generated_boilerplate(cleaned).strip()


def chunk_text(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        target = min(len(text), start + chunk_chars)
        end = target
        if target < len(text):
            boundary = text.rfind("\n\n", start + chunk_chars // 2, target)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap_chars)
    return chunks


def title_from_text(path: Path, text: str) -> str:
    for line in text.splitlines()[:30]:
        match = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if match:
            return match.group(1)[:240]
    return path.parent.name if path.name.upper().startswith("README") else path.stem[:240]


def evidence_from_files(files: Iterable[SourceFile], config: dict[str, Any]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    chunk_chars = int(config.get("chunk_chars", 3200))
    overlap_chars = int(config.get("chunk_overlap_chars", 240))
    for source in files:
        try:
            text = normalize_text(source.path.read_text(errors="replace"))
            modified_at = iso_from_timestamp(source.path.stat().st_mtime)
        except OSError:
            continue
        chunks = chunk_text(text, chunk_chars, overlap_chars)
        title = title_from_text(source.path, text)
        for index, chunk in enumerate(chunks):
            content_checksum = hashlib.sha256(chunk.encode()).hexdigest()
            identity = f"{source.path}|{index}|{content_checksum}".encode()
            record_id = f"ev_{hashlib.sha256(identity).hexdigest()[:24]}"
            records.append(EvidenceRecord(
                id=record_id,
                source_id=source_id_for_path(source.path),
                title=title,
                text=chunk,
                source_path=str(source.path),
                source_kind=source.source_kind,
                project=source.project,
                source_modified_at=modified_at,
                content_checksum=content_checksum,
                chunk_index=index,
                chunk_count=len(chunks),
                authority=source.authority,
                pinned=source.pinned,
            ))
    return sorted(records, key=lambda record: (record.source_path, record.chunk_index))


def first_paragraph(text: str, limit: int = 500) -> str:
    text = normalize_text(text)
    paragraphs = [re.sub(r"\s+", " ", part).strip("# ") for part in text.split("\n\n")]
    for paragraph in paragraphs:
        if len(paragraph) >= 30 and not paragraph.startswith("<"):
            return paragraph[:limit]
    return text[:limit]


def build_projects(files: Iterable[SourceFile], config: dict[str, Any], *, now: datetime | None = None) -> list[ProjectRecord]:
    now = now or datetime.now(UTC)
    active_cutoff = now - timedelta(days=90)
    by_project: dict[tuple[str, str], list[SourceFile]] = {}
    for source in files:
        if not source.project:
            continue
        project_path = str(source.path)
        for root_item in config.get("roots") or []:
            root = Path(str(root_item["path"])).expanduser()
            try:
                relative = source.path.relative_to(root)
            except ValueError:
                continue
            if relative.parts:
                project_path = str((root / relative.parts[0]).resolve())
                break
        by_project.setdefault((source.project, project_path), []).append(source)

    required = {str(Path(path).expanduser().resolve()) for path in config.get("required_projects") or []}
    for path_text in required:
        path = Path(path_text)
        if path.is_dir():
            by_project.setdefault((path.name, str(path)), [])

    projects: list[ProjectRecord] = []
    for (name, path_text), source_files in sorted(by_project.items()):
        path = Path(path_text)
        timestamps: list[float] = []
        status_sources = [
            source for source in source_files
            if source.path.name.upper() != "CLAUDE.MD" and ".pks/agent-context" not in source.path.as_posix()
        ] or source_files
        for source in status_sources:
            try:
                timestamps.append(source.path.stat().st_mtime)
            except OSError:
                pass
        if not timestamps and path.exists():
            try:
                timestamps.append(path.stat().st_mtime)
            except OSError:
                pass
        touched = datetime.fromtimestamp(max(timestamps) if timestamps else 0, UTC)
        summary = ""
        ordered = sorted(source_files, key=lambda item: (item.path.name.upper() != "README.MD", str(item.path)))
        for source in ordered:
            try:
                summary = first_paragraph(source.path.read_text(errors="replace"))
            except OSError:
                continue
            if summary:
                break
        record_id = f"prj_{hashlib.sha256(str(path).encode()).hexdigest()[:20]}"
        projects.append(ProjectRecord(
            id=record_id,
            name=name,
            path=str(path),
            status="active" if touched >= active_cutoff else "dormant",
            last_touched=touched.replace(microsecond=0).isoformat(),
            summary=summary or f"Project at {path}",
            source_files=[str(source.path) for source in ordered],
        ))
    return projects
