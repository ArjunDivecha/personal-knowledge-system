from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from agent_sessions.parsers import parse_codex
import agent_sessions.run as agent_run


class _DummyLinker:
    def get_repo_info(self, cwd: str):
        return None


class AgentSessionParserTests(unittest.TestCase):
    def test_parse_codex_accepts_user_and_developer_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout-test.jsonl"
            events = [
                {
                    "type": "session_meta",
                    "payload": {"cwd": "/tmp/knowledge-system"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "pull the latest main"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "developer instructions"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Working on it."}],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            turns, _, meta = parse_codex(path, 0)

            self.assertEqual([turn["role"] for turn in turns], ["user", "user", "assistant"])
            self.assertEqual(meta["cwd"], "/tmp/knowledge-system")
            self.assertEqual(meta["project"], "knowledge-system")
            self.assertIn("pull the latest", turns[0]["content"])
            self.assertIn("developer instructions", turns[1]["content"])


class AgentSessionCheckpointTests(unittest.TestCase):
    def test_process_file_does_not_advance_checkpoint_on_distillation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout-test.jsonl"
            path.write_text("{}\n")

            state = agent_run._default_state()
            save_calls: list[dict] = []
            turns = [
                {
                    "role": "user",
                    "content": "x" * 400,
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
                {
                    "role": "assistant",
                    "content": "Acknowledged.",
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
                {
                    "role": "user",
                    "content": "y" * 400,
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
                {
                    "role": "assistant",
                    "content": "Still working.",
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
            ]

            with patch.object(agent_run, "parse_codex", return_value=(turns, 99, {"cwd": "", "project": "knowledge-system"})):
                with patch.object(agent_run, "distill", side_effect=agent_run.DistillationFailure("bad json")):
                    with patch.object(agent_run, "save_state", side_effect=lambda new_state: save_calls.append(dict(new_state))):
                        saved = agent_run.process_file(
                            path=path,
                            source_type="codex_cli",
                            state=state,
                            anthropic_client=object(),
                            storage=object(),
                            linker=_DummyLinker(),
                            dry_run=False,
                        )

            self.assertEqual(saved, 0)
            self.assertEqual(save_calls, [])
            self.assertNotIn(str(path), state["files"])

    def test_process_file_advances_checkpoint_when_distillation_succeeds_with_no_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "rollout-test.jsonl"
            path.write_text("{}\n")

            state = agent_run._default_state()
            save_calls: list[dict] = []
            turns = [
                {
                    "role": "user",
                    "content": "x" * 400,
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
                {
                    "role": "assistant",
                    "content": "Acknowledged.",
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
                {
                    "role": "user",
                    "content": "y" * 400,
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
                {
                    "role": "assistant",
                    "content": "Still working.",
                    "timestamp": "",
                    "session_id": "sid",
                    "source": "codex_cli",
                    "project": "knowledge-system",
                    "cwd": "",
                },
            ]

            with patch.object(agent_run, "parse_codex", return_value=(turns, 99, {"cwd": "", "project": "knowledge-system"})):
                with patch.object(agent_run, "distill", return_value=[]):
                    with patch.object(agent_run, "save_state", side_effect=lambda new_state: save_calls.append(dict(new_state))):
                        saved = agent_run.process_file(
                            path=path,
                            source_type="codex_cli",
                            state=state,
                            anthropic_client=object(),
                            storage=object(),
                            linker=_DummyLinker(),
                            dry_run=False,
                        )

            self.assertEqual(saved, 0)
            self.assertEqual(len(save_calls), 1)
            self.assertEqual(state["files"][str(path)]["offset"], 99)
            self.assertEqual(state["files"][str(path)]["mtime"], path.stat().st_mtime)


if __name__ == "__main__":
    unittest.main()
