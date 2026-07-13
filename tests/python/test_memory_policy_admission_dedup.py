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
matching its DELIBERATE, signed-off configuration, with link_threshold
strictly below append_threshold. If someone changes a flag in the JSON by
accident (e.g. while tuning thresholds), this test fails loudly rather than
letting the change land silently.

STATE HISTORY — this guard originally pinned the safe/off state
(enabled=false, dry_run=true) and asserted live mode could never be checked
in without explicit sign-off. It did exactly that job: it failed the moment
live mode was first flipped, forcing the decision to be made consciously.

On 2026-07-12 Arjun explicitly signed off on LIVE mode (enabled=true,
dry_run=false). The evidence: a shadow ingestion run of two already-ingested
repos (ASADO, Triptych) extracted 316 entries, of which 263 were near-
duplicates of entries that already existed (median cosine 0.951). Shadow
mode logs that problem but does not prevent it — by design it behaves like
today's pipeline, creating every entry — so it was polluting the corpus on
every ingestion. Live mode is what actually stops the leak.

This guard now pins the live configuration. Changing it in EITHER direction
requires a deliberate decision and a deliberate edit to this file.

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

    def test_enabled_matches_signed_off_state(self):
        self.assertIs(
            self.policy["admission_dedup"]["enabled"], True,
            "admission_dedup.enabled was signed off as True on 2026-07-12; "
            "setting it back to False re-opens the near-duplicate ingestion "
            "leak (263 of 316 entries in the shadow run were near-dups)",
        )

    def test_dry_run_matches_signed_off_live_state(self):
        self.assertIs(
            self.policy["admission_dedup"]["dry_run"], False,
            "admission_dedup.dry_run was signed off as False (LIVE) on "
            "2026-07-12. In dry_run, route() computes and logs a decision but "
            "does NOT act on it — the near-duplicate entry still gets created. "
            "Only live mode actually prevents the pollution. Changing this "
            "requires a deliberate decision and a deliberate edit here.",
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
