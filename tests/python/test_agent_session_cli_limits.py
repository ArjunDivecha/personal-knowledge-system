from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

import agent_sessions.run as agent_run


class _DummyLinker:
    def get_repo_info(self, cwd: str):
        return None


class AgentSessionCliLimitTests(unittest.TestCase):
    def test_limit_zero_processes_no_files(self) -> None:
        fake_path = Path(tempfile.gettempdir()) / "rollout-fake.jsonl"
        process_calls: list[Path] = []

        with patch.object(sys, "argv", ["run.py", "--dry-run", "--source", "codex_cli", "--limit", "0"]):
            with patch.object(agent_run, "load_state", return_value=(agent_run._default_state(), "redis")):
                with patch.object(agent_run, "StorageClient", return_value=object()):
                    with patch.object(agent_run, "GitHubClient", return_value=object()):
                        with patch.object(agent_run, "GitHubLinker", return_value=_DummyLinker()):
                            with patch.object(agent_run, "discover_codex_files", return_value=[fake_path]):
                                with patch.object(agent_run, "process_file", side_effect=lambda **kwargs: process_calls.append(kwargs["path"]) or 0):
                                    agent_run.main()

        self.assertEqual(process_calls, [])


if __name__ == "__main__":
    unittest.main()
