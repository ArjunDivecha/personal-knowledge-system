from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvidenceRecord:
    id: str
    source_id: str
    title: str
    text: str
    source_path: str
    source_kind: str
    project: str | None
    source_modified_at: str
    content_checksum: str
    chunk_index: int
    chunk_count: int
    authority: float
    evidence_role: str = "authoritative"
    session_surface: str | None = None
    session_id: str | None = None
    session_started_at: str | None = None
    session_ended_at: str | None = None
    attention_observed_at: str | None = None
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    path: str
    status: str
    last_touched: str
    summary: str
    source_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceFirstManifest:
    schema_version: int
    generation: str
    built_at: str
    evidence_count: int
    project_count: int
    source_file_count: int
    source_checksum: str
    required_projects_present: list[str]
    required_projects_missing: list[str]
    recent_sessions: dict[str, Any] = field(default_factory=dict)
    previous_generation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
