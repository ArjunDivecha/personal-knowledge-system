from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

import github.run as github_run


class _FakeStorage:
    def __init__(self, processed: Optional[list[str]] = None, metadata: Optional[dict] = None):
        self.processed = processed or []
        self.metadata = metadata or {}
        self.marked: list[tuple[str, str, Optional[dict]]] = []
        self.saved_batches: list[list[dict]] = []
        self.thin_index_updates: list[list[dict]] = []

    def test_connection(self) -> tuple[bool, str]:
        return True, "ok"

    def get_processed_sources(self, source_type: str) -> list[str]:
        if source_type == "github":
            return list(self.processed)
        return []

    def get_source_metadata(self, source_type: str, source_id: str) -> Optional[dict]:
        return self.metadata.get((source_type, source_id))

    def is_source_processed(self, source_type: str, source_id: str) -> bool:
        return False

    def mark_source_processed(self, source_type: str, source_id: str, metadata: Optional[dict] = None):
        self.marked.append((source_type, source_id, metadata))

    def save_knowledge_entries_batch(self, entries: list[dict]):
        self.saved_batches.append(entries)

    def update_thin_index(self, entries: list[dict]):
        self.thin_index_updates.append(entries)

    def get_stats(self) -> dict:
        return {"knowledge_entries": 1, "project_entries": 1, "total_vectors": 1}


class _FakeGitHubClient:
    def __init__(self, repos: list[dict]):
        self.repos = repos
        self.readme_calls = 0
        self.commit_calls = 0
        self.code_calls = 0
        self.agent_calls = 0

    def get_rate_limit(self) -> dict:
        return {"remaining": 5000, "limit": 5000}

    def list_repos(self, include_forks: bool = False) -> list[dict]:
        return list(self.repos)

    def get_repo_info(self, repo_name: str) -> Optional[dict]:
        for repo in self.repos:
            if repo["name"] == repo_name:
                return dict(repo)
        return None

    def get_readme(self, repo_name: str) -> Optional[str]:
        self.readme_calls += 1
        return "x" * 200

    def get_commits(self, repo_name: str, max_commits: int = 50) -> list[dict]:
        self.commit_calls += 1
        return []

    def get_code_files(self, repo_name: str, max_files: int = 20) -> list[dict]:
        self.code_calls += 1
        return []

    def get_agent_context_files(self, repo_name: str) -> list[dict]:
        self.agent_calls += 1
        return []


class _FakeExtractor:
    def extract_from_readme(self, readme_content: str, repo_name: str, repo_url: str) -> list[dict]:
        return [
            {
                "id": "ke_demo",
                "domain": "demo domain",
                "current_view": "demo summary",
                "state": "active",
                "confidence": "high",
                "metadata": {
                    "updated_at": "2026-04-22T00:00:00Z",
                    "github_repo": f"ArjunDivecha/{repo_name}",
                },
                "related_repos": [],
            }
        ]

    def extract_from_commits(self, commits: list[dict], repo_name: str) -> list[dict]:
        return []

    def extract_from_code_comments(self, files: list[dict], repo_name: str) -> list[dict]:
        return []

    def extract_from_agent_context_artifact(self, **kwargs) -> list[dict]:
        return []


class GitHubRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = {
            "name": "demo",
            "full_name": "ArjunDivecha/demo",
            "url": "https://github.com/ArjunDivecha/demo",
            "default_branch": "main",
            "updated_at": "2026-04-22T01:00:00Z",
            "pushed_at": "2026-04-22T01:00:00Z",
        }

    def test_run_github_ingestion_refreshes_changed_repo_baseline(self) -> None:
        storage = _FakeStorage(
            processed=["demo"],
            metadata={
                ("github", "demo"): {
                    "baseline_signature": "main:2026-04-21T01:00:00Z",
                }
            },
        )
        github = _FakeGitHubClient([self.repo])
        extractor = _FakeExtractor()

        with patch.object(github_run, "validate_github_config", return_value=[]):
            with patch.object(github_run, "GitHubClient", return_value=github):
                with patch.object(github_run, "Extractor", return_value=extractor):
                    with patch.object(github_run, "StorageClient", return_value=storage):
                        github_run.run_github_ingestion(
                            skip_commits=True,
                            skip_code=True,
                            dry_run=False,
                            resume=True,
                        )

        self.assertEqual(github.readme_calls, 1)
        self.assertEqual(len(storage.saved_batches), 1)
        github_marks = [mark for mark in storage.marked if mark[0] == "github"]
        self.assertEqual(len(github_marks), 1)
        self.assertEqual(
            github_marks[0][2]["baseline_signature"],
            "main:2026-04-22T01:00:00Z",
        )

    def test_run_github_ingestion_skips_unchanged_repo_baseline_but_scans_agent_context(self) -> None:
        storage = _FakeStorage(
            processed=["demo"],
            metadata={
                ("github", "demo"): {
                    "baseline_signature": "main:2026-04-22T01:00:00Z",
                }
            },
        )
        github = _FakeGitHubClient([self.repo])
        extractor = _FakeExtractor()

        with patch.object(github_run, "validate_github_config", return_value=[]):
            with patch.object(github_run, "GitHubClient", return_value=github):
                with patch.object(github_run, "Extractor", return_value=extractor):
                    with patch.object(github_run, "StorageClient", return_value=storage):
                        github_run.run_github_ingestion(
                            skip_commits=True,
                            skip_code=True,
                            dry_run=False,
                            resume=True,
                        )

        self.assertEqual(github.readme_calls, 0)
        self.assertEqual(github.agent_calls, 1)
        github_marks = [mark for mark in storage.marked if mark[0] == "github"]
        self.assertEqual(github_marks, [])


if __name__ == "__main__":
    unittest.main()
