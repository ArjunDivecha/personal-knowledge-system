from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from github.client import GitHubClient


class GitHubClientTests(unittest.TestCase):
    def test_list_repos_skips_empty_and_fork_repos(self) -> None:
        client = GitHubClient(token="test-token", username="ArjunDivecha")
        client._request = MagicMock(return_value=[
            {
                "name": "active-repo",
                "full_name": "ArjunDivecha/active-repo",
                "description": "real repo",
                "language": "Python",
                "stargazers_count": 2,
                "html_url": "https://github.com/ArjunDivecha/active-repo",
                "fork": False,
                "size": 12,
                "archived": False,
                "default_branch": "main",
                "updated_at": "2026-04-22T00:00:00Z",
            },
            {
                "name": "empty-repo",
                "full_name": "ArjunDivecha/empty-repo",
                "description": "empty repo",
                "language": None,
                "stargazers_count": 0,
                "html_url": "https://github.com/ArjunDivecha/empty-repo",
                "fork": False,
                "size": 0,
                "archived": True,
                "default_branch": "main",
                "updated_at": "2026-04-22T00:00:00Z",
            },
            {
                "name": "fork-repo",
                "full_name": "ArjunDivecha/fork-repo",
                "description": "fork repo",
                "language": "Python",
                "stargazers_count": 0,
                "html_url": "https://github.com/ArjunDivecha/fork-repo",
                "fork": True,
                "size": 42,
                "archived": False,
                "default_branch": "main",
                "updated_at": "2026-04-22T00:00:00Z",
            },
        ])

        repos = client.list_repos(include_forks=False)

        self.assertEqual(len(repos), 1)
        self.assertEqual(repos[0]["name"], "active-repo")
        self.assertEqual(repos[0]["size"], 12)
        self.assertFalse(repos[0]["archived"])

    @patch("github.client.requests.get")
    def test_request_treats_empty_repo_conflict_as_none(self, mock_get) -> None:
        response = MagicMock()
        response.status_code = 409
        response.json.return_value = {"message": "Git Repository is empty."}
        mock_get.return_value = response

        client = GitHubClient(token="test-token", username="ArjunDivecha")
        result = client._request("/repos/ArjunDivecha/empty-repo/commits")

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
