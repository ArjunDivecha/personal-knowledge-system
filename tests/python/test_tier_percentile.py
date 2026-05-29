"""
Unit tests for Phase 3 assign_tiers_by_percentile (PRD R3.1):
percentile cutoffs + identity-floor protection. No network.

Run: python -m unittest tests.python.test_tier_percentile
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION = REPO_ROOT / "distillation"
if str(DISTILLATION) not in sys.path:
    sys.path.insert(0, str(DISTILLATION))

from utils.salience import assign_tiers_by_percentile  # noqa: E402

POLICY = {
    "tier_percentiles": {
        "tier_1_top_pct": 0.10,
        "tier_2_next_pct": 0.20,
        "identity_floor_context_types": ["professional_identity", "stated_preference"],
    }
}


class TierPercentileTests(unittest.TestCase):
    def test_percentile_cutoffs(self):
        # 10 entries, salience 0.10..1.00 (ke_10 highest). top 10% -> 1 entry
        # Tier 1; next 20% -> 2 entries Tier 2; rest Tier 3.
        salience = {f"ke_{i:02d}": i / 10 for i in range(1, 11)}
        ctype = {k: "task_query" for k in salience}
        tiers = assign_tiers_by_percentile(salience, ctype, POLICY)
        # highest salience = ke_10
        self.assertEqual(tiers["ke_10"], 1)
        # next two (ke_09, ke_08) -> tier 2
        self.assertEqual(tiers["ke_09"], 2)
        self.assertEqual(tiers["ke_08"], 2)
        # the rest -> tier 3
        self.assertEqual(tiers["ke_01"], 3)
        counts = {t: sum(1 for v in tiers.values() if v == t) for t in (1, 2, 3)}
        self.assertEqual(counts[1], 1)
        self.assertEqual(counts[2], 2)
        self.assertEqual(counts[3], 7)

    def test_identity_floor_prevents_tier3(self):
        # A low-salience professional_identity entry must not fall below Tier 2.
        salience = {f"ke_{i:02d}": i / 100 for i in range(1, 21)}
        ctype = {k: "task_query" for k in salience}
        ctype["ke_01"] = "professional_identity"  # lowest salience
        tiers = assign_tiers_by_percentile(salience, ctype, POLICY)
        self.assertLessEqual(tiers["ke_01"], 2)  # floored to 2, not 3
        self.assertEqual(tiers["ke_01"], 2)

    def test_identity_can_still_be_tier1_if_high_salience(self):
        salience = {f"ke_{i:02d}": i / 20 for i in range(1, 21)}
        ctype = {k: "task_query" for k in salience}
        ctype["ke_20"] = "professional_identity"  # highest salience
        tiers = assign_tiers_by_percentile(salience, ctype, POLICY)
        self.assertEqual(tiers["ke_20"], 1)

    def test_empty(self):
        self.assertEqual(assign_tiers_by_percentile({}, {}, POLICY), {})

    def test_deterministic_tie_break(self):
        # All equal salience -> ranked by id; cutoffs still produce the right counts.
        salience = {f"ke_{i:02d}": 0.5 for i in range(1, 11)}
        ctype = {k: "task_query" for k in salience}
        tiers = assign_tiers_by_percentile(salience, ctype, POLICY)
        counts = {t: sum(1 for v in tiers.values() if v == t) for t in (1, 2, 3)}
        self.assertEqual(counts[1], 1)
        self.assertEqual(counts[2], 2)
        self.assertEqual(counts[3], 7)


if __name__ == "__main__":
    unittest.main()
