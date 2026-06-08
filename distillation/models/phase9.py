from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .phase8 import Phase8EvalCheck, Phase8EvalReport

PHASE9_SCHEMA_VERSION = 1
PHASE9_VALIDATION_GATE = "dream_outcome_quality"


@dataclass
class Phase9OutcomeRegression:
    check_id: str
    query: str
    reason: str
    pre_passed: bool
    post_passed: bool | None
    pre_actual: dict[str, Any] = field(default_factory=dict)
    post_actual: dict[str, Any] | None = None
    pre_issues: list[str] = field(default_factory=list)
    post_issues: list[str] | None = None
    schema_version: int = PHASE9_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase9OutcomeRegression":
        return cls(
            check_id=data["check_id"],
            query=data["query"],
            reason=data["reason"],
            pre_passed=bool(data["pre_passed"]),
            post_passed=(
                None if data.get("post_passed") is None else bool(data["post_passed"])
            ),
            pre_actual=dict(data.get("pre_actual") or {}),
            post_actual=(
                None
                if data.get("post_actual") is None
                else dict(data.get("post_actual") or {})
            ),
            pre_issues=list(data.get("pre_issues") or []),
            post_issues=(
                None
                if data.get("post_issues") is None
                else list(data.get("post_issues") or [])
            ),
            schema_version=int(data.get("schema_version", PHASE9_SCHEMA_VERSION)),
        )


@dataclass
class Phase9OutcomeGateReport:
    generated_at: str
    passed: bool
    rollback_required: bool
    rollback_reason: str | None
    regression_count: int
    regressions: list[Phase9OutcomeRegression]
    pre_report: Phase8EvalReport
    post_report: Phase8EvalReport
    proposal_id: str | None = None
    apply_mutation_id: str | None = None
    schema_version: int = PHASE9_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "passed": self.passed,
            "rollback_required": self.rollback_required,
            "rollback_reason": self.rollback_reason,
            "regression_count": self.regression_count,
            "regressions": [regression.to_dict() for regression in self.regressions],
            "pre_report": self.pre_report.to_dict(),
            "post_report": self.post_report.to_dict(),
            "proposal_id": self.proposal_id,
            "apply_mutation_id": self.apply_mutation_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase9OutcomeGateReport":
        return cls(
            generated_at=data["generated_at"],
            passed=bool(data["passed"]),
            rollback_required=bool(data["rollback_required"]),
            rollback_reason=data.get("rollback_reason"),
            regression_count=int(data["regression_count"]),
            regressions=[
                Phase9OutcomeRegression.from_dict(item)
                for item in data.get("regressions", [])
            ],
            pre_report=Phase8EvalReport.from_dict(data["pre_report"]),
            post_report=Phase8EvalReport.from_dict(data["post_report"]),
            proposal_id=data.get("proposal_id"),
            apply_mutation_id=data.get("apply_mutation_id"),
            schema_version=int(data.get("schema_version", PHASE9_SCHEMA_VERSION)),
        )


@dataclass
class Phase9RollbackRecommendation:
    required: bool
    ready: bool
    proposal_id: str | None
    apply_mutation_id: str | None
    rollback_mutation_id: str | None
    reason: str | None
    issues: list[str] = field(default_factory=list)
    operation_ids: list[str] | None = None
    schema_version: int = PHASE9_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase9RollbackRecommendation":
        return cls(
            required=bool(data["required"]),
            ready=bool(data["ready"]),
            proposal_id=data.get("proposal_id"),
            apply_mutation_id=data.get("apply_mutation_id"),
            rollback_mutation_id=data.get("rollback_mutation_id"),
            reason=data.get("reason"),
            issues=list(data.get("issues") or []),
            operation_ids=(
                None
                if data.get("operation_ids") is None
                else list(data.get("operation_ids") or [])
            ),
            schema_version=int(data.get("schema_version", PHASE9_SCHEMA_VERSION)),
        )


def evaluate_phase9_outcome_gate(
    pre_report: Phase8EvalReport,
    post_report: Phase8EvalReport,
    *,
    generated_at: str | None = None,
    proposal_id: str | None = None,
    apply_mutation_id: str | None = None,
    block_on_new_post_failures: bool = True,
) -> Phase9OutcomeGateReport:
    """Compare pre/post Phase 8 eval reports and flag outcome regressions."""
    timestamp = generated_at or _utc_now()

    if not pre_report.passed:
        return Phase9OutcomeGateReport(
            generated_at=timestamp,
            passed=False,
            rollback_required=False,
            rollback_reason="pre_outcome_baseline_failed",
            regression_count=0,
            regressions=[],
            pre_report=pre_report,
            post_report=post_report,
            proposal_id=proposal_id,
            apply_mutation_id=apply_mutation_id,
        )

    regressions = _find_phase9_regressions(
        pre_report,
        post_report,
        block_on_new_post_failures=block_on_new_post_failures,
    )
    regression_count = len(regressions)
    passed = post_report.passed and regression_count == 0
    rollback_required = regression_count > 0

    return Phase9OutcomeGateReport(
        generated_at=timestamp,
        passed=passed,
        rollback_required=rollback_required,
        rollback_reason=(
            "phase9_outcome_probe_regression" if rollback_required else None
        ),
        regression_count=regression_count,
        regressions=regressions,
        pre_report=pre_report,
        post_report=post_report,
        proposal_id=proposal_id,
        apply_mutation_id=apply_mutation_id,
    )


def run_phase9_outcome_gate_fixture(
    fixture: dict[str, Any],
) -> Phase9OutcomeGateReport:
    metadata = fixture.get("metadata") or {}
    return evaluate_phase9_outcome_gate(
        Phase8EvalReport.from_dict(fixture["pre_report"]),
        Phase8EvalReport.from_dict(fixture["post_report"]),
        generated_at=metadata.get("now_utc"),
        proposal_id=metadata.get("proposal_id"),
        apply_mutation_id=metadata.get("apply_mutation_id"),
    )


def build_phase9_validation_gate_details(
    report: Phase9OutcomeGateReport,
) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "gate": PHASE9_VALIDATION_GATE,
        "passed": report.passed,
        "rollback_required": report.rollback_required,
        "rollback_reason": report.rollback_reason,
        "proposal_id": report.proposal_id,
        "apply_mutation_id": report.apply_mutation_id,
        "pre_check_count": report.pre_report.check_count,
        "pre_failure_count": report.pre_report.failure_count,
        "post_check_count": report.post_report.check_count,
        "post_failure_count": report.post_report.failure_count,
        "regression_count": report.regression_count,
        "regressed_check_ids": [
            regression.check_id for regression in report.regressions
        ],
    }


def build_phase9_rollback_recommendation(
    report: Phase9OutcomeGateReport,
    *,
    rollback_mutation_id: str | None = None,
    operation_ids: list[str] | None = None,
) -> Phase9RollbackRecommendation:
    if not report.rollback_required:
        return Phase9RollbackRecommendation(
            required=False,
            ready=False,
            proposal_id=report.proposal_id,
            apply_mutation_id=report.apply_mutation_id,
            rollback_mutation_id=None,
            reason=None,
            issues=[],
            operation_ids=operation_ids,
        )

    issues: list[str] = []
    if not report.proposal_id:
        issues.append("missing_proposal_id")
    if not report.apply_mutation_id:
        issues.append("missing_apply_mutation_id")

    ready = len(issues) == 0
    resolved_rollback_mutation_id = rollback_mutation_id
    if ready and not resolved_rollback_mutation_id:
        resolved_rollback_mutation_id = _default_rollback_mutation_id(report)

    return Phase9RollbackRecommendation(
        required=True,
        ready=ready,
        proposal_id=report.proposal_id,
        apply_mutation_id=report.apply_mutation_id,
        rollback_mutation_id=resolved_rollback_mutation_id,
        reason=_rollback_reason(report),
        issues=issues,
        operation_ids=operation_ids,
    )


def _find_phase9_regressions(
    pre_report: Phase8EvalReport,
    post_report: Phase8EvalReport,
    *,
    block_on_new_post_failures: bool,
) -> list[Phase9OutcomeRegression]:
    regressions: list[Phase9OutcomeRegression] = []
    post_by_id = _checks_by_id(post_report.checks)

    for pre_check in pre_report.checks:
        post_check = post_by_id.get(pre_check.check_id)
        if post_check is None:
            regressions.append(
                _regression_from_checks(
                    pre_check,
                    None,
                    reason="check_missing_after_apply",
                )
            )
            continue
        if pre_check.passed and not post_check.passed:
            regressions.append(
                _regression_from_checks(
                    pre_check,
                    post_check,
                    reason="passed_pre_failed_post",
                )
            )

    if block_on_new_post_failures:
        pre_ids = {check.check_id for check in pre_report.checks}
        for post_check in post_report.checks:
            if post_check.check_id not in pre_ids and not post_check.passed:
                regressions.append(
                    Phase9OutcomeRegression(
                        check_id=post_check.check_id,
                        query=post_check.query,
                        reason="new_post_failure",
                        pre_passed=True,
                        post_passed=False,
                        pre_actual={},
                        post_actual=dict(post_check.actual),
                        pre_issues=[],
                        post_issues=list(post_check.issues),
                    )
                )

    return regressions


def _checks_by_id(
    checks: list[Phase8EvalCheck],
) -> dict[str, Phase8EvalCheck]:
    return {check.check_id: check for check in checks}


def _regression_from_checks(
    pre_check: Phase8EvalCheck,
    post_check: Phase8EvalCheck | None,
    *,
    reason: str,
) -> Phase9OutcomeRegression:
    return Phase9OutcomeRegression(
        check_id=pre_check.check_id,
        query=pre_check.query,
        reason=reason,
        pre_passed=pre_check.passed,
        post_passed=None if post_check is None else post_check.passed,
        pre_actual=dict(pre_check.actual),
        post_actual=None if post_check is None else dict(post_check.actual),
        pre_issues=list(pre_check.issues),
        post_issues=None if post_check is None else list(post_check.issues),
    )


def _rollback_reason(report: Phase9OutcomeGateReport) -> str | None:
    if not report.rollback_required:
        return None
    ids = ", ".join(regression.check_id for regression in report.regressions)
    return f"phase9_outcome_probe_regression: {ids}"


def _default_rollback_mutation_id(report: Phase9OutcomeGateReport) -> str:
    timestamp = _safe_token(report.generated_at)
    proposal = _safe_token(report.proposal_id or "proposal")
    apply = _safe_token(report.apply_mutation_id or "apply")
    return f"phase9_rollback_{proposal}_{apply}_{timestamp}"


def _safe_token(value: str) -> str:
    cleaned = "".join(
        char.lower() if char.isalnum() else "_"
        for char in value
    ).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned or "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

