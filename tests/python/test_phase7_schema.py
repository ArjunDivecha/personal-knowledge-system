from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase7_migration_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    KnowledgeEntry,
    Phase7CompiledClaim,
    Phase7Observation,
    Phase7SupersessionEdge,
    ProjectEntry,
    compiled_claims_from_observations,
    highest_source_authority,
    normalize_claim_text,
    observations_from_legacy_entry,
    preview_phase7_migration,
    provisional_claim_from_observation,
    retrieval_projection_from_claims,
    stable_phase7_id,
)


class Phase7SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())

    def legacy_entry(self, entry_id: str) -> dict:
        for entry in self.fixture["legacy_entries"]:
            if entry["id"] == entry_id:
                return entry
        raise AssertionError(f"Missing fixture entry {entry_id}")

    def observation(self, **overrides) -> Phase7Observation:
        data = {
            "observation_id": "obs_test",
            "subject_id": "subject-test",
            "memory_lane": "semantic",
            "source_authority": "inferred",
            "claim_text": "A test claim.",
            "source_type": "fixture",
            "source_id": "source-test",
            "message_ids": ["msg-test"],
            "source_path": "observations[0]",
            "snippet": "A test claim.",
            "observed_at": "2026-06-05T00:00:00+00:00",
            "learned_at": "2026-06-05T00:00:00+00:00",
        }
        data.update(overrides)
        return Phase7Observation(**data)

    def test_dataclasses_round_trip_with_full_optional_fields(self) -> None:
        observation = self.observation(
            valid_from="2026-06-01T00:00:00+00:00",
            valid_to=None,
            invalidated_at="2026-06-04T00:00:00+00:00",
            confidence="high",
            entity_mentions=["Phase 7"],
            relationship_edges=[{"from": "a", "to": "b"}],
            scope={"repo": "fixture"},
            signal_flags=["explicit_save"],
            extraction_method="unit_test",
        )
        self.assertEqual(Phase7Observation.from_dict(observation.to_dict()), observation)

        claim = Phase7CompiledClaim(
            claim_id="claim_full",
            subject_id="subject-test",
            memory_lane="semantic",
            compiled_text="A compiled claim.",
            status="current",
            support_observation_ids=["obs_test"],
            primary_source_authority="explicit",
            confidence="high",
            temporal_status="current",
            valid_from="2026-06-01T00:00:00+00:00",
            valid_to=None,
            ttl_expires_at="2026-06-12T00:00:00+00:00",
            invalidated_at="2026-06-04T00:00:00+00:00",
            supersedes_claim_ids=["claim_old"],
            superseded_by_claim_id=None,
            taxonomy_decision="refinement",
            scope={"repo": "fixture"},
            compile_notes=["full round trip"],
            compiled_at="2026-06-05T00:00:00+00:00",
            compiled_by="unit_test",
            expected_source_revisions={"obs_test": 1},
        )
        self.assertEqual(Phase7CompiledClaim.from_dict(claim.to_dict()), claim)

        edge = Phase7SupersessionEdge(
            from_claim_id="claim_old",
            to_claim_id="claim_new",
            decision="supersession",
            reason="newer claim",
            observation_id="obs_test",
            observed_at=None,
        )
        self.assertEqual(Phase7SupersessionEdge.from_dict(edge.to_dict()), edge)

    def test_stable_ids_are_deterministic(self) -> None:
        first = stable_phase7_id("obs", "subject", ["a", "b"])
        second = stable_phase7_id("obs", "subject", ["a", "b"])
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("obs_"))
        self.assertEqual(len(first.split("_", 1)[1]), 16)

    def test_normalize_claim_text_exact_rule(self) -> None:
        self.assertEqual(
            normalize_claim_text("  The   QUICK, brown Fox.  "),
            "the quick, brown fox.",
        )

    def test_stable_id_canonicalizes_dict_and_list_parts(self) -> None:
        first = stable_phase7_id("claim", {"b": 2, "a": [1, 2]})
        second = stable_phase7_id("claim", {"a": [1, 2], "b": 2})
        self.assertEqual(first, second)

    def test_legacy_knowledge_dataclass_creates_expected_observations(self) -> None:
        entry = KnowledgeEntry.from_dict(self.legacy_entry("ke_phase7_primary"))
        observations = observations_from_legacy_entry(entry)
        self.assertEqual(len(observations), 5)
        self.assertEqual(sum(1 for item in observations if item.memory_lane == "semantic"), 4)
        self.assertEqual(sum(1 for item in observations if item.memory_lane == "episodic"), 1)

    def test_legacy_knowledge_dict_matches_dataclass_count(self) -> None:
        entry_data = self.legacy_entry("ke_phase7_primary")
        dataclass_count = len(observations_from_legacy_entry(KnowledgeEntry.from_dict(entry_data)))
        dict_count = len(observations_from_legacy_entry(entry_data))
        self.assertEqual(dict_count, dataclass_count)

    def test_legacy_project_dataclass_creates_subject_scoped_observations(self) -> None:
        entry = ProjectEntry.from_dict(self.legacy_entry("pe_phase7_project"))
        observations = observations_from_legacy_entry(entry)
        self.assertGreaterEqual(len(observations), 4)
        self.assertTrue(all(item.subject_id == "pe_phase7_project" for item in observations))

    def test_legacy_project_dict_creates_project_observations(self) -> None:
        observations = observations_from_legacy_entry(self.legacy_entry("pe_phase7_project"))
        self.assertIn("goal", {item.source_path for item in observations})
        self.assertIn("decisions_made[0]", {item.source_path for item in observations})

    def test_legacy_observations_inherit_parent_subject_id(self) -> None:
        entry = self.legacy_entry("ke_phase7_primary")
        observations = observations_from_legacy_entry(entry)
        self.assertTrue(all(item.subject_id == entry["id"] for item in observations))

    def test_explicit_save_maps_to_explicit_authority(self) -> None:
        observations = observations_from_legacy_entry(self.legacy_entry("ke_phase7_explicit"))
        self.assertEqual(observations[0].source_authority, "explicit")

    def test_correction_flag_preserved_without_authority_promotion(self) -> None:
        entry = {
            "id": "ke_correction",
            "type": "knowledge",
            "domain": "Correction flag fixture",
            "current_view": "Correction-derived entries stay inferred in Phase 7A.",
            "metadata": {
                "created_at": "2026-06-05T00:00:00+00:00",
                "updated_at": "2026-06-05T00:00:00+00:00",
                "source_conversations": [],
                "source_messages": [],
                "signal_flags": ["correction_derived"],
            },
        }
        observation = observations_from_legacy_entry(entry)[0]
        self.assertEqual(observation.source_authority, "inferred")
        self.assertEqual(observation.signal_flags, ["correction_derived"])

    def test_duplicate_observations_merge_into_one_claim(self) -> None:
        observations = [Phase7Observation.from_dict(item) for item in self.fixture["observations"][1:3]]
        claims = compiled_claims_from_observations(observations)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].support_observation_ids, ["obs_dup_a", "obs_dup_b"])

    def test_duplicate_merge_sets_highest_source_authority(self) -> None:
        observations = [Phase7Observation.from_dict(item) for item in self.fixture["observations"][1:3]]
        claim = compiled_claims_from_observations(observations)[0]
        self.assertEqual(claim.primary_source_authority, "explicit")

    def test_highest_source_authority_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            highest_source_authority([])

    def test_duplicate_group_uses_first_raw_claim_text(self) -> None:
        observations = [Phase7Observation.from_dict(item) for item in self.fixture["observations"][1:3]]
        claim = compiled_claims_from_observations(observations)[0]
        self.assertEqual(claim.compiled_text, "Duplicate semantic claim.")

    def test_duplicate_group_claim_id_uses_normalized_compiled_text(self) -> None:
        observations = [Phase7Observation.from_dict(item) for item in self.fixture["observations"][1:3]]
        claim = compiled_claims_from_observations(observations)[0]
        self.assertEqual(
            claim.claim_id,
            stable_phase7_id("claim", "subject-duplicate", normalize_claim_text(claim.compiled_text)),
        )

    def test_list_derived_observations_use_indexes_and_unique_ids(self) -> None:
        observations = observations_from_legacy_entry(self.legacy_entry("ke_phase7_multi_positions"))
        self.assertEqual({item.source_path for item in observations}, {"positions[0]", "positions[1]"})
        self.assertEqual(len({item.observation_id for item in observations}), 2)

    def test_evidence_less_item_defaults_and_warns(self) -> None:
        entry = {
            "id": "ke_missing_evidence",
            "type": "knowledge",
            "domain": "Missing evidence",
            "current_view": "",
            "key_insights": [{"insight": "Evidence-less nested item should warn."}],
            "metadata": {
                "created_at": "2026-06-05T00:00:00+00:00",
                "updated_at": "2026-06-05T00:00:00+00:00",
                "source_conversations": [],
                "source_messages": [],
                "signal_flags": [],
            },
        }
        preview = preview_phase7_migration([entry])
        observation = preview.observations[0]
        self.assertEqual(observation.snippet, "")
        self.assertEqual(observation.message_ids, [])
        self.assertEqual(observation.source_id, "ke_missing_evidence")
        self.assertEqual(len(preview.errors), 1)

    def test_legacy_confidence_does_not_propagate(self) -> None:
        observations = observations_from_legacy_entry(self.legacy_entry("ke_phase7_primary"))
        self.assertTrue(all(item.confidence == "medium" for item in observations))

    def test_timestamp_mapping(self) -> None:
        observations = observations_from_legacy_entry(self.legacy_entry("ke_phase7_primary"))
        by_path = {item.source_path: item for item in observations}
        self.assertEqual(by_path["positions[0]"].observed_at, "2026-06-05T10:00:00+00:00")
        self.assertEqual(by_path["positions[0]"].learned_at, "2026-06-05T09:45:00+00:00")

    def test_scalar_observation_source_defaults(self) -> None:
        observations = observations_from_legacy_entry(self.legacy_entry("ke_phase7_primary"))
        current_view = next(item for item in observations if item.source_path == "current_view")
        self.assertEqual(current_view.source_id, "ke_phase7_primary")
        self.assertEqual(current_view.message_ids, [])
        self.assertEqual(current_view.snippet, "")

    def test_procedural_observations_do_not_compile(self) -> None:
        observation = Phase7Observation.from_dict(self.fixture["observations"][0])
        self.assertEqual(compiled_claims_from_observations([observation]), [])

    def test_explicit_observation_creates_seven_day_provisional(self) -> None:
        observation = self.observation(source_authority="explicit")
        claim = provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.status, "pending_compile")
        self.assertEqual(claim.ttl_expires_at, "2026-06-12T00:00:00+00:00")

    def test_manual_observation_creates_seven_day_provisional(self) -> None:
        observation = self.observation(source_authority="manual")
        claim = provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.ttl_expires_at, "2026-06-12T00:00:00+00:00")

    def test_scoped_system_observation_creates_two_day_provisional(self) -> None:
        observation = self.observation(source_authority="system", scope={"repo": "fixture"})
        claim = provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.ttl_expires_at, "2026-06-07T00:00:00+00:00")

    def test_provisional_claim_uses_raw_observation_text(self) -> None:
        observation = self.observation(source_authority="explicit", claim_text="  Raw Claim Text. ")
        claim = provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.compiled_text, "  Raw Claim Text. ")

    def test_unscoped_system_observation_does_not_create_provisional(self) -> None:
        observation = self.observation(source_authority="system")
        self.assertIsNone(provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00"))

    def test_inferred_observation_does_not_create_provisional(self) -> None:
        observation = self.observation(source_authority="inferred")
        self.assertIsNone(provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00"))

    def test_expired_provisional_excluded_from_projection(self) -> None:
        expired = Phase7CompiledClaim(
            claim_id="claim_expired_pending",
            subject_id="subject-test",
            memory_lane="semantic",
            compiled_text="Expired pending claim.",
            status="pending_compile",
            support_observation_ids=["obs_test"],
            ttl_expires_at="2026-06-04T00:00:00+00:00",
            compiled_by="provisional_projection",
        )
        self.assertEqual(retrieval_projection_from_claims([expired], now_utc="2026-06-05T00:00:00+00:00"), [])

    def test_current_claim_included_in_projection(self) -> None:
        current = Phase7CompiledClaim(
            claim_id="claim_current",
            subject_id="subject-test",
            memory_lane="semantic",
            compiled_text="Current claim.",
            status="current",
            support_observation_ids=["obs_test"],
        )
        self.assertEqual(retrieval_projection_from_claims([current], now_utc="2026-06-05T00:00:00+00:00"), [current])

    def test_conflicting_pending_and_current_claims_coexist_with_distinct_ids(self) -> None:
        observation = self.observation(source_authority="explicit")
        pending = provisional_claim_from_observation(observation, now_utc="2026-06-05T00:00:00+00:00")
        current = Phase7CompiledClaim(
            claim_id=stable_phase7_id("claim", observation.subject_id, normalize_claim_text(observation.claim_text)),
            subject_id=observation.subject_id,
            memory_lane="semantic",
            compiled_text=observation.claim_text,
            status="current",
            support_observation_ids=["obs_current"],
        )
        self.assertIsNotNone(pending)
        self.assertNotEqual(current.claim_id, pending.claim_id)
        self.assertFalse(hasattr(current, "conflict_marker"))

    def test_supersession_edge_rejects_equal_claim_ids(self) -> None:
        with self.assertRaises(ValueError):
            Phase7SupersessionEdge(
                from_claim_id="claim_same",
                to_claim_id="claim_same",
                decision="supersession",
                reason="bad edge",
                observation_id="obs_test",
                observed_at=None,
            )

    def test_supersession_edge_accepts_only_transition_decisions(self) -> None:
        for decision in ("refinement", "supersession", "temporal_expiry", "deprecation"):
            edge = Phase7SupersessionEdge(
                from_claim_id=f"claim_old_{decision}",
                to_claim_id=f"claim_new_{decision}",
                decision=decision,
                reason="valid transition",
                observation_id=f"obs_{decision}",
                observed_at=None,
            )
            self.assertEqual(edge.decision, decision)
        with self.assertRaises(ValueError):
            Phase7SupersessionEdge(
                from_claim_id="claim_old",
                to_claim_id="claim_new",
                decision="contestation",
                reason="not edge-legal",
                observation_id="obs_bad",
                observed_at=None,
            )

    def test_compiled_claim_taxonomy_accepts_all_decisions(self) -> None:
        for decision in (
            "duplicate",
            "refinement",
            "supersession",
            "scoped_exception",
            "contestation",
            "temporal_expiry",
            "deprecation",
        ):
            claim = Phase7CompiledClaim(
                claim_id=f"claim_{decision}",
                subject_id=f"subject_{decision}",
                memory_lane="semantic",
                compiled_text=f"{decision} claim.",
                status="deprecated" if decision == "deprecation" else "current",
                support_observation_ids=[] if decision == "deprecation" else [f"obs_{decision}"],
                taxonomy_decision=decision,
                compile_notes=["deprecated fixture"] if decision == "deprecation" else [],
            )
            self.assertEqual(claim.taxonomy_decision, decision)

    def test_fixture_compiled_claims_cover_all_taxonomy_decisions(self) -> None:
        decisions = {
            item.get("taxonomy_decision")
            for item in self.fixture["compiled_claims"]
            if item.get("taxonomy_decision")
        }
        self.assertEqual(
            decisions,
            {
                "duplicate",
                "refinement",
                "supersession",
                "scoped_exception",
                "contestation",
                "temporal_expiry",
                "deprecation",
            },
        )

    def test_fixture_edges_cover_legal_edge_decisions(self) -> None:
        decisions = {item["decision"] for item in self.fixture["supersession_edges"]}
        self.assertEqual(decisions, {"refinement", "supersession", "temporal_expiry", "deprecation"})
        for item in self.fixture["supersession_edges"]:
            Phase7SupersessionEdge.from_dict(item)

    def test_deprecated_claim_without_support_requires_notes(self) -> None:
        with self.assertRaises(ValueError):
            Phase7CompiledClaim(
                claim_id="claim_deprecated_bad",
                subject_id="subject-deprecated",
                memory_lane="semantic",
                compiled_text="Deprecated without notes.",
                status="deprecated",
                support_observation_ids=[],
            )
        claim = Phase7CompiledClaim.from_dict(self.fixture["compiled_claims"][-1])
        self.assertEqual(claim.status, "deprecated")

    def test_invalid_enums_and_missing_required_fields_raise(self) -> None:
        with self.assertRaises(ValueError):
            self.observation(memory_lane="bad")
        data = self.observation().to_dict()
        del data["claim_text"]
        with self.assertRaises(ValueError):
            Phase7Observation.from_dict(data)

    def test_unknown_input_keys_are_ignored(self) -> None:
        data = self.observation().to_dict()
        data["fixture_note"] = "ignored"
        observation = Phase7Observation.from_dict(data)
        self.assertFalse(hasattr(observation, "fixture_note"))

    def test_migration_preview_returns_observations_and_claims(self) -> None:
        entries = self.fixture["legacy_entries"][:4]
        preview = preview_phase7_migration(entries)
        self.assertGreater(len(preview.observations), 0)
        self.assertGreater(len(preview.claims), 0)
        self.assertEqual(preview.skipped_count, 0)
        self.assertEqual(preview.errors, [])

    def test_migration_preview_keeps_moving_on_malformed_input(self) -> None:
        entries = [self.legacy_entry("ke_phase7_primary"), *self.fixture["malformed_entries"]]
        preview = preview_phase7_migration(entries)
        self.assertGreater(len(preview.observations), 0)
        self.assertEqual(preview.skipped_count, 1)
        self.assertEqual(len(preview.errors), 1)


if __name__ == "__main__":
    unittest.main()
