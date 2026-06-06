from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase8_retrieval_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    Phase7CompiledClaim,
    Phase7Observation,
    Phase8EvalReport,
    Phase8QueryIntent,
    Phase8RetrievalReport,
    build_phase7d_memory_blocks,
    build_phase8_candidates,
    classify_phase8_query,
    generate_compiled_current_view,
    retrieve_phase8,
    run_phase8_retrieval_fixture,
)


class Phase8RetrievalTests(unittest.TestCase):
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
        cls.current_view = generate_compiled_current_view(
            cls.observations,
            cls.claims,
            now_utc=cls.now_utc,
        )
        cls.memory_blocks = build_phase7d_memory_blocks(
            cls.current_view,
            cls.observations,
            generated_at=cls.now_utc,
            policy_source_paths=cls.fixture["policy_source_paths"],
            project_scope_ref=cls.fixture["project_scope_ref"],
        )
        cls.vector_scores = dict(cls.fixture["vector_scores"])

    def retrieve(self, query: str, *, limit: int = 6) -> Phase8RetrievalReport:
        return retrieve_phase8(
            query,
            current_view=self.current_view,
            observations=self.observations,
            memory_blocks=self.memory_blocks,
            now_utc=self.now_utc,
            vector_scores=self.vector_scores,
            limit=limit,
        )

    def ids(self, report: Phase8RetrievalReport) -> list[str]:
        return [result.candidate.candidate_id for result in report.results]

    def test_query_classifier_detects_supported_phase8_intents(self) -> None:
        cases = {
            "what is the live MCP runtime path now?": "current_answer",
            "why did the live MCP runtime path change?": "evidence_history",
            "what was true as of 2026-04-01 for the MCP runtime path?": "point_in_time",
            "what policy file defines compile latency?": "procedural_policy",
        }
        for query, expected_intent in cases.items():
            with self.subTest(query=query):
                intent = classify_phase8_query(query)
                self.assertEqual(intent.intent, expected_intent)
                self.assertEqual(Phase8QueryIntent.from_dict(intent.to_dict()), intent)

    def test_current_answer_prefers_compiled_current_claim(self) -> None:
        report = self.retrieve("what is the live MCP runtime path now?")
        result_ids = self.ids(report)
        self.assertEqual(report.intent.intent, "current_answer")
        self.assertEqual(result_ids[0], "claim_current_runtime")
        self.assertNotIn("claim_old_runtime", result_ids)
        self.assertNotIn("obs_old_runtime", result_ids)
        self.assertNotIn("claim_expired_trip", result_ids)
        self.assertIn("compiled_current_preferred", report.results[0].reasons)

    def test_current_candidate_builder_omits_observations_by_default(self) -> None:
        candidates = build_phase8_candidates(
            self.current_view,
            self.observations,
            self.memory_blocks,
            now_utc=self.now_utc,
            query_intent=classify_phase8_query("what is the live MCP runtime path now?"),
        )
        self.assertNotIn("observation", {candidate.source_type for candidate in candidates})

    def test_evidence_history_query_reaches_observations(self) -> None:
        report = self.retrieve("why did the live MCP runtime path change?", limit=8)
        result_ids = self.ids(report)
        self.assertEqual(report.intent.intent, "evidence_history")
        self.assertIn("obs_current_runtime", result_ids)
        self.assertIn("obs_old_runtime", result_ids)
        observation_results = [
            result for result in report.results
            if result.candidate.source_type == "observation"
        ]
        self.assertGreaterEqual(len(observation_results), 2)

    def test_point_in_time_query_prefers_valid_observation(self) -> None:
        report = self.retrieve(
            "what was true as of 2026-04-01 for the MCP runtime path?",
            limit=8,
        )
        self.assertEqual(report.intent.intent, "point_in_time")
        self.assertEqual(report.intent.as_of, "2026-04-01")
        self.assertEqual(self.ids(report)[0], "obs_old_runtime")
        self.assertIn("point_in_time_temporal_fit", report.results[0].reasons)

    def test_policy_query_prefers_read_only_policy_pointer(self) -> None:
        report = self.retrieve("what policy file defines compile latency?")
        self.assertEqual(report.intent.intent, "procedural_policy")
        top = report.results[0].candidate
        self.assertEqual(top.candidate_id, "block:policy_pointer:knowledge-system")
        self.assertEqual(top.source_label, "policy_pointer")
        self.assertEqual(top.memory_lane, "procedural")
        self.assertTrue(top.metadata["read_only"])

    def test_pending_compile_claim_is_available_as_provisional(self) -> None:
        report = self.retrieve("what is pending in Phase 8?")
        result_by_id = {result.candidate.candidate_id: result for result in report.results}
        self.assertIn("claim_pending_phase8", result_by_id)
        self.assertEqual(
            result_by_id["claim_pending_phase8"].candidate.source_type,
            "provisional_claim",
        )
        self.assertIn("provisional_allowed", result_by_id["claim_pending_phase8"].reasons)

    def test_expired_temporal_claim_stays_out_of_current_answer(self) -> None:
        excluded_reasons = {
            excluded.claim_id: excluded.reason
            for excluded in self.current_view.excluded_claims
        }
        self.assertEqual(
            excluded_reasons["claim_expired_trip"],
            "temporal_not_current:expired",
        )
        report = self.retrieve("what is the Singapore trip plan now?")
        result_ids = self.ids(report)
        self.assertNotIn("claim_expired_trip", result_ids)
        self.assertNotIn("obs_expired_trip", result_ids)

    def test_retrieval_report_round_trips(self) -> None:
        report = self.retrieve("what is the live MCP runtime path now?")
        self.assertEqual(Phase8RetrievalReport.from_dict(report.to_dict()), report)

    def test_fixture_eval_passes(self) -> None:
        report = run_phase8_retrieval_fixture(self.fixture)
        self.assertTrue(report.passed, report.to_dict())
        self.assertEqual(report.check_count, len(self.fixture["probes"]))
        self.assertEqual(report.failure_count, 0)

    def test_fixture_eval_report_round_trips(self) -> None:
        report = run_phase8_retrieval_fixture(self.fixture)
        self.assertEqual(Phase8EvalReport.from_dict(report.to_dict()), report)

    def test_fixture_eval_fails_when_expected_top_result_is_wrong(self) -> None:
        fixture = copy.deepcopy(self.fixture)
        fixture["probes"][0]["expected_top_candidate_id"] = "claim_phase8_status"
        report = run_phase8_retrieval_fixture(fixture)
        self.assertFalse(report.passed)
        check = next(
            item for item in report.checks
            if item.check_id == "current_runtime_prefers_compiled_current"
        )
        self.assertFalse(check.passed)
        self.assertIn("top result mismatch", check.issues[0])


if __name__ == "__main__":
    unittest.main()
