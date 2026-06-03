from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_overnight_dream_run import (
    is_scheduled_governed_run,
    load_scheduled_archive_limit,
    most_recent_scheduled_boundary,
    render_sleep_report,
    validate_dream_run,
)

UTC = timezone.utc


class CheckOvernightDreamRunTests(unittest.TestCase):
    def test_most_recent_scheduled_boundary_after_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        boundary = most_recent_scheduled_boundary(now_utc, 7, 10)
        self.assertEqual(boundary, datetime(2026, 3, 28, 7, 10, tzinfo=UTC))

    def test_most_recent_scheduled_boundary_before_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 1, 0, tzinfo=UTC)
        boundary = most_recent_scheduled_boundary(now_utc, 7, 10)
        self.assertEqual(boundary, datetime(2026, 3, 27, 7, 10, tzinfo=UTC))

    def test_load_scheduled_archive_limit_reads_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "memory_policy.json"
            policy_path.write_text(
                '{"dream_thresholds": {"scheduled_archive_limit": 50}}',
                encoding="utf-8",
            )

            self.assertEqual(load_scheduled_archive_limit(policy_path), 50)

    def test_validate_dream_run_passes_for_governed_live_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        run_at = "2026-03-28T07:10:41+00:00"
        health = {
            "status": "ok",
        }
        dream_run = {
            "run_at": run_at,
            "status": "completed",
            "trigger": "scheduled",
            "dry_run": False,
            "auto_apply_mode": "governed",
            "counts": {
                "archive_limit": 10,
                "promotion_limit": 10,
                "operation_count": 17,
                "selected_operation_count": 4,
                "held_operation_count": 13,
                "applied_count": 4,
            },
            "verification": {"passed": True, "checks": []},
        }

        result = validate_dream_run(
            health=health,
            dream_run=dream_run,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
            expected_archive_limit=10,
            expected_promotion_limit=10,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])

    def test_scheduled_governed_detection_accepts_dga_attempts(self) -> None:
        self.assertTrue(
            is_scheduled_governed_run(
                {
                    "run_id": "dga_2026-03-28T07-10-41-000Z",
                    "trigger": "scheduled",
                    "auto_apply_mode": "governed",
                },
            ),
        )
        self.assertFalse(
            is_scheduled_governed_run(
                {
                    "run_id": "dr_2026-03-28T07-10-41-000Z",
                    "trigger": "scheduled",
                },
            ),
        )

    def test_validate_dream_run_fails_when_applied_count_does_not_match_selection(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        result = validate_dream_run(
            health={"status": "ok"},
            dream_run={
                "run_at": "2026-03-28T07:10:41+00:00",
                "status": "completed",
                "trigger": "scheduled",
                "dry_run": False,
                "auto_apply_mode": "governed",
                "counts": {
                    "archive_limit": 10,
                    "promotion_limit": 10,
                    "operation_count": 2,
                    "selected_operation_count": 2,
                    "held_operation_count": 0,
                    "applied_count": 1,
                },
                "verification": {"passed": False},
            },
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
            expected_archive_limit=10,
            expected_promotion_limit=10,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("applied_count is 1" in issue for issue in result.issues))
        self.assertTrue(any("verification did not pass" in issue for issue in result.issues))

    def test_validate_dream_run_fails_for_old_proposal_only_shape(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        created_at = "2026-03-28T07:10:41+00:00"
        health = {
            "status": "ok",
        }
        dream_run = {
            "created_at": created_at,
            "status": "proposal_ready",
            "trigger": "manual",
            "dry_run": True,
            "actor_id": "scheduled:dream-governance",
            "operations": [],
            "counts": {
                "archive_limit": 10,
                "promotion_limit": 10,
                "operation_count": 0,
                "selected_operation_count": 0,
                "held_operation_count": 0,
                "applied_count": 0,
            },
            "verification": {"passed": True},
        }

        result = validate_dream_run(
            health=health,
            dream_run=dream_run,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
            expected_archive_limit=10,
            expected_promotion_limit=10,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("status is not governed-live" in issue for issue in result.issues))
        self.assertTrue(any("trigger is not scheduled" in issue for issue in result.issues))
        self.assertTrue(any("dry_run=false" in issue for issue in result.issues))

    def test_validate_dream_run_allows_on_demand_operator_check(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        health = {
            "status": "ok",
        }
        dream_run = {
            "run_at": "2026-03-28T12:15:00+00:00",
            "status": "completed_with_holds",
            "trigger": "scheduled",
            "dry_run": False,
            "auto_apply_mode": "governed",
            "counts": {
                "archive_limit": 10,
                "promotion_limit": 10,
            },
        }

        result = validate_dream_run(
            health=health,
            dream_run=dream_run,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
            expected_archive_limit=10,
            expected_promotion_limit=10,
            allow_on_demand=True,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])

    def test_render_sleep_report_includes_governed_counts_and_tripwire_state(self) -> None:
        md = render_sleep_report(
            {
                "generated_at": "2026-03-28T15:00:00+00:00",
                "expected_boundary_utc": "2026-03-28T07:10:00+00:00",
                "expected_boundary_local": "2026-03-28T00:10:00-07:00",
                "passed": True,
                "issues": [],
                "health": {"status": "ok", "last_dream_run": "2026-03-28T07:10:41Z"},
                "tripwire_status": {
                    "modes": {
                        "DREAM_AUTO_APPLY_MODE": {
                            "effective": "governed",
                            "tripped": False,
                        },
                    },
                    "tripwires": {
                        "destructive_action_volume": {
                            "tripped": False,
                            "consecutive_breaches": 0,
                            "threshold": 25,
                        },
                    },
                },
                "dream_run": {
                    "_report_source": "dream_run_index",
                    "run_id": "dga_2026-03-28T07-10-41-000Z",
                    "run_at": "2026-03-28T07:10:41Z",
                    "completed_at": "2026-03-28T07:11:00Z",
                    "status": "completed_with_holds",
                    "trigger": "scheduled",
                    "auto_apply_mode": "governed",
                    "dry_run": False,
                    "proposal_id": "dpr_2026-03-28T07-10-41-000Z",
                    "risk_score": "medium",
                    "grade_status": "passed",
                    "counts": {
                        "operation_count": 3,
                        "selected_operation_count": 2,
                        "held_operation_count": 1,
                        "applied_count": 2,
                        "archive_limit": 10,
                        "promotion_limit": 10,
                        "duplicate_merge_limit": 10,
                        "mark_contested_limit": 5,
                        "operation_counts": {
                            "archive_entry": 2,
                            "duplicate_merge": 1,
                        },
                        "selected_counts": {
                            "archive_entry": 2,
                        },
                    },
                    "held_operations": [
                        {
                            "operation_id": "dop_merge_1",
                            "type": "duplicate_merge",
                            "reason": "scheduled_cap_reached:duplicate_merge:0",
                        },
                    ],
                    "verification": {"passed": True, "checks": []},
                },
            },
        )

        self.assertIn("# Dream Sleep Report - 2026-03-28", md)
        self.assertIn("Verdict: `PASS`", md)
        self.assertIn("`dga_2026-03-28T07-10-41-000Z`", md)
        self.assertIn("Operations selected | 2", md)
        self.assertIn("`archive_entry`: 2", md)
        self.assertIn("DREAM_AUTO_APPLY_MODE", md)


if __name__ == "__main__":
    unittest.main()
