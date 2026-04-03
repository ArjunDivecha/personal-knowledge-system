from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"

if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from models.entries import KnowledgeEntry, KnowledgeMetadata
from utils.salience import compute_salience, load_memory_policy, resolve_stored_tier


def build_knowledge_entry(
    *,
    entry_id: str,
    context_type: str,
    confidence: str = "high",
    mention_count: int = 1,
    access_count: int = 0,
    last_seen: str = "2026-03-28T00:00:00+00:00",
    last_accessed: str | None = None,
) -> KnowledgeEntry:
    return KnowledgeEntry(
        id=entry_id,
        domain=f"Test domain {entry_id}",
        current_view="Test memory entry",
        confidence=confidence,
        metadata=KnowledgeMetadata(
            created_at=last_seen,
            updated_at=last_seen,
            source_conversations=["conv-1"],
            source_messages=["msg-1"],
            access_count=access_count,
            last_accessed=last_accessed,
            schema_version=2,
            classification_status="complete",
            context_type=context_type,
            mention_count=mention_count,
            first_seen=last_seen,
            last_seen=last_seen,
            auto_inferred=False,
            source_weights={"codex transcript": 1.0},
            injection_tier=None,
            salience_score=None,
            last_consolidated=None,
            consolidation_notes=[],
            archived=False,
        ),
    )


def is_archive_candidate(entry: KnowledgeEntry, *, now: datetime) -> bool:
    policy = load_memory_policy()
    metadata = entry.metadata
    salience = compute_salience(entry, now=now)
    return (
        metadata.context_type in {"task_query", "passing_reference"}
        and (metadata.mention_count or 1) == 1
        and metadata.access_count == 0
        and salience < float(policy["dream_thresholds"]["archive_candidate_salience"])
    )


class MemoryFadingTests(unittest.TestCase):
    def test_professional_identity_does_not_decay_over_time(self) -> None:
        entry = build_knowledge_entry(
            entry_id="ke_identity",
            context_type="professional_identity",
            mention_count=5,
            last_seen="2020-01-01T00:00:00+00:00",
        )

        near_score = compute_salience(entry, now=datetime(2026, 3, 28, 0, 0, tzinfo=UTC))
        far_score = compute_salience(entry, now=datetime(2032, 3, 28, 0, 0, tzinfo=UTC))

        self.assertEqual(resolve_stored_tier(entry), 1)
        self.assertEqual(near_score, far_score)
        self.assertGreater(near_score, 0.5)

    def test_recurring_pattern_salience_fades_over_time(self) -> None:
        entry = build_knowledge_entry(
            entry_id="ke_pattern",
            context_type="recurring_pattern",
            mention_count=3,
            last_seen="2026-03-28T00:00:00+00:00",
        )

        recent_score = compute_salience(entry, now=datetime(2026, 4, 1, 0, 0, tzinfo=UTC))
        later_score = compute_salience(entry, now=datetime(2026, 10, 1, 0, 0, tzinfo=UTC))

        self.assertEqual(resolve_stored_tier(entry), 2)
        self.assertGreater(recent_score, later_score)
        self.assertGreater(recent_score, 0.1)
        self.assertLess(later_score, 0.1)

    def test_one_off_task_query_becomes_dream_archive_candidate_without_reinforcement(self) -> None:
        entry = build_knowledge_entry(
            entry_id="ke_one_off",
            context_type="task_query",
            mention_count=1,
            access_count=0,
            last_seen="2026-03-20T00:00:00+00:00",
        )

        self.assertEqual(resolve_stored_tier(entry), 3)
        self.assertTrue(
            is_archive_candidate(
                entry,
                now=datetime(2026, 3, 28, 12, 0, tzinfo=UTC),
            )
        )

    def test_retrieved_one_off_resists_archive_until_reinforcement_expires(self) -> None:
        entry = build_knowledge_entry(
            entry_id="ke_retrieved_one_off",
            context_type="task_query",
            mention_count=1,
            access_count=1,
            last_seen="2026-03-20T00:00:00+00:00",
            last_accessed="2026-03-28T00:00:00+00:00",
        )

        immediate_score = compute_salience(entry, now=datetime(2026, 3, 28, 12, 0, tzinfo=UTC))
        later_score = compute_salience(entry, now=datetime(2026, 9, 28, 12, 0, tzinfo=UTC))

        self.assertGreater(immediate_score, later_score)
        self.assertFalse(
            is_archive_candidate(
                entry,
                now=datetime(2026, 3, 28, 12, 0, tzinfo=UTC),
            )
        )

    def test_passing_reference_has_low_tier_and_low_salience(self) -> None:
        entry = build_knowledge_entry(
            entry_id="ke_passing_reference",
            context_type="passing_reference",
            confidence="medium",
            mention_count=1,
            access_count=0,
            last_seen="2026-03-27T00:00:00+00:00",
        )

        score = compute_salience(entry, now=datetime(2026, 3, 28, 12, 0, tzinfo=UTC))

        self.assertEqual(resolve_stored_tier(entry), 3)
        self.assertLess(score, 0.05)
        self.assertTrue(
            is_archive_candidate(
                entry,
                now=datetime(2026, 3, 28, 12, 0, tzinfo=UTC),
            )
        )


if __name__ == "__main__":
    unittest.main()
