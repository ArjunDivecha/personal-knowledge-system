"""
=============================================================================
SCRIPT NAME: test_precedence_lattice.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/precedence_fixtures.json
  (read indirectly via utils.precedence.evaluate_precedence_fixtures)
OUTPUT FILES: None.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Covers INV2 of contract PKS-CONTRADICTION-LIFECYCLE-001: the precedence
lattice comparator (distillation/utils/precedence.py's compare_claims) is
correct on the full labeled pair set in shared/precedence_fixtures.json —
any user assertion outranks any assistant assertion regardless of recency;
decision > preference > fact > hypothesis within equal authority; equal
authority and kind falls back to as_of recency; behavioral-vs-stated user
conflicts return "escalate", never an automatic winner.

The same fixture table is replayed by the TypeScript twin
(cloudflare-mcp/mcp-server/test/precedence.test.ts) — that shared table,
not this file, is the lockstep proof the two implementations agree.

DEPENDENCIES: Python 3.14 stdlib unittest only.
USAGE:
  python -m unittest tests.python.test_precedence_lattice -v
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

from utils.precedence import compare_claims, evaluate_precedence_fixtures  # noqa: E402

MIN_FIXTURE_COUNT = 20


class PrecedenceFixtureTableTests(unittest.TestCase):
    def test_fixture_table_has_adequate_coverage(self) -> None:
        results = evaluate_precedence_fixtures()
        self.assertGreaterEqual(
            len(results), MIN_FIXTURE_COUNT,
            f"expected at least {MIN_FIXTURE_COUNT} labeled cases, found {len(results)}",
        )

    def test_every_fixture_case_passes(self) -> None:
        results = evaluate_precedence_fixtures()
        failures = [r for r in results if not r["ok"]]
        self.assertEqual(
            failures, [],
            f"{len(failures)}/{len(results)} precedence fixture cases failed: {failures}",
        )


class PrecedenceDirectAssertionTests(unittest.TestCase):
    """Direct, human-readable assertions for the invariant's headline claims,
    independent of the fixture table (belt-and-suspenders on the two cases
    the contract calls out by name)."""

    def test_march_user_decision_beats_yesterday_assistant_fact(self) -> None:
        march_decision = {"asserted_by": "user", "assertion_kind": "decision",
                          "behavioral": False, "as_of": "2026-03-01T00:00:00Z"}
        yesterday_fact = {"asserted_by": "assistant", "assertion_kind": "fact",
                          "behavioral": False, "as_of": "2026-07-08T00:00:00Z"}
        self.assertEqual(compare_claims(march_decision, yesterday_fact), "a_wins")
        self.assertEqual(compare_claims(yesterday_fact, march_decision), "b_wins")

    def test_behavioral_vs_stated_user_conflict_always_escalates(self) -> None:
        stated = {"asserted_by": "user", "assertion_kind": "preference",
                  "behavioral": False, "as_of": "2026-01-01T00:00:00Z"}
        behavioral = {"asserted_by": "behavioral", "assertion_kind": "fact",
                      "behavioral": False, "as_of": "2026-07-01T00:00:00Z"}
        self.assertEqual(compare_claims(stated, behavioral), "escalate")
        self.assertEqual(compare_claims(behavioral, stated), "escalate")

    def test_recency_is_never_consulted_when_authority_differs(self) -> None:
        old_user = {"asserted_by": "user", "assertion_kind": "fact",
                    "behavioral": False, "as_of": "2020-01-01T00:00:00Z"}
        new_assistant = {"asserted_by": "assistant", "assertion_kind": "decision",
                         "behavioral": False, "as_of": "2026-07-10T00:00:00Z"}
        # Even though new_assistant is far more recent AND higher durability,
        # authority (user > assistant) decides first.
        self.assertEqual(compare_claims(old_user, new_assistant), "a_wins")


if __name__ == "__main__":
    unittest.main()
