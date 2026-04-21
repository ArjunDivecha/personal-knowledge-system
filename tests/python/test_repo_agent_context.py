from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"
EXPORTER_PATH = REPO_ROOT / "scripts" / "export_repo_agent_context.py"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from core.storage import StorageClient


def _load_exporter_module():
    spec = importlib.util.spec_from_file_location("export_repo_agent_context", EXPORTER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


exporter = _load_exporter_module()


class RepoAgentContextExporterTests(unittest.TestCase):
    def test_build_output_path_is_session_scoped(self) -> None:
        artifact = exporter.SessionArtifact(
            surface="claude_code",
            source_path=Path("/Users/arjun/.claude/projects/demo/session.jsonl"),
            session_id="session/with spaces:42",
            exported_at="2026-04-21T08:00:00+00:00",
            commit_sha="abc123",
            github_repo="ArjunDivecha/demo",
            repo_root=Path("/tmp/demo"),
            turns=[],
        )

        output_path = exporter.build_output_path(Path("/tmp/demo/.pks/agent-context"), artifact)

        self.assertEqual(output_path.name, "claude-code-session-with-spaces-42.md")

    def test_render_markdown_omits_absolute_source_path(self) -> None:
        artifact = exporter.SessionArtifact(
            surface="codex_cli",
            source_path=Path("/Users/arjun/.codex/sessions/2026/rollout-123.jsonl"),
            session_id="rollout-123",
            exported_at="2026-04-21T08:00:00+00:00",
            commit_sha="abc123",
            github_repo="ArjunDivecha/demo",
            repo_root=Path("/tmp/demo"),
            turns=[{"role": "user", "content": "Ship the repo context hook."}],
        )

        rendered = exporter.render_markdown(artifact)

        self.assertIn("source_file: rollout-123.jsonl", rendered)
        self.assertNotIn("/Users/arjun/.codex", rendered)
        self.assertIn("_Session:_ `rollout-123`", rendered)

    def test_codex_jsonl_mentions_repo_via_function_call_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            child_dir = repo_root / "ingestion" / "github"
            child_dir.mkdir(parents=True)
            rollout = Path(tmpdir) / "rollout-test.jsonl"
            events = [
                {"type": "session_meta", "payload": {"cwd": str(Path(tmpdir))}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {
                                "cmd": "python3 run.py --dry-run",
                                "workdir": str(child_dir),
                            }
                        ),
                    },
                },
            ]
            rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            self.assertTrue(exporter.codex_jsonl_mentions_repo(rollout, repo_root))


class RepoAgentContextThinIndexTests(unittest.TestCase):
    def test_update_thin_index_refreshes_existing_entry(self) -> None:
        existing_index = {
            "generated_at": "2026-04-20T00:00:00",
            "token_count": 0,
            "topics": [
                {
                    "id": "ke_repo_topic",
                    "domain": "repo workflow",
                    "current_view_summary": "Old summary",
                    "state": "active",
                    "confidence": "medium",
                    "last_updated": "2026-04-20T00:00:00",
                    "context_type": None,
                    "mention_count": None,
                    "archived": False,
                    "top_repo": None,
                }
            ],
            "projects": [],
            "recent_evolutions": [],
            "contested_count": 0,
        }

        saved_indexes: list[dict] = []
        storage = StorageClient.__new__(StorageClient)
        storage.get_thin_index = lambda: existing_index
        storage.save_thin_index = lambda index: saved_indexes.append(index)

        storage.update_thin_index(
            [
                {
                    "id": "ke_repo_topic",
                    "domain": "repo workflow",
                    "current_view": "Updated repo-specific summary",
                    "state": "active",
                    "confidence": "high",
                    "metadata": {
                        "updated_at": "2026-04-21T01:00:00",
                        "context_type": "active_project",
                        "mention_count": 4,
                        "archived": False,
                        "github_repo": "ArjunDivecha/demo",
                    },
                    "related_repos": [],
                }
            ]
        )

        self.assertEqual(len(saved_indexes), 1)
        topic = saved_indexes[0]["topics"][0]
        self.assertEqual(topic["current_view_summary"], "Updated repo-specific summary")
        self.assertEqual(topic["confidence"], "high")
        self.assertEqual(topic["context_type"], "active_project")
        self.assertEqual(topic["mention_count"], 4)
        self.assertEqual(topic["top_repo"], "ArjunDivecha/demo")


if __name__ == "__main__":
    unittest.main()
