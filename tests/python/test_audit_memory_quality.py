"""
Unit tests for the pure logic in scripts/audit_memory_quality.py:
union-find clustering and the metric calculators (M1 tiers, M3 salience,
gate evaluation). No network — synthetic inputs only.

Run:
    python -m unittest tests.python.test_audit_memory_quality
or via:
    make test-python-checker
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "scripts"
DISTILLATION = REPO_ROOT / "distillation"
for p in (str(SCRIPTS), str(DISTILLATION)):
    if p not in sys.path:
        sys.path.insert(0, p)

import audit_memory_quality as audit  # noqa: E402


# --- synthetic entry shapes -------------------------------------------------
@dataclass
class _Meta:
    injection_tier: Optional[int] = None
    salience_score: Optional[float] = None
    access_count: int = 0
    last_accessed: Optional[str] = None
    last_touched: Optional[str] = None
    last_seen: Optional[str] = None
    updated_at: Optional[str] = None
    archived: bool = False


@dataclass
class _Entry:
    id: str
    domain: str
    metadata: _Meta


@dataclass
class _Decision:
    decision: str


@dataclass
class _Project:
    id: str
    name: str
    status: str
    goal: str = ""
    current_phase: str = ""
    blocked_on: Optional[str] = None
    decisions_made: list[_Decision] = field(default_factory=list)
    metadata: _Meta = field(default_factory=_Meta)


def mk(eid, tier=None, salience=None, access=0, last=None):
    return _Entry(id=eid, domain=f"dom-{eid}", metadata=_Meta(
        injection_tier=tier, salience_score=salience, access_count=access, last_accessed=last))


class UnionFindTests(unittest.TestCase):
    def test_transitive_grouping(self):
        # A~B, B~C => one cluster {A,B,C}; D alone.
        comps = audit.clusters_from_edges(
            ["A", "B", "C", "D"],
            [("A", "B"), ("B", "C")],
        )
        sizes = sorted(len(c) for c in comps)
        self.assertEqual(sizes, [1, 3])
        big = max(comps, key=len)
        self.assertEqual(sorted(big), ["A", "B", "C"])

    def test_no_edges_all_singletons(self):
        comps = audit.clusters_from_edges(["A", "B", "C"], [])
        self.assertEqual(sorted(len(c) for c in comps), [1, 1, 1])

    def test_two_separate_clusters(self):
        comps = audit.clusters_from_edges(
            ["A", "B", "C", "D"],
            [("A", "B"), ("C", "D")],
        )
        self.assertEqual(sorted(len(c) for c in comps), [2, 2])


class M1TierTests(unittest.TestCase):
    def test_shares(self):
        active = [mk("ke_1", tier=1), mk("ke_2", tier=1), mk("ke_3", tier=1),
                  mk("ke_4", tier=2), mk("ke_5", tier=3)]
        m1 = audit.compute_m1_tiers(active)
        self.assertEqual(m1["tier_1"], 3)
        self.assertEqual(m1["tier_2"], 1)
        self.assertEqual(m1["tier_3"], 1)
        self.assertAlmostEqual(m1["tier_1_share"], 0.6, places=3)

    def test_missing_tier_ignored_in_counts(self):
        active = [mk("ke_1", tier=1), mk("ke_2", tier=None)]
        m1 = audit.compute_m1_tiers(active)
        self.assertEqual(m1["tier_1"], 1)
        # share is over all active entries (2), so 0.5
        self.assertAlmostEqual(m1["tier_1_share"], 0.5, places=3)


class M3SalienceTests(unittest.TestCase):
    def test_degenerate_value_detected(self):
        # 8 of 10 share 0.2163 -> max_single_value_share = 0.8
        active = [mk(f"ke_{i}", salience=0.2163) for i in range(8)]
        active += [mk("ke_8", salience=0.5), mk("ke_9", salience=0.9)]
        m3 = audit.compute_m3_salience(active)
        self.assertAlmostEqual(m3["max_single_value_share"], 0.8, places=3)
        self.assertEqual(m3["top_values"][0]["value"], 0.2163)
        self.assertEqual(m3["top_values"][0]["count"], 8)

    def test_spread_values_low_share(self):
        active = [mk(f"ke_{i}", salience=round(0.1 * i, 4)) for i in range(10)]
        m3 = audit.compute_m3_salience(active)
        self.assertLessEqual(m3["max_single_value_share"], 0.1 + 1e-9)


class GateTests(unittest.TestCase):
    POLICY = {
        "quality_gate": {
            "threshold_tier1": 0.40,
            "threshold_dup": 0.20,
            "threshold_recall": 0.60,
            "threshold_temporal_freshness": 0.80,
            "threshold_stale_active_projects": 0,
        }
    }

    def _report(self, tier1_share, dup_entries, total, recall, temporal=None, stale_projects=0):
        return {
            "active_counts": {"total": total},
            "m1_tiers": {"tier_1_share": tier1_share, "tier_2_share": 0.05},
            "m3_salience": {"max_single_value_share": 0.5},
            "m4_duplicates": {"entries_in_clusters": dup_entries, "multi_member_clusters": 1, "skipped": False},
            "m6_recall": {"recall_at_5": recall},
            "m7_access": {"active_with_access_share": 0.0},
            "m8_temporal_freshness": {"freshness_at_5": temporal, "probe_count": 0},
            "m9_project_lifecycle": {"stale_active_project_count": stale_projects},
        }

    def test_gate_fails_on_high_tier1(self):
        report = self._report(tier1_share=0.75, dup_entries=0, total=100, recall=0.9)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertFalse(passed)
        self.assertTrue(any("tier_1_share" in i for i in issues))

    def test_gate_fails_on_high_dup_share(self):
        report = self._report(tier1_share=0.2, dup_entries=30, total=100, recall=0.9)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertFalse(passed)
        self.assertTrue(any("duplicate" in i for i in issues))

    def test_gate_fails_on_low_recall(self):
        report = self._report(tier1_share=0.2, dup_entries=0, total=100, recall=0.4)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertFalse(passed)
        self.assertTrue(any("recall" in i for i in issues))

    def test_gate_passes_when_all_within_thresholds(self):
        report = self._report(tier1_share=0.2, dup_entries=5, total=100, recall=0.9)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertTrue(passed, msg=f"unexpected issues: {issues}")

    def test_recall_none_does_not_fail_gate(self):
        report = self._report(tier1_share=0.2, dup_entries=0, total=100, recall=None)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertTrue(passed, msg=f"unexpected issues: {issues}")

    def test_gate_fails_on_low_temporal_freshness(self):
        report = self._report(tier1_share=0.2, dup_entries=0, total=100, recall=0.9, temporal=0.5)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertFalse(passed)
        self.assertTrue(any("M8 temporal_freshness_at_5" in i for i in issues))

    def test_gate_fails_on_stale_active_projects(self):
        report = self._report(tier1_share=0.2, dup_entries=0, total=100, recall=0.9, stale_projects=1)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertFalse(passed)
        self.assertTrue(any("M9 stale_active_project_count" in i for i in issues))

    def test_temporal_none_does_not_fail_gate(self):
        report = self._report(tier1_share=0.2, dup_entries=0, total=100, recall=0.9, temporal=None)
        passed, issues = audit.evaluate_gate(report, self.POLICY)
        self.assertTrue(passed, msg=f"unexpected issues: {issues}")


class TemporalProbeTests(unittest.TestCase):
    def test_load_temporal_staleness_probes_ignores_disabled(self):
        original = audit.TEMPORAL_PROBES_PATH
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "temporal.json"
            path.write_text(
                '[{"id":"enabled","query":"q"}, {"id":"disabled","enabled":false,"query":"q"}]',
                encoding="utf-8",
            )
            audit.TEMPORAL_PROBES_PATH = path
            try:
                probes = audit.load_temporal_staleness_probes()
            finally:
                audit.TEMPORAL_PROBES_PATH = original
        self.assertEqual([p["id"] for p in probes], ["enabled"])

    def test_entry_text_for_probe_extracts_dict_knowledge(self):
        entry = {
            "id": "ke_1",
            "domain": "Singapore Trip",
            "current_view": "Old plan says going to Singapore in July.",
            "key_insights": [{"insight": "Use current itinerary only."}],
            "positions": [{"view": "Position text"}],
            "evolution": [{"from_view": "old view", "to_view": "new view"}],
        }
        text = audit.entry_text_for_probe(entry)
        self.assertIn("singapore trip", text)
        self.assertIn("use current itinerary only", text)
        self.assertIn("new view", text)

    def test_entry_text_for_probe_extracts_dataclass_project(self):
        project = _Project(
            id="pe_1",
            name="Memory Upgrade",
            status="active",
            goal="Improve recall quality",
            current_phase="measurement",
            blocked_on="temporal probes",
            decisions_made=[_Decision("Keep Dream grade separate from outcome grade")],
        )
        text = audit.entry_text_for_probe(project)
        self.assertIn("memory upgrade", text)
        self.assertIn("improve recall quality", text)
        self.assertIn("keep dream grade separate", text)

    def test_temporal_probe_fails_when_stale_phrase_appears(self):
        passed, issues = audit.evaluate_temporal_probe_text(
            ["user is going to singapore in july"],
            {"expect_no_text_any_of": ["going to singapore in july"]},
        )
        self.assertFalse(passed)
        self.assertTrue(issues)

    def test_temporal_probe_passes_when_stale_phrase_absent(self):
        passed, issues = audit.evaluate_temporal_probe_text(
            ["current travel plan is complete"],
            {"expect_no_text_any_of": ["going to singapore in july"]},
        )
        self.assertTrue(passed, msg=f"unexpected issues: {issues}")

    def test_temporal_probe_honors_expected_fresh_text(self):
        passed, issues = audit.evaluate_temporal_probe_text(
            ["current travel plan is complete"],
            {"expect_text_any_of": ["current travel plan"]},
        )
        self.assertTrue(passed, msg=f"unexpected issues: {issues}")

        passed, issues = audit.evaluate_temporal_probe_text(
            ["old travel plan"],
            {"expect_text_any_of": ["current travel plan"]},
        )
        self.assertFalse(passed)
        self.assertTrue(any("expected fresh phrases" in issue for issue in issues))


class ProjectLifecycleTests(unittest.TestCase):
    POLICY = {
        "project_lifecycle": {
            "active_stale_after_days": 90,
            "active_recent_access_grace_days": 30,
        }
    }
    NOW = "2026-06-04T00:00:00+00:00"

    def test_flags_active_project_older_than_threshold(self):
        project = _Project(
            id="pe_old",
            name="Old Active Project",
            status="active",
            metadata=_Meta(last_touched="2026-01-01T00:00:00+00:00"),
        )
        result = audit.compute_m9_project_lifecycle([project], self.POLICY, now_iso=self.NOW)
        self.assertEqual(result["stale_active_project_count"], 1)
        self.assertEqual(result["stale_projects"][0]["id"], "pe_old")

    def test_recently_touched_active_project_not_flagged(self):
        project = _Project(
            id="pe_recent",
            name="Recent Active Project",
            status="active",
            metadata=_Meta(last_touched="2026-06-01T00:00:00+00:00"),
        )
        result = audit.compute_m9_project_lifecycle([project], self.POLICY, now_iso=self.NOW)
        self.assertEqual(result["stale_active_project_count"], 0)

    def test_recent_access_grace_prevents_stale_flag(self):
        project = _Project(
            id="pe_accessed",
            name="Accessed Active Project",
            status="active",
            metadata=_Meta(
                last_touched="2026-01-01T00:00:00+00:00",
                last_accessed="2026-05-25T00:00:00+00:00",
            ),
        )
        result = audit.compute_m9_project_lifecycle([project], self.POLICY, now_iso=self.NOW)
        self.assertEqual(result["stale_active_project_count"], 0)

    def test_inactive_project_statuses_not_flagged(self):
        projects = [
            _Project(id="pe_paused", name="Paused", status="paused", metadata=_Meta(last_touched="2025-01-01T00:00:00+00:00")),
            _Project(id="pe_completed", name="Completed", status="completed", metadata=_Meta(last_touched="2025-01-01T00:00:00+00:00")),
            _Project(id="pe_abandoned", name="Abandoned", status="abandoned", metadata=_Meta(last_touched="2025-01-01T00:00:00+00:00")),
        ]
        result = audit.compute_m9_project_lifecycle(projects, self.POLICY, now_iso=self.NOW)
        self.assertEqual(result["active_project_count"], 0)
        self.assertEqual(result["stale_active_project_count"], 0)

    def test_missing_timestamps_count_as_stale(self):
        project = _Project(id="pe_missing", name="Missing", status="active", metadata=_Meta())
        result = audit.compute_m9_project_lifecycle([project], self.POLICY, now_iso=self.NOW)
        self.assertEqual(result["stale_active_project_count"], 1)
        self.assertEqual(result["stale_projects"][0]["reason"], "active_project_missing_activity_timestamp")


if __name__ == "__main__":
    unittest.main()
