from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_DIR = REPO_ROOT / "distillation"

if str(DISTILLATION_DIR) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_DIR))

from models.entries import KnowledgeEntry


class KnowledgeEntryDeserializationTests(unittest.TestCase):
    def test_legacy_evolution_without_delta_deserializes(self) -> None:
        entry = KnowledgeEntry.from_dict(
            {
                "id": "ke_legacy_delta",
                "type": "knowledge",
                "domain": "Legacy evolution parsing",
                "current_view": "Legacy records should not break validation.",
                "evolution": [
                    {
                        "trigger": "migration",
                        "from_view": "old view",
                        "to_view": "new view",
                        "date": "2026-05-07T00:00:00+00:00",
                        "evidence": None,
                    }
                ],
                "metadata": {
                    "created_at": "2026-05-07T00:00:00+00:00",
                    "updated_at": "2026-05-07T00:00:00+00:00",
                    "source_conversations": [],
                    "source_messages": [],
                },
            }
        )

        self.assertEqual(len(entry.evolution), 1)
        self.assertIn("old view", entry.evolution[0].delta)
        self.assertIn("new view", entry.evolution[0].delta)


if __name__ == "__main__":
    unittest.main()
