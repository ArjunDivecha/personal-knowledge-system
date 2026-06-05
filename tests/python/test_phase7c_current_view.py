from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase7c_current_projection_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    Phase7CompileProposalOperation,
    Phase7CompiledClaim,
    Phase7CurrentView,
    Phase7Observation,
    build_compile_claim_operation,
    build_compile_claim_operations_from_observations,
    build_mark_current_operation,
    build_supersede_claim_operation,
    generate_compiled_current_view,
    grade_phase7c_compile_operations,
)


class Phase7CCurrentViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())
        cls.now_utc = cls.fixture["metadata"]["now_utc"]
        cls.observations = [
            Phase7Observation.from_dict(item)
            for item in cls.fixture["observations"]
        ]
        cls.claims = [
            Phase7CompiledClaim.from_dict(item)
            for item in cls.fixture["claims"]
        ]
        cls.operations = [
            Phase7CompileProposalOperation.from_dict(item)
            for item in cls.fixture["proposal_operations"]
        ]

    def observation(self, observation_id: str) -> Phase7Observation:
        return next(item for item in self.observations if item.observation_id == observation_id)

    def claim(self, claim_id: str) -> Phase7CompiledClaim:
        return next(item for item in self.claims if item.claim_id == claim_id)

    def grade_issue_names(self, operations) -> set[str]:
        grade = grade_phase7c_compile_operations(
            operations,
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        return {issue.name for issue in grade.issues}

    def test_current_view_projection_matches_fixture(self) -> None:
        current_view = generate_compiled_current_view(
            self.observations,
            self.claims,
            now_utc=self.now_utc,
        )
        expected = self.fixture["expected_projection"]
        self.assertEqual(
            [claim.claim_id for claim in current_view.current_claims],
            expected["current_claim_ids"],
        )
        self.assertEqual(
            [claim.claim_id for claim in current_view.provisional_claims],
            expected["provisional_claim_ids"],
        )
        excluded = {claim.claim_id: claim.reason for claim in current_view.excluded_claims}
        self.assertEqual(excluded, expected["excluded_reasons"])

    def test_current_view_round_trips(self) -> None:
        current_view = generate_compiled_current_view(
            self.observations,
            self.claims,
            now_utc=self.now_utc,
        )
        self.assertEqual(Phase7CurrentView.from_dict(current_view.to_dict()), current_view)

    def test_operation_fixture_round_trips(self) -> None:
        for operation in self.operations:
            self.assertEqual(
                Phase7CompileProposalOperation.from_dict(operation.to_dict()),
                operation,
            )

    def test_fixture_operations_pass_grade(self) -> None:
        grade = grade_phase7c_compile_operations(
            self.operations,
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        self.assertTrue(grade.passed, grade.to_dict())
        self.assertEqual(grade.hard_fail_count, 0)

    def test_compile_builder_groups_duplicate_unsupported_observations(self) -> None:
        observations = [
            self.observation("obs_compile_a"),
            self.observation("obs_compile_b"),
        ]
        operations = build_compile_claim_operations_from_observations(
            observations,
            [],
            now_utc=self.now_utc,
        )
        self.assertEqual(len(operations), 1)
        proposed = Phase7CompiledClaim.from_dict(operations[0].proposed_claim)
        self.assertEqual(proposed.support_observation_ids, ["obs_compile_a", "obs_compile_b"])
        self.assertEqual(proposed.primary_source_authority, "manual")

    def test_mark_current_builder_passes_grade(self) -> None:
        operation = build_mark_current_operation(
            pending_claim=self.claim("claim_pending_explicit"),
            source_observation=self.observation("obs_pending_explicit"),
            now_utc=self.now_utc,
            expected_revision=2,
        )
        grade = grade_phase7c_compile_operations(
            [operation],
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        self.assertTrue(grade.passed, grade.to_dict())

    def test_supersede_builder_passes_grade(self) -> None:
        proposed = Phase7CompiledClaim(
            claim_id="claim_new_path_builder",
            subject_id="subject-mcp-path",
            memory_lane="semantic",
            compiled_text="The live MCP runtime path is cloudflare-mcp/mcp-server.",
            status="current",
            support_observation_ids=["obs_new_path_support"],
            primary_source_authority="system",
            taxonomy_decision="supersession",
            scope={"repo": "knowledge-system"},
        )
        operation = build_supersede_claim_operation(
            old_claim=self.claim("claim_old_path"),
            proposed_claim=proposed,
            source_observation=self.observation("obs_new_path_support"),
            decision="supersession",
            reason="New scoped runtime path supersedes old path.",
            old_expected_revision=3,
            new_expected_revision=0,
        )
        grade = grade_phase7c_compile_operations(
            [operation],
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        self.assertTrue(grade.passed, grade.to_dict())

    def test_unsupported_operation_type_fails_grade(self) -> None:
        operation = self.operations[0].to_dict()
        operation["type"] = "delete_claim"
        issues = self.grade_issue_names([operation])
        self.assertIn("unsupported_operation_type", issues)

    def test_missing_rollback_and_revision_fail_grade(self) -> None:
        operation = self.operations[0].to_dict()
        operation["rollback"] = {}
        operation["expected_revisions"] = {}
        issues = self.grade_issue_names([operation])
        self.assertIn("missing_rollback_metadata", issues)
        self.assertIn("missing_expected_revision", issues)

    def test_procedural_source_observation_fails_semantic_compile(self) -> None:
        proposed = Phase7CompiledClaim(
            claim_id="claim_bad_procedural",
            subject_id="subject-procedural",
            memory_lane="semantic",
            compiled_text="Procedural memory must not be compiled through semantic current view.",
            status="current",
            support_observation_ids=["obs_procedural_compile"],
            primary_source_authority="manual",
            taxonomy_decision="duplicate",
        )
        operation = build_compile_claim_operation(
            proposed_claim=proposed,
            source_observation=self.observation("obs_procedural_compile"),
            expected_revision=0,
            decision="duplicate",
            reason="Bad procedural compile fixture.",
        )
        issues = self.grade_issue_names([operation])
        self.assertIn("procedural_memory_semantic_mutation", issues)

    def test_cross_subject_supersession_without_entity_match_fails_grade(self) -> None:
        operation = self.operations[1].to_dict()
        operation["proposed_claim"]["subject_id"] = "different-subject"
        issues = self.grade_issue_names([operation])
        self.assertIn("supersession_without_same_subject_or_entity", issues)

    def test_cross_subject_supersession_with_resolved_entity_match_passes(self) -> None:
        operation = self.operations[1].to_dict()
        operation["proposed_claim"]["subject_id"] = "different-subject"
        operation["evidence"]["resolved_entity_match"] = True
        grade = grade_phase7c_compile_operations(
            [operation],
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        self.assertTrue(grade.passed, grade.to_dict())

    def test_refinement_dropping_old_support_fails_grade(self) -> None:
        operation = self.operations[1].to_dict()
        operation["decision"] = "refinement"
        operation["proposed_claim"]["taxonomy_decision"] = "refinement"
        operation["proposed_claim"]["support_observation_ids"] = ["obs_new_path_support"]
        issues = self.grade_issue_names([operation])
        self.assertIn("refinement_drops_old_support", issues)

    def test_refinement_preserving_old_support_passes_grade(self) -> None:
        operation = self.operations[1].to_dict()
        operation["decision"] = "refinement"
        operation["proposed_claim"]["taxonomy_decision"] = "refinement"
        operation["proposed_claim"]["support_observation_ids"] = [
            "obs_old_path_support",
            "obs_new_path_support",
        ]
        grade = grade_phase7c_compile_operations(
            [operation],
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        self.assertTrue(grade.passed, grade.to_dict())

    def test_temporal_expiry_without_valid_window_fails_grade(self) -> None:
        operation = self.operations[1].to_dict()
        operation["decision"] = "temporal_expiry"
        operation["proposed_claim"]["taxonomy_decision"] = "temporal_expiry"
        issues = self.grade_issue_names([operation])
        self.assertIn("temporal_expiry_without_resolved_window", issues)

    def test_temporal_expiry_with_valid_window_passes_grade(self) -> None:
        operation = self.operations[1].to_dict()
        operation["decision"] = "temporal_expiry"
        operation["proposed_claim"]["taxonomy_decision"] = "temporal_expiry"
        operation["proposed_claim"]["valid_to"] = "2026-06-01"
        grade = grade_phase7c_compile_operations(
            [operation],
            observations=self.observations,
            claims=self.claims,
            now_utc=self.now_utc,
        )
        self.assertTrue(grade.passed, grade.to_dict())

    def test_scoped_exception_without_scope_fails_grade(self) -> None:
        operation = self.operations[0].to_dict()
        operation["decision"] = "scoped_exception"
        operation["proposed_claim"]["taxonomy_decision"] = "scoped_exception"
        operation["proposed_claim"]["scope"] = {}
        issues = self.grade_issue_names([operation])
        self.assertIn("scoped_exception_without_scope", issues)

    def test_expired_pending_claim_cannot_mark_current(self) -> None:
        operation = build_mark_current_operation(
            pending_claim=self.claim("claim_pending_expired"),
            source_observation=self.observation("obs_pending_expired"),
            now_utc=self.now_utc,
            expected_revision=4,
        )
        issues = self.grade_issue_names([operation])
        self.assertIn("mark_current_ttl_expired", issues)

    def test_inferred_source_cannot_mark_current(self) -> None:
        pending = Phase7CompiledClaim(
            claim_id="claim_inferred_pending",
            subject_id="subject-compile",
            memory_lane="semantic",
            compiled_text="Inferred observations should wait for Dream compile.",
            status="pending_compile",
            support_observation_ids=["obs_compile_b"],
            primary_source_authority="inferred",
            ttl_expires_at="2026-06-12T00:00:00+00:00",
        )
        operation = build_mark_current_operation(
            pending_claim=pending,
            source_observation=self.observation("obs_compile_b"),
            now_utc=self.now_utc,
            expected_revision=0,
        )
        issues = self.grade_issue_names([operation])
        self.assertIn("missing_pending_claim", issues)

        issues = {
            issue.name
            for issue in grade_phase7c_compile_operations(
                [operation],
                observations=self.observations,
                claims=[*self.claims, pending],
                now_utc=self.now_utc,
            ).issues
        }
        self.assertIn("disallowed_source_authority", issues)

    def test_unscoped_system_source_cannot_mark_current(self) -> None:
        pending = Phase7CompiledClaim(
            claim_id="claim_system_unscoped_pending",
            subject_id="subject-system-unscoped",
            memory_lane="semantic",
            compiled_text="Unscoped system observations cannot be promoted directly.",
            status="pending_compile",
            support_observation_ids=["obs_system_unscoped"],
            primary_source_authority="system",
            ttl_expires_at="2026-06-12T00:00:00+00:00",
        )
        operation = build_mark_current_operation(
            pending_claim=pending,
            source_observation=self.observation("obs_system_unscoped"),
            now_utc=self.now_utc,
            expected_revision=0,
        )
        issues = {
            issue.name
            for issue in grade_phase7c_compile_operations(
                [operation],
                observations=self.observations,
                claims=[*self.claims, pending],
                now_utc=self.now_utc,
            ).issues
        }
        self.assertIn("system_mark_current_without_scope", issues)

    def test_mark_current_requires_conflict_check_result(self) -> None:
        operation = self.operations[2].to_dict()
        del operation["evidence"]["conflict_check_result"]
        issues = self.grade_issue_names([operation])
        self.assertIn("missing_conflict_check_result", issues)

    def test_contestation_requires_operator_review_flag(self) -> None:
        operation = self.operations[0].to_dict()
        operation["decision"] = "contestation"
        operation["proposed_claim"]["taxonomy_decision"] = "contestation"
        issues = self.grade_issue_names([operation])
        self.assertIn("contest_without_operator_review", issues)


if __name__ == "__main__":
    unittest.main()
