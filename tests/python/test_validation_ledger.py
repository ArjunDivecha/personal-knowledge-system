from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _validation_ledger import (  # noqa: E402
    VALIDATION_GATE_STATUS_KEY,
    VALIDATION_LAST_KEY,
    ValidationGateRecord,
    write_validation_gate,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str):
        self.store[key] = value
        return "OK"

    def lpush(self, key: str, value: str):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def ltrim(self, key: str, start: int, stop: int):
        self.lists[key] = self.lists.get(key, [])[start:stop + 1]
        return "OK"


class ValidationLedgerTests(unittest.TestCase):
    def test_write_validation_gate_updates_last_and_gate_status(self) -> None:
        redis = _FakeRedis()

        write_validation_gate(
            redis,
            ValidationGateRecord(
                gate="unit_gate",
                passed=True,
                issues=[],
                report_path="/tmp/report.json",
                details={"checked": 1},
            ),
        )

        last = json.loads(redis.store[VALIDATION_LAST_KEY])
        status = json.loads(redis.store[VALIDATION_GATE_STATUS_KEY])

        self.assertEqual(last["gate"], "unit_gate")
        self.assertEqual(last["status"], "pass")
        self.assertEqual(status["overall_status"], "green")
        self.assertTrue(status["gates"]["unit_gate"]["passed"])
        self.assertEqual(len(redis.lists), 1)


if __name__ == "__main__":
    unittest.main()
