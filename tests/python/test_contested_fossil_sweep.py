"""
=============================================================================
SCRIPT NAME: test_contested_fossil_sweep.py
=============================================================================

INPUT FILES: None. An in-memory FakeRedis fixture stands in for Upstash; no
file or network I/O.
OUTPUT FILES: None. This test never invokes scripts/sweep_contested_fossils.py's
main() or its report-writing path — it exercises the FossilSweep class
directly against the fake, so no scripts/reports/*.json file is written.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Covers INV3 and INV4 of contract PKS-CONTRADICTION-LIFECYCLE-001:

INV3 - the fossil sweep in dry-run mode (FossilSweep.find_fossils) performs
       zero writes to the store — proven by a spy on FakeRedis.set that
       asserts a call count of exactly zero, on a fixture with a known-
       nonempty candidate list.
INV4 - every state change made by FossilSweep.apply appends a
       consolidation_notes receipt naming the run id, basis, and counterpart
       ids, and bumps metadata.revision monotonically (reversible via the
       entry's revision history, matching the Worker's rollback convention
       from PKS-USAGE-SIGNAL-001's sibling work on dream.ts rollback).

Also exercises the "exposure != use"-style precision of the fossil
definition: a legitimate contested entry (a live, non-self, non-archived
counterpart) must NEVER be touched by dry-run or apply, even when it sits
alongside real fossils in the same sweep.

DEPENDENCIES: Python 3.14 stdlib unittest only.
USAGE:
  python -m unittest tests.python.test_contested_fossil_sweep -v
=============================================================================
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sweep_contested_fossils import FossilSweep  # noqa: E402


class FakeRedis:
    """Dict-backed fake implementing the same scan/mget/get/set surface as
    the real upstash_redis.Redis client (see
    distillation/storage/redis_client.py's usage pattern), matching the
    FakeRedis idiom already established in
    tests/python/test_repo_agent_context.py."""

    def __init__(self, entries: dict[str, dict]):
        self.values: dict[str, str] = {
            key: json.dumps(entry) for key, entry in entries.items()
        }
        self.set_calls: list[tuple[str, str]] = []

    def scan(self, cursor, match: str, count: int = 100):
        prefix = match.rstrip("*")
        matching = [k for k in self.values if k.startswith(prefix)]
        return 0, matching

    def mget(self, *keys: str):
        return [self.values.get(key) for key in keys]

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str):
        self.set_calls.append((key, value))
        self.values[key] = value
        return "OK"


def _entry(entry_id: str, state: str, contradicts: list[str] | None = None,
          archived: bool = False, revision: int | None = None) -> dict:
    metadata: dict = {"archived": archived, "consolidation_notes": []}
    if revision is not None:
        metadata["revision"] = revision
    entry: dict = {
        "id": entry_id,
        "type": "knowledge",
        "state": state,
        "related_knowledge": [
            {"knowledge_id": target, "relationship": "contradicts"}
            for target in (contradicts or [])
        ],
        "metadata": metadata,
    }
    return entry


class FossilDetectionTests(unittest.TestCase):
    """The candidate-selection logic (INV: only true fossils are named)."""

    def test_self_referential_contradicts_is_a_fossil(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_self": _entry("ke_self", "contested", contradicts=["ke_self"]),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual([c["id"] for c in candidates], ["ke_self"])

    def test_missing_counterpart_is_a_fossil(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_orphan": _entry("ke_orphan", "contested", contradicts=["ke_gone"]),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual([c["id"] for c in candidates], ["ke_orphan"])

    def test_archived_counterpart_is_a_fossil(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_stale": _entry("ke_stale", "contested", contradicts=["ke_dead"]),
            "knowledge:ke_dead": _entry("ke_dead", "active", archived=True),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual([c["id"] for c in candidates], ["ke_stale"])

    def test_no_contradicts_links_at_all_is_a_fossil(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_lonely": _entry("ke_lonely", "contested", contradicts=[]),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual([c["id"] for c in candidates], ["ke_lonely"])

    def test_live_non_self_counterpart_is_not_a_fossil(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_real": _entry("ke_real", "contested", contradicts=["ke_opponent"]),
            "knowledge:ke_opponent": _entry("ke_opponent", "active"),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual(candidates, [])

    def test_one_live_counterpart_among_several_dead_ones_is_not_a_fossil(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_mixed": _entry("ke_mixed", "contested",
                                         contradicts=["ke_mixed", "ke_gone", "ke_opponent"]),
            "knowledge:ke_opponent": _entry("ke_opponent", "active"),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual(candidates, [])

    def test_active_entries_are_never_candidates(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_fine": _entry("ke_fine", "active"),
        })
        candidates = FossilSweep(redis).find_fossils()
        self.assertEqual(candidates, [])


class DryRunIsWriteFreeTests(unittest.TestCase):
    """INV3: dry-run performs zero writes even when candidates exist."""

    def test_find_fossils_issues_zero_set_calls_on_a_nonempty_fixture(self) -> None:
        redis = FakeRedis({
            "knowledge:ke_fossil_a": _entry("ke_fossil_a", "contested", contradicts=["ke_fossil_a"]),
            "knowledge:ke_fossil_b": _entry("ke_fossil_b", "contested", contradicts=["ke_missing"]),
            "knowledge:ke_real": _entry("ke_real", "contested", contradicts=["ke_opponent"]),
            "knowledge:ke_opponent": _entry("ke_opponent", "active"),
        })
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()

        self.assertEqual(len(candidates), 2)
        self.assertEqual(redis.set_calls, [])


class ApplyReceiptAndReversibilityTests(unittest.TestCase):
    """INV4: applied changes are receipted and the legitimate contest is
    left untouched even when swept in the same run."""

    def _mixed_fixture(self) -> FakeRedis:
        return FakeRedis({
            "knowledge:ke_fossil": _entry("ke_fossil", "contested",
                                          contradicts=["ke_fossil", "ke_gone"], revision=3),
            "knowledge:ke_real": _entry("ke_real", "contested", contradicts=["ke_opponent"]),
            "knowledge:ke_opponent": _entry("ke_opponent", "active"),
        })

    def test_apply_reverts_fossil_state_to_active(self) -> None:
        redis = self._mixed_fixture()
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()
        changed = sweep.apply(candidates, run_id="run_test_1")

        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["id"], "ke_fossil")
        updated = json.loads(redis.values["knowledge:ke_fossil"])
        self.assertEqual(updated["state"], "active")

    def test_apply_bumps_revision_monotonically(self) -> None:
        redis = self._mixed_fixture()
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()
        sweep.apply(candidates, run_id="run_test_2")

        updated = json.loads(redis.values["knowledge:ke_fossil"])
        self.assertEqual(updated["metadata"]["revision"], 4)  # was 3

    def test_apply_appends_a_receipt_naming_run_id_basis_and_counterparts(self) -> None:
        # INV4: the receipt must name run id, basis, AND counterpart ids as
        # distinct, findable components (regression: adversarial review
        # 2026-07-10 — an earlier draft implied basis via the counterpart
        # summary alone, which doesn't satisfy "naming the basis").
        redis = self._mixed_fixture()
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()
        sweep.apply(candidates, run_id="run_test_3")

        updated = json.loads(redis.values["knowledge:ke_fossil"])
        notes = updated["metadata"]["consolidation_notes"]
        self.assertEqual(len(notes), 1)
        receipt = notes[0]
        self.assertIn("run_test_3", receipt)
        self.assertIn("fossil_sweep", receipt)
        expected_basis = [c for c in candidates if c["id"] == "ke_fossil"][0]["basis"]
        self.assertIn(expected_basis, receipt)
        self.assertIn("ke_fossil:self_referential", receipt)
        self.assertIn("ke_gone:missing", receipt)

    def test_apply_never_touches_the_legitimate_contest(self) -> None:
        redis = self._mixed_fixture()
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()
        sweep.apply(candidates, run_id="run_test_4")

        untouched = json.loads(redis.values["knowledge:ke_real"])
        self.assertEqual(untouched["state"], "contested")
        self.assertEqual(untouched["metadata"]["consolidation_notes"], [])

    def test_apply_only_writes_the_fossil_not_every_entry(self) -> None:
        redis = self._mixed_fixture()
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()
        sweep.apply(candidates, run_id="run_test_5")

        written_keys = {key for key, _ in redis.set_calls}
        self.assertEqual(written_keys, {"knowledge:ke_fossil"})

    def test_reversibility_prior_state_is_recoverable_from_the_changed_record(self) -> None:
        redis = self._mixed_fixture()
        sweep = FossilSweep(redis)
        candidates = sweep.find_fossils()
        changed = sweep.apply(candidates, run_id="run_test_6")

        record = changed[0]
        self.assertEqual(record["prior_state"], "contested")
        self.assertEqual(record["prior_revision"], 3)
        self.assertEqual(record["new_revision"], 4)


if __name__ == "__main__":
    unittest.main()
