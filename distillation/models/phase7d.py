"""
Phase 7D offline read-only memory block helpers.

Memory blocks are compact presentation slots generated from compiled current
claims or version-controlled policy pointers. This module is pure and does not
touch Redis, Vector, MCP, or Dream apply code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .phase7 import Phase7CompiledClaim, Phase7Observation, stable_phase7_id
from .phase7c import Phase7CurrentView

PHASE7D_SCHEMA_VERSION = 1
MIN_BLOCK_CHAR_LIMIT = 80
MAX_BLOCK_CHAR_LIMIT = 4000

MemoryBlockLabel = Literal["operator_profile", "project_status", "policy_pointer"]
MemoryBlockScope = Literal["operator", "project", "repo", "agent"]

MEMORY_BLOCK_LABELS = {"operator_profile", "project_status", "policy_pointer"}
MEMORY_BLOCK_SCOPES = {"operator", "project", "repo", "agent"}


def _validate_non_empty_string(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _claim_sort_key(claim: Phase7CompiledClaim) -> tuple[str, str]:
    return claim.scope.get("rank", ""), claim.subject_id, claim.claim_id


def _claims_for_block(
    current_view: Phase7CurrentView,
    label: str,
    *,
    scope_ref: str | None = None,
) -> list[Phase7CompiledClaim]:
    claims = [
        claim
        for claim in current_view.current_claims
        if claim.scope.get("memory_block") == label
    ]
    if scope_ref is not None:
        claims = [
            claim
            for claim in claims
            if claim.scope.get("project_id") == scope_ref
            or claim.scope.get("project") == scope_ref
        ]
    return sorted(claims, key=_claim_sort_key)


def _support_observation_ids(claims: list[Phase7CompiledClaim]) -> list[str]:
    return _dedupe(
        [
            observation_id
            for claim in claims
            for observation_id in claim.support_observation_ids
        ]
    )


def _source_paths_for_observations(
    observation_ids: list[str],
    observations_by_id: dict[str, Phase7Observation],
) -> list[str]:
    return _dedupe(
        [
            observation.source_path
            for observation_id in observation_ids
            if (observation := observations_by_id.get(observation_id)) is not None
            and observation.source_path
        ]
    )


def _bounded_lines(lines: list[str], chars_limit: int) -> str:
    value = "\n".join(lines)
    if len(value) <= chars_limit:
        return value

    suffix = "\n- [truncated]"
    kept: list[str] = []
    for line in lines:
        candidate_lines = [*kept, line]
        candidate = "\n".join(candidate_lines) + suffix
        if len(candidate) <= chars_limit:
            kept.append(line)
            continue
        break

    if kept:
        return "\n".join(kept) + suffix

    ellipsis = "..."
    return lines[0][: max(chars_limit - len(ellipsis), 0)] + ellipsis


def _claim_lines(claims: list[Phase7CompiledClaim]) -> list[str]:
    return [f"- {claim.compiled_text.strip()}" for claim in claims if claim.compiled_text.strip()]


def stable_memory_block_id(label: str, scope: str, scope_ref: str | None = None) -> str:
    return stable_phase7_id("block", label, scope, scope_ref)


@dataclass
class Phase7MemoryBlock:
    block_id: str
    label: MemoryBlockLabel
    description: str
    value: str
    scope: MemoryBlockScope
    read_only: bool
    chars_limit: int
    compiled_from_claim_ids: list[str] = field(default_factory=list)
    source_observation_ids: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    scope_ref: str | None = None
    generated_at: str | None = None
    schema_version: int = PHASE7D_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name in ("block_id", "label", "description", "value", "scope"):
            _validate_non_empty_string(name, getattr(self, name))
        if self.label not in MEMORY_BLOCK_LABELS:
            raise ValueError(f"label must be one of {sorted(MEMORY_BLOCK_LABELS)}")
        if self.scope not in MEMORY_BLOCK_SCOPES:
            raise ValueError(f"scope must be one of {sorted(MEMORY_BLOCK_SCOPES)}")
        if not self.read_only:
            raise ValueError("Phase 7D memory blocks must be read_only")
        if not isinstance(self.chars_limit, int) or not (
            MIN_BLOCK_CHAR_LIMIT <= self.chars_limit <= MAX_BLOCK_CHAR_LIMIT
        ):
            raise ValueError(
                f"chars_limit must be between {MIN_BLOCK_CHAR_LIMIT} and {MAX_BLOCK_CHAR_LIMIT}"
            )
        if len(self.value) > self.chars_limit:
            raise ValueError("value exceeds chars_limit")
        if self.schema_version != PHASE7D_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE7D_SCHEMA_VERSION}")
        if not self.source_paths:
            raise ValueError("memory blocks require source_paths for traceability")
        if self.label != "policy_pointer":
            if not self.compiled_from_claim_ids:
                raise ValueError("claim-backed memory blocks require compiled_from_claim_ids")
            if not self.source_observation_ids:
                raise ValueError("claim-backed memory blocks require source_observation_ids")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7MemoryBlock":
        return cls(
            block_id=data["block_id"],
            label=data["label"],
            description=data["description"],
            value=data["value"],
            scope=data["scope"],
            read_only=bool(data.get("read_only", False)),
            chars_limit=data["chars_limit"],
            compiled_from_claim_ids=list(data.get("compiled_from_claim_ids") or []),
            source_observation_ids=list(data.get("source_observation_ids") or []),
            source_paths=list(data.get("source_paths") or []),
            scope_ref=data.get("scope_ref"),
            generated_at=data.get("generated_at"),
            schema_version=data.get("schema_version", PHASE7D_SCHEMA_VERSION),
        )


def build_operator_profile_block(
    current_view: Phase7CurrentView,
    observations: list[Phase7Observation],
    *,
    generated_at: str,
    chars_limit: int = 2000,
) -> Phase7MemoryBlock | None:
    claims = _claims_for_block(current_view, "operator_profile")
    if not claims:
        return None
    observations_by_id = {observation.observation_id: observation for observation in observations}
    source_observation_ids = _support_observation_ids(claims)
    source_paths = _source_paths_for_observations(source_observation_ids, observations_by_id)
    return Phase7MemoryBlock(
        block_id=stable_memory_block_id("operator_profile", "operator"),
        label="operator_profile",
        description="Stable operator context derived from compiled current claims.",
        value=_bounded_lines(_claim_lines(claims), chars_limit),
        scope="operator",
        read_only=True,
        chars_limit=chars_limit,
        compiled_from_claim_ids=[claim.claim_id for claim in claims],
        source_observation_ids=source_observation_ids,
        source_paths=source_paths,
        generated_at=generated_at,
    )


def build_project_status_block(
    current_view: Phase7CurrentView,
    observations: list[Phase7Observation],
    *,
    generated_at: str,
    scope_ref: str | None = None,
    chars_limit: int = 2000,
) -> Phase7MemoryBlock | None:
    claims = _claims_for_block(current_view, "project_status", scope_ref=scope_ref)
    if not claims:
        return None
    observations_by_id = {observation.observation_id: observation for observation in observations}
    source_observation_ids = _support_observation_ids(claims)
    source_paths = _source_paths_for_observations(source_observation_ids, observations_by_id)
    block_scope_ref = scope_ref or claims[0].scope.get("project_id") or claims[0].scope.get("project")
    return Phase7MemoryBlock(
        block_id=stable_memory_block_id("project_status", "project", block_scope_ref),
        label="project_status",
        description="Current project status derived from compiled current claims.",
        value=_bounded_lines(_claim_lines(claims), chars_limit),
        scope="project",
        scope_ref=block_scope_ref,
        read_only=True,
        chars_limit=chars_limit,
        compiled_from_claim_ids=[claim.claim_id for claim in claims],
        source_observation_ids=source_observation_ids,
        source_paths=source_paths,
        generated_at=generated_at,
    )


def build_policy_pointer_block(
    *,
    source_paths: list[str],
    generated_at: str,
    chars_limit: int = 1200,
    scope_ref: str | None = "knowledge-system",
) -> Phase7MemoryBlock:
    paths = _dedupe(source_paths)
    lines = [
        "Procedural and policy memory lives in version-controlled files:",
        *[f"- {path}" for path in paths],
    ]
    return Phase7MemoryBlock(
        block_id=stable_memory_block_id("policy_pointer", "repo", scope_ref),
        label="policy_pointer",
        description="Read-only pointer to procedural and policy memory sources.",
        value=_bounded_lines(lines, chars_limit),
        scope="repo",
        scope_ref=scope_ref,
        read_only=True,
        chars_limit=chars_limit,
        compiled_from_claim_ids=[],
        source_observation_ids=[],
        source_paths=paths,
        generated_at=generated_at,
    )


def build_phase7d_memory_blocks(
    current_view: Phase7CurrentView,
    observations: list[Phase7Observation],
    *,
    generated_at: str,
    policy_source_paths: list[str],
    project_scope_ref: str | None = None,
    chars_limit: int = 2000,
) -> list[Phase7MemoryBlock]:
    blocks: list[Phase7MemoryBlock] = []
    operator_block = build_operator_profile_block(
        current_view,
        observations,
        generated_at=generated_at,
        chars_limit=chars_limit,
    )
    if operator_block:
        blocks.append(operator_block)
    project_block = build_project_status_block(
        current_view,
        observations,
        generated_at=generated_at,
        scope_ref=project_scope_ref,
        chars_limit=chars_limit,
    )
    if project_block:
        blocks.append(project_block)
    blocks.append(
        build_policy_pointer_block(
            source_paths=policy_source_paths,
            generated_at=generated_at,
        )
    )
    return blocks
