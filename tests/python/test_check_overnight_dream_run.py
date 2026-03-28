from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_overnight_dream_run import most_recent_scheduled_boundary, validate_dream_run


class CheckOvernightDreamRunTests(unittest.TestCase):
    def test_most_recent_scheduled_boundary_after_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        boundary = most_recent_scheduled_boundary(now_utc, 7, 10)
        self.assertEqual(boundary, datetime(2026, 3, 28, 7, 10, tzinfo=UTC))

    def test_most_recent_scheduled_boundary_before_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 1, 0, tzinfo=UTC)
        boundary = most_recent_scheduled_boundary(now_utc, 7, 10)
        self.assertEqual(boundary, datetime(2026, 3, 27, 7, 10, tzinfo=UTC))

    def test_validate_dream_run_passes_for_full_live_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        run_at = "2026-03-28T07:10:41+00:00"
        health = {
            "last_dream_run": run_at,
            "last_dream_dry_run": False,
        }
        dream_summary = {
            "run_at": run_at,
            "status": "completed",
            "trigger": "scheduled",
            "dry_run": False,
            "counts": {
                "archive_limit": None,
                "promotion_limit": None,
                "archived": 17,
                "promoted": 4,
            },
        }

        result = validate_dream_run(
            health=health,
            dream_summary=dream_summary,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])

    def test_validate_dream_run_fails_for_old_dry_run(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        run_at = "2026-03-27T07:10:41+00:00"
        health = {
            "last_dream_run": run_at,
            "last_dream_dry_run": True,
        }
        dream_summary = {
            "run_at": run_at,
            "status": "completed",
            "trigger": "scheduled",
            "dry_run": True,
            "counts": {
                "archive_limit": None,
                "promotion_limit": None,
                "archived": 0,
                "promoted": 0,
            },
        }

        result = validate_dream_run(
            health=health,
            dream_summary=dream_summary,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("dry_run=True" in issue for issue in result.issues))
        self.assertTrue(any("too old" in issue for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
