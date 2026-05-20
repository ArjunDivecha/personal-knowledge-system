from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import KnowledgeEntry, KnowledgeMetadata, NormalizedConversation, NormalizedMessage
from pipeline.extract import apply_explicit_save_flags, explicit_save_message_ids
from utils.signal_flags import has_explicit_save_marker


class SignalFlagTests(unittest.TestCase):
    def test_explicit_save_regexes_match_user_markers(self) -> None:
        self.assertTrue(has_explicit_save_marker("Remember this for later: Arjun prefers repo-grounded answers."))
        self.assertTrue(has_explicit_save_marker("Please commit that to memory."))
        self.assertTrue(has_explicit_save_marker("Keep this in mind when editing the worker."))
        self.assertFalse(has_explicit_save_marker("I remember that command failing yesterday."))

    def test_explicit_save_flag_applies_to_entries_with_matching_evidence(self) -> None:
        conversation = NormalizedConversation(
            id="conv-1",
            source="claude",
            title="Signal fixture",
            created_at="2026-05-17T00:00:00Z",
            updated_at="2026-05-17T00:01:00Z",
            messages=[
                NormalizedMessage(
                    message_id="msg-save",
                    role="user",
                    content="Remember this for later: the Worker is the production MCP path.",
                    created_at="2026-05-17T00:00:00Z",
                ),
                NormalizedMessage(
                    message_id="msg-assistant",
                    role="assistant",
                    content="Noted.",
                    created_at="2026-05-17T00:01:00Z",
                ),
            ],
        )
        entry = KnowledgeEntry(
            id="ke_signal",
            domain="MCP production path",
            current_view="The Worker is the production MCP path.",
            metadata=KnowledgeMetadata(
                created_at="2026-05-17T00:00:00Z",
                updated_at="2026-05-17T00:01:00Z",
                source_conversations=["conv-1"],
                source_messages=["msg-save"],
            ),
        )

        self.assertEqual(explicit_save_message_ids(conversation), {"msg-save"})
        apply_explicit_save_flags([entry], conversation)

        self.assertEqual(entry.metadata.signal_flags, ["explicit_save"])


if __name__ == "__main__":
    unittest.main()
