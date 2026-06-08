from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
PHASE8_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase8_retrieval_fixture.json"
PHASE9_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase9_outcome_gate_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    PHASE9_VALIDATION_GATE,
    Phase8EvalReport,
    Phase9OutcomeGateReport,
    Phase9RollbackRecommendation,
    build_phase9_rollback_recommendation,
    build_phase9_validation_gate_details,
    evaluate_phase9_outcome_gate,
    run_phase8_retrieval_fixture,
    run_phase9_outcome_gate_fixture,
)


class Phase9OutcomeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.phase8_fixture = json.loads(PHASE8_FIXTURE_PATH.read_text())
        cls.phase9_fixture = json.loads(PHASE9_FIXTURE_PATH.read_text())

    def test_fixture_gate_passes_and_round_trips(self) -> None:
        report = run_phase9_outcome_gate_fixture(self.phase9_fixture)
        expected = self.phase9_fixture["expected"]

        self.assertEqual(report.passed, expected["passed"])
        self.assertEqual(report.rollback_required, expected["rollback_required"])
        self.assertEqual(report.regression_count, expected["regression_count"])
        self.assertEqual(Phase9OutcomeGateReport.from_dict(report.to_dict()), report)

    def test_identical_phase8_reports_pass_gate(self) -> None:
        phase8_report = run_phase8_retrieval_fixture(self.phase8_fixture)
        gate = evaluate_phase9_outcome_gate(
            phase8_report,
            Phase8EvalReport.from_dict(phase8_report.to_dict()),
            generated_at=self.phase8_fixture["metadata"]["now_utc"],
            proposal_id="dream_phase9_noop",
            apply_mutation_id="apply_phase9_noop",
        )

        self.assertTrue(gate.passed, gate.to_dict())
        self.assertFalse(gate.rollback_required)
        self.assertEqual(gate.regression_count, 0)

    def test_actual_phase8_retrieval_regression_requires_rollback(self) -> None:
        pre_report = run_phase8_retrieval_fixture(self.phase8_fixture)
        post_fixture = copy.deepcopy(self.phase8_fixture)
        for claim in post_fixture["claims"]:
            if claim["claim_id"] == "claim_current_runtime":
                claim["status"] = "historical"
                claim["temporal_status"] = "historical"
                claim["valid_from"] = "2026-03-01"
                claim["valid_to"] = "2026-04-30"

        post_report = run_phase8_retrieval_fixture(post_fixture)
        gate = evaluate_phase9_outcome_gate(
            pre_report,
            post_report,
            generated_at=self.phase8_fixture["metadata"]["now_utc"],
            proposal_id="dream_phase9_regression",
            apply_mutation_id="apply_phase9_regression",
        )

        self.assertFalse(post_report.passed)
        self.assertFalse(gate.passed)
        self.assertTrue(gate.rollback_required)
        self.assertEqual(gate.rollback_reason, "phase9_outcome_probe_regression")
        self.assertEqual(gate.regression_count, 1)
        self.assertEqual(
            gate.regressions[0].check_id,
            "current_runtime_prefers_compiled_current",
        )
        self.assertEqual(gate.regressions[0].reason, "passed_pre_failed_post")

    def test_rollback_recommendation_targets_existing_dream_rollback_contract(self) -> None:
        pre_report = run_phase8_retrieval_fixture(self.phase8_fixture)
        post_report = Phase8EvalReport.from_dict(pre_report.to_dict())
        post_check = post_report.checks[0]
        post_check.passed = False
        post_check.issues = ["top result mismatch: fixture regression"]
        post_report.passed = False
        post_report.failure_count = 1
        gate = evaluate_phase9_outcome_gate(
            pre_report,
            post_report,
            generated_at="2026-06-07T00:00:00+00:00",
            proposal_id="dream_phase9_recommendation",
            apply_mutation_id="apply_phase9_recommendation",
        )

        recommendation = build_phase9_rollback_recommendation(gate)

        self.assertTrue(recommendation.required)
        self.assertTrue(recommendation.ready)
        self.assertEqual(recommendation.proposal_id, "dream_phase9_recommendation")
        self.assertEqual(
            recommendation.apply_mutation_id,
            "apply_phase9_recommendation",
        )
        self.assertTrue(
            recommendation.rollback_mutation_id.startswith(
                "phase9_rollback_dream_phase9_recommendation_"
            )
        )
        self.assertIn("current_runtime_prefers_compiled_current", recommendation.reason)
        self.assertEqual(
            Phase9RollbackRecommendation.from_dict(recommendation.to_dict()),
            recommendation,
        )

    def test_missing_apply_identity_blocks_ready_rollback_recommendation(self) -> None:
        pre_report = run_phase8_retrieval_fixture(self.phase8_fixture)
        post_report = Phase8EvalReport.from_dict(pre_report.to_dict())
        post_report.checks[0].passed = False
        post_report.passed = False
        post_report.failure_count = 1
        gate = evaluate_phase9_outcome_gate(pre_report, post_report)

        recommendation = build_phase9_rollback_recommendation(gate)

        self.assertTrue(recommendation.required)
        self.assertFalse(recommendation.ready)
        self.assertEqual(
            recommendation.issues,
            ["missing_proposal_id", "missing_apply_mutation_id"],
        )

    def test_pre_existing_probe_failure_does_not_recommend_rollback(self) -> None:
        pre_fixture = copy.deepcopy(self.phase8_fixture)
        pre_fixture["probes"][0]["expected_top_candidate_id"] = "claim_phase8_status"
        pre_report = run_phase8_retrieval_fixture(pre_fixture)
        post_report = run_phase8_retrieval_fixture(self.phase8_fixture)

        gate = evaluate_phase9_outcome_gate(pre_report, post_report)

        self.assertFalse(pre_report.passed)
        self.assertFalse(gate.passed)
        self.assertFalse(gate.rollback_required)
        self.assertEqual(gate.rollback_reason, "pre_outcome_baseline_failed")
        self.assertEqual(gate.regression_count, 0)

    def test_missing_post_check_is_a_regression(self) -> None:
        pre_report = run_phase8_retrieval_fixture(self.phase8_fixture)
        post_report = Phase8EvalReport.from_dict(pre_report.to_dict())
        post_report.checks = post_report.checks[1:]
        post_report.check_count -= 1

        gate = evaluate_phase9_outcome_gate(pre_report, post_report)

        self.assertFalse(gate.passed)
        self.assertTrue(gate.rollback_required)
        self.assertEqual(gate.regressions[0].reason, "check_missing_after_apply")
        self.assertEqual(
            gate.regressions[0].check_id,
            "current_runtime_prefers_compiled_current",
        )

    def test_new_post_failure_is_a_regression_when_pre_baseline_is_clean(self) -> None:
        pre_report = run_phase8_retrieval_fixture(self.phase8_fixture)
        post_report = Phase8EvalReport.from_dict(pre_report.to_dict())
        new_check = copy.deepcopy(post_report.checks[0])
        new_check.check_id = "new_outcome_probe"
        new_check.passed = False
        new_check.issues = ["new fixture failure"]
        post_report.checks.append(new_check)
        post_report.check_count += 1
        post_report.failure_count = 1
        post_report.passed = False

        gate = evaluate_phase9_outcome_gate(pre_report, post_report)

        self.assertFalse(gate.passed)
        self.assertTrue(gate.rollback_required)
        self.assertEqual(gate.regressions[0].check_id, "new_outcome_probe")
        self.assertEqual(gate.regressions[0].reason, "new_post_failure")

    def test_validation_gate_details_are_compact_and_ledger_ready(self) -> None:
        report = run_phase9_outcome_gate_fixture(self.phase9_fixture)
        details = build_phase9_validation_gate_details(report)

        self.assertEqual(details["gate"], PHASE9_VALIDATION_GATE)
        self.assertTrue(details["passed"])
        self.assertFalse(details["rollback_required"])
        self.assertEqual(details["pre_failure_count"], 0)
        self.assertEqual(details["post_failure_count"], 0)
        self.assertEqual(details["regressed_check_ids"], [])


if __name__ == "__main__":
    unittest.main()

