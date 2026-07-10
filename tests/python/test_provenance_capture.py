"""
=============================================================================
SCRIPT NAME: test_provenance_capture.py
=============================================================================

INPUT FILES: None. All fixtures are constructed in-memory; no file I/O.
OUTPUT FILES: None. unittest reports to stdout only.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Covers INV1 and INV5 of contract PKS-CONTRADICTION-LIFECYCLE-001
(/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/contradiction-lifecycle.spec.md):

INV1 - asserted_by derives correctly from message role at extraction:
       user-authored messages yield "user", assistant messages yield
       "assistant", extractor generalizations (no role map) yield
       "inferred"; missing/unknown message ids are never invented as user
       or assistant, they fall back to "inferred".
INV5 - the Evidence schema change is additive and backward-compatible: an
       old-format entry JSON with no asserted_by/assertion_kind keys
       round-trips through KnowledgeEntry.from_dict -> .to_dict unchanged,
       and new entries omit the keys entirely when the values are None
       (never emit "asserted_by": null).

Also exercises the two real call sites that populate provenance at
extraction time: distillation.pipeline.extract.convert_to_knowledge_entry
(role-derived) and distillation.pipeline.corrections.correction_event_to_entry
(hardcoded user/correction — the user explicitly stated the correction).

DEPENDENCIES: Python 3.14 stdlib unittest only; imports distillation package
modules directly (sys.path insert, matching the convention in
tests/python/test_tier_percentile.py and friends).

USAGE:
  python -m unittest tests.python.test_provenance_capture -v
  (or via the repo-wide checker: make test-python-checker)
=============================================================================
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION = REPO_ROOT / "distillation"
if str(DISTILLATION) not in sys.path:
    sys.path.insert(0, str(DISTILLATION))

from models.entries import Evidence, KnowledgeEntry  # noqa: E402
from models.normalized import NormalizedConversation, NormalizedMessage  # noqa: E402
from pipeline.corrections import CorrectionEvent, correction_event_to_entry  # noqa: E402
from pipeline.extract import convert_to_knowledge_entry  # noqa: E402
from utils.precedence import derive_asserted_by  # noqa: E402


class DeriveAssertedByTests(unittest.TestCase):
    """INV1: role -> asserted_by mapping, the pure function."""

    def test_user_message_yields_user(self) -> None:
        roles = {"m1": "user", "m2": "assistant"}
        self.assertEqual(derive_asserted_by(["m1"], roles), "user")

    def test_any_cited_user_message_wins_even_if_others_are_assistant(self) -> None:
        roles = {"m1": "assistant", "m2": "user"}
        self.assertEqual(derive_asserted_by(["m1", "m2"], roles), "user")

    def test_all_assistant_messages_yield_assistant(self) -> None:
        roles = {"m1": "assistant", "m2": "assistant"}
        self.assertEqual(derive_asserted_by(["m1", "m2"], roles), "assistant")

    def test_unknown_message_id_falls_back_to_inferred(self) -> None:
        roles = {"m1": "assistant"}
        self.assertEqual(derive_asserted_by(["m1", "m_unknown"], roles), "inferred")

    def test_no_role_map_yields_inferred(self) -> None:
        self.assertEqual(derive_asserted_by(["m1"], None), "inferred")

    def test_empty_message_ids_yields_inferred(self) -> None:
        self.assertEqual(derive_asserted_by([], {"m1": "user"}), "inferred")

    def test_empty_role_map_yields_inferred(self) -> None:
        self.assertEqual(derive_asserted_by(["m1"], {}), "inferred")

    def test_unrecognized_role_is_never_invented_as_assistant(self) -> None:
        # A found-but-unrecognized role (e.g. "system", "tool") must fall back
        # to inferred, never be silently promoted to assistant-level authority.
        # Regression case: caught by adversarial review 2026-07-10.
        self.assertEqual(derive_asserted_by(["m1"], {"m1": "system"}), "inferred")
        self.assertEqual(derive_asserted_by(["m1"], {"m1": "tool"}), "inferred")

    def test_mixed_assistant_and_unrecognized_role_yields_inferred(self) -> None:
        roles = {"m1": "assistant", "m2": "system"}
        self.assertEqual(derive_asserted_by(["m1", "m2"], roles), "inferred")

    def test_user_still_wins_even_alongside_an_unrecognized_role(self) -> None:
        roles = {"m1": "system", "m2": "user"}
        self.assertEqual(derive_asserted_by(["m1", "m2"], roles), "user")


class ExtractionWiringTests(unittest.TestCase):
    """INV1 at the real extraction call site: convert_to_knowledge_entry."""

    def _conversation(self) -> NormalizedConversation:
        return NormalizedConversation(
            id="conv_1",
            source="claude",
            title="test",
            created_at="2026-07-01T00:00:00Z",
            updated_at="2026-07-01T00:00:00Z",
            messages=[
                NormalizedMessage(message_id="msg_user", role="user",
                                  created_at="2026-07-01T00:00:00Z", content="I decided X"),
                NormalizedMessage(message_id="msg_asst", role="assistant",
                                  created_at="2026-07-01T00:01:00Z", content="Noted, X it is"),
            ],
        )

    def test_key_insight_evidence_citing_user_message_is_asserted_by_user(self) -> None:
        roles_by_id = {m.message_id: m.role for m in self._conversation().messages}
        data = {
            "current_view": "irrelevant for this test",
            "key_insights": [
                {"insight": "Arjun decided X", "evidence": {"message_ids": ["msg_user"], "snippet": "I decided X"}},
            ],
        }
        entry = convert_to_knowledge_entry(data, "conv_1", roles_by_id=roles_by_id)
        self.assertEqual(entry.key_insights[0].evidence.asserted_by, "user")
        self.assertEqual(entry.key_insights[0].evidence.assertion_kind, "fact")

    def test_key_insight_evidence_citing_only_assistant_message_is_asserted_by_assistant(self) -> None:
        roles_by_id = {m.message_id: m.role for m in self._conversation().messages}
        data = {
            "current_view": "irrelevant for this test",
            "key_insights": [
                {"insight": "assistant suggested Y", "evidence": {"message_ids": ["msg_asst"], "snippet": "Noted, X it is"}},
            ],
        }
        entry = convert_to_knowledge_entry(data, "conv_1", roles_by_id=roles_by_id)
        self.assertEqual(entry.key_insights[0].evidence.asserted_by, "assistant")

    def test_no_roles_by_id_yields_inferred(self) -> None:
        data = {
            "current_view": "irrelevant for this test",
            "key_insights": [
                {"insight": "generalized insight", "evidence": {"message_ids": ["msg_user"], "snippet": "..."}},
            ],
        }
        entry = convert_to_knowledge_entry(data, "conv_1", roles_by_id=None)
        self.assertEqual(entry.key_insights[0].evidence.asserted_by, "inferred")

    def test_capability_evidence_is_assertion_kind_hypothesis(self) -> None:
        data = {
            "current_view": "irrelevant for this test",
            "knows_how_to": [{"capability": "debug MCP", "evidence": {"message_ids": [], "snippet": "s"}}],
        }
        entry = convert_to_knowledge_entry(data, "conv_1", roles_by_id=None)
        self.assertEqual(entry.knows_how_to[0].evidence.assertion_kind, "hypothesis")


class CorrectionEventProvenanceTests(unittest.TestCase):
    """The correction pipeline (distillation/pipeline/corrections.py) hardcodes
    asserted_by=user, assertion_kind=correction — a user correction is by
    definition the user directly stating a durable correction."""

    def test_correction_event_evidence_is_user_correction(self) -> None:
        event = CorrectionEvent(
            event_id="ce_abc123def456",
            conversation_id="conv_correction",
            message_id="msg_correction",
            corrected_belief="old belief",
            new_belief="corrected belief",
            confidence=0.9,
            user_text="Actually, that's wrong — the real answer is corrected belief.",
            source_timestamp="2026-07-01T00:00:00Z",
        )
        entry = correction_event_to_entry(event)
        evidence = entry.positions[0].evidence
        self.assertEqual(evidence.asserted_by, "user")
        self.assertEqual(evidence.assertion_kind, "correction")


class AdditiveSchemaRoundTripTests(unittest.TestCase):
    """INV5: schema change is additive and backward-compatible."""

    def _old_format_entry_dict(self) -> dict:
        """A minimal, realistic pre-change entry: no asserted_by/assertion_kind
        anywhere in its evidence blocks."""
        return {
            "id": "ke_oldformat0001",
            "domain": "pre-existing entry",
            "type": "knowledge",
            "subdomain": None,
            "state": "active",
            "detail_level": "full",
            "current_view": "Some durable summary written before this contract.",
            "confidence": "medium",
            "positions": [{
                "view": "Some durable summary written before this contract.",
                "confidence": "medium",
                "as_of": "2026-01-01T00:00:00Z",
                "evidence": {
                    "conversation_id": "conv_old",
                    "message_ids": ["msg_old_1"],
                    "snippet": "an old snippet",
                },
            }],
            "key_insights": [{
                "insight": "an old insight",
                "evidence": {
                    "conversation_id": "conv_old",
                    "message_ids": ["msg_old_2"],
                    "snippet": "another old snippet",
                },
            }],
            "knows_how_to": [],
            "open_questions": [],
            "related_repos": [],
            "related_knowledge": [],
            "evolution": [],
            "metadata": {
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "source_conversations": ["conv_old"],
                "source_messages": ["msg_old_1", "msg_old_2"],
            },
            "full_content_ref": None,
        }

    def test_old_format_entry_round_trips_unchanged(self) -> None:
        original = self._old_format_entry_dict()
        entry = KnowledgeEntry.from_dict(original)
        result = entry.to_dict()

        self.assertNotIn("asserted_by", result["positions"][0]["evidence"])
        self.assertNotIn("assertion_kind", result["positions"][0]["evidence"])
        self.assertNotIn("asserted_by", result["key_insights"][0]["evidence"])
        self.assertNotIn("assertion_kind", result["key_insights"][0]["evidence"])

        self.assertEqual(result["id"], original["id"])
        self.assertEqual(result["current_view"], original["current_view"])
        self.assertEqual(
            result["positions"][0]["evidence"]["snippet"],
            original["positions"][0]["evidence"]["snippet"],
        )
        self.assertEqual(
            result["key_insights"][0]["evidence"]["message_ids"],
            original["key_insights"][0]["evidence"]["message_ids"],
        )

    def test_evidence_with_none_provenance_serializes_without_the_keys(self) -> None:
        evidence = Evidence(conversation_id="c", message_ids=["m"], snippet="s")
        self.assertIsNone(evidence.asserted_by)
        self.assertIsNone(evidence.assertion_kind)
        entry_dict = self._old_format_entry_dict()
        entry_dict["positions"][0]["evidence"] = {
            "conversation_id": evidence.conversation_id,
            "message_ids": evidence.message_ids,
            "snippet": evidence.snippet,
        }
        result = KnowledgeEntry.from_dict(entry_dict).to_dict()
        self.assertNotIn("asserted_by", result["positions"][0]["evidence"])

    def test_new_format_entry_with_provenance_round_trips_with_the_keys(self) -> None:
        entry_dict = self._old_format_entry_dict()
        entry_dict["positions"][0]["evidence"]["asserted_by"] = "user"
        entry_dict["positions"][0]["evidence"]["assertion_kind"] = "decision"
        result = KnowledgeEntry.from_dict(entry_dict).to_dict()
        self.assertEqual(result["positions"][0]["evidence"]["asserted_by"], "user")
        self.assertEqual(result["positions"][0]["evidence"]["assertion_kind"], "decision")


if __name__ == "__main__":
    unittest.main()
