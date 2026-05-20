from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models import KnowledgeEntry, KnowledgeMetadata, NormalizedConversation, NormalizedMessage
from pipeline.corrections import (
    CORRECTION_CONTEST_HINT_PREFIX,
    build_correction_entries,
    detect_correction_events,
    propose_correction_contest_hints,
)


def load_roundtrip_fixture() -> dict:
    fixtures = json.loads((REPO_ROOT / "shared" / "salience_fixtures.json").read_text())
    return next(fixture for fixture in fixtures if fixture.get("name") == "correction_event_roundtrip")


class FakeRedis:
    def __init__(self, entries: dict[str, KnowledgeEntry]):
        self.entries = entries
        self.values: dict[str, str] = {}

    def get_knowledge_entry(self, entry_id: str):
        return self.entries.get(entry_id)

    def set(self, key: str, value: str):
        self.values[key] = value


class FakeVector:
    def search_by_text(self, query_embedding, top_k, entry_type, min_score):
        return [
            {
                "id": "ke_prior",
                "score": 0.91,
                "metadata": {"type": entry_type},
            }
        ]


def make_conversation() -> NormalizedConversation:
    return NormalizedConversation(
        id="conv-correction",
        source="claude",
        title="Correction fixture",
        created_at="2026-05-17T00:00:00Z",
        updated_at="2026-05-17T00:02:00Z",
        messages=[
            NormalizedMessage(
                message_id="msg-assistant",
                role="assistant",
                created_at="2026-05-17T00:00:00Z",
                content="The PM MCP server is read-only.",
            ),
            NormalizedMessage(
                message_id="msg-user",
                role="user",
                created_at="2026-05-17T00:01:00Z",
                content="No, the PM MCP has a write-capable /mcp endpoint; only /openai/mcp is read-only.",
            ),
        ],
    )


class CorrectionEventTests(unittest.TestCase):
    def test_detection_creates_correction_derived_entry(self) -> None:
        fixture = load_roundtrip_fixture()

        def classifier(prompt: str, **kwargs):
            return (
                fixture["classifier_output"],
                11,
                7,
            )

        result = detect_correction_events(make_conversation(), classifier=classifier)
        entries = build_correction_entries(result.events)

        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.input_tokens, 11)
        self.assertEqual(result.output_tokens, 7)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].current_view, "The PM MCP has a write-capable /mcp endpoint; only /openai/mcp is read-only.")
        self.assertEqual(entries[0].metadata.signal_flags, fixture["expected"]["new_entry_signal_flags"])
        self.assertEqual(entries[0].metadata.context_type, fixture["expected"]["new_entry_context_type"])

    def test_contest_hint_is_written_for_tier_1_or_2_prior_entry(self) -> None:
        event_result = detect_correction_events(
            make_conversation(),
            classifier=lambda prompt, **kwargs: (
                {
                    "is_correction": True,
                    "corrected_belief": "The PM MCP server is read-only.",
                    "new_belief": "The PM MCP has a write-capable /mcp endpoint; only /openai/mcp is read-only.",
                    "confidence": 0.9,
                },
                1,
                1,
            ),
        )
        prior = KnowledgeEntry(
            id="ke_prior",
            domain="PM MCP endpoint split",
            current_view="The PM MCP server is read-only.",
            state="active",
            metadata=KnowledgeMetadata(
                created_at="2026-05-16T00:00:00Z",
                updated_at="2026-05-16T00:00:00Z",
                source_conversations=["old-conv"],
                source_messages=["old-msg"],
                context_type="professional_identity",
                mention_count=1,
            ),
        )
        redis = FakeRedis({"ke_prior": prior})

        result = propose_correction_contest_hints(
            events=event_result.events,
            redis_client=redis,
            vector_client=FakeVector(),
            embedding_fn=lambda text: ([0.1, 0.2], 5),
            judge=lambda prompt, **kwargs: (
                {
                    "contradicts": True,
                    "confidence": 0.88,
                    "reason": "prior says read-only, correction says write-capable /mcp exists",
                },
                13,
                8,
            ),
        )

        self.assertEqual(result.hints_created, 1)
        self.assertEqual(result.candidates_checked, 1)
        self.assertEqual(result.input_tokens, 13)
        self.assertEqual(result.output_tokens, 8)
        [(key, raw_hint)] = redis.values.items()
        self.assertTrue(key.startswith(CORRECTION_CONTEST_HINT_PREFIX))
        hint = json.loads(raw_hint)
        self.assertEqual(hint["proposal_kind"], load_roundtrip_fixture()["expected"]["contest_proposal_kind"])
        self.assertEqual(hint["status"], "pending")
        self.assertEqual(hint["target_entry_id"], load_roundtrip_fixture()["expected"]["contest_target_entry_id"])
        self.assertEqual(hint["new_belief"], "The PM MCP has a write-capable /mcp endpoint; only /openai/mcp is read-only.")


if __name__ == "__main__":
    unittest.main()
