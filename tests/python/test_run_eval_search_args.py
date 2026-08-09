"""
=============================================================================
SCRIPT NAME: test_run_eval_search_args.py
=============================================================================

INPUT FILES:
- None. No file I/O; the module under test is imported directly.

OUTPUT FILES:
- None. unittest reports to stdout only.

VERSION: 1.0
LAST UPDATED: 2026-07-09
AUTHOR: Claude (Fable 5) for Arjun Divecha

DESCRIPTION:
Pins the synthetic-traffic marker on the eval runner's MCP search calls
(contract PKS-USAGE-SIGNAL-001,
/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/usage-signal-loop.spec.md).

/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/run_eval.py
must pass suppress_access_signals=True on every search probe so that nightly
regression-gate runs do not fabricate usage signal (access_count /
last_accessed reinforcement) that salience would then treat as organic use.
If someone removes the flag, this test fails and names why it matters.

DEPENDENCIES: Python stdlib unittest only.
USAGE: python -m unittest tests.python.test_run_eval_search_args -v
       (or via the repo-wide checker: make test-python-checker)
=============================================================================
"""

from __future__ import annotations

import sys
import unittest
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_eval  # noqa: E402
from check_overnight_dream_run import parse_sse_json  # noqa: E402


class SearchArgumentsTests(unittest.TestCase):
    def test_result_text_covers_source_first_evidence_fields(self) -> None:
        text = run_eval.result_text({
            "title": "Tracker",
            "text": "Current source-backed project evidence",
            "project": "Tracker",
            "source_path": "/projects/Tracker/README.md",
        })
        self.assertIn("source-backed project evidence", text)
        self.assertIn("/projects/tracker/readme.md", text)

    def test_large_wrapped_sse_payload_is_reassembled_before_json_decode(self) -> None:
        inner = json.dumps({"mode": "source_first", "results": [{"id": "ev_1"}]})
        outer = json.dumps({"result": {"content": [{"type": "text", "text": inner}]}})
        wrapped = "event: message\ndata: " + outer[:35] + "\n" + outer[35:] + "\n\n"
        decoded = parse_sse_json(wrapped)
        self.assertEqual(decoded["result"]["content"][0]["type"], "text")

    def test_every_probe_query_is_marked_as_synthetic_traffic(self) -> None:
        args = run_eval.build_search_arguments("what is the PKS architecture?")
        self.assertEqual(args["query"], "what is the PKS architecture?")
        self.assertIs(args["suppress_access_signals"], True,
                      "eval probes must never count as organic use "
                      "(PKS-USAGE-SIGNAL-001): removing this flag silently "
                      "re-poisons the usage-reinforcement loop")

    def test_no_unexpected_arguments_leak_into_the_tool_call(self) -> None:
        args = run_eval.build_search_arguments("q")
        self.assertEqual(set(args), {"query", "suppress_access_signals"})


if __name__ == "__main__":
    unittest.main()
