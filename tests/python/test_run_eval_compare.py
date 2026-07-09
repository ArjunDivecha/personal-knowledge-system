"""
=============================================================================
SCRIPT NAME: test_run_eval_compare.py
=============================================================================

INPUT FILES:
- None. Synthetic eval-report fixtures are built in-memory (make_report())
  and written to a per-test tempfile.TemporaryDirectory(), never to a fixed
  repo path. No file under the repo is read by this test module.

OUTPUT FILES:
- None. Test fixtures are written only under the OS temp directory created by
  tempfile.TemporaryDirectory() for the duration of each test and are deleted
  automatically on cleanup (self.addCleanup(self._tmp.cleanup)). No file is
  written under
  /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system.

VERSION: 1.0
LAST UPDATED: 2026-07-09
AUTHOR: Claude (Sonnet 5) for Arjun Divecha

DESCRIPTION:
Offline unit tests for the retrieval-regression gate defined by contract
PKS-RETRIEVAL-REGRESSION-GATE-001
(/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/retrieval-regression-gate.spec.md).
Exercises
/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/run_eval.py's
`compare(old_path, new_path, fail_on_regression=True)` function directly (no
subprocess, no network) against synthetic OLD/NEW report fixtures, and proves:

  INV1 - comparing a report against itself exits 0.
  INV2 - a probe flipping pass->fail, or a measured axis metric degrading
         beyond REGRESSION_TOLERANCE (direction-aware: higher-is-better for
         all axes except stale_leak_rate, which is lower-is-better), forces
         a nonzero exit and names the offending axis/probe in stdout.
  INV3 - the compare path performs no network calls: fetch_dream_session and
         call_mcp_tool are patched to raise AssertionError if invoked, and
         the regression is still correctly detected offline.
  INV4 - an axis reported as UNMEASURED (null value) in either report is
         skipped rather than scored as a regression.

DEPENDENCIES:
- Python 3.14 stdlib only: unittest, tempfile, json, contextlib, io,
  unittest.mock, pathlib.
- scripts/run_eval.py (imported directly via sys.path insert; no repo
  dependency install beyond what run_eval.py itself already requires).

USAGE:
  python -m unittest tests.python.test_run_eval_compare -v
  (or via the repo-wide checker: make test-python-checker)

NOTES:
- Purely offline and side-effect-free by construction (see INV3 test); safe
  to run in CI or any sandbox with no network access.
=============================================================================
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_eval  # noqa: E402


def make_report(config_tag: str, axes_values: dict, probes: list[dict],
                 tokens_median: int = 100) -> dict:
    """Build a minimal report dict shaped like run_eval.run_eval()'s output.
    axes_values: {axis_name: value_or_None} for any subset of run_eval.AXIS_METRIC.
    probes: [{"id": ..., "enabled": True, "passed": True/False}, ...]
    """
    axes = {}
    for axis, metric in run_eval.AXIS_METRIC.items():
        value = axes_values.get(axis, 0.9)
        axes[axis] = {"metric": metric, "value": value,
                      "n": 0 if value is None else 10,
                      "status": "UNMEASURED" if value is None else "measured"}
    return {
        "generated_at": "2026-07-07T00:00:00+00:00",
        "config_tag": config_tag,
        "axes": axes,
        "tokens_per_query": {"median": tokens_median, "p95": tokens_median},
        "probes": probes,
    }


class RegressionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

    def _write(self, name: str, report: dict) -> str:
        p = self.tmp_path / name
        p.write_text(json.dumps(report))
        return str(p)

    def _run_compare(self, old_path: str, new_path: str, fail_on_regression: bool) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run_eval.compare(old_path, new_path, fail_on_regression=fail_on_regression)
        return rc, buf.getvalue()

    # --- INV1: self-compare always exits 0 ---------------------------------

    def test_self_compare_exits_zero(self) -> None:
        report = make_report("baseline", {}, [
            {"id": "p1", "enabled": True, "passed": True},
            {"id": "p2", "enabled": True, "passed": False},
        ])
        path = self._write("report.json", report)
        rc, _ = self._run_compare(path, path, fail_on_regression=True)
        self.assertEqual(rc, 0)

    # --- INV4: UNMEASURED axis (null in either report) is skipped ----------

    def test_unmeasured_axis_in_new_is_skipped_not_scored_as_regression(self) -> None:
        old = make_report("baseline", {"carry_forward_recall": 0.90}, [])
        new = make_report("fresh", {"carry_forward_recall": None}, [])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 0, out)

    def test_unmeasured_axis_in_old_is_skipped_not_scored_as_regression(self) -> None:
        old = make_report("baseline", {"carry_forward_recall": None}, [])
        new = make_report("fresh", {"carry_forward_recall": 0.10}, [])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 0, out)

    # --- INV2: degraded axis metric or probe flip forces nonzero exit ------

    def test_axis_metric_drop_beyond_tolerance_exits_nonzero_and_names_axis(self) -> None:
        old = make_report("baseline", {"carry_forward_recall": 0.90}, [])
        new = make_report("fresh", {"carry_forward_recall": 0.80}, [])  # -0.10, tolerance is 0.02
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 1)
        self.assertIn("carry_forward_recall", out)
        self.assertIn("REGRESSION GATE: FAIL", out)

    def test_axis_metric_drop_within_tolerance_exits_zero(self) -> None:
        old = make_report("baseline", {"carry_forward_recall": 0.90}, [])
        new = make_report("fresh", {"carry_forward_recall": 0.89}, [])  # -0.01, within tolerance
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 0, out)

    def test_probe_flip_pass_to_fail_exits_nonzero_and_names_probe(self) -> None:
        old = make_report("baseline", {}, [{"id": "carry_forward_01", "enabled": True, "passed": True}])
        new = make_report("fresh", {}, [{"id": "carry_forward_01", "enabled": True, "passed": False}])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 1)
        self.assertIn("carry_forward_01", out)

    def test_probe_missing_from_new_report_counts_as_regression(self) -> None:
        # A probe that passed in OLD but is entirely absent from NEW (e.g. a
        # --only-axis run, or a dropped/renamed probe id) must not silently
        # pass the gate just because it never flipped to an explicit False.
        old = make_report("baseline", {}, [{"id": "carry_forward_01", "enabled": True, "passed": True}])
        new = make_report("fresh", {}, [])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 1)
        self.assertIn("carry_forward_01", out)

    def test_probe_flip_fail_to_pass_is_an_improvement_not_a_regression(self) -> None:
        old = make_report("baseline", {}, [{"id": "carry_forward_01", "enabled": True, "passed": False}])
        new = make_report("fresh", {}, [{"id": "carry_forward_01", "enabled": True, "passed": True}])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 0, out)

    def test_stale_leak_rate_increase_beyond_tolerance_regresses(self) -> None:
        # stale_leak_rate is the one LOWER-is-better metric: an INCREASE is the regression.
        old = make_report("baseline", {"stale_fact": 0.0}, [])
        new = make_report("fresh", {"stale_fact": 0.05}, [])  # +0.05, tolerance is 0.02
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 1)
        self.assertIn("stale_fact", out)

    def test_stale_leak_rate_decrease_is_an_improvement_not_a_regression(self) -> None:
        old = make_report("baseline", {"stale_fact": 0.10}, [])
        new = make_report("fresh", {"stale_fact": 0.0}, [])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 0, out)

    def test_fail_on_regression_flag_off_never_forces_nonzero(self) -> None:
        # Regression-shaped diff, but the flag is off: --compare stays descriptive-only.
        old = make_report("baseline", {"carry_forward_recall": 0.90}, [])
        new = make_report("fresh", {"carry_forward_recall": 0.10}, [])
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        rc, _ = self._run_compare(old_p, new_p, fail_on_regression=False)
        self.assertEqual(rc, 0)

    # --- INV3: the compare path is offline and side-effect free ------------

    def test_compare_never_touches_the_network_client(self) -> None:
        old = make_report("baseline", {"carry_forward_recall": 0.90}, [])
        new = make_report("fresh", {"carry_forward_recall": 0.10}, [])  # regression-shaped
        old_p, new_p = self._write("old.json", old), self._write("new.json", new)
        with mock.patch.object(run_eval, "fetch_dream_session",
                                side_effect=AssertionError("network call attempted")), \
             mock.patch.object(run_eval, "call_mcp_tool",
                                side_effect=AssertionError("network call attempted")):
            rc, out = self._run_compare(old_p, new_p, fail_on_regression=True)
        self.assertEqual(rc, 1, out)  # still correctly detects the regression, offline


if __name__ == "__main__":
    unittest.main()
