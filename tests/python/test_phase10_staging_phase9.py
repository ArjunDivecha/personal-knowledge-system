from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
PHASE9_STAGING_PROBES = REPO_ROOT / "tests" / "fixtures" / "phase9_staging_outcome_probes.json"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_e2e_staging  # noqa: E402
import seed_staging_env  # noqa: E402


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> str:
        self.store[key] = value
        return "OK"


class Phase10StagingPhase9Tests(unittest.TestCase):
    def test_staging_phase9_probe_fixture_loads(self) -> None:
        payload = seed_staging_env.load_phase9_probes(PHASE9_STAGING_PROBES)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(len(payload["probes"]), 2)
        self.assertEqual(
            payload["metadata"]["probe_set_key"],
            seed_staging_env.PHASE9_PROBE_SET_KEY,
        )
        self.assertEqual(payload["metadata"]["seeded_from"], str(PHASE9_STAGING_PROBES))

    def test_write_phase9_probes_to_redis_uses_worker_probe_contract(self) -> None:
        redis = _FakeRedis()
        payload = seed_staging_env.load_phase9_probes(PHASE9_STAGING_PROBES)

        count = seed_staging_env.write_phase9_probes_to_redis(redis, payload)

        self.assertEqual(count, 2)
        stored = json.loads(redis.store[seed_staging_env.PHASE9_PROBE_SET_KEY])
        self.assertEqual(stored["metadata"]["fixture"], "phase9_staging_outcome_probes")
        self.assertEqual(stored["probes"][0]["expected_entry_ids"], ["ke_fixture_identity_001"])

    def test_phase9_apply_arguments_enable_gate_without_auto_rollback(self) -> None:
        base = {
            "proposal_id": "dpr_fixture",
            "mutation_id": "apply_fixture",
            "reason": "staging apply",
            "operation_ids": ["dop_fixture"],
        }

        arguments = run_e2e_staging.build_phase9_apply_arguments(base)

        self.assertEqual(arguments["proposal_id"], "dpr_fixture")
        self.assertTrue(arguments["phase9_outcome_gate"])
        self.assertFalse(arguments["phase9_auto_rollback"])
        self.assertEqual(arguments["phase9_probe_set_key"], run_e2e_staging.PHASE9_PROBE_SET_KEY)
        self.assertTrue(arguments["phase9_write_validation_ledger"])
        self.assertNotIn("phase9_outcome_gate", base)

    def test_phase9_apply_arguments_can_be_disabled_for_debugging(self) -> None:
        base = {"proposal_id": "dpr_fixture"}

        arguments = run_e2e_staging.build_phase9_apply_arguments(base, enabled=False)

        self.assertEqual(arguments, base)
        self.assertIsNot(arguments, base)


if __name__ == "__main__":
    unittest.main()

