"""
Unit tests for the weekly digest renderer.

Run:
    python -m unittest scripts.test_weekly_dream_digest
"""

import sys
import types
import unittest
from datetime import datetime, timezone

# Stub deps so we can import without installing requests/dotenv.
for mod in ("requests", "dotenv"):
    if mod not in sys.modules:
        m = types.ModuleType(mod)
        if mod == "dotenv":
            m.load_dotenv = lambda *a, **k: None
        sys.modules[mod] = m

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "generate_weekly_dream_digest",
    Path(__file__).parent / "generate_weekly_dream_digest.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


UTC = timezone.utc


class WeekMathTests(unittest.TestCase):
    def test_parse_iso_week(self):
        self.assertEqual(mod.parse_iso_week("2026-W20"), (2026, 20))

    def test_parse_iso_week_invalid(self):
        with self.assertRaises(ValueError):
            mod.parse_iso_week("bogus")

    def test_week_range_is_7_days(self):
        start, end = mod.week_range(2026, 20)
        self.assertEqual((end - start).days, 7)

    def test_week_range_starts_monday(self):
        start, _ = mod.week_range(2026, 20)
        # isoweekday: Monday = 1
        self.assertEqual(start.isoweekday(), 1)


class RenderDigestTests(unittest.TestCase):
    def _render(self, **overrides):
        defaults = dict(
            year=2026,
            week=20,
            start=datetime(2026, 5, 11, tzinfo=UTC),
            end=datetime(2026, 5, 18, tzinfo=UTC),
            cycle_runs=[],
            proposal_runs=[],
            judge_history=[],
            pending_judge=[],
            tripwire_status=None,
        )
        defaults.update(overrides)
        return mod.render_digest(**defaults)

    def test_empty_week_renders(self):
        md = self._render()
        self.assertIn("# Dream + Forgetting — Weekly Digest, 2026-W20", md)
        self.assertIn("_No cycle runs this week._", md)
        self.assertIn("_No judge decisions this week._", md)
        self.assertIn("_None pending._", md)

    def test_cycle_summary_aggregates_counts(self):
        cycles = [
            {
                "run_id": "dr_001",
                "status": "completed",
                "run_at": "2026-05-12T07:10:00Z",
                "counts": {
                    "merged_duplicates": 3,
                    "archived": 2,
                    "promoted": 0,
                    "quarantined": 4,
                    "demoted": 1,
                },
                "phases": {
                    "layer2_quarantine_and_demote": {
                        "quarantined_count": 4,
                        "demoted_count": 1,
                        "streak_reset_count": 6,
                        "streak_increment_count": 12,
                        "cap_hit": False,
                    },
                    "judge_queue": {
                        "opus_mode": "on",
                        "enqueued_count": 1,
                        "verdicts_applied_count": 0,
                        "verdicts_skipped_count": 0,
                    },
                },
            },
            {
                "run_id": "dr_002",
                "status": "completed",
                "run_at": "2026-05-13T07:10:00Z",
                "counts": {
                    "merged_duplicates": 1,
                    "archived": 1,
                    "promoted": 0,
                    "quarantined": 0,
                    "demoted": 0,
                },
                "phases": {},
            },
        ]
        md = self._render(cycle_runs=cycles)
        # Top-line totals should aggregate across runs
        self.assertIn("L1 duplicate merges applied | 4", md)
        self.assertIn("L1 archives applied | 3", md)
        self.assertIn("L2 quarantines applied | 4", md)
        self.assertIn("L2 tier demotions applied | 1", md)
        # Per-run sections render
        self.assertIn("dr_001", md)
        self.assertIn("dr_002", md)

    def test_governed_run_counts_selected_operations(self):
        cycles = [
            {
                "run_id": "dga_2026-05-20T07-10-42-454Z",
                "status": "completed_with_holds",
                "run_at": "2026-05-20T07:10:42Z",
                "completed_at": "2026-05-20T07:11:10Z",
                "auto_apply_mode": "governed",
                "counts": {
                    "operation_count": 8,
                    "selected_operation_count": 5,
                    "held_operation_count": 3,
                    "applied_count": 5,
                    "operation_counts": {
                        "archive_entry": 4,
                        "duplicate_merge": 2,
                        "mark_contested": 1,
                        "promote_context_type": 1,
                    },
                    "selected_counts": {
                        "archive_entry": 2,
                        "duplicate_merge": 1,
                        "mark_contested": 1,
                        "promote_context_type": 1,
                    },
                },
                "phases": {},
            },
        ]

        md = self._render(cycle_runs=cycles)

        self.assertIn("completed / held / skipped / failed) | 1 / 0 / 0 / 0", md)
        self.assertIn("Governed operations selected / held / applied | 5 / 3 / 5", md)
        self.assertIn("Total L1 operations applied | 5", md)
        self.assertIn("L1 duplicate merges applied | 1", md)
        self.assertIn("L1 archives applied | 2", md)
        self.assertIn("L1 promotions applied | 1", md)
        self.assertIn("L1 contested marks applied | 1", md)
        self.assertIn("governed: selected 5, applied 5, held 3", md)
        self.assertIn("selected_by_type: archive_entry: 2", md)

    def test_judge_history_section(self):
        history = [
            {
                "op_id": "op_xyz",
                "outcome": "applied",
                "settled_at": "2026-05-14T07:11:00Z",
                "verdict": {
                    "verdict": "apply",
                    "reason": "labels match exactly and content overlaps",
                    "judge_model": "claude-opus-5",
                    "judge_source": "claude_cli",
                },
                "item": {"op_type": "duplicate_merge_borderline"},
            },
        ]
        md = self._render(judge_history=history)
        self.assertIn("op_xyz", md)
        self.assertIn("labels match exactly", md)
        self.assertIn("claude_cli", md)
        # Top-line summary
        self.assertIn("applied / skipped / stale | 1 / 0 / 0", md)

    def test_tripwire_status_with_active_flag(self):
        ts = {
            "modes": {
                "DREAM_AUTO_APPLY_MODE": {
                    "effective": "off",
                    "tripped": True,
                    "trip_record": {
                        "reason": "destructive spike",
                        "tripped_at": "2026-05-15T00:00:00Z",
                        "source_tripwire": "destructive_spike",
                    },
                },
                "RETRIEVAL_POLICY_MODE": {
                    "effective": "on",
                    "tripped": False,
                    "trip_record": None,
                },
            },
            "tripwires": {
                "destructive_action_volume": {"tripped": True, "threshold": 9, "consecutive_breaches": 2},
                "retrieval_hit_collapse": {"tripped": False, "threshold_ratio": 0.5, "consecutive_breaches": 0},
            },
        }
        md = self._render(tripwire_status=ts)
        self.assertIn("DREAM_AUTO_APPLY_MODE", md)
        self.assertIn("destructive spike", md)
        self.assertIn("destructive_action_volume tripped: `True`", md)
        self.assertIn("retrieval_hit_collapse tripped: `False`", md)


if __name__ == "__main__":
    unittest.main()
