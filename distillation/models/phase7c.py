"""
Phase 7C offline compiled-current-view and compile-proposal helpers.

This module is intentionally pure. It models the current-view projection and
Dream compile proposal records without touching Redis, Vector, MCP, or live
Dream apply code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from .phase7 import (
    EDGE_DECISIONS,
    TAXONOMY_DECISIONS,
    Phase7CompiledClaim,
    Phase7Observation,
    compiled_claims_from_observations,
    normalize_claim_text,
    stable_phase7_id,
)
from .phase7b import (
    enrich_claim_temporal,
    enrich_claims_phase7b,
    enrich_observations_phase7b,
)

PHASE7C_SCHEMA_VERSION = 1

COMPILE_OPERATION_TYPES = {"compile_claim", "supersede_claim", "mark_current"}
NON_CURRENT_TEMPORAL_STATUSES = {"expired", "historical"}
NON_CURRENT_CLAIM_STATUSES = {
    "historical",
    "superseded",
    "contested",
    "stale",
    "deprecated",
    "expired",
}
PROVISIONAL_AUTHORITIES = {"explicit", "manual", "system"}


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_dateish(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return _parse_datetime(value).date()


def _now_datetime(now_utc: str) -> datetime:
    return _parse_datetime(now_utc)


def _claim_to_dict(claim: Phase7CompiledClaim | dict[str, Any] | None) -> dict[str, Any] | None:
    if claim is None:
        return None
    if isinstance(claim, Phase7CompiledClaim):
        return claim.to_dict()
    if isinstance(claim, dict):
        return dict(claim)
    raise ValueError(f"Unsupported proposed claim type: {type(claim).__name__}")


def _claim_from_dict(data: dict[str, Any] | Phase7CompiledClaim | None) -> Phase7CompiledClaim | None:
    if data is None:
        return None
    if isinstance(data, Phase7CompiledClaim):
        return data
    return Phase7CompiledClaim.from_dict(data)


def _append_issue(
    issues: list["Phase7CompileGradeIssue"],
    name: str,
    message: str,
    operation_id: str | None = None,
) -> None:
    issues.append(Phase7CompileGradeIssue(name=name, message=message, operation_id=operation_id))


def _sorted_claims(claims: list[Phase7CompiledClaim]) -> list[Phase7CompiledClaim]:
    return sorted(claims, key=lambda item: (item.subject_id, item.claim_id))


@dataclass
class Phase7ProjectionExcludedClaim:
    claim_id: str
    status: str
    reason: str
    temporal_status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7ProjectionExcludedClaim":
        return cls(
            claim_id=data["claim_id"],
            status=data["status"],
            reason=data["reason"],
            temporal_status=data.get("temporal_status", "unknown"),
        )


@dataclass
class Phase7CurrentView:
    generated_at: str
    current_claims: list[Phase7CompiledClaim]
    provisional_claims: list[Phase7CompiledClaim]
    excluded_claims: list[Phase7ProjectionExcludedClaim]
    observation_count: int
    claim_count: int
    schema_version: int = PHASE7C_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "current_claims": [claim.to_dict() for claim in self.current_claims],
            "provisional_claims": [claim.to_dict() for claim in self.provisional_claims],
            "excluded_claims": [claim.to_dict() for claim in self.excluded_claims],
            "observation_count": self.observation_count,
            "claim_count": self.claim_count,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7CurrentView":
        return cls(
            generated_at=data["generated_at"],
            current_claims=[
                Phase7CompiledClaim.from_dict(item)
                for item in data.get("current_claims", [])
            ],
            provisional_claims=[
                Phase7CompiledClaim.from_dict(item)
                for item in data.get("provisional_claims", [])
            ],
            excluded_claims=[
                Phase7ProjectionExcludedClaim.from_dict(item)
                for item in data.get("excluded_claims", [])
            ],
            observation_count=data["observation_count"],
            claim_count=data["claim_count"],
            schema_version=data.get("schema_version", PHASE7C_SCHEMA_VERSION),
        )


@dataclass
class Phase7CompileProposalOperation:
    operation_id: str
    type: str
    target_claim_id: str
    source_observation_id: str
    decision: str
    reason: str
    evidence: dict[str, Any]
    rollback: dict[str, Any]
    expected_revisions: dict[str, int]
    old_claim_id: str | None = None
    proposed_claim: dict[str, Any] | None = None
    scope_comparison: dict[str, Any] = field(default_factory=dict)
    temporal_comparison: dict[str, Any] = field(default_factory=dict)
    requires_operator_review: bool = False
    schema_version: int = PHASE7C_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id:
            raise ValueError("operation_id must be a non-empty string")
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("type must be a non-empty string")
        if not isinstance(self.target_claim_id, str) or not self.target_claim_id:
            raise ValueError("target_claim_id must be a non-empty string")
        if not isinstance(self.source_observation_id, str) or not self.source_observation_id:
            raise ValueError("source_observation_id must be a non-empty string")
        if not isinstance(self.decision, str) or not self.decision:
            raise ValueError("decision must be a non-empty string")
        if self.schema_version != PHASE7C_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE7C_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "type": self.type,
            "target_claim_id": self.target_claim_id,
            "source_observation_id": self.source_observation_id,
            "decision": self.decision,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "rollback": dict(self.rollback),
            "expected_revisions": dict(self.expected_revisions),
            "old_claim_id": self.old_claim_id,
            "proposed_claim": _claim_to_dict(self.proposed_claim),
            "scope_comparison": dict(self.scope_comparison),
            "temporal_comparison": dict(self.temporal_comparison),
            "requires_operator_review": self.requires_operator_review,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7CompileProposalOperation":
        return cls(
            operation_id=data["operation_id"],
            type=data["type"],
            target_claim_id=data["target_claim_id"],
            source_observation_id=data["source_observation_id"],
            decision=data["decision"],
            reason=data.get("reason", ""),
            evidence=dict(data.get("evidence") or {}),
            rollback=dict(data.get("rollback") or {}),
            expected_revisions=dict(data.get("expected_revisions") or {}),
            old_claim_id=data.get("old_claim_id"),
            proposed_claim=_claim_to_dict(data.get("proposed_claim")),
            scope_comparison=dict(data.get("scope_comparison") or {}),
            temporal_comparison=dict(data.get("temporal_comparison") or {}),
            requires_operator_review=bool(data.get("requires_operator_review", False)),
            schema_version=data.get("schema_version", PHASE7C_SCHEMA_VERSION),
        )


@dataclass
class Phase7CompileGradeIssue:
    name: str
    message: str
    operation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Phase7CompileGrade:
    status: str
    passed: bool
    hard_fail_count: int
    issues: list[Phase7CompileGradeIssue]
    graded_at: str
    rubric_version: str = "phase7c-deterministic-v1"
    schema_version: int = PHASE7C_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "hard_fail_count": self.hard_fail_count,
            "issues": [issue.to_dict() for issue in self.issues],
            "graded_at": self.graded_at,
            "rubric_version": self.rubric_version,
            "schema_version": self.schema_version,
        }


def generate_compiled_current_view(
    observations: list[Phase7Observation],
    claims: list[Phase7CompiledClaim],
    *,
    now_utc: str,
) -> Phase7CurrentView:
    """Generate the offline default retrieval projection for compiled claims."""
    now = _now_datetime(now_utc)
    enriched_observations = enrich_observations_phase7b(observations, now_utc=now_utc)
    enriched_claims = enrich_claims_phase7b(claims, enriched_observations, now_utc=now_utc)

    current_claims: list[Phase7CompiledClaim] = []
    provisional_claims: list[Phase7CompiledClaim] = []
    excluded_claims: list[Phase7ProjectionExcludedClaim] = []

    for claim in enriched_claims:
        if claim.status == "current":
            reason = _current_exclusion_reason(claim, now)
            if reason:
                excluded_claims.append(
                    Phase7ProjectionExcludedClaim(
                        claim_id=claim.claim_id,
                        status=claim.status,
                        reason=reason,
                        temporal_status=claim.temporal_status,
                    )
                )
            else:
                current_claims.append(claim)
            continue

        if claim.status == "pending_compile":
            if claim.ttl_expires_at and _parse_datetime(claim.ttl_expires_at) > now:
                provisional_claims.append(claim)
            else:
                excluded_claims.append(
                    Phase7ProjectionExcludedClaim(
                        claim_id=claim.claim_id,
                        status=claim.status,
                        reason="pending_compile_ttl_expired",
                        temporal_status=claim.temporal_status,
                    )
                )
            continue

        excluded_claims.append(
            Phase7ProjectionExcludedClaim(
                claim_id=claim.claim_id,
                status=claim.status,
                reason=f"status_not_current:{claim.status}",
                temporal_status=claim.temporal_status,
            )
        )

    return Phase7CurrentView(
        generated_at=now_utc,
        current_claims=_sorted_claims(current_claims),
        provisional_claims=_sorted_claims(provisional_claims),
        excluded_claims=sorted(excluded_claims, key=lambda item: item.claim_id),
        observation_count=len(observations),
        claim_count=len(claims),
    )


def _current_exclusion_reason(claim: Phase7CompiledClaim, now: datetime) -> str | None:
    if claim.status in NON_CURRENT_CLAIM_STATUSES:
        return f"status_not_current:{claim.status}"
    if claim.temporal_status in NON_CURRENT_TEMPORAL_STATUSES:
        return f"temporal_not_current:{claim.temporal_status}"
    if claim.valid_to and _parse_dateish(claim.valid_to) < now.date():
        return "valid_window_expired"
    return None


def build_compile_claim_operations_from_observations(
    observations: list[Phase7Observation],
    existing_claims: list[Phase7CompiledClaim],
    *,
    now_utc: str,
) -> list[Phase7CompileProposalOperation]:
    """Build deterministic compile operations for unsupported semantic observations."""
    supported_observation_ids = {
        observation_id
        for claim in existing_claims
        for observation_id in claim.support_observation_ids
    }
    candidates = [
        observation
        for observation in enrich_observations_phase7b(observations, now_utc=now_utc)
        if observation.memory_lane == "semantic"
        and observation.observation_id not in supported_observation_ids
    ]

    grouped: dict[tuple[str, str], list[Phase7Observation]] = {}
    for observation in candidates:
        key = (observation.subject_id, normalize_claim_text(observation.claim_text))
        grouped.setdefault(key, []).append(observation)

    operations: list[Phase7CompileProposalOperation] = []
    for group in grouped.values():
        claim = compiled_claims_from_observations(group, compiled_at=now_utc)[0]
        observations_by_id = {observation.observation_id: observation for observation in group}
        enriched_claim = enrich_claim_temporal(claim, observations_by_id, now_utc=now_utc)
        operations.append(
            build_compile_claim_operation(
                proposed_claim=enriched_claim,
                source_observation=group[0],
                expected_revision=0,
                decision="duplicate",
                reason="Dream can compile unsupported duplicate-group semantic observations into the current view.",
            )
        )
    return sorted(operations, key=lambda item: item.operation_id)


def build_compile_claim_operation(
    *,
    proposed_claim: Phase7CompiledClaim,
    source_observation: Phase7Observation,
    expected_revision: int,
    decision: str,
    reason: str,
) -> Phase7CompileProposalOperation:
    return Phase7CompileProposalOperation(
        operation_id=stable_phase7_id(
            "op",
            "compile_claim",
            proposed_claim.claim_id,
            proposed_claim.support_observation_ids,
        ),
        type="compile_claim",
        target_claim_id=proposed_claim.claim_id,
        source_observation_id=source_observation.observation_id,
        decision=decision,
        reason=reason,
        evidence={
            "source_observation_id": source_observation.observation_id,
            "source_authority": source_observation.source_authority,
            "support_observation_ids": list(proposed_claim.support_observation_ids),
            "source_path": source_observation.source_path,
        },
        rollback={
            "method": "delete_compiled_claim",
            "claim_id": proposed_claim.claim_id,
        },
        expected_revisions={proposed_claim.claim_id: expected_revision},
        proposed_claim=proposed_claim.to_dict(),
        scope_comparison={"same_scope": True, "scope": dict(proposed_claim.scope)},
        temporal_comparison={
            "temporal_status": proposed_claim.temporal_status,
            "valid_from": proposed_claim.valid_from,
            "valid_to": proposed_claim.valid_to,
        },
    )


def build_supersede_claim_operation(
    *,
    old_claim: Phase7CompiledClaim,
    proposed_claim: Phase7CompiledClaim,
    source_observation: Phase7Observation,
    decision: str,
    reason: str,
    old_expected_revision: int,
    new_expected_revision: int,
) -> Phase7CompileProposalOperation:
    return Phase7CompileProposalOperation(
        operation_id=stable_phase7_id(
            "op",
            "supersede_claim",
            old_claim.claim_id,
            proposed_claim.claim_id,
            source_observation.observation_id,
        ),
        type="supersede_claim",
        target_claim_id=proposed_claim.claim_id,
        old_claim_id=old_claim.claim_id,
        source_observation_id=source_observation.observation_id,
        decision=decision,
        reason=reason,
        evidence={
            "source_observation_id": source_observation.observation_id,
            "source_authority": source_observation.source_authority,
            "old_claim_id": old_claim.claim_id,
            "new_claim_id": proposed_claim.claim_id,
        },
        rollback={
            "method": "restore_claim_pair",
            "old_claim_id": old_claim.claim_id,
            "new_claim_id": proposed_claim.claim_id,
        },
        expected_revisions={
            old_claim.claim_id: old_expected_revision,
            proposed_claim.claim_id: new_expected_revision,
        },
        proposed_claim=proposed_claim.to_dict(),
        scope_comparison={
            "old_scope": dict(old_claim.scope),
            "new_scope": dict(proposed_claim.scope),
            "same_subject": old_claim.subject_id == proposed_claim.subject_id,
        },
        temporal_comparison={
            "old_temporal_status": old_claim.temporal_status,
            "new_temporal_status": proposed_claim.temporal_status,
            "old_valid_to": old_claim.valid_to,
            "new_valid_from": proposed_claim.valid_from,
        },
    )


def build_mark_current_operation(
    *,
    pending_claim: Phase7CompiledClaim,
    source_observation: Phase7Observation,
    now_utc: str,
    expected_revision: int,
    conflict_check_result: str = "clear",
) -> Phase7CompileProposalOperation:
    proposed_claim = replace(
        pending_claim,
        status="current",
        ttl_expires_at=None,
        taxonomy_decision=pending_claim.taxonomy_decision or "duplicate",
        compiled_at=now_utc,
        compiled_by="dream_compile",
    )
    return Phase7CompileProposalOperation(
        operation_id=stable_phase7_id(
            "op",
            "mark_current",
            pending_claim.claim_id,
            source_observation.observation_id,
        ),
        type="mark_current",
        target_claim_id=pending_claim.claim_id,
        source_observation_id=source_observation.observation_id,
        decision=proposed_claim.taxonomy_decision or "duplicate",
        reason="Dream reconciled an unexpired provisional claim into the compiled current view.",
        evidence={
            "source_observation_id": source_observation.observation_id,
            "source_authority": source_observation.source_authority,
            "ttl_expires_at": pending_claim.ttl_expires_at,
            "conflict_check_result": conflict_check_result,
        },
        rollback={
            "method": "restore_pending_compile",
            "claim_id": pending_claim.claim_id,
            "ttl_expires_at": pending_claim.ttl_expires_at,
        },
        expected_revisions={pending_claim.claim_id: expected_revision},
        proposed_claim=proposed_claim.to_dict(),
        scope_comparison={"scope": dict(proposed_claim.scope)},
        temporal_comparison={
            "temporal_status": proposed_claim.temporal_status,
            "valid_from": proposed_claim.valid_from,
            "valid_to": proposed_claim.valid_to,
        },
    )


def grade_phase7c_compile_operations(
    operations: list[Phase7CompileProposalOperation | dict[str, Any]],
    *,
    observations: list[Phase7Observation],
    claims: list[Phase7CompiledClaim],
    now_utc: str,
    rubric_version: str = "phase7c-deterministic-v1",
) -> Phase7CompileGrade:
    issues: list[Phase7CompileGradeIssue] = []
    seen_operation_ids: set[str] = set()
    observations_by_id = {observation.observation_id: observation for observation in observations}
    claims_by_id = {claim.claim_id: claim for claim in claims}
    now = _now_datetime(now_utc)

    for raw_operation in operations:
        operation = _operation_to_dict(raw_operation)
        operation_id = operation.get("operation_id") if isinstance(operation.get("operation_id"), str) else None
        if not operation_id:
            _append_issue(issues, "missing_operation_id", "Operation is missing operation_id.")
        elif operation_id in seen_operation_ids:
            _append_issue(issues, "duplicate_operation_id", "Operation id is duplicated.", operation_id)
        else:
            seen_operation_ids.add(operation_id)

        operation_type = operation.get("type")
        if operation_type not in COMPILE_OPERATION_TYPES:
            _append_issue(
                issues,
                "unsupported_operation_type",
                f"Unsupported operation type {operation_type}.",
                operation_id,
            )

        if not isinstance(operation.get("reason"), str) or not operation["reason"].strip():
            _append_issue(issues, "missing_reason", "Operation is missing a reason.", operation_id)
        if not isinstance(operation.get("rollback"), dict) or not operation["rollback"]:
            _append_issue(issues, "missing_rollback_metadata", "Operation is missing rollback metadata.", operation_id)
        if not isinstance(operation.get("evidence"), dict) or not operation["evidence"]:
            _append_issue(issues, "missing_evidence", "Operation is missing evidence.", operation_id)

        source_observation_id = operation.get("source_observation_id")
        source_observation = observations_by_id.get(source_observation_id) if isinstance(source_observation_id, str) else None
        if not source_observation:
            _append_issue(issues, "missing_source_observation", "Operation does not cite a known source observation.", operation_id)
        elif source_observation.memory_lane != "semantic":
            _append_issue(
                issues,
                "procedural_memory_semantic_mutation",
                "Procedural observations cannot be mutated through the semantic compiler.",
                operation_id,
            )

        decision = operation.get("decision")
        if decision not in TAXONOMY_DECISIONS:
            _append_issue(issues, "invalid_taxonomy_decision", f"Invalid taxonomy decision {decision}.", operation_id)
        if decision == "contestation" and not operation.get("requires_operator_review"):
            _append_issue(
                issues,
                "contest_without_operator_review",
                "Contestation compile operations require operator review.",
                operation_id,
            )

        proposed_claim = _safe_claim_from_operation(operation, issues, operation_id)
        touched_claim_ids = _touched_claim_ids(operation)
        expected_revisions = operation.get("expected_revisions") if isinstance(operation.get("expected_revisions"), dict) else {}
        for claim_id in touched_claim_ids:
            revision = expected_revisions.get(claim_id)
            if not isinstance(revision, int) or revision < 0:
                _append_issue(
                    issues,
                    "missing_expected_revision",
                    f"Operation is missing expected revision for {claim_id}.",
                    operation_id,
                )

        if operation_type == "compile_claim":
            _grade_compile_claim_operation(operation, proposed_claim, source_observation, issues, operation_id)
        elif operation_type == "supersede_claim":
            _grade_supersede_claim_operation(
                operation,
                proposed_claim,
                source_observation,
                claims_by_id,
                issues,
                operation_id,
            )
        elif operation_type == "mark_current":
            _grade_mark_current_operation(
                operation,
                proposed_claim,
                source_observation,
                claims_by_id,
                now,
                issues,
                operation_id,
            )

    passed = not issues
    return Phase7CompileGrade(
        status="passed" if passed else "failed",
        passed=passed,
        hard_fail_count=len(issues),
        issues=issues,
        graded_at=now_utc,
        rubric_version=rubric_version,
    )


def _operation_to_dict(operation: Phase7CompileProposalOperation | dict[str, Any]) -> dict[str, Any]:
    if isinstance(operation, Phase7CompileProposalOperation):
        return operation.to_dict()
    if isinstance(operation, dict):
        return dict(operation)
    raise ValueError(f"Unsupported operation type: {type(operation).__name__}")


def _safe_claim_from_operation(
    operation: dict[str, Any],
    issues: list[Phase7CompileGradeIssue],
    operation_id: str | None,
) -> Phase7CompiledClaim | None:
    proposed_claim_data = operation.get("proposed_claim")
    if not isinstance(proposed_claim_data, dict):
        _append_issue(issues, "missing_proposed_claim", "Operation is missing proposed_claim.", operation_id)
        return None
    try:
        return Phase7CompiledClaim.from_dict(proposed_claim_data)
    except Exception as exc:  # noqa: BLE001 - grade should report and continue.
        _append_issue(issues, "invalid_proposed_claim", str(exc), operation_id)
        return None


def _touched_claim_ids(operation: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for field_name in ("old_claim_id", "target_claim_id"):
        value = operation.get(field_name)
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    return ids


def _grade_compile_claim_operation(
    operation: dict[str, Any],
    proposed_claim: Phase7CompiledClaim | None,
    source_observation: Phase7Observation | None,
    issues: list[Phase7CompileGradeIssue],
    operation_id: str | None,
) -> None:
    if not proposed_claim or not source_observation:
        return
    if proposed_claim.claim_id != operation.get("target_claim_id"):
        _append_issue(issues, "target_claim_mismatch", "target_claim_id does not match proposed_claim.claim_id.", operation_id)
    if proposed_claim.status != "current":
        _append_issue(issues, "compile_claim_not_current", "compile_claim must propose a current claim.", operation_id)
    if source_observation.observation_id not in proposed_claim.support_observation_ids:
        _append_issue(
            issues,
            "source_observation_not_supporting_claim",
            "Proposed claim does not include the source observation as support.",
            operation_id,
        )
    if operation.get("decision") == "scoped_exception" and not proposed_claim.scope:
        _append_issue(issues, "scoped_exception_without_scope", "Scoped exceptions require explicit scope fields.", operation_id)


def _grade_supersede_claim_operation(
    operation: dict[str, Any],
    proposed_claim: Phase7CompiledClaim | None,
    source_observation: Phase7Observation | None,
    claims_by_id: dict[str, Phase7CompiledClaim],
    issues: list[Phase7CompileGradeIssue],
    operation_id: str | None,
) -> None:
    if not proposed_claim or not source_observation:
        return
    old_claim_id = operation.get("old_claim_id")
    old_claim = claims_by_id.get(old_claim_id) if isinstance(old_claim_id, str) else None
    if not old_claim:
        _append_issue(issues, "missing_old_claim", "supersede_claim requires a known old_claim_id.", operation_id)
        return
    if proposed_claim.claim_id != operation.get("target_claim_id"):
        _append_issue(issues, "target_claim_mismatch", "target_claim_id does not match proposed_claim.claim_id.", operation_id)
    if operation.get("decision") not in EDGE_DECISIONS:
        _append_issue(
            issues,
            "edge_illegal_decision",
            "supersede_claim decisions must be edge-legal taxonomy decisions.",
            operation_id,
        )
    evidence = operation.get("evidence") if isinstance(operation.get("evidence"), dict) else {}
    if old_claim.subject_id != proposed_claim.subject_id and evidence.get("resolved_entity_match") is not True:
        _append_issue(
            issues,
            "supersession_without_same_subject_or_entity",
            "supersede_claim requires the same subject or resolved entity match.",
            operation_id,
        )
    if source_observation.observation_id not in proposed_claim.support_observation_ids:
        _append_issue(
            issues,
            "source_observation_not_supporting_claim",
            "Proposed claim does not include the source observation as support.",
            operation_id,
        )
    if operation.get("decision") == "refinement":
        old_support = set(old_claim.support_observation_ids)
        new_support = set(proposed_claim.support_observation_ids)
        if not old_support.issubset(new_support):
            _append_issue(
                issues,
                "refinement_drops_old_support",
                "Refinement must preserve old support evidence.",
                operation_id,
            )
    if operation.get("decision") == "temporal_expiry":
        has_temporal_window = any(
            [
                old_claim.valid_from,
                old_claim.valid_to,
                proposed_claim.valid_from,
                proposed_claim.valid_to,
                source_observation.valid_from,
                source_observation.valid_to,
            ]
        )
        if not has_temporal_window:
            _append_issue(
                issues,
                "temporal_expiry_without_resolved_window",
                "Temporal expiry requires a resolved date or validity window.",
                operation_id,
            )
    if operation.get("decision") == "scoped_exception" and not proposed_claim.scope:
        _append_issue(issues, "scoped_exception_without_scope", "Scoped exceptions require explicit scope fields.", operation_id)


def _grade_mark_current_operation(
    operation: dict[str, Any],
    proposed_claim: Phase7CompiledClaim | None,
    source_observation: Phase7Observation | None,
    claims_by_id: dict[str, Phase7CompiledClaim],
    now: datetime,
    issues: list[Phase7CompileGradeIssue],
    operation_id: str | None,
) -> None:
    if not proposed_claim or not source_observation:
        return
    target_claim_id = operation.get("target_claim_id")
    pending_claim = claims_by_id.get(target_claim_id) if isinstance(target_claim_id, str) else None
    if not pending_claim:
        _append_issue(issues, "missing_pending_claim", "mark_current requires a known target pending claim.", operation_id)
        return
    if pending_claim.status != "pending_compile":
        _append_issue(issues, "mark_current_target_not_pending", "mark_current target must be pending_compile.", operation_id)
    if not pending_claim.ttl_expires_at:
        _append_issue(issues, "mark_current_missing_ttl", "mark_current target must have ttl_expires_at.", operation_id)
    elif _parse_datetime(pending_claim.ttl_expires_at) <= now:
        _append_issue(issues, "mark_current_ttl_expired", "Expired provisional claim cannot be marked current.", operation_id)
    if proposed_claim.claim_id != pending_claim.claim_id:
        _append_issue(issues, "target_claim_mismatch", "mark_current must preserve the pending claim id.", operation_id)
    if proposed_claim.status != "current":
        _append_issue(issues, "mark_current_not_current", "mark_current must propose status=current.", operation_id)
    if source_observation.source_authority not in PROVISIONAL_AUTHORITIES:
        _append_issue(
            issues,
            "disallowed_source_authority",
            "mark_current can promote only explicit, manual, or scoped system observations.",
            operation_id,
        )
    if source_observation.source_authority == "system" and not source_observation.scope and not proposed_claim.scope:
        _append_issue(
            issues,
            "system_mark_current_without_scope",
            "System observations need exact scope before mark_current.",
            operation_id,
        )
    evidence = operation.get("evidence") if isinstance(operation.get("evidence"), dict) else {}
    if not evidence.get("conflict_check_result"):
        _append_issue(
            issues,
            "missing_conflict_check_result",
            "mark_current requires a conflict check result.",
            operation_id,
        )
