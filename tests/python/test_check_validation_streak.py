from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from check_validation_streak import evaluate_streak

UTC = timezone.utc


class FakeRedis:
    def __init__(self, records_by_key: dict[str, list[dict[str, object]]]) -> None:
        self.records_by_key = records_by_key

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        records = self.records_by_key.get(key, [])
        return [json.dumps(record) for record in records[start:end + 1]]


class CheckValidationStreakTests(unittest.TestCase):
    def test_evaluate_streak_passes_for_required_consecutive_days(self) -> None:
        redis = FakeRedis(
            {
                "validation:history:2026-03-28": [
                    {"gate": "check_overnight_dream", "passed": True, "generated_at": "2026-03-28T08:00:00+00:00"}
                ],
                "validation:history:2026-03-27": [
                    {"gate": "check_overnight_dream", "passed": True, "generated_at": "2026-03-27T08:00:00+00:00"}
                ],
            }
        )

        result = evaluate_streak(
            redis,
            gate="check_overnight_dream",
            required_days=2,
            now_utc=datetime(2026, 3, 28, 15, 0, tzinfo=UTC),
        )

        self.assertTrue(result["passed"])
        self.assertTrue(all(day["passed"] for day in result["days"]))

    def test_evaluate_streak_fails_when_a_day_is_missing(self) -> None:
        redis = FakeRedis(
            {
                "validation:history:2026-03-28": [
                    {"gate": "check_overnight_dream", "passed": True, "generated_at": "2026-03-28T08:00:00+00:00"}
                ],
            }
        )

        result = evaluate_streak(
            redis,
            gate="check_overnight_dream",
            required_days=2,
            now_utc=datetime(2026, 3, 28, 15, 0, tzinfo=UTC),
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["days"][1]["date"], "2026-03-27")
        self.assertFalse(result["days"][1]["passed"])


if __name__ == "__main__":
    unittest.main()
