"""
Phase 7 offline acceptance harness.

The harness validates that the Phase 7 layers work together as an outcome
contract: current facts stay visible, stale facts are excluded, provisional
facts obey TTL, compile operations grade safely, memory blocks are traceable,
and procedural memory does not leak into semantic current view.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .phase7 import Phase7CompiledClaim, Phase7Observation
from .phase7c import (
    Phase7CompileProposalOperation,
    Phase7CurrentView,
    generate_compiled_current_view,
    grade_phase7c_compile_operations,
)
from .phase7d import Phase7MemoryBlock, build_phase7d_memory_blocks

PHASE7_ACCEPTANCE_SCHEMA_VERSION = 1


@dataclass
class Phase7AcceptanceCheck:
    check_id: str
    axis: str
    passed: bool
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7AcceptanceCheck":
        return cls(
            check_id=data["check_id"],
            axis=data["axis"],
            passed=bool(data["passed"]),
            expected=dict(data.get("expected") or {}),
            actual=dict(data.get("actual") or {}),
            issues=list(data.get("issues") or []),
        )


@dataclass
class Phase7AcceptanceReport:
    generated_at: str
    passed: bool
    check_count: int
    failure_count: int
    checks: list[Phase7AcceptanceCheck]
    current_view: Phase7CurrentView
    memory_blocks: list[Phase7MemoryBlock]
    schema_version: int = PHASE7_ACCEPTANCE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "passed": self.passed,
            "check_count": self.check_count,
            "failure_count": self.failure_count,
            "checks": [check.to_dict() for check in self.checks],
            "current_view": self.current_view.to_dict(),
            "memory_blocks": [block.to_dict() for block in self.memory_blocks],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7AcceptanceReport":
        return cls(
            generated_at=data["generated_at"],
            passed=bool(data["passed"]),
            check_count=int(data["check_count"]),
            failure_count=int(data["failure_count"]),
            checks=[
                Phase7AcceptanceCheck.from_dict(item)
                for item in data.get("checks", [])
            ],
            current_view=Phase7CurrentView.from_dict(data["current_view"]),
            memory_blocks=[
                Phase7MemoryBlock.from_dict(item)
                for item in data.get("memory_blocks", [])
            ],
            schema_version=data.get("schema_version", PHASE7_ACCEPTANCE_SCHEMA_VERSION),
        )


def run_phase7_acceptance_fixture(fixture: dict[str, Any]) -> Phase7AcceptanceReport:
    observations = [
        Phase7Observation.from_dict(item)
        for item in fixture.get("observations", [])
    ]
    claims = [
        Phase7CompiledClaim.from_dict(item)
        for item in fixture.get("claims", [])
    ]
    operations = [
        Phase7CompileProposalOperation.from_dict(item)
        for item in fixture.get("compile_operations", [])
    ]
    metadata = fixture.get("metadata") or {}
    return run_phase7_acceptance(
        observations=observations,
        claims=claims,
        probes=list(fixture.get("probes") or []),
        now_utc=metadata["now_utc"],
        compile_operations=operations,
        policy_source_paths=list(fixture.get("policy_source_paths") or []),
        project_scope_ref=fixture.get("project_scope_ref"),
    )


def run_phase7_acceptance(
    *,
    observations: list[Phase7Observation],
    claims: list[Phase7CompiledClaim],
    probes: list[dict[str, Any]],
    now_utc: str,
    compile_operations: list[Phase7CompileProposalOperation | dict[str, Any]] | None = None,
    policy_source_paths: list[str] | None = None,
    project_scope_ref: str | None = None,
    block_chars_limit: int = 2000,
) -> Phase7AcceptanceReport:
    current_view = generate_compiled_current_view(
        observations,
        claims,
        now_utc=now_utc,
    )
    memory_blocks = (
        build_phase7d_memory_blocks(
            current_view,
            observations,
            generated_at=now_utc,
            policy_source_paths=list(policy_source_paths or []),
            project_scope_ref=project_scope_ref,
            chars_limit=block_chars_limit,
        )
        if policy_source_paths
        else []
    )
    compile_grade = grade_phase7c_compile_operations(
        list(compile_operations or []),
        observations=observations,
        claims=claims,
        now_utc=now_utc,
    )

    checks = [
        _evaluate_probe(
            probe,
            current_view=current_view,
            memory_blocks=memory_blocks,
            compile_grade=compile_grade.to_dict(),
        )
        for probe in probes
    ]
    failure_count = sum(1 for check in checks if not check.passed)
    return Phase7AcceptanceReport(
        generated_at=now_utc,
        passed=failure_count == 0,
        check_count=len(checks),
        failure_count=failure_count,
        checks=checks,
        current_view=current_view,
        memory_blocks=memory_blocks,
    )


def _evaluate_probe(
    probe: dict[str, Any],
    *,
    current_view: Phase7CurrentView,
    memory_blocks: list[Phase7MemoryBlock],
    compile_grade: dict[str, Any],
) -> Phase7AcceptanceCheck:
    kind = probe["kind"]
    check_id = probe["id"]
    axis = probe.get("axis", kind)
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    issues: list[str] = []

    current_ids = [claim.claim_id for claim in current_view.current_claims]
    provisional_ids = [claim.claim_id for claim in current_view.provisional_claims]
    excluded_reasons = {
        claim.claim_id: claim.reason
        for claim in current_view.excluded_claims
    }
    blocks_by_label = {block.label: block for block in memory_blocks}

    if kind == "current_contains":
        expected_ids = list(probe.get("expected_claim_ids") or [])
        expected["claim_ids"] = expected_ids
        actual["current_claim_ids"] = current_ids
        issues.extend(_missing_items(expected_ids, current_ids, "current_claim"))
    elif kind == "current_excludes":
        expected_ids = list(probe.get("claim_ids") or [])
        expected["claim_ids"] = expected_ids
        actual["current_claim_ids"] = current_ids
        unexpected = [claim_id for claim_id in expected_ids if claim_id in current_ids]
        issues.extend([f"claim unexpectedly current: {claim_id}" for claim_id in unexpected])
    elif kind == "provisional_contains":
        expected_ids = list(probe.get("expected_claim_ids") or [])
        expected["claim_ids"] = expected_ids
        actual["provisional_claim_ids"] = provisional_ids
        issues.extend(_missing_items(expected_ids, provisional_ids, "provisional_claim"))
    elif kind == "excluded_contains":
        expected_reasons = dict(probe.get("expected_reasons") or {})
        expected["excluded_reasons"] = expected_reasons
        actual["excluded_reasons"] = excluded_reasons
        for claim_id, reason in expected_reasons.items():
            actual_reason = excluded_reasons.get(claim_id)
            if actual_reason != reason:
                issues.append(f"excluded reason mismatch for {claim_id}: expected {reason}, got {actual_reason}")
    elif kind == "compile_grade_passes":
        expected_passed = bool(probe.get("expected_passed", True))
        expected["passed"] = expected_passed
        actual["passed"] = compile_grade.get("passed")
        actual["issues"] = compile_grade.get("issues", [])
        if compile_grade.get("passed") != expected_passed:
            issues.append(f"compile grade expected passed={expected_passed}")
    elif kind == "memory_blocks_include":
        expected_labels = list(probe.get("expected_labels") or [])
        actual_labels = [block.label for block in memory_blocks]
        expected["labels"] = expected_labels
        actual["labels"] = actual_labels
        issues.extend(_missing_items(expected_labels, actual_labels, "memory_block"))
    elif kind == "memory_block_claims":
        label = probe["label"]
        block = blocks_by_label.get(label)
        expected_ids = list(probe.get("expected_claim_ids") or [])
        actual_ids = list(block.compiled_from_claim_ids if block else [])
        expected["label"] = label
        expected["claim_ids"] = expected_ids
        actual["claim_ids"] = actual_ids
        if not block:
            issues.append(f"missing memory block: {label}")
        issues.extend(_missing_items(expected_ids, actual_ids, "block_claim"))
    elif kind == "memory_block_sources":
        label = probe["label"]
        block = blocks_by_label.get(label)
        expected_paths = list(probe.get("expected_source_paths") or [])
        expected_observations = list(probe.get("expected_source_observation_ids") or [])
        actual_paths = list(block.source_paths if block else [])
        actual_observations = list(block.source_observation_ids if block else [])
        expected["label"] = label
        expected["source_paths"] = expected_paths
        expected["source_observation_ids"] = expected_observations
        actual["source_paths"] = actual_paths
        actual["source_observation_ids"] = actual_observations
        if not block:
            issues.append(f"missing memory block: {label}")
        issues.extend(_missing_items(expected_paths, actual_paths, "source_path"))
        issues.extend(_missing_items(expected_observations, actual_observations, "source_observation"))
    elif kind == "policy_pointer_only":
        block = blocks_by_label.get("policy_pointer")
        expected_paths = list(probe.get("expected_source_paths") or [])
        actual_paths = list(block.source_paths if block else [])
        expected["source_paths"] = expected_paths
        actual["source_paths"] = actual_paths
        actual["compiled_from_claim_ids"] = list(block.compiled_from_claim_ids if block else [])
        actual["source_observation_ids"] = list(block.source_observation_ids if block else [])
        if not block:
            issues.append("missing memory block: policy_pointer")
        issues.extend(_missing_items(expected_paths, actual_paths, "policy_source_path"))
        if block and block.compiled_from_claim_ids:
            issues.append("policy pointer unexpectedly has compiled claim provenance")
        if block and block.source_observation_ids:
            issues.append("policy pointer unexpectedly has observation provenance")
    elif kind == "procedural_isolation":
        observation_ids = list(probe.get("observation_ids") or [])
        current_support = _support_ids(current_view.current_claims)
        provisional_support = _support_ids(current_view.provisional_claims)
        block_sources = [
            observation_id
            for block in memory_blocks
            for observation_id in block.source_observation_ids
        ]
        expected["observation_ids"] = observation_ids
        actual["current_support_observation_ids"] = current_support
        actual["provisional_support_observation_ids"] = provisional_support
        actual["block_source_observation_ids"] = block_sources
        for observation_id in observation_ids:
            if observation_id in current_support:
                issues.append(f"procedural observation is in current support: {observation_id}")
            if observation_id in provisional_support:
                issues.append(f"procedural observation is in provisional support: {observation_id}")
            if observation_id in block_sources:
                issues.append(f"procedural observation is in memory block sources: {observation_id}")
    else:
        issues.append(f"unsupported probe kind: {kind}")

    return Phase7AcceptanceCheck(
        check_id=check_id,
        axis=axis,
        passed=not issues,
        expected=expected,
        actual=actual,
        issues=issues,
    )


def _missing_items(expected: list[str], actual: list[str], label: str) -> list[str]:
    actual_set = set(actual)
    return [
        f"missing {label}: {item}"
        for item in expected
        if item not in actual_set
    ]


def _support_ids(claims: list[Phase7CompiledClaim]) -> list[str]:
    result: list[str] = []
    for claim in claims:
        for observation_id in claim.support_observation_ids:
            if observation_id not in result:
                result.append(observation_id)
    return result
