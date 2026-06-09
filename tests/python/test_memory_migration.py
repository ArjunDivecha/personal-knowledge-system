from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
DISTILLATION = REPO_ROOT / "distillation"
for path in (str(SCRIPTS), str(DISTILLATION)):
    if path not in sys.path:
        sys.path.insert(0, path)

from _memory_migration import normalize_entry_for_phase2  # noqa: E402
from models.entries import (  # noqa: E402
    KnowledgeEntry,
    KnowledgeMetadata,
    ProjectEntry,
    ProjectMetadata,
)


class Phase2NormalizationTierTests(unittest.TestCase):
    def test_knowledge_normalization_preserves_stored_percentile_tier(self) -> None:
        entry = KnowledgeEntry(
            id="ke_test",
            domain="PKS verifier",
            metadata=KnowledgeMetadata(
                created_at="2026-06-08T00:00:00Z",
                updated_at="2026-06-08T00:00:00Z",
                source_conversations=["conv"],
                source_messages=[],
                context_type="active_project",
                injection_tier=2,
            ),
        )

        normalize_entry_for_phase2(entry)

        self.assertIsNotNone(entry.metadata)
        self.assertEqual(entry.metadata.injection_tier, 2)

    def test_project_normalization_preserves_stored_percentile_tier(self) -> None:
        entry = ProjectEntry(
            id="pe_test",
            name="PKS verifier",
            metadata=ProjectMetadata(
                created_at="2026-06-08T00:00:00Z",
                updated_at="2026-06-08T00:00:00Z",
                last_touched="2026-06-08T00:00:00Z",
                source_conversations=["conv"],
                source_messages=[],
                context_type="active_project",
                injection_tier=2,
            ),
        )

        normalize_entry_for_phase2(entry)

        self.assertIsNotNone(entry.metadata)
        self.assertEqual(entry.metadata.injection_tier, 2)

    def test_normalization_defaults_missing_tier_from_context(self) -> None:
        entry = KnowledgeEntry(
            id="ke_default",
            domain="PKS verifier",
            metadata=KnowledgeMetadata(
                created_at="2026-06-08T00:00:00Z",
                updated_at="2026-06-08T00:00:00Z",
                source_conversations=["conv"],
                source_messages=[],
                context_type="active_project",
                injection_tier=None,
            ),
        )

        normalize_entry_for_phase2(entry)

        self.assertIsNotNone(entry.metadata)
        self.assertEqual(entry.metadata.injection_tier, 1)


if __name__ == "__main__":
    unittest.main()
