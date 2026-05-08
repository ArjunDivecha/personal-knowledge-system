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
            export_base_commit_sha="abc123",
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
            export_base_commit_sha="abc123",
            github_repo="ArjunDivecha/demo",
            repo_root=Path("/tmp/demo"),
            turns=[{"role": "user", "content": "Ship the repo context hook."}],
        )

        rendered = exporter.render_markdown(artifact)

        self.assertIn("source_file: rollout-123.jsonl", rendered)
        self.assertNotIn("/Users/arjun/.codex", rendered)
        self.assertIn("_Session:_ `rollout-123`", rendered)

    def test_render_markdown_uses_truthful_export_base_commit_field(self) -> None:
        artifact = exporter.SessionArtifact(
            surface="claude_code",
            source_path=Path("/Users/arjun/.claude/projects/demo/session.jsonl"),
            session_id="session-123",
            exported_at="2026-04-21T08:00:00+00:00",
            export_base_commit_sha="deadbeef",
            github_repo="ArjunDivecha/demo",
            repo_root=Path("/tmp/demo"),
            turns=[{"role": "user", "content": "Update the exporter."}],
        )

        rendered = exporter.render_markdown(artifact)

        self.assertIn("export_base_commit_sha: deadbeef", rendered)
        self.assertNotIn("\ncommit_sha:", rendered)

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

    def test_cursor_jsonl_mentions_repo_via_tool_use_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir()
            transcript = Path(tmpdir) / "cursor-test.jsonl"
            events = [
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"path": str(repo_root / "README.md")},
                            }
                        ]
                    },
                }
            ]
            transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")

            self.assertTrue(exporter.cursor_jsonl_mentions_repo(transcript, repo_root))


class RepoAgentContextThinIndexTests(unittest.TestCase):
    def test_normalize_knowledge_metadata_sets_default_injection_tier(self) -> None:
        storage = StorageClient.__new__(StorageClient)

        metadata = storage._normalize_knowledge_metadata(
            {
                "created_at": "2026-04-21T00:00:00",
                "updated_at": "2026-04-21T01:00:00",
                "context_type": "active_project",
                "source_conversations": ["github:demo:readme"],
            }
        )

        self.assertEqual(metadata["injection_tier"], 1)
        self.assertEqual(metadata["mention_count"], 1)
        self.assertEqual(metadata["first_seen"], "2026-04-21T00:00:00")
        self.assertEqual(metadata["last_seen"], "2026-04-21T01:00:00")

    def test_update_thin_index_rebuilds_canonical_counts_from_redis(self) -> None:
        class FakeRedis:
            def __init__(self):
                self.values = {
                    "knowledge:ke_repo_topic": {
                        "id": "ke_repo_topic",
                        "type": "knowledge",
                        "domain": "repo workflow",
                        "state": "active",
                        "current_view": "Updated repo-specific summary",
                        "confidence": "high",
                        "positions": [],
                        "key_insights": [],
                        "knows_how_to": [],
                        "open_questions": [],
                        "related_repos": [
                            {"repo": "ArjunDivecha/demo", "link_type": "explicit"}
                        ],
                        "related_knowledge": [],
                        "evolution": [],
                        "metadata": {
                            "created_at": "2026-04-21T00:00:00",
                            "updated_at": "2026-04-21T01:00:00",
                            "source_conversations": ["github:demo:readme"],
                            "source_messages": [],
                            "classification_status": "pending",
                            "context_type": "active_project",
                            "mention_count": 1,
                            "archived": False,
                        },
                    }
                }

            def scan(self, cursor, match: str, count: int = 100):
                if match == "knowledge:*":
                    return 0, ["knowledge:ke_repo_topic"]
                if match == "project:*":
                    return 0, []
                return 0, []

            def mget(self, *keys):
                return [self.values[key] for key in keys]

            def set(self, key: str, value: str):
                self.values[key] = json.loads(value)

        fake_redis = FakeRedis()
        storage = StorageClient.__new__(StorageClient)
        storage.redis = fake_redis

        storage.update_thin_index([])

        saved = fake_redis.values["index:current"]
        self.assertEqual(saved["total_topic_count"], 1)
        self.assertEqual(saved["total_project_count"], 0)
        self.assertEqual(saved["tier_1_count"], 1)
        self.assertEqual(saved["topics"][0]["id"], "ke_repo_topic")
        self.assertEqual(saved["topics"][0]["injection_tier"], 1)

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
