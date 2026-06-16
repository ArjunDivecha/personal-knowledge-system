from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from ensure_overnight_dream_run import trigger_scheduled_governed_repair


class EnsureOvernightDreamRunTests(unittest.TestCase):
    def test_repair_timeout_returns_pollable_in_flight_result(self) -> None:
        with patch.dict(os.environ, {"DREAM_OPERATOR_TOKEN": "test-token"}):
            with patch(
                "ensure_overnight_dream_run.requests.post",
                side_effect=requests.exceptions.Timeout,
            ):
                result = trigger_scheduled_governed_repair(
                    base_url="https://example.test",
                    cron="operator-repair 10 7 * * *",
                    scheduled_time_ms=1773645000000,
                    timeout_seconds=3,
                )

        self.assertEqual(result["status"], "repair_request_timed_out")
        self.assertTrue(result["possible_in_flight"])
        self.assertEqual(result["timeout_seconds"], 3)

    def test_repair_posts_operator_token_and_returns_json_payload(self) -> None:
        response = Mock()
        response.json.return_value = {"status": "completed"}
        response.raise_for_status.return_value = None

        with patch.dict(os.environ, {"DREAM_OPERATOR_TOKEN": "test-token"}):
            with patch(
                "ensure_overnight_dream_run.requests.post",
                return_value=response,
            ) as post:
                result = trigger_scheduled_governed_repair(
                    base_url="https://example.test/",
                    cron="operator-repair 10 7 * * *",
                    scheduled_time_ms=1773645000000,
                    timeout_seconds=30,
                )

        self.assertEqual(result, {"status": "completed"})
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(
            kwargs["json"],
            {
                "cron": "operator-repair 10 7 * * *",
                "scheduled_time": 1773645000000,
            },
        )


if __name__ == "__main__":
    unittest.main()
