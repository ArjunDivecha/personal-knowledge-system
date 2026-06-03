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
    load_scheduled_archive_limit,
    most_recent_scheduled_boundary,
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
                "applied_count": 4,
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
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])

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


if __name__ == "__main__":
    unittest.main()
