from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase7_acceptance_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    Phase7AcceptanceReport,
    Phase7CompiledClaim,
    Phase7Observation,
    run_phase7_acceptance,
    run_phase7_acceptance_fixture,
)


class Phase7AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())

    def run_fixture(self, fixture: dict | None = None):
        return run_phase7_acceptance_fixture(fixture or self.fixture)

    def check(self, report, check_id: str):
        return next(check for check in report.checks if check.check_id == check_id)

    def test_acceptance_fixture_passes(self) -> None:
        report = self.run_fixture()
        self.assertTrue(report.passed, report.to_dict())
        self.assertEqual(report.check_count, len(self.fixture["probes"]))
        self.assertEqual(report.failure_count, 0)

    def test_acceptance_report_round_trips(self) -> None:
        report = self.run_fixture()
        self.assertEqual(Phase7AcceptanceReport.from_dict(report.to_dict()), report)

    def test_acceptance_report_exposes_current_provisional_and_excluded_sets(self) -> None:
        report = self.run_fixture()
        self.assertEqual(
            {claim.claim_id for claim in report.current_view.current_claims},
            {"claim_accept_operator", "claim_accept_project"},
        )
        self.assertEqual(
            [claim.claim_id for claim in report.current_view.provisional_claims],
            ["claim_accept_pending"],
        )
        excluded = {claim.claim_id: claim.reason for claim in report.current_view.excluded_claims}
        self.assertEqual(
            excluded["claim_accept_temporal_expired"],
            "temporal_not_current:expired",
        )

    def test_acceptance_report_exposes_memory_blocks(self) -> None:
        report = self.run_fixture()
        labels = [block.label for block in report.memory_blocks]
        self.assertEqual(labels, ["operator_profile", "project_status", "policy_pointer"])
        operator = next(block for block in report.memory_blocks if block.label == "operator_profile")
        self.assertEqual(operator.compiled_from_claim_ids, ["claim_accept_operator"])
        self.assertEqual(operator.source_observation_ids, ["obs_accept_operator"])

    def test_missing_current_claim_fails_probe(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["claims"] = [
            claim for claim in fixture["claims"]
            if claim["claim_id"] != "claim_accept_project"
        ]
        report = self.run_fixture(fixture)
        self.assertFalse(report.passed)
        check = self.check(report, "current_claims_preserved")
        self.assertFalse(check.passed)
        self.assertIn("missing current_claim: claim_accept_project", check.issues)

    def test_stale_temporal_claim_current_fails_probe(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        for claim in fixture["claims"]:
            if claim["claim_id"] == "claim_accept_temporal_expired":
                claim["compiled_text"] = "The Phase 7 acceptance review is complete."
        for observation in fixture["observations"]:
            if observation["observation_id"] == "obs_accept_temporal_expired":
                observation["claim_text"] = "The Phase 7 acceptance review is complete."
        report = self.run_fixture(fixture)
        self.assertFalse(report.passed)
        reason_check = self.check(report, "stale_claims_excluded")
        current_check = self.check(report, "excluded_claims_not_current")
        self.assertFalse(reason_check.passed)
        self.assertFalse(current_check.passed)
        self.assertIn(
            "claim unexpectedly current: claim_accept_temporal_expired",
            current_check.issues,
        )

    def test_compile_grade_regression_fails_probe(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["compile_operations"][0]["rollback"] = {}
        report = self.run_fixture(fixture)
        self.assertFalse(report.passed)
        check = self.check(report, "compile_operations_grade")
        self.assertFalse(check.passed)
        self.assertIn("compile grade expected passed=True", check.issues)

    def test_procedural_leak_fails_probe(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["claims"].append(
            {
                "claim_id": "claim_bad_procedural",
                "subject_id": "subject-procedural",
                "memory_lane": "semantic",
                "compiled_text": "Procedural memory leaked into semantic current view.",
                "status": "current",
                "support_observation_ids": ["obs_accept_procedural"],
                "primary_source_authority": "manual"
            }
        )
        report = self.run_fixture(fixture)
        self.assertFalse(report.passed)
        check = self.check(report, "procedural_observation_not_compiled")
        self.assertFalse(check.passed)
        self.assertIn(
            "procedural observation is in current support: obs_accept_procedural",
            check.issues,
        )

    def test_run_phase7_acceptance_can_run_without_blocks(self) -> None:
        observations = [
            Phase7Observation.from_dict(item)
            for item in self.fixture["observations"]
        ]
        claims = [
            Phase7CompiledClaim.from_dict(item)
            for item in self.fixture["claims"]
        ]
        report = run_phase7_acceptance(
            observations=observations,
            claims=claims,
            probes=[
                {
                    "id": "current_only",
                    "kind": "current_contains",
                    "expected_claim_ids": ["claim_accept_operator"],
                }
            ],
            now_utc=self.fixture["metadata"]["now_utc"],
        )
        self.assertTrue(report.passed, report.to_dict())
        self.assertEqual(report.memory_blocks, [])


if __name__ == "__main__":
    unittest.main()
