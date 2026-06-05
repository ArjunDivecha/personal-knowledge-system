from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
TEMPORAL_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase7b_temporal_outcome_probes.json"
ENTITY_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase7b_entity_index_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    Phase7CompiledClaim,
    Phase7Observation,
    build_entity_index,
    enrich_claim_temporal,
    enrich_claims_phase7b,
    enrich_observation_temporal,
    enrich_observations_phase7b,
    evaluate_phase7b_temporal_probe,
    extract_entity_mentions,
    normalize_entity_name,
    normalize_temporal_text,
    stable_entity_id,
)


class Phase7BEnrichmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporal_fixture = json.loads(TEMPORAL_FIXTURE_PATH.read_text())
        cls.entity_fixture = json.loads(ENTITY_FIXTURE_PATH.read_text())

    def observation(self, **overrides) -> Phase7Observation:
        data = {
            "observation_id": "obs_7b",
            "subject_id": "subject-7b",
            "memory_lane": "semantic",
            "source_authority": "manual",
            "claim_text": "PKS plans a review tomorrow.",
            "source_type": "fixture",
            "source_id": "fixture-7b",
            "message_ids": ["msg-7b"],
            "source_path": "observations[0]",
            "snippet": "PKS plans a review tomorrow.",
            "observed_at": "2026-06-05T00:00:00+00:00",
            "learned_at": "2026-06-05T00:00:00+00:00",
        }
        data.update(overrides)
        return Phase7Observation(**data)

    def test_stable_entity_id_is_deterministic_and_normalized(self) -> None:
        self.assertEqual(stable_entity_id("PKS"), stable_entity_id(" pks "))
        self.assertTrue(stable_entity_id("PKS").startswith("ent_"))

    def test_normalize_entity_name_collapses_case_and_space(self) -> None:
        self.assertEqual(normalize_entity_name("  Phase   Seven  "), "phase seven")

    def test_extract_entity_mentions_finds_acronym_title_and_artifact(self) -> None:
        mentions = extract_entity_mentions("PKS uses Redis via `distillation/models/phase7b.py`.")
        names = {mention.canonical_name for mention in mentions}
        self.assertIn("PKS", names)
        self.assertIn("Redis", names)
        self.assertIn("distillation/models/phase7b.py", names)

    def test_build_entity_index_is_source_aware(self) -> None:
        observations = [Phase7Observation.from_dict(item) for item in self.entity_fixture["observations"]]
        index = build_entity_index(observations)
        by_name = {entry.canonical_name: entry for entry in index}
        for expected in self.entity_fixture["expected_entities"]:
            entry = by_name[expected["canonical_name"]]
            self.assertEqual(entry.source_observation_ids, expected["source_observation_ids"])

    def test_entity_index_includes_claim_sources(self) -> None:
        observation = self.observation(claim_text="PKS uses MCP.")
        claim = Phase7CompiledClaim(
            claim_id="claim_entity_source",
            subject_id="subject-7b",
            memory_lane="semantic",
            compiled_text="MCP supports PKS.",
            status="current",
            support_observation_ids=[observation.observation_id],
        )
        index = build_entity_index([observation], [claim])
        mcp = next(entry for entry in index if entry.canonical_name == "MCP")
        self.assertEqual(mcp.source_observation_ids, ["obs_7b"])
        self.assertEqual(mcp.source_claim_ids, ["claim_entity_source"])

    def test_normalize_temporal_text_resolves_future_month(self) -> None:
        resolution = normalize_temporal_text(
            "The operator is going to Singapore in July 2026.",
            observed_at="2026-06-05T00:00:00+00:00",
            now_utc="2026-06-05T00:00:00+00:00",
        )
        self.assertEqual(resolution.temporal_status, "future")
        self.assertEqual(resolution.valid_from, "2026-07-01")
        self.assertEqual(resolution.valid_to, "2026-07-31")

    def test_normalize_temporal_text_resolves_relative_days(self) -> None:
        resolution = normalize_temporal_text(
            "Review is planned for tomorrow.",
            observed_at="2026-06-05T00:00:00+00:00",
            now_utc="2026-06-05T00:00:00+00:00",
        )
        self.assertEqual(resolution.temporal_status, "future")
        self.assertEqual(resolution.valid_from, "2026-06-06")
        self.assertEqual(resolution.valid_to, "2026-06-06")

    def test_normalize_temporal_text_returns_unknown_for_unsupported_text(self) -> None:
        resolution = normalize_temporal_text(
            "PKS separates observations from claims.",
            observed_at="2026-06-05T00:00:00+00:00",
            now_utc="2026-06-05T00:00:00+00:00",
        )
        self.assertEqual(resolution.temporal_status, "unknown")
        self.assertIsNone(resolution.valid_from)

    def test_enrich_observation_temporal_sets_valid_window(self) -> None:
        observation = self.observation(claim_text="The fixture review is planned for tomorrow.")
        enriched = enrich_observation_temporal(observation, now_utc="2026-06-05T00:00:00+00:00")
        self.assertEqual(enriched.valid_from, "2026-06-06")
        self.assertEqual(enriched.valid_to, "2026-06-06")

    def test_enrich_observations_adds_entity_ids(self) -> None:
        observation = self.observation(claim_text="PKS uses MCP for Phase 7B.")
        enriched = enrich_observations_phase7b([observation], now_utc="2026-06-05T00:00:00+00:00")[0]
        self.assertIn(stable_entity_id("PKS"), enriched.entity_mentions)
        self.assertIn(stable_entity_id("MCP"), enriched.entity_mentions)

    def test_enrich_claim_temporal_from_claim_text(self) -> None:
        claim = Phase7CompiledClaim(
            claim_id="claim_temporal_text",
            subject_id="subject-7b",
            memory_lane="semantic",
            compiled_text="The fixture review is planned for tomorrow.",
            status="current",
            support_observation_ids=["obs_missing"],
        )
        enriched = enrich_claim_temporal(claim, {}, now_utc="2026-06-05T00:00:00+00:00")
        self.assertEqual(enriched.temporal_status, "future")
        self.assertEqual(enriched.valid_from, "2026-06-06")

    def test_enrich_claim_temporal_from_support_observation(self) -> None:
        observation = self.observation(
            observation_id="obs_support_temporal",
            claim_text="The fixture review is planned for tomorrow.",
        )
        enriched_observation = enrich_observation_temporal(observation, now_utc="2026-06-05T00:00:00+00:00")
        claim = Phase7CompiledClaim(
            claim_id="claim_temporal_support",
            subject_id="subject-7b",
            memory_lane="semantic",
            compiled_text="The fixture review should be scheduled.",
            status="current",
            support_observation_ids=[enriched_observation.observation_id],
        )
        enriched = enrich_claim_temporal(
            claim,
            {enriched_observation.observation_id: enriched_observation},
            now_utc="2026-06-05T00:00:00+00:00",
        )
        self.assertEqual(enriched.temporal_status, "future")
        self.assertEqual(enriched.valid_from, "2026-06-06")

    def test_enrich_claims_phase7b_applies_to_claim_list(self) -> None:
        observation = enrich_observation_temporal(
            self.observation(observation_id="obs_claim_list"),
            now_utc="2026-06-05T00:00:00+00:00",
        )
        claim = Phase7CompiledClaim(
            claim_id="claim_list",
            subject_id="subject-7b",
            memory_lane="semantic",
            compiled_text="PKS review.",
            status="current",
            support_observation_ids=[observation.observation_id],
        )
        enriched = enrich_claims_phase7b([claim], [observation], now_utc="2026-06-05T00:00:00+00:00")
        self.assertEqual(enriched[0].temporal_status, "future")

    def test_temporal_probe_fixture_passes(self) -> None:
        results = [
            evaluate_phase7b_temporal_probe(probe)
            for probe in self.temporal_fixture["probes"]
        ]
        self.assertTrue(all(result["passed"] for result in results), results)

    def test_temporal_probe_reports_failure(self) -> None:
        result = evaluate_phase7b_temporal_probe(
            {
                "id": "bad_probe",
                "text": "Review is planned for tomorrow.",
                "observed_at": "2026-06-05T00:00:00+00:00",
                "now_utc": "2026-06-05T00:00:00+00:00",
                "expected_temporal_status": "expired",
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["failures"]), 1)


if __name__ == "__main__":
    unittest.main()
