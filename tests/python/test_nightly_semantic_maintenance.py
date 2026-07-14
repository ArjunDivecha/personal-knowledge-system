from __future__ import annotations

import importlib.util
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
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.locked = False
        self.latest_value = None

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

    def test_stalled_backlog_fails_loudly(self) -> None:
        operator = FakeOperator([{"status": "held", "reason": "not_duplicate"}])
        store = FakeStore()

        def audit_runner(**kwargs):
            return audit_report([["a", "b"]]), Path("audit.json")

        code, report = module.run_night(
            options(max_applied=1),
            operator=operator,
            store=store,
            audit_runner=audit_runner,
            verifier=lambda: {"passed": True},
        )
        self.assertEqual(code, 1)
        self.assertIn("stalled", report["error"])
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
        self.assertIn("github.event_name == 'schedule' && '5'", maintenance)
        self.assertIn("--audit-workers 4", maintenance)
        self.assertIn('SEMANTIC_SLICE_SIZE"] == 0', maintenance)
        self.assertNotIn("ensure_overnight_dream_run.py", report)
        self.assertIn("--check-latest", report)


if __name__ == "__main__":
    unittest.main()
