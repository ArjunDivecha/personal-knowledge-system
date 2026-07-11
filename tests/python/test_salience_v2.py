"""
=============================================================================
SCRIPT NAME: test_salience_v2.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/salience_v2_fixtures.json
    (read indirectly via utils.salience_v2.evaluate_salience_v2_fixtures)
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
    (read indirectly via utils.salience.load_memory_policy for the salience_v2 block)

OUTPUT FILES: None. unittest reports to stdout only.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Covers INV2 and INV3 of contract PKS-INJECTION-RANKING-002
(/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/injection-ranking-v2.spec.md):

INV2 - salience_v2 is the documented additive form (0.30 usage + 0.25
       evidence + 0.20 recency + 0.15 authority + 0.10 corroboration, each
       component clamped to [0,1]) with finite half-lives for every context
       type except explicit_save, and all five components persisted
       alongside the score. Asserted via the shared hand-computed fixture
       table (shared/salience_v2_fixtures.json), which includes an
       active_project case whose recency now decays with a 180-day
       half-life (distinct from v1's infinite half-life for the same
       context type).
INV3 - ordering ties break by (salience_v2, last_seen, evidence_count, id)
       — entry-id order can decide only when all preceding keys are equal.

The same fixture table is replayed by the TypeScript twin
(cloudflare-mcp/mcp-server/test/salience_v2.test.ts) — that shared table,
not this file, is the lockstep proof the two implementations agree.

DEPENDENCIES: Python 3.14 stdlib unittest only.
USAGE:
  python -m unittest tests.python.test_salience_v2 -v
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

from utils.salience_v2 import (  # noqa: E402
    compute_salience_v2,
    evaluate_salience_v2_fixtures,
    tiebreak_key,
)

MIN_FIXTURE_COUNT = 8


class SalienceV2FixtureTableTests(unittest.TestCase):
    def test_fixture_table_has_adequate_coverage(self) -> None:
        results = evaluate_salience_v2_fixtures()
        self.assertGreaterEqual(
            len(results), MIN_FIXTURE_COUNT,
            f"expected at least {MIN_FIXTURE_COUNT} labeled cases, found {len(results)}",
        )

    def test_every_fixture_case_matches_to_four_decimals(self) -> None:
        results = evaluate_salience_v2_fixtures()
        failures = [
            r for r in results
            if r["actual"]["salience_v2"] != r["expected"]["salience_v2"]
            or r["actual"]["components"] != r["expected"]["components"]
        ]
        self.assertEqual(
            failures, [],
            f"{len(failures)}/{len(results)} salience_v2 fixture cases failed: {failures}",
        )


class SalienceV2DirectAssertionTests(unittest.TestCase):
    """Belt-and-suspenders direct assertions for the invariant's headline
    claims, independent of the fixture table."""

    def test_active_project_recency_now_decays_at_180_days(self) -> None:
        # v1's half_lives_days has "infinity" for active_project (memory_policy.json).
        # v2's recency_half_lives_days gives it a finite 180-day half-life —
        # this is the specific regression INV2 calls out by name. At exactly
        # 180 days since last_seen, recency must be 0.5.
        from datetime import datetime, timezone

        entry = {
            "id": "ke_test_active_project",
            "metadata": {
                "context_type": "active_project",
                "mention_count": 1,
                "last_seen": "2026-01-11T00:00:00Z",
                "last_accessed": None,
                "source_conversations": ["c1"],
            },
            "key_insights": [],
        }
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        _score, components = compute_salience_v2(entry, now=now)
        self.assertEqual(components["recency"], 0.5)

    def test_explicit_save_recency_stays_1_regardless_of_age(self) -> None:
        from datetime import datetime, timezone

        entry = {
            "id": "ke_test_explicit_save",
            "metadata": {
                "context_type": "explicit_save",
                "mention_count": 1,
                "last_seen": "2020-01-01T00:00:00Z",
                "last_accessed": None,
                "source_conversations": ["c1"],
            },
            "key_insights": [],
        }
        now = datetime(2026, 7, 10, tzinfo=timezone.utc)
        _score, components = compute_salience_v2(entry, now=now)
        self.assertEqual(components["recency"], 1.0)

    def test_components_and_score_are_clamped_to_unit_interval(self) -> None:
        results = evaluate_salience_v2_fixtures()
        for r in results:
            score = r["actual"]["salience_v2"]
            self.assertGreaterEqual(score, 0.0, r["name"])
            self.assertLessEqual(score, 1.0, r["name"])
            for name, value in r["actual"]["components"].items():
                self.assertGreaterEqual(value, 0.0, f"{r['name']}.{name}")
                self.assertLessEqual(value, 1.0, f"{r['name']}.{name}")


class TiebreakOrderingTests(unittest.TestCase):
    """INV3: (salience_v2 desc, last_seen desc, evidence_count desc, id asc)."""

    def _entry(self, entry_id: str, last_seen: str | None, n_insights: int,
               n_positions: int = 0, n_knows: int = 0) -> dict:
        return {
            "id": entry_id,
            "metadata": {"last_seen": last_seen} if last_seen else {},
            "key_insights": [{}] * n_insights,
            "positions": [{}] * n_positions,
            "knows_how_to": [{}] * n_knows,
        }

    def test_higher_salience_wins_regardless_of_other_fields(self) -> None:
        a = self._entry("ke_a", "2020-01-01T00:00:00Z", 0)
        b = self._entry("ke_b", "2026-01-01T00:00:00Z", 5)
        ordered = sorted([(b, 0.10), (a, 0.90)], key=lambda pair: tiebreak_key(pair[0], pair[1]))
        self.assertEqual([e["id"] for e, _ in ordered], ["ke_a", "ke_b"])

    def test_equal_salience_breaks_on_last_seen_desc(self) -> None:
        older = self._entry("ke_older", "2026-01-01T00:00:00Z", 0)
        newer = self._entry("ke_newer", "2026-06-01T00:00:00Z", 0)
        ordered = sorted(
            [(older, 0.5), (newer, 0.5)],
            key=lambda pair: tiebreak_key(pair[0], pair[1]),
        )
        self.assertEqual([e["id"] for e, _ in ordered], ["ke_newer", "ke_older"])

    def test_equal_salience_and_last_seen_breaks_on_evidence_count_desc(self) -> None:
        thin = self._entry("ke_thin", "2026-01-01T00:00:00Z", 1)
        rich = self._entry("ke_rich", "2026-01-01T00:00:00Z", 2, n_positions=3, n_knows=1)
        ordered = sorted(
            [(thin, 0.5), (rich, 0.5)],
            key=lambda pair: tiebreak_key(pair[0], pair[1]),
        )
        self.assertEqual([e["id"] for e, _ in ordered], ["ke_rich", "ke_thin"])

    def test_full_tie_falls_back_to_id_ascending(self) -> None:
        z = self._entry("ke_z", "2026-01-01T00:00:00Z", 1)
        a = self._entry("ke_a", "2026-01-01T00:00:00Z", 1)
        ordered = sorted([(z, 0.5), (a, 0.5)], key=lambda pair: tiebreak_key(pair[0], pair[1]))
        self.assertEqual([e["id"] for e, _ in ordered], ["ke_a", "ke_z"])

    def test_missing_last_seen_sorts_as_oldest(self) -> None:
        missing = self._entry("ke_missing", None, 0)
        present = self._entry("ke_present", "2020-01-01T00:00:00Z", 0)
        ordered = sorted(
            [(missing, 0.5), (present, 0.5)],
            key=lambda pair: tiebreak_key(pair[0], pair[1]),
        )
        self.assertEqual([e["id"] for e, _ in ordered], ["ke_present", "ke_missing"])


if __name__ == "__main__":
    unittest.main()
