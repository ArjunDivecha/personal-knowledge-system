"""
=============================================================================
SCRIPT NAME: test_memory_policy_admission_dedup.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
  : read-only, the real checked-in file (no mocking, no tempdir).

OUTPUT FILES: None.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Regression guard for contract PKS-ADMISSION-DEDUP-001 (INV3/INV5): the
checked-in shared/memory_policy.json must define an `admission_dedup` block
whose `enabled` and `dry_run` flags default to the safe/off state
(enabled=false, dry_run=true), and whose link_threshold sits strictly below
append_threshold. If someone flips either flag in the JSON by accident
(e.g. while tuning thresholds), this test fails loudly rather than letting
live writes silently turn on.

No other test file in tests/python/ currently loads memory_policy.json as a
whole document for a checked-in-defaults assertion (test_memory_fading.py
reads dream_thresholds via distillation's load_memory_policy() for value
lookups, not a defaults regression guard), so this is a new file rather than
an addition to an existing one.

DEPENDENCIES: Python 3.14 stdlib unittest only.
USAGE:
  python -m unittest tests.python.test_memory_policy_admission_dedup -v
=============================================================================
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "shared" / "memory_policy.json"


class AdmissionDedupCheckedInDefaultsTests(unittest.TestCase):
    def setUp(self):
        with POLICY_PATH.open() as handle:
            self.policy = json.load(handle)

    def test_admission_dedup_block_is_present(self):
        self.assertIn("admission_dedup", self.policy)

    def test_enabled_defaults_to_false(self):
        self.assertIs(
            self.policy["admission_dedup"]["enabled"], False,
            "admission_dedup.enabled must default to False — flipping this "
            "on accidentally turns on live routing writes",
        )

    def test_dry_run_defaults_to_true(self):
        self.assertIs(
            self.policy["admission_dedup"]["dry_run"], True,
            "admission_dedup.dry_run must default to True — flipping this "
            "off accidentally makes route() decisions mutate storage",
        )

    def test_link_threshold_is_below_append_threshold(self):
        block = self.policy["admission_dedup"]
        self.assertLess(
            block["link_threshold"], block["append_threshold"],
            "link band must sit strictly below the append threshold",
        )

    def test_thresholds_are_present_and_in_unit_range(self):
        block = self.policy["admission_dedup"]
        for key in ("append_threshold", "link_threshold"):
            self.assertIn(key, block)
            self.assertGreaterEqual(block[key], 0.0)
            self.assertLessEqual(block[key], 1.0)


if __name__ == "__main__":
    unittest.main()
