from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_overnight_dream_run import most_recent_scheduled_boundary, validate_dream_proposal

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

    def test_validate_dream_proposal_passes_for_scheduled_proposal(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        created_at = "2026-03-28T07:10:41+00:00"
        health = {
            "status": "ok",
        }
        dream_proposal = {
            "created_at": created_at,
            "status": "proposal_ready",
            "trigger": "manual",
            "actor_id": "scheduled:dream-governance",
            "operations": [{"operation_id": "dop_archive_ke_test", "type": "archive_entry"}],
            "counts": {
                "archive_limit": 10,
                "promotion_limit": 10,
                "archive_candidates": 17,
                "promotion_candidates": 4,
            },
        }

        result = validate_dream_proposal(
            health=health,
            dream_proposal=dream_proposal,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
            expected_archive_limit=10,
            expected_promotion_limit=10,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])

    def test_validate_dream_proposal_fails_for_old_live_run_shape(self) -> None:
        now_utc = datetime(2026, 3, 28, 15, 0, tzinfo=UTC)
        run_at = "2026-03-27T07:10:41+00:00"
        health = {
            "status": "ok",
        }
        dream_proposal = {
            "run_at": run_at,
            "status": "completed",
            "trigger": "scheduled",
            "dry_run": True,
            "actor_id": "dream_scheduler",
            "operations": [],
            "counts": {
                "archive_limit": 10,
                "promotion_limit": 10,
                "archived": 0,
                "promoted": 0,
            },
        }

        result = validate_dream_proposal(
            health=health,
            dream_proposal=dream_proposal,
            now_utc=now_utc,
            cron_hour_utc=7,
            cron_minute_utc=10,
            max_start_delay_minutes=45,
            expected_archive_limit=10,
            expected_promotion_limit=10,
        )

        self.assertFalse(result.passed)
        self.assertTrue(any("status is not proposal_ready" in issue for issue in result.issues))
        self.assertTrue(any("unexpectedly includes dry_run" in issue for issue in result.issues))
        self.assertTrue(any("too old" in issue for issue in result.issues))


if __name__ == "__main__":
    unittest.main()
