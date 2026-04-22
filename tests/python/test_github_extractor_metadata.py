from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from core.extractor import Extractor


class _FakeMessages:
    def __init__(self, text: str):
        self._text = text

    def create(self, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


class GitHubExtractorMetadataTests(unittest.TestCase):
    def _extractor_with_payload(self, payload: list[dict]) -> Extractor:
        extractor = Extractor.__new__(Extractor)
        extractor.client = SimpleNamespace(messages=_FakeMessages(json.dumps(payload)))
        return extractor

    def test_extract_from_readme_sets_repo_metadata(self) -> None:
        extractor = self._extractor_with_payload(
            [
                {
                    "domain": "Repo overview",
                    "current_view": "Tracks the project architecture and recent capabilities.",
                    "confidence": "high",
                    "key_insights": [{"insight": "Architecture captured", "evidence_snippet": "README text"}],
                    "capabilities": ["System design"],
                }
            ]
        )

        entries = extractor.extract_from_readme(
            readme_content="A" * 200,
            repo_name="Pattern",
            repo_url="https://github.com/ArjunDivecha/Pattern",
            repo_full_name="ArjunDivecha/Pattern",
        )

        self.assertEqual(len(entries), 1)
        metadata = entries[0]["metadata"]
        self.assertEqual(metadata["github_repo"], "ArjunDivecha/Pattern")
        self.assertEqual(metadata["github_url"], "https://github.com/ArjunDivecha/Pattern")
        self.assertEqual(metadata["source_type"], "github_readme")
        self.assertEqual(entries[0]["related_repos"][0]["repo"], "ArjunDivecha/Pattern")

    def test_extract_from_commits_sets_repo_metadata(self) -> None:
        extractor = self._extractor_with_payload(
            [
                {
                    "domain": "Commit workflow",
                    "current_view": "Commit messages document architecture and validation decisions.",
                    "confidence": "high",
                    "evidence_snippet": "Add trash-tier slicing",
                }
            ]
        )

        entries = extractor.extract_from_commits(
            commits=[
                {
                    "sha": "abc123",
                    "message": "Add trash-tier slicing with validation and capacity notes " * 2,
                    "date": "2026-04-21T19:32:10Z",
                    "author": "Arjun",
                }
            ],
            repo_name="Pattern",
            repo_url="https://github.com/ArjunDivecha/Pattern",
            repo_full_name="ArjunDivecha/Pattern",
        )

        self.assertEqual(len(entries), 1)
        metadata = entries[0]["metadata"]
        self.assertEqual(metadata["github_repo"], "ArjunDivecha/Pattern")
        self.assertEqual(metadata["github_url"], "https://github.com/ArjunDivecha/Pattern")
        self.assertEqual(metadata["source_type"], "github_commits")

    def test_extract_from_code_comments_sets_repo_metadata(self) -> None:
        extractor = self._extractor_with_payload(
            [
                {
                    "domain": "Code rationale",
                    "current_view": "The code preserves a rationale comment for a pricing workaround.",
                    "confidence": "medium",
                    "source_file": "src/pricing.py",
                    "evidence_snippet": "# workaround: preserve the live spread estimate",
                }
            ]
        )

        entries = extractor.extract_from_code_comments(
            files=[
                {
                    "path": "src/pricing.py",
                    "content": "line1\n# workaround: preserve the live spread estimate\nline3",
                }
            ],
            repo_name="Pattern",
            repo_url="https://github.com/ArjunDivecha/Pattern",
            repo_full_name="ArjunDivecha/Pattern",
        )

        self.assertEqual(len(entries), 1)
        metadata = entries[0]["metadata"]
        self.assertEqual(metadata["github_repo"], "ArjunDivecha/Pattern")
        self.assertEqual(metadata["github_url"], "https://github.com/ArjunDivecha/Pattern")
        self.assertEqual(metadata["source_type"], "github_code")


if __name__ == "__main__":
    unittest.main()
