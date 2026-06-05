"""
Phase 7A offline observation and compiled-claim schema.

This module is intentionally pure: no Redis, Vector, Worker, or MCP side
effects. It provides the schema and deterministic migration-preview helpers that
later Phase 7 work can consume.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypeVar

from .entries import KnowledgeEntry, ProjectEntry

try:
    from ..utils.signal_flags import CORRECTION_DERIVED_FLAG, EXPLICIT_SAVE_FLAG
except ImportError:  # pragma: no cover - supports tests importing models as top-level package.
    from utils.signal_flags import CORRECTION_DERIVED_FLAG, EXPLICIT_SAVE_FLAG

PHASE7_SCHEMA_VERSION = 1

MemoryLane = Literal["semantic", "episodic", "procedural"]
SourceAuthority = Literal["explicit", "manual", "system", "inferred"]
ClaimStatus = Literal[
    "current",
    "historical",
    "superseded",
    "contested",
    "stale",
    "deprecated",
    "pending_compile",
    "expired",
]
TemporalStatus = Literal[
    "unknown",
    "timeless",
    "current",
    "future",
    "expired",
    "historical",
]
TaxonomyDecision = Literal[
    "duplicate",
    "refinement",
    "supersession",
    "scoped_exception",
    "contestation",
    "temporal_expiry",
    "deprecation",
]

MEMORY_LANES = {"semantic", "episodic", "procedural"}
SOURCE_AUTHORITIES = {"explicit", "manual", "system", "inferred"}
CLAIM_STATUSES = {
    "current",
    "historical",
    "superseded",
    "contested",
    "stale",
    "deprecated",
    "pending_compile",
    "expired",
}
TEMPORAL_STATUSES = {
    "unknown",
    "timeless",
    "current",
    "future",
    "expired",
    "historical",
}
TAXONOMY_DECISIONS = {
    "duplicate",
    "refinement",
    "supersession",
    "scoped_exception",
    "contestation",
    "temporal_expiry",
    "deprecation",
}
EDGE_DECISIONS = {"refinement", "supersession", "temporal_expiry", "deprecation"}

AUTHORITY_RANK = {
    "inferred": 0,
    "system": 1,
    "manual": 2,
    "explicit": 3,
}

T = TypeVar("T")


def _validate_allowed(name: str, value: str | None, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")


def _validate_non_empty_string(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _from_dict(cls: type[T], data: dict[str, Any]) -> T:
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__}.from_dict requires a dict")

    kwargs: dict[str, Any] = {}
    for item in fields(cls):
        if not item.init:
            continue
        if item.name in data:
            kwargs[item.name] = data[item.name]
            continue
        if item.default is not MISSING or item.default_factory is not MISSING:
            continue
        raise ValueError(f"Missing required field: {item.name}")
    return cls(**kwargs)


def _as_dict(instance: Any) -> dict[str, Any]:
    return asdict(instance)


def _parse_utc(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_after(value: str, delta: timedelta) -> str:
    return (_parse_utc(value) + delta).isoformat()


def stable_phase7_id(prefix: str, *parts: object) -> str:
    """Return a stable prefix_hash ID from canonical JSON-encoded parts."""
    _validate_non_empty_string("prefix", prefix)
    encoded_parts = [
        json.dumps(part, sort_keys=True, separators=(",", ":"), default=str)
        for part in parts
    ]
    digest = hashlib.sha256("\x1f".join([prefix, *encoded_parts]).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:16]}"


def normalize_claim_text(text: str) -> str:
    return " ".join(text.casefold().strip().split())


def highest_source_authority(authorities: list[SourceAuthority]) -> SourceAuthority:
    if not authorities:
        raise ValueError("authorities cannot be empty")
    for authority in authorities:
        _validate_allowed("source_authority", authority, SOURCE_AUTHORITIES)
    return max(authorities, key=lambda authority: AUTHORITY_RANK[authority])


@dataclass
class Phase7Observation:
    observation_id: str
    subject_id: str
    memory_lane: MemoryLane
    source_authority: SourceAuthority
    claim_text: str
    source_type: str
    source_id: str
    message_ids: list[str]
    source_path: str | None
    snippet: str
    observed_at: str | None
    learned_at: str | None
    valid_from: str | None = None
    valid_to: str | None = None
    invalidated_at: str | None = None
    confidence: str = "medium"
    entity_mentions: list[str] = field(default_factory=list)
    relationship_edges: list[dict[str, Any]] = field(default_factory=list)
    scope: dict[str, str] = field(default_factory=dict)
    signal_flags: list[str] = field(default_factory=list)
    extraction_method: str = "migration_preview"
    schema_version: int = PHASE7_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name in (
            "observation_id",
            "subject_id",
            "memory_lane",
            "source_authority",
            "claim_text",
            "source_type",
            "source_id",
        ):
            _validate_non_empty_string(name, getattr(self, name))
        _validate_allowed("memory_lane", self.memory_lane, MEMORY_LANES)
        _validate_allowed("source_authority", self.source_authority, SOURCE_AUTHORITIES)
        if not isinstance(self.message_ids, list):
            raise ValueError("message_ids must be a list")
        if not isinstance(self.snippet, str):
            raise ValueError("snippet must be a string")
        if self.schema_version != PHASE7_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE7_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return _as_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7Observation":
        return _from_dict(cls, data)


@dataclass
class Phase7CompiledClaim:
    claim_id: str
    subject_id: str
    memory_lane: Literal["semantic"]
    compiled_text: str
    status: ClaimStatus
    support_observation_ids: list[str]
    primary_source_authority: SourceAuthority = "inferred"
    confidence: str = "medium"
    temporal_status: TemporalStatus = "unknown"
    valid_from: str | None = None
    valid_to: str | None = None
    ttl_expires_at: str | None = None
    invalidated_at: str | None = None
    supersedes_claim_ids: list[str] = field(default_factory=list)
    superseded_by_claim_id: str | None = None
    taxonomy_decision: TaxonomyDecision | None = None
    scope: dict[str, str] = field(default_factory=dict)
    compile_notes: list[str] = field(default_factory=list)
    compiled_at: str | None = None
    compiled_by: str = "migration_preview"
    expected_source_revisions: dict[str, int] = field(default_factory=dict)
    schema_version: int = PHASE7_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name in ("claim_id", "subject_id", "compiled_text", "status"):
            _validate_non_empty_string(name, getattr(self, name))
        if self.memory_lane != "semantic":
            raise ValueError("compiled claims must be semantic")
        _validate_allowed("status", self.status, CLAIM_STATUSES)
        _validate_allowed("primary_source_authority", self.primary_source_authority, SOURCE_AUTHORITIES)
        _validate_allowed("temporal_status", self.temporal_status, TEMPORAL_STATUSES)
        if self.taxonomy_decision is not None:
            _validate_allowed("taxonomy_decision", self.taxonomy_decision, TAXONOMY_DECISIONS)
        if not isinstance(self.support_observation_ids, list):
            raise ValueError("support_observation_ids must be a list")
        if not self.support_observation_ids and (
            self.status != "deprecated" or not self.compile_notes
        ):
            raise ValueError("support_observation_ids cannot be empty unless deprecated with compile_notes")
        if self.status == "pending_compile" and not self.ttl_expires_at:
            raise ValueError("pending_compile claims require ttl_expires_at")
        if self.schema_version != PHASE7_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE7_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return _as_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7CompiledClaim":
        return _from_dict(cls, data)


@dataclass
class Phase7SupersessionEdge:
    from_claim_id: str
    to_claim_id: str
    decision: TaxonomyDecision
    reason: str
    observation_id: str
    observed_at: str | None
    schema_version: int = PHASE7_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name in ("from_claim_id", "to_claim_id", "reason", "observation_id"):
            _validate_non_empty_string(name, getattr(self, name))
        _validate_allowed("decision", self.decision, EDGE_DECISIONS)
        if self.from_claim_id == self.to_claim_id:
            raise ValueError("from_claim_id and to_claim_id cannot be equal")
        if self.schema_version != PHASE7_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE7_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return _as_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7SupersessionEdge":
        return _from_dict(cls, data)


@dataclass
class Phase7MigrationPreview:
    observations: list[Phase7Observation]
    claims: list[Phase7CompiledClaim]
    supersession_edges: list[Phase7SupersessionEdge]
    skipped_count: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": [observation.to_dict() for observation in self.observations],
            "claims": [claim.to_dict() for claim in self.claims],
            "supersession_edges": [edge.to_dict() for edge in self.supersession_edges],
            "skipped_count": self.skipped_count,
            "errors": list(self.errors),
        }


def _entry_to_dict(entry: dict[str, Any] | KnowledgeEntry | ProjectEntry) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry)
    if isinstance(entry, (KnowledgeEntry, ProjectEntry)):
        return entry.to_dict()
    raise ValueError(f"Unsupported legacy entry type: {type(entry).__name__}")


def _metadata(entry_data: dict[str, Any]) -> dict[str, Any]:
    metadata = entry_data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _evidence_dict(item: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    evidence = item.get("evidence")
    return evidence if isinstance(evidence, dict) else {}


def _source_authority(entry_data: dict[str, Any]) -> SourceAuthority:
    flags = list(_metadata(entry_data).get("signal_flags") or [])
    if EXPLICIT_SAVE_FLAG in flags:
        return "explicit"
    return "inferred"


def _signal_flags(entry_data: dict[str, Any]) -> list[str]:
    flags = list(_metadata(entry_data).get("signal_flags") or [])
    return [flag for flag in flags if isinstance(flag, str)]


def _observed_at(metadata: dict[str, Any], item_timestamp: str | None = None) -> str | None:
    return (
        item_timestamp
        or metadata.get("created_at")
        or metadata.get("last_touched")
        or None
    )


def _learned_at(metadata: dict[str, Any], observed_at: str | None) -> str | None:
    return metadata.get("updated_at") or observed_at


def _message_ids_from_evidence(evidence: dict[str, Any]) -> list[str]:
    return list(evidence.get("message_ids") or [])


def _observation(
    *,
    subject_id: str,
    source_type: str,
    source_path: str,
    claim_text: str,
    memory_lane: MemoryLane,
    source_authority: SourceAuthority,
    metadata: dict[str, Any],
    evidence: dict[str, Any] | None,
    item_timestamp: str | None = None,
    signal_flags: list[str] | None = None,
) -> tuple[Phase7Observation, str | None]:
    evidence = evidence or {}
    conversation_id = evidence.get("conversation_id")
    source_id = conversation_id if isinstance(conversation_id, str) and conversation_id else subject_id
    message_ids = _message_ids_from_evidence(evidence)
    snippet = evidence.get("snippet") if isinstance(evidence.get("snippet"), str) else ""
    observed = _observed_at(metadata, item_timestamp)
    warning = None
    if not message_ids or not snippet:
        warning = f"{subject_id}:{source_path} missing evidence"

    observation = Phase7Observation(
        observation_id=stable_phase7_id(
            "obs",
            subject_id,
            source_type,
            source_id,
            source_path,
            claim_text,
            message_ids,
        ),
        subject_id=subject_id,
        memory_lane=memory_lane,
        source_authority=source_authority,
        claim_text=claim_text,
        source_type=source_type,
        source_id=source_id,
        message_ids=message_ids,
        source_path=source_path,
        snippet=snippet,
        observed_at=observed,
        learned_at=_learned_at(metadata, observed),
        signal_flags=list(signal_flags or []),
    )
    return observation, warning


def _add_scalar_observation(
    observations: list[Phase7Observation],
    *,
    entry_data: dict[str, Any],
    source_path: str,
    claim_text: str,
    source_authority: SourceAuthority,
    signal_flags: list[str],
) -> None:
    if not isinstance(claim_text, str) or not claim_text:
        return
    metadata = _metadata(entry_data)
    subject_id = entry_data["id"]
    source_type = entry_data.get("type", "knowledge")
    observed = _observed_at(metadata)
    observations.append(
        Phase7Observation(
            observation_id=stable_phase7_id(
                "obs",
                subject_id,
                source_type,
                subject_id,
                source_path,
                claim_text,
                [],
            ),
            subject_id=subject_id,
            memory_lane="semantic",
            source_authority=source_authority,
            claim_text=claim_text,
            source_type=source_type,
            source_id=subject_id,
            message_ids=[],
            source_path=source_path,
            snippet="",
            observed_at=observed,
            learned_at=_learned_at(metadata, observed),
            signal_flags=list(signal_flags),
        )
    )


def _observations_with_warnings(
    entry: dict[str, Any] | KnowledgeEntry | ProjectEntry,
) -> tuple[list[Phase7Observation], list[str]]:
    entry_data = _entry_to_dict(entry)
    entry_type = entry_data.get("type")
    if entry_type not in {"knowledge", "project"}:
        raise ValueError("legacy entry type must be 'knowledge' or 'project'")
    _validate_non_empty_string("id", entry_data.get("id"))

    metadata = _metadata(entry_data)
    authority = _source_authority(entry_data) if entry_type == "knowledge" else "inferred"
    signal_flags = _signal_flags(entry_data) if entry_type == "knowledge" else []
    observations: list[Phase7Observation] = []
    warnings: list[str] = []

    if entry_type == "knowledge":
        _add_scalar_observation(
            observations,
            entry_data=entry_data,
            source_path="current_view",
            claim_text=entry_data.get("current_view", ""),
            source_authority=authority,
            signal_flags=signal_flags,
        )

        for index, position in enumerate(entry_data.get("positions") or []):
            text = position.get("view") if isinstance(position, dict) else None
            if not isinstance(text, str) or not text:
                continue
            observation, warning = _observation(
                subject_id=entry_data["id"],
                source_type="knowledge",
                source_path=f"positions[{index}]",
                claim_text=text,
                memory_lane="semantic",
                source_authority=authority,
                metadata=metadata,
                evidence=_evidence_dict(position),
                item_timestamp=position.get("as_of"),
                signal_flags=signal_flags,
            )
            observations.append(observation)
            if warning:
                warnings.append(warning)

        for index, insight in enumerate(entry_data.get("key_insights") or []):
            text = insight.get("insight") if isinstance(insight, dict) else None
            if not isinstance(text, str) or not text:
                continue
            observation, warning = _observation(
                subject_id=entry_data["id"],
                source_type="knowledge",
                source_path=f"key_insights[{index}]",
                claim_text=text,
                memory_lane="semantic",
                source_authority=authority,
                metadata=metadata,
                evidence=_evidence_dict(insight),
                signal_flags=signal_flags,
            )
            observations.append(observation)
            if warning:
                warnings.append(warning)

        for index, capability in enumerate(entry_data.get("knows_how_to") or []):
            text = capability.get("capability") if isinstance(capability, dict) else None
            if not isinstance(text, str) or not text:
                continue
            observation, warning = _observation(
                subject_id=entry_data["id"],
                source_type="knowledge",
                source_path=f"knows_how_to[{index}]",
                claim_text=text,
                memory_lane="semantic",
                source_authority=authority,
                metadata=metadata,
                evidence=_evidence_dict(capability),
                signal_flags=signal_flags,
            )
            observations.append(observation)
            if warning:
                warnings.append(warning)

        for index, question in enumerate(entry_data.get("open_questions") or []):
            text = question.get("question") if isinstance(question, dict) else None
            if not isinstance(text, str) or not text:
                continue
            observation, warning = _observation(
                subject_id=entry_data["id"],
                source_type="knowledge",
                source_path=f"open_questions[{index}]",
                claim_text=text,
                memory_lane="episodic",
                source_authority=authority,
                metadata=metadata,
                evidence=_evidence_dict(question),
                signal_flags=signal_flags,
            )
            observations.append(observation)
            if warning:
                warnings.append(warning)

    if entry_type == "project":
        for path in ("goal", "current_phase", "status", "blocked_on"):
            _add_scalar_observation(
                observations,
                entry_data=entry_data,
                source_path=path,
                claim_text=entry_data.get(path, "") or "",
                source_authority="inferred",
                signal_flags=[],
            )

        for index, decision in enumerate(entry_data.get("decisions_made") or []):
            text = decision.get("decision") if isinstance(decision, dict) else None
            if not isinstance(text, str) or not text:
                continue
            observation, warning = _observation(
                subject_id=entry_data["id"],
                source_type="project",
                source_path=f"decisions_made[{index}]",
                claim_text=text,
                memory_lane="semantic",
                source_authority="inferred",
                metadata=metadata,
                evidence=_evidence_dict(decision),
                item_timestamp=decision.get("date"),
                signal_flags=[],
            )
            observations.append(observation)
            if warning:
                warnings.append(warning)

    return observations, warnings


def observations_from_legacy_entry(
    entry: dict[str, Any] | KnowledgeEntry | ProjectEntry,
) -> list[Phase7Observation]:
    observations, _ = _observations_with_warnings(entry)
    return observations


def compiled_claims_from_observations(
    observations: list[Phase7Observation],
    *,
    compiled_at: str | None = None,
) -> list[Phase7CompiledClaim]:
    grouped: OrderedDict[tuple[str, str], list[Phase7Observation]] = OrderedDict()
    for observation in observations:
        if observation.memory_lane != "semantic":
            continue
        key = (observation.subject_id, normalize_claim_text(observation.claim_text))
        grouped.setdefault(key, []).append(observation)

    claims: list[Phase7CompiledClaim] = []
    for (subject_id, _), group in grouped.items():
        compiled_text = group[0].claim_text
        support_ids = list(dict.fromkeys(item.observation_id for item in group))
        claims.append(
            Phase7CompiledClaim(
                claim_id=stable_phase7_id(
                    "claim",
                    subject_id,
                    normalize_claim_text(compiled_text),
                ),
                subject_id=subject_id,
                memory_lane="semantic",
                compiled_text=compiled_text,
                status="current",
                support_observation_ids=support_ids,
                primary_source_authority=highest_source_authority(
                    [item.source_authority for item in group]
                ),
                compiled_at=compiled_at,
                expected_source_revisions={},
            )
        )
    return claims


def provisional_claim_from_observation(
    observation: Phase7Observation,
    *,
    now_utc: str,
) -> Phase7CompiledClaim | None:
    if observation.memory_lane != "semantic":
        return None
    if observation.source_authority in {"explicit", "manual"}:
        ttl = timedelta(days=7)
    elif observation.source_authority == "system" and observation.scope:
        ttl = timedelta(hours=48)
    else:
        return None

    return Phase7CompiledClaim(
        claim_id=stable_phase7_id("claim", observation.subject_id, "pending", observation.observation_id),
        subject_id=observation.subject_id,
        memory_lane="semantic",
        compiled_text=observation.claim_text,
        status="pending_compile",
        support_observation_ids=[observation.observation_id],
        primary_source_authority=observation.source_authority,
        ttl_expires_at=_iso_after(now_utc, ttl),
        scope=dict(observation.scope),
        compiled_at=now_utc,
        compiled_by="provisional_projection",
        expected_source_revisions={},
    )


def retrieval_projection_from_claims(
    claims: list[Phase7CompiledClaim],
    *,
    now_utc: str,
) -> list[Phase7CompiledClaim]:
    now = _parse_utc(now_utc)
    projected: list[Phase7CompiledClaim] = []
    for claim in claims:
        if claim.memory_lane != "semantic":
            continue
        if claim.status == "current":
            projected.append(claim)
            continue
        if claim.status == "pending_compile" and claim.ttl_expires_at:
            if _parse_utc(claim.ttl_expires_at) > now:
                projected.append(claim)
    return projected


def preview_phase7_migration(
    entries: list[dict[str, Any] | KnowledgeEntry | ProjectEntry],
) -> Phase7MigrationPreview:
    observations: list[Phase7Observation] = []
    errors: list[str] = []
    skipped_count = 0

    for entry in entries:
        try:
            entry_observations, warnings = _observations_with_warnings(entry)
            observations.extend(entry_observations)
            errors.extend(warnings)
        except Exception as exc:  # noqa: BLE001 - preview must keep moving.
            skipped_count += 1
            errors.append(str(exc))

    return Phase7MigrationPreview(
        observations=observations,
        claims=compiled_claims_from_observations(observations),
        supersession_edges=[],
        skipped_count=skipped_count,
        errors=errors,
    )
