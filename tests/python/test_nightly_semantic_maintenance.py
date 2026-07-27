from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "nightly_semantic_maintenance",
    ROOT / "scripts" / "nightly_semantic_maintenance.py",
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def audit_report(clusters: list[list[str]], *, capped: bool = False, count: int | None = None) -> dict:
    return {
        "m4_duplicates": {
            "skipped": False,
            "query_capped": capped,
            "all_tight_clusters": clusters,
            "multi_member_clusters": len(clusters) if count is None else count,
            "entries_in_clusters": sum(len(cluster) for cluster in clusters),
        }
    }


class FakeOperator:
    def __init__(self, statuses: list[dict] | None = None) -> None:
        self.statuses = list(statuses or [])
        self.enqueued: list[dict] = []
        self.rolled_back: list[str] = []

    def health(self) -> dict:
        return {"status": "ok"}

    def enqueue(self, *, task_id: str, candidate_ids: list[str], plan_id: str) -> dict:
        self.enqueued.append({"task_id": task_id, "candidate_ids": candidate_ids, "plan_id": plan_id})
        return {"accepted": True, "task_id": task_id}

    def wait_terminal(self, task_id: str, *, timeout_seconds: int, poll_seconds: float) -> dict:
        if not self.statuses:
            return {"status": "completed", "task_id": task_id, "journal_key": f"journal:{task_id}"}
        status = self.statuses.pop(0)
        return {"task_id": task_id, **status}

    def rollback(self, task_id: str) -> dict:
        self.rolled_back.append(task_id)
        return {"status": "rolled_back", "task_id": task_id}


class FakeStore:
    def __init__(
        self,
        prepared: list | None = None,
        revisions: dict | None = None,
        archived: set | None = None,
        tasks: set | None = None,
    ) -> None:
        self.saved: list[dict] = []
        self.locked = False
        self.latest_value = None
        # Orphan-reconciler inputs. Defaults (no prepared entries) make the
        # reconciler a no-op, so every pre-existing run_night test is unaffected.
        self._prepared = list(prepared or [])   # list of (outbox_key, journal)
        self._revisions = dict(revisions or {})  # entry_id -> int; absent key => entry missing (None)
        self._archived = set(archived or set())  # entry_ids currently archived
        self._tasks = set(tasks or set())        # task_ids that have a maintenance:task:* record
        self.terminalized: list[str] = []        # outbox keys flipped to rolled_back directly

    def list_prepared_outbox(self) -> list:
        return list(self._prepared)

    def entry_state(self, entry_id: str):
        if entry_id not in self._revisions:
            return None
        return (self._revisions[entry_id], entry_id in self._archived)

    def entry_revision(self, entry_id: str):
        state = self.entry_state(entry_id)
        return None if state is None else state[0]

    def task_exists(self, task_id: str) -> bool:
        return task_id in self._tasks

    def terminalize_orphan_outbox(self, outbox_key: str, journal: dict) -> None:
        self.terminalized.append(outbox_key)

    def acquire(self, run_id: str, ttl_seconds: int) -> bool:
        self.locked = True
        return True

    def release(self, run_id: str) -> None:
        self.locked = False

    def save(self, report: dict) -> None:
        self.saved.append(dict(report))
        self.latest_value = report

    def latest(self):
        return self.latest_value

    def maintenance_status_counts(self) -> dict[str, dict[str, int]]:
        return {
            "maintenance:task:*": {"completed": 1},
            "maintenance:outbox:*": {"derived_complete": 1},
        }


def options(**overrides):
    values = {
        "live": True,
        "max_applied": 2,
        "max_candidates": 5,
        "cohort_size": 1,
        "max_queries": 100,
        "audit_workers": 1,
        "task_timeout_seconds": 10,
        "poll_seconds": 0.1,
        "lock_ttl_seconds": 300,
        "rollback_after_run": False,
    }
    values.update(overrides)
    return module.RunOptions(**values)


class NightlySemanticMaintenanceTests(unittest.TestCase):
    def test_workflow_targets_100_merges_with_candidate_headroom(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "nightly-semantic-maintenance.yml").read_text()
        self.assertIn("default: \"100\"", workflow)
        self.assertIn("github.event_name == 'schedule' && '100'", workflow)
        self.assertIn("--max-candidates 300 --cohort-size 5", workflow)

    def test_redis_lock_accepts_boolean_upstash_success(self) -> None:
        class Redis:
            def set(self, *args, **kwargs):
                return True

        store = module.RedisRunStore(redis=Redis())
        self.assertTrue(store.acquire("run", 300))

    def test_operator_rejects_html_route_fallthrough(self) -> None:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/html"
        response._content = b"<html>landing page</html>"

        with self.assertRaisesRegex(
            RuntimeError,
            "worker_non_json_response:rollback:status=200:content_type=text/html",
        ):
            module.OperatorClient._json_object(response, operation="rollback")

    def test_rejects_capped_audit_and_prioritizes_pairs(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "capped_or_incomplete"):
            module.load_audit_clusters(audit_report([["a", "b"]], capped=True))

        report = audit_report([
            ["z", "y", "x"],
            ["b", "a"],
            ["d", "c"],
        ])
        self.assertEqual(
            module.select_candidate_clusters(report, 2),
            [["a", "b"], ["c", "d"]],
        )

    def test_plan_only_never_acquires_lock_or_enqueues(self) -> None:
        operator = FakeOperator()
        store = FakeStore()

        def audit_runner(**kwargs):
            return audit_report([["a", "b"]]), Path("audit.json")

        code, report = module.run_night(
            options(live=False),
            operator=operator,
            store=store,
            audit_runner=audit_runner,
            verifier=lambda: {"passed": True},
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "planned")
        self.assertFalse(store.locked)
        self.assertEqual(store.saved, [])
        self.assertEqual(operator.enqueued, [])

    def test_live_run_applies_and_verifies_each_cohort(self) -> None:
        operator = FakeOperator()
        store = FakeStore()
        reports = iter([
            audit_report([["a", "b"], ["c", "d"]], count=10),
            audit_report([["x", "y"]], count=8),
        ])

        def audit_runner(**kwargs):
            return next(reports), Path("audit.json")

        verifier_calls = []

        def verifier():
            verifier_calls.append(True)
            return {"passed": True, "returncode": 0}

        code, report = module.run_night(
            options(),
            operator=operator,
            store=store,
            audit_runner=audit_runner,
            verifier=verifier,
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["applied_count"], 2)
        self.assertEqual(len(report["barriers"]), 2)
        self.assertGreaterEqual(len(verifier_calls), 3)  # preflight plus one per cohort
        self.assertTrue(any(saved.get("status") == "verified" for saved in store.saved))
        self.assertFalse(store.locked)

    def test_failure_rolls_back_current_unverified_cohort(self) -> None:
        operator = FakeOperator([
            {"status": "completed", "journal_key": "j1"},
            {"status": "failed", "error": "boom"},
        ])
        store = FakeStore()

        def audit_runner(**kwargs):
            return audit_report([["a", "b"], ["c", "d"]]), Path("audit.json")

        code, report = module.run_night(
            options(cohort_size=2),
            operator=operator,
            store=store,
            audit_runner=audit_runner,
            verifier=lambda: {"passed": True},
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(operator.rolled_back), 1)
        self.assertIn("terminal_task_failure", report["error"])

    def test_all_held_run_completes_as_verified_noop(self) -> None:
        operator = FakeOperator([{"status": "held", "reason": "not_duplicate"}])
        store = FakeStore()
        reports = iter([
            audit_report([["a", "b"]]),
            audit_report([["a", "b"]]),
        ])

        def audit_runner(**kwargs):
            return next(reports), Path("audit.json")

        code, report = module.run_night(
            options(max_applied=1),
            operator=operator,
            store=store,
            audit_runner=audit_runner,
            verifier=lambda: {"passed": True},
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["progress_status"], "no_candidate_applied")
        self.assertIn("semantic_maintenance_no_candidate_applied", report["warnings"])
        self.assertIn("post_audit", report)
        self.assertTrue(report["barriers"][0]["passed"])

    def test_latest_check_requires_fresh_completed_verified_run(self) -> None:
        store = FakeStore()
        store.latest_value = {
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "barriers": [{"passed": True}],
        }
        code, result = module.check_latest(store, max_age_hours=4)
        self.assertEqual(code, 0)
        self.assertTrue(result["passed"])

    def test_workflows_schedule_safe_driver_and_never_repair_legacy_dream(self) -> None:
        maintenance = (ROOT / ".github" / "workflows" / "nightly-semantic-maintenance.yml").read_text()
        report = (ROOT / ".github" / "workflows" / "nightly-sleep-report.yml").read_text()
        self.assertIn('cron: "20 7 * * *"', maintenance)
        self.assertIn("nightly_semantic_maintenance.py", maintenance)
        self.assertIn("--live", maintenance)
        self.assertIn("github.event_name == 'schedule' && '100'", maintenance)
        self.assertIn("--audit-workers 4", maintenance)
        self.assertIn('SEMANTIC_SLICE_SIZE"] == 0', maintenance)
        self.assertNotIn("ensure_overnight_dream_run.py", report)
        self.assertIn("--check-latest", report)

    # --- PKS-MAINT-ORPHAN-RECONCILE-001 -----------------------------------

    @staticmethod
    def _orphan(task_id="T", ids=("ke_A", "ke_B")):
        return (
            f"maintenance:outbox:{task_id}:{ids[0]}",
            {
                "status": "prepared",
                "task_id": task_id,
                "expected_revisions": {i: 0 for i in ids},
                "before_snapshots": [{"entry": {"id": i}} for i in ids],
            },
        )

    def test_reconcile_task_bearing_orphan_uses_rollback_endpoint(self) -> None:
        # B1: an untouched orphan WITH a task record is rolled back through the
        # existing endpoint (not terminalized directly).
        store = FakeStore(prepared=[self._orphan("T")], revisions={"ke_A": 0, "ke_B": 0}, tasks={"T"})
        operator = FakeOperator()
        result = module.reconcile_orphan_outbox(store=store, operator=operator)
        self.assertEqual(operator.rolled_back, ["T"])
        self.assertEqual(store.terminalized, [])
        self.assertEqual(result["reconciled"], ["T"])
        self.assertEqual(result["skipped"], [])

    def test_reconcile_task_less_orphan_terminalizes_outbox(self) -> None:
        # B1b (the real 07-25 case): an untouched orphan with NO task record —
        # the one the rollback endpoint 400s on — is cleared by flipping the
        # outbox journal to rolled_back directly. The endpoint is NOT called.
        key, journal = self._orphan("T")
        store = FakeStore(prepared=[(key, journal)], revisions={"ke_A": 0, "ke_B": 0}, tasks=set())
        operator = FakeOperator()
        result = module.reconcile_orphan_outbox(store=store, operator=operator)
        self.assertEqual(operator.rolled_back, [])
        self.assertEqual(store.terminalized, [key])
        self.assertEqual(result["reconciled"], ["T"])
        self.assertEqual(result["skipped"], [])

    def test_reconcile_skips_diverged_snapshot(self) -> None:
        # B2: an entry whose revision changed since prepare -> never touched
        # (no rollback, no terminalize); recorded for review.
        store = FakeStore(prepared=[self._orphan("T")], revisions={"ke_A": 2, "ke_B": 0}, tasks={"T"})
        operator = FakeOperator()
        result = module.reconcile_orphan_outbox(store=store, operator=operator)
        self.assertEqual(operator.rolled_back, [])
        self.assertEqual(store.terminalized, [])
        self.assertEqual(result["reconciled"], [])
        self.assertEqual(result["skipped"][0], {"task_id": "T", "reason": "revision_diverged", "detail": "ke_A"})

    def test_reconcile_skips_archived_entry(self) -> None:
        # B2b: an archived entry means the merge may have partially applied ->
        # never touch it (archiving the duplicate is exactly what an applied
        # merge does).
        store = FakeStore(
            prepared=[self._orphan("T")], revisions={"ke_A": 0, "ke_B": 0}, archived={"ke_B"}, tasks=set()
        )
        operator = FakeOperator()
        result = module.reconcile_orphan_outbox(store=store, operator=operator)
        self.assertEqual(operator.rolled_back, [])
        self.assertEqual(store.terminalized, [])
        self.assertEqual(result["skipped"][0]["reason"], "entry_archived")

    def test_reconcile_skips_when_entry_missing(self) -> None:
        # B2c: a snapshot entry that no longer exists -> restoring it could
        # resurrect a deleted memory; skip.
        store = FakeStore(prepared=[self._orphan("T")], revisions={"ke_A": 0}, tasks=set())  # ke_B absent
        operator = FakeOperator()
        result = module.reconcile_orphan_outbox(store=store, operator=operator)
        self.assertEqual(operator.rolled_back, [])
        self.assertEqual(store.terminalized, [])
        self.assertEqual(result["skipped"][0]["reason"], "entry_missing")

    def test_reconcile_noop_when_no_orphans(self) -> None:
        # B3: no prepared entries => zero effects, empty result.
        store = FakeStore(prepared=[])
        operator = FakeOperator()
        result = module.reconcile_orphan_outbox(store=store, operator=operator)
        self.assertEqual(operator.rolled_back, [])
        self.assertEqual(store.terminalized, [])
        self.assertEqual(result, {"reconciled": [], "skipped": []})

    def test_entry_state_normalizes_absent_field_archived_and_missing(self) -> None:
        # Guards the exact case that would have skipped the real orphan: a
        # present entry with no `revision` field must read as (0, False), while
        # an absent entry key must read as None (divergence), and archived is
        # surfaced for the partial-apply guard.
        class _Redis:
            def __init__(self, data: dict) -> None:
                self.data = data

            def get(self, key: str):
                return self.data.get(key)

        store = module.RedisRunStore(
            redis=_Redis(
                {
                    "knowledge:ke_A": json.dumps({"id": "ke_A", "metadata": {"revision": 3}}),
                    "knowledge:ke_B": json.dumps({"id": "ke_B", "metadata": {}}),
                    "knowledge:ke_arch": json.dumps({"id": "ke_arch", "metadata": {"revision": 1, "archived": True}}),
                    "project:pe_C": json.dumps({"id": "pe_C", "metadata": {"revision": 1}}),
                }
            )
        )
        self.assertEqual(store.entry_state("ke_A"), (3, False))
        self.assertEqual(store.entry_state("ke_B"), (0, False))
        self.assertEqual(store.entry_state("ke_arch"), (1, True))
        self.assertEqual(store.entry_state("pe_C"), (1, False))
        self.assertIsNone(store.entry_state("ke_missing"))
        self.assertEqual(store.entry_revision("ke_B"), 0)
        self.assertIsNone(store.entry_revision("ke_missing"))

    def test_run_night_reconciles_task_less_orphan_before_barrier(self) -> None:
        # Integration mirroring the real 07-25 orphan: run_night terminalizes a
        # task-less never-applied orphan in live mode and records it, before any
        # cohort work.
        key, journal = self._orphan("ORPHAN")
        store = FakeStore(prepared=[(key, journal)], revisions={"ke_A": 0, "ke_B": 0}, tasks=set())
        operator = FakeOperator([{"status": "held", "reason": "not_duplicate"}])

        def audit_runner(**kwargs):
            return audit_report([["a", "b"]]), Path("audit.json")

        code, report = module.run_night(
            options(max_applied=1),
            operator=operator,
            store=store,
            audit_runner=audit_runner,
            verifier=lambda: {"passed": True},
        )
        self.assertEqual(code, 0)
        self.assertEqual(store.terminalized, [key])
        self.assertEqual(report["reconciled"], ["ORPHAN"])


if __name__ == "__main__":
    unittest.main()
