from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MONITOR_PATH = REPO_ROOT / "scripts" / "nightly_health_monitor.py"

spec = importlib.util.spec_from_file_location("nightly_health_monitor", MONITOR_PATH)
assert spec is not None and spec.loader is not None
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def _snapshot(label, knowledge, vectors, tw_sources, gh_sources, ag_files, marker=None, last_run=None):
    return {
        "label": label,
        "storage": {"knowledge_entries": knowledge, "project_entries": 0, "total_vectors": vectors},
        "processed_sources": {"twitter": tw_sources, "github": gh_sources},
        "checkpoints": {
            "agent_sessions_state": {"file_count": ag_files, "total_saved": knowledge},
            "agent_sessions_last_run": last_run,
            "nightly_success_marker": marker,
        },
    }


CLEAN_LOG = """
[00:00:00] === Nightly ingestion started ===
[00:00:01] Agent SDK: OK
[00:00:02] Claude Agent SDK inference preflight: OK (model=sonnet); using SDK billing route.
[00:01:00] --- Twitter ingestion starting ---
[00:02:00] --- Twitter ingestion done ---
[00:02:01] --- GitHub ingestion starting ---
[00:03:00] --- GitHub ingestion done ---
[00:03:01] --- Agent sessions ingestion starting ---
[00:04:00] --- Agent sessions ingestion done ---
[00:04:01] --- Dream judge starting ---
[00:05:00] --- Dream judge done ---
[00:05:01] === Nightly ingestion complete ===
"""


class BuildVerificationTests(unittest.TestCase):
    def test_clean_run_with_new_data_is_pass(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot(
            "after", 112, 112, 12, 6, 53,
            marker={"completed_at": "2026-06-07T10:00:00Z", "api_fallback": "0"},
            last_run={"total_saved": 3, "redis_write_failed": False},
        )
        report = monitor.build_verification(before, after, CLEAN_LOG)
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(report["pipelines"]["twitter"]["status"], "PASS")
        self.assertEqual(report["pipelines"]["github"]["status"], "PASS")
        self.assertEqual(report["pipelines"]["agent_sessions"]["status"], "PASS")
        self.assertEqual(report["deltas"]["knowledge_entries"], 12)
        self.assertEqual(report["deltas"]["twitter_sources"], 2)

    def test_no_new_data_but_clean_is_warn_not_fail(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot(
            "after", 100, 100, 10, 5, 50,
            marker={"completed_at": "x"},
            last_run={"total_saved": 0, "redis_write_failed": False},
        )
        report = monitor.build_verification(before, after, CLEAN_LOG)
        self.assertEqual(report["overall"], "WARN")
        self.assertEqual(report["pipelines"]["twitter"]["status"], "WARN")
        self.assertEqual(report["pipelines"]["github"]["status"], "WARN")

    def test_incomplete_run_is_fail(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot("after", 105, 105, 11, 5, 51, marker=None)
        truncated = CLEAN_LOG.replace("=== Nightly ingestion complete ===", "")
        truncated = truncated.replace("--- Agent sessions ingestion done ---", "")
        report = monitor.build_verification(before, after, truncated)
        self.assertEqual(report["overall"], "FAIL")
        self.assertFalse(report["log"]["completed"])
        self.assertEqual(report["pipelines"]["agent_sessions"]["status"], "FAIL")

    def test_error_lines_force_fail(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot("after", 110, 110, 12, 6, 53, marker={"completed_at": "x"})
        log = CLEAN_LOG + "\n[00:02:30] FATAL: Twitter storage connection failed\n"
        report = monitor.build_verification(before, after, log)
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(report["log"]["error_line_count"], 1)

    def test_browser_storm_hit_forces_fail(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot("after", 110, 110, 12, 6, 53, marker={"completed_at": "x"})
        log = CLEAN_LOG + "\nopening http://localhost:18043/oauth/callback?code=...\n"
        report = monitor.build_verification(before, after, log)
        self.assertEqual(report["overall"], "FAIL")
        self.assertTrue(report["log"]["browser_storm_hits"])

    def test_agent_sessions_redis_write_failure_is_warn_not_fail(self) -> None:
        # Disk checkpoint is authoritative and re-syncs Redis next run, so a
        # mirror write failure is surfaced as WARN, not a hard FAIL.
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot(
            "after", 112, 112, 12, 6, 53,
            marker={"completed_at": "x"},
            last_run={"total_saved": 3, "redis_write_failed": True},
        )
        report = monitor.build_verification(before, after, CLEAN_LOG)
        self.assertEqual(report["pipelines"]["agent_sessions"]["status"], "WARN")
        self.assertEqual(report["overall"], "WARN")

    def test_marker_ok_false_forces_fail(self) -> None:
        # The wrapper's own self-reported verdict (ok=false) fails the run even if
        # the log slice and counts look fine.
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot(
            "after", 112, 112, 12, 6, 53,
            marker={"completed_at": "x", "ok": False, "failed_stages": ["GitHub ingestion(rc=1)"]},
        )
        report = monitor.build_verification(before, after, CLEAN_LOG)
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(report["log"]["marker_failed_stages"], ["GitHub ingestion(rc=1)"])

    def test_dream_judge_nonzero_is_tolerated_warn(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot("after", 112, 112, 12, 6, 53, marker={"completed_at": "x"})
        log = CLEAN_LOG + "\n[00:05:00] Dream judge exited with non-zero status (see log)\n"
        report = monitor.build_verification(before, after, log)
        self.assertEqual(report["pipelines"]["dream_judge"]["status"], "WARN")
        # A tolerated dream-judge warning alone should not FAIL the overall run.
        self.assertIn(report["overall"], {"PASS", "WARN"})

    def test_markdown_renders_without_error(self) -> None:
        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot("after", 112, 112, 12, 6, 53, marker={"completed_at": "x"})
        report = monitor.build_verification(before, after, CLEAN_LOG)
        md = monitor.render_markdown(report, before, after)
        self.assertIn("Nightly Ingestion Health Report", md)
        self.assertIn("Overall", md)


class ScopeLogTests(unittest.TestCase):
    def test_scoping_ignores_earlier_runs_in_daily_log(self) -> None:
        earlier_failed_run = (
            "=== Nightly ingestion started ===\n"
            "FATAL: Claude Agent SDK real inference preflight failed.\n"
            "Traceback (most recent call last):\n"
        )
        daily_log = earlier_failed_run + CLEAN_LOG
        scoped = monitor.scope_log_to_last_run(daily_log)
        # The current (clean) run must not inherit the earlier run's FATAL/Traceback.
        self.assertNotIn("FATAL", scoped)
        self.assertNotIn("Traceback", scoped)
        self.assertIn("Nightly ingestion complete", scoped)

        before = _snapshot("before", 100, 100, 10, 5, 50)
        after = _snapshot("after", 112, 112, 12, 6, 53, marker={"completed_at": "x"})
        report = monitor.build_verification(before, after, scoped)
        self.assertEqual(report["log"]["error_line_count"], 0)
        self.assertNotEqual(report["overall"], "FAIL")


class SummarizePreflightTests(unittest.TestCase):
    def test_hard_failure_fails_overall(self) -> None:
        checks = {
            "env_keys": {"ok": False, "hard": True, "detail": "MISSING"},
            "source_dirs": {"ok": True, "hard": False, "detail": "ok"},
        }
        ok, lines = monitor.summarize_preflight(checks)
        self.assertFalse(ok)
        self.assertTrue(any("[FAIL] env_keys" in ln for ln in lines))

    def test_soft_failure_warns_but_passes(self) -> None:
        checks = {
            "env_keys": {"ok": True, "hard": True, "detail": "ok"},
            "source_dirs": {"ok": False, "hard": False, "detail": "no dirs"},
        }
        ok, lines = monitor.summarize_preflight(checks)
        self.assertTrue(ok)
        self.assertTrue(any("[WARN] source_dirs" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
