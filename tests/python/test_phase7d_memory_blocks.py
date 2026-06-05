from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "phase7d_memory_blocks_fixture.json"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import (  # noqa: E402
    Phase7CurrentView,
    Phase7MemoryBlock,
    Phase7Observation,
    build_operator_profile_block,
    build_phase7d_memory_blocks,
    build_policy_pointer_block,
    build_project_status_block,
    stable_memory_block_id,
)


class Phase7DMemoryBlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text())
        cls.generated_at = cls.fixture["metadata"]["generated_at"]
        cls.observations = [
            Phase7Observation.from_dict(item)
            for item in cls.fixture["observations"]
        ]
        cls.current_view = Phase7CurrentView.from_dict(cls.fixture["current_view"])

    def test_stable_memory_block_id_is_deterministic(self) -> None:
        first = stable_memory_block_id("operator_profile", "operator")
        second = stable_memory_block_id("operator_profile", "operator")
        project = stable_memory_block_id("project_status", "project", "pks-phase-7")
        self.assertEqual(first, second)
        self.assertNotEqual(first, project)
        self.assertTrue(first.startswith("block_"))

    def test_operator_profile_block_uses_current_claims(self) -> None:
        block = build_operator_profile_block(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.label, "operator_profile")
        self.assertEqual(
            block.compiled_from_claim_ids,
            self.fixture["expected_blocks"]["operator_claim_ids"],
        )
        self.assertIn("direct autonomous execution", block.value)
        self.assertIn("done as tested", block.value)
        self.assertTrue(block.read_only)

    def test_project_status_block_uses_project_scope_ref(self) -> None:
        block = build_project_status_block(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
            scope_ref="pks-phase-7",
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.label, "project_status")
        self.assertEqual(block.scope_ref, "pks-phase-7")
        self.assertEqual(
            block.compiled_from_claim_ids,
            self.fixture["expected_blocks"]["project_claim_ids"],
        )
        self.assertIn("Phase 8 should wire retrieval", block.value)

    def test_project_status_block_returns_none_for_unknown_project(self) -> None:
        block = build_project_status_block(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
            scope_ref="missing-project",
        )
        self.assertIsNone(block)

    def test_policy_pointer_block_is_pointer_only(self) -> None:
        block = build_policy_pointer_block(
            source_paths=self.fixture["policy_source_paths"],
            generated_at=self.generated_at,
        )
        self.assertEqual(block.label, "policy_pointer")
        self.assertEqual(block.compiled_from_claim_ids, [])
        self.assertEqual(block.source_observation_ids, [])
        self.assertEqual(block.source_paths, self.fixture["expected_blocks"]["policy_paths"])
        self.assertIn("version-controlled files", block.value)

    def test_build_phase7d_memory_blocks_returns_expected_order(self) -> None:
        blocks = build_phase7d_memory_blocks(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
            policy_source_paths=self.fixture["policy_source_paths"],
            project_scope_ref="pks-phase-7",
        )
        self.assertEqual(
            [block.label for block in blocks],
            self.fixture["expected_blocks"]["labels"],
        )

    def test_memory_block_round_trip(self) -> None:
        block = build_operator_profile_block(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
        )
        self.assertIsNotNone(block)
        self.assertEqual(Phase7MemoryBlock.from_dict(block.to_dict()), block)

    def test_claim_backed_blocks_include_source_traceability(self) -> None:
        block = build_operator_profile_block(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
        )
        self.assertIsNotNone(block)
        self.assertEqual(block.source_observation_ids, ["obs_operator_style", "obs_operator_testing"])
        self.assertEqual(block.source_paths, ["observations[operator_style]", "observations[operator_testing]"])

    def test_claim_backed_block_requires_source_paths(self) -> None:
        with self.assertRaises(ValueError) as context:
            build_operator_profile_block(
                self.current_view,
                [],
                generated_at=self.generated_at,
            )
        self.assertIn("source_paths", str(context.exception))

    def test_blocks_are_read_only(self) -> None:
        with self.assertRaises(ValueError) as context:
            Phase7MemoryBlock(
                block_id="block_bad",
                label="operator_profile",
                description="Bad writable block.",
                value="Bad writable block.",
                scope="operator",
                read_only=False,
                chars_limit=200,
                compiled_from_claim_ids=["claim_operator_style"],
                source_observation_ids=["obs_operator_style"],
                source_paths=["observations[operator_style]"],
            )
        self.assertIn("read_only", str(context.exception))

    def test_value_cannot_exceed_chars_limit(self) -> None:
        with self.assertRaises(ValueError) as context:
            Phase7MemoryBlock(
                block_id="block_too_large",
                label="operator_profile",
                description="Oversized block.",
                value="x" * 81,
                scope="operator",
                read_only=True,
                chars_limit=80,
                compiled_from_claim_ids=["claim_operator_style"],
                source_observation_ids=["obs_operator_style"],
                source_paths=["observations[operator_style]"],
            )
        self.assertIn("value exceeds chars_limit", str(context.exception))

    def test_builder_truncates_to_chars_limit(self) -> None:
        block = build_operator_profile_block(
            self.current_view,
            self.observations,
            generated_at=self.generated_at,
            chars_limit=110,
        )
        self.assertIsNotNone(block)
        self.assertLessEqual(len(block.value), 110)
        self.assertIn("[truncated]", block.value)

    def test_invalid_chars_limit_fails(self) -> None:
        with self.assertRaises(ValueError) as context:
            build_policy_pointer_block(
                source_paths=self.fixture["policy_source_paths"],
                generated_at=self.generated_at,
                chars_limit=40,
            )
        self.assertIn("chars_limit", str(context.exception))

    def test_policy_pointer_requires_source_paths(self) -> None:
        with self.assertRaises(ValueError) as context:
            build_policy_pointer_block(source_paths=[], generated_at=self.generated_at)
        self.assertIn("source_paths", str(context.exception))

    def test_claim_backed_block_requires_claim_provenance(self) -> None:
        with self.assertRaises(ValueError) as context:
            Phase7MemoryBlock(
                block_id="block_no_claims",
                label="project_status",
                description="Missing claim provenance.",
                value="Missing claim provenance.",
                scope="project",
                read_only=True,
                chars_limit=200,
                compiled_from_claim_ids=[],
                source_observation_ids=["obs_project_goal"],
                source_paths=["observations[project_goal]"],
            )
        self.assertIn("compiled_from_claim_ids", str(context.exception))


if __name__ == "__main__":
    unittest.main()
