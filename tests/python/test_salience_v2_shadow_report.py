"""
=============================================================================
SCRIPT NAME: test_salience_v2_shadow_report.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/tests/fixtures/salience_v2_shadow_corpus_fixture.json
    (the bundled healthy fixture; read via report_salience_v2_distribution.evaluate)
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/tests/fixtures/salience_v2_shadow_corpus_degenerate_fixture.json
    (the bundled degenerate negative-control fixture)

OUTPUT FILES: None. unittest reports to stdout only.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Covers gate G3 of contract PKS-INJECTION-RANKING-002
(/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/injection-ranking-v2.spec.md):
scripts/report_salience_v2_distribution.py must pass (exit 0 / tie_rate <
1%, all 10 deciles occupied) on the bundled healthy fixture, and must FAIL
(exit 1) on a bundled degenerate fixture where ~40 entries cluster tightly
around one salience_v2 value — proving the gate can actually detect bad
discrimination, not just rubber-stamp its input. Imports the script's
functions directly (evaluate(), compute_tie_rate(), compute_decile_occupancy(),
main()) rather than shelling out to a subprocess, matching the preference in
tests/python/test_run_eval_compare.py for direct-import testing of scripts/
modules.

DEPENDENCIES: Python 3.14 stdlib only (unittest, pathlib, sys).
USAGE:
  python -m unittest tests.python.test_salience_v2_shadow_report -v
  (or via the repo-wide checker: make test-python-checker)
=============================================================================
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import report_salience_v2_distribution as report_mod  # noqa: E402

HEALTHY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "salience_v2_shadow_corpus_fixture.json"
DEGENERATE_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "salience_v2_shadow_corpus_degenerate_fixture.json"


class ShadowReportHealthyFixtureTests(unittest.TestCase):
    def test_healthy_fixture_passes(self) -> None:
        report = report_mod.evaluate(HEALTHY_FIXTURE)
        self.assertTrue(report["passed"], report)
        self.assertLess(report["tie_rate"], report_mod.TIE_RATE_THRESHOLD)
        self.assertEqual(report["empty_deciles"], [])

    def test_main_exits_zero_on_the_default_bundled_fixture(self) -> None:
        exit_code = report_mod.main([])
        self.assertEqual(exit_code, 0)

    def test_main_exits_zero_when_pointed_explicitly_at_the_healthy_fixture(self) -> None:
        exit_code = report_mod.main(["--fixture-file", str(HEALTHY_FIXTURE)])
        self.assertEqual(exit_code, 0)


class ShadowReportDegenerateFixtureTests(unittest.TestCase):
    """The negative control: proves the gate can fail, not just pass."""

    def test_degenerate_fixture_fails(self) -> None:
        report = report_mod.evaluate(DEGENERATE_FIXTURE)
        self.assertFalse(report["passed"], report)

    def test_degenerate_fixture_fails_on_tie_rate(self) -> None:
        report = report_mod.evaluate(DEGENERATE_FIXTURE)
        self.assertGreaterEqual(report["tie_rate"], report_mod.TIE_RATE_THRESHOLD)

    def test_degenerate_fixture_fails_on_decile_occupancy(self) -> None:
        report = report_mod.evaluate(DEGENERATE_FIXTURE)
        self.assertGreater(len(report["empty_deciles"]), 0)

    def test_main_exits_one_on_the_degenerate_fixture(self) -> None:
        exit_code = report_mod.main(["--fixture-file", str(DEGENERATE_FIXTURE)])
        self.assertEqual(exit_code, 1)


class TieRateAndDecileHelperTests(unittest.TestCase):
    """Direct unit tests of the pure helpers, independent of any fixture file."""

    def test_tie_rate_is_zero_for_all_distinct_values(self) -> None:
        self.assertEqual(report_mod.compute_tie_rate([0.1, 0.2, 0.3]), 0.0)

    def test_tie_rate_counts_only_entries_that_share_a_value(self) -> None:
        # 0.5 appears twice (2 tied entries); 0.9 is unique.
        values = [0.5, 0.5, 0.9]
        self.assertAlmostEqual(report_mod.compute_tie_rate(values), 2 / 3)

    def test_tie_rate_raises_on_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            report_mod.compute_tie_rate([])

    def test_decile_occupancy_buckets_correctly_including_the_1_0_edge(self) -> None:
        occupancy = report_mod.compute_decile_occupancy([0.0, 0.05, 0.55, 1.0])
        self.assertEqual(occupancy[0], 2)  # 0.0 and 0.05
        self.assertEqual(occupancy[5], 1)  # 0.55
        self.assertEqual(occupancy[9], 1)  # 1.0 falls in the last bin, not an 11th


if __name__ == "__main__":
    unittest.main()
