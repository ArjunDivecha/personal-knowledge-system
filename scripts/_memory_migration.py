from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models.entries import (  # noqa: E402
    MEMORY_SCHEMA_VERSION,
    KnowledgeEntry,
    KnowledgeMetadata,
    ProjectEntry,
    ProjectMetadata,
    normalize_knowledge_metadata_dict,
    normalize_project_metadata_dict,
)
from pipeline.index import generate_thin_index  # noqa: E402


VALID_CONTEXT_TYPES = {
    "professional_identity",
    "stated_preference",
    "explicit_save",
    "active_project",
    "recurring_pattern",
    "task_query",
    "passing_reference",
}

INJECTION_TIER_BY_CONTEXT_TYPE = {
    "professional_identity": 1,
    "stated_preference": 1,
    "explicit_save": 1,
    "active_project": 1,
    "recurring_pattern": 2,
    "task_query": 3,
    "passing_reference": 3,
}

MIGRATION_FLAG_KEY = "migration:backfill_complete"
DEFAULT_CLASSIFICATION_MODEL = "claude-haiku-4-5"

CHECKPOINT_DIR = REPO_ROOT / "scripts" / "checkpoints"
REPORT_DIR = REPO_ROOT / "scripts" / "reports"


def ensure_runtime_dirs() -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def default_checkpoint(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "completed_ids": [],
        "failed_ids": [],
        "stats": {},
        "errors": [],
    }


def load_checkpoint(path: Path, name: str) -> dict[str, Any]:
    ensure_runtime_dirs()
    if not path.exists():
        return default_checkpoint(name)

    with path.open() as handle:
        data = json.load(handle)

    checkpoint = default_checkpoint(name)
    checkpoint.update(data)
    checkpoint["completed_ids"] = list(dict.fromkeys(checkpoint.get("completed_ids", [])))
    checkpoint["failed_ids"] = list(dict.fromkeys(checkpoint.get("failed_ids", [])))
    checkpoint["errors"] = list(checkpoint.get("errors", []))
    checkpoint["stats"] = dict(checkpoint.get("stats", {}))
    return checkpoint


def save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    checkpoint["updated_at"] = utc_now_iso()
    with path.open("w") as handle:
        json.dump(checkpoint, handle, indent=2, sort_keys=True)


def append_report(report_name: str, payload: dict[str, Any]) -> Path:
    ensure_runtime_dirs()
    report_path = REPORT_DIR / report_name
    with report_path.open("w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return report_path


def normalize_knowledge_entry_for_phase2(entry: KnowledgeEntry) -> KnowledgeEntry:
    meta_dict = normalize_knowledge_metadata_dict(asdict(entry.metadata) if entry.metadata else {})

    source_conversations = dedupe_preserve_order(meta_dict.get("source_conversations", []))
    source_messages = dedupe_preserve_order(meta_dict.get("source_messages", []))
    mention_count = len(source_conversations) if source_conversations else int(meta_dict.get("mention_count") or 1)
    mention_count = max(1, mention_count)

    first_seen = meta_dict.get("first_seen") or meta_dict.get("created_at") or meta_dict.get("updated_at")
    last_seen = meta_dict.get("last_seen") or meta_dict.get("updated_at") or first_seen

    context_type = meta_dict.get("context_type")
    injection_tier = (
        INJECTION_TIER_BY_CONTEXT_TYPE.get(context_type)
        if context_type in VALID_CONTEXT_TYPES
        else None
    )

    entry.metadata = KnowledgeMetadata(
        created_at=meta_dict.get("created_at", ""),
        updated_at=meta_dict.get("updated_at", ""),
        source_conversations=source_conversations,
        source_messages=source_messages,
        access_count=int(meta_dict.get("access_count") or 0),
        last_accessed=meta_dict.get("last_accessed"),
        schema_version=MEMORY_SCHEMA_VERSION,
        classification_status=meta_dict.get("classification_status") or "pending",
        context_type=context_type if context_type in VALID_CONTEXT_TYPES else None,
        mention_count=mention_count,
        first_seen=first_seen,
        last_seen=last_seen,
        auto_inferred=meta_dict.get("auto_inferred"),
        source_weights=dict(meta_dict.get("source_weights") or {}),
        injection_tier=injection_tier,
        salience_score=meta_dict.get("salience_score"),
        last_consolidated=meta_dict.get("last_consolidated"),
        consolidation_notes=list(meta_dict.get("consolidation_notes") or []),
        archived=bool(meta_dict.get("archived", False)),
    )
    return entry


def normalize_project_entry_for_phase2(entry: ProjectEntry) -> ProjectEntry:
    meta_dict = normalize_project_metadata_dict(asdict(entry.metadata) if entry.metadata else {})

    source_conversations = dedupe_preserve_order(meta_dict.get("source_conversations", []))
    source_messages = dedupe_preserve_order(meta_dict.get("source_messages", []))
    mention_count = len(source_conversations) if source_conversations else int(meta_dict.get("mention_count") or 1)
    mention_count = max(1, mention_count)

    first_seen = meta_dict.get("first_seen") or meta_dict.get("created_at") or meta_dict.get("updated_at")
    last_seen = meta_dict.get("last_seen") or meta_dict.get("last_touched") or meta_dict.get("updated_at") or first_seen

    context_type = meta_dict.get("context_type")
    injection_tier = (
        INJECTION_TIER_BY_CONTEXT_TYPE.get(context_type)
        if context_type in VALID_CONTEXT_TYPES
        else None
    )

    entry.metadata = ProjectMetadata(
        created_at=meta_dict.get("created_at", ""),
        updated_at=meta_dict.get("updated_at", ""),
        source_conversations=source_conversations,
        source_messages=source_messages,
        last_touched=meta_dict.get("last_touched") or meta_dict.get("updated_at", ""),
        access_count=int(meta_dict.get("access_count") or 0),
        last_accessed=meta_dict.get("last_accessed"),
        schema_version=MEMORY_SCHEMA_VERSION,
        classification_status=meta_dict.get("classification_status") or "pending",
        context_type=context_type if context_type in VALID_CONTEXT_TYPES else None,
        mention_count=mention_count,
        first_seen=first_seen,
        last_seen=last_seen,
        auto_inferred=meta_dict.get("auto_inferred"),
        source_weights=dict(meta_dict.get("source_weights") or {}),
        injection_tier=injection_tier,
        salience_score=meta_dict.get("salience_score"),
        last_consolidated=meta_dict.get("last_consolidated"),
        consolidation_notes=list(meta_dict.get("consolidation_notes") or []),
        archived=bool(meta_dict.get("archived", False)),
    )
    return entry


def normalize_entry_for_phase2(entry: KnowledgeEntry | ProjectEntry) -> KnowledgeEntry | ProjectEntry:
    if isinstance(entry, KnowledgeEntry):
        return normalize_knowledge_entry_for_phase2(entry)
    return normalize_project_entry_for_phase2(entry)


def get_entry_label(entry: KnowledgeEntry | ProjectEntry) -> str:
    return entry.domain if isinstance(entry, KnowledgeEntry) else entry.name


def get_entry_state(entry: KnowledgeEntry | ProjectEntry) -> str:
    return entry.state if isinstance(entry, KnowledgeEntry) else entry.status


def get_entry_updated_at(entry: KnowledgeEntry | ProjectEntry) -> str:
    if not entry.metadata:
        return ""
    if isinstance(entry, KnowledgeEntry):
        return entry.metadata.updated_at
    return entry.metadata.updated_at or entry.metadata.last_touched


def get_entry_source_hint(entry: KnowledgeEntry | ProjectEntry) -> str:
    metadata = entry.metadata
    if metadata and metadata.source_weights:
        return ", ".join(sorted(metadata.source_weights.keys()))

    if metadata:
        raw_sources = [value.lower() for value in metadata.source_conversations]
        if any("gmail" in value or "email" in value for value in raw_sources):
            return "email"
        if any("codex" in value for value in raw_sources):
            return "codex transcript"
        if any("claude" in value and "code" in value for value in raw_sources):
            return "claude_code transcript"
        if any("claude" in value for value in raw_sources):
            return "claude_ai transcript"
        if any("github" in value for value in raw_sources):
            return "github"

    if entry.related_repos:
        return "github-linked"

    return "conversation transcript"


def get_entry_mention_count(entry: KnowledgeEntry | ProjectEntry) -> int:
    if not entry.metadata:
        return 1
    if entry.metadata.mention_count:
        return max(1, int(entry.metadata.mention_count))
    if entry.metadata.source_conversations:
        return max(1, len(dedupe_preserve_order(entry.metadata.source_conversations)))
    return 1


def build_classification_summary(entry: KnowledgeEntry | ProjectEntry) -> str:
    if isinstance(entry, KnowledgeEntry):
        parts = [entry.current_view.strip()]
        parts.extend(insight.insight.strip() for insight in entry.key_insights[:3] if insight.insight.strip())
        parts.extend(capability.capability.strip() for capability in entry.knows_how_to[:2] if capability.capability.strip())
        summary = " ".join(part for part in parts if part)
        return summary[:600]

    parts = [entry.goal.strip(), entry.current_phase.strip()]
    parts.extend(decision.decision.strip() for decision in entry.decisions_made[:2] if decision.decision.strip())
    if entry.tech_stack:
        parts.append("Tech stack: " + ", ".join(entry.tech_stack[:6]))
    summary = " ".join(part for part in parts if part)
    return summary[:600]


def build_embedding_text(entry: KnowledgeEntry | ProjectEntry) -> str:
    if isinstance(entry, KnowledgeEntry):
        parts = [entry.domain, entry.current_view]
        parts.extend(insight.insight for insight in entry.key_insights[:3])
        return " ".join(part.strip() for part in parts if part).strip()

    parts = [entry.name, entry.goal, entry.current_phase]
    return " ".join(part.strip() for part in parts if part).strip()


def build_vector_metadata(entry: KnowledgeEntry | ProjectEntry) -> dict[str, Any]:
    metadata = entry.metadata
    if not metadata:
        raise ValueError(f"{entry.id} is missing metadata")

    vector_metadata: dict[str, Any] = {
        "type": entry.type,
        "domain": get_entry_label(entry),
        "state": get_entry_state(entry),
        "updated_at": get_entry_updated_at(entry),
        "classification_status": metadata.classification_status or "pending",
        "archived": bool(metadata.archived),
    }

    if metadata.source_conversations:
        vector_metadata["source"] = (
            metadata.source_conversations[0]
            if len(metadata.source_conversations) == 1
            else ",".join(metadata.source_conversations[:3])
        )
    if metadata.context_type:
        vector_metadata["context_type"] = metadata.context_type
    if metadata.injection_tier is not None:
        vector_metadata["injection_tier"] = metadata.injection_tier
    if metadata.salience_score is not None:
        vector_metadata["salience_score"] = metadata.salience_score

    return vector_metadata


def metadata_matches(expected: dict[str, Any], actual: dict[str, Any] | None) -> bool:
    actual = actual or {}
    for key, value in expected.items():
        if actual.get(key) != value:
            return False
    return True


def make_json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return {key: make_json_safe(subvalue) for key, subvalue in asdict(value).items()}
    if isinstance(value, dict):
        return {key: make_json_safe(subvalue) for key, subvalue in value.items()}
    if isinstance(value, list):
        return [make_json_safe(item) for item in value]
    return value


def rebuild_thin_index(redis_client: Any, knowledge_entries: list[KnowledgeEntry], project_entries: list[ProjectEntry]) -> dict[str, Any]:
    thin_index = generate_thin_index(knowledge_entries, project_entries)
    redis_client.save_thin_index(thin_index)
    return thin_index.to_dict()


def chunked(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def load_entries(redis_client: Any, entry_type: Literal["all", "knowledge", "project"] = "all") -> list[KnowledgeEntry | ProjectEntry]:
    entries: list[KnowledgeEntry | ProjectEntry] = []
    if entry_type in ("all", "knowledge"):
        entries.extend(redis_client.get_all_knowledge_entries())
    if entry_type in ("all", "project"):
        entries.extend(redis_client.get_all_project_entries())
    return entries
