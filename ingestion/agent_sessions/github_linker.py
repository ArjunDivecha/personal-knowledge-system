"""
=============================================================================
SCRIPT NAME: github_linker.py
=============================================================================

INPUT FILES:
- None directly (uses cwd paths from parsed session data)

OUTPUT FILES:
- None (returns GitHub metadata in memory)

VERSION: 1.0
LAST UPDATED: 2026-03-25
AUTHOR: Arjun Divecha

DESCRIPTION:
Detects which GitHub repo a session was working in by:
1. Looking at the cwd path from session events
2. Walking up to find a .git directory
3. Parsing the git remote to get the GitHub owner/repo
4. Fetching the README via GitHubClient (cached to avoid repeated API calls)

This enriches knowledge entries with repo context (URL, README summary).

DEPENDENCIES:
- subprocess (stdlib) for git commands
- pathlib (stdlib)
- ingestion.github.client.GitHubClient

USAGE:
    from agent_sessions.github_linker import GitHubLinker
    linker = GitHubLinker(github_client)
    info = linker.get_repo_info("/Users/arjun/src/my-project")
    # Returns: {"repo_name": "my-project", "full_name": "ArjunDivecha/my-project",
    #           "url": "https://github.com/...", "readme_summary": "..."}
=============================================================================
"""

import re
import subprocess
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class GitHubLinker:
    """
    Resolves local directory paths to GitHub repo metadata.

    Caches results per directory to avoid repeated git/API calls.
    """

    def __init__(self, github_client=None):
        """
        Args:
            github_client: Optional GitHubClient instance. If None, README
                          fetching is skipped but repo URL detection still works.
        """
        self.github_client = github_client
        self._cache: dict[str, Optional[dict]] = {}

    def get_repo_info(self, cwd: str) -> Optional[dict]:
        """
        Get GitHub repo info for a working directory.

        Args:
            cwd: The working directory path from a session event

        Returns:
            dict with {repo_name, full_name, url, readme_summary, owner, default_branch}
            or None if not a GitHub repo
        """
        if not cwd:
            return None

        # Check cache
        if cwd in self._cache:
            return self._cache[cwd]

        result = self._resolve(cwd)
        self._cache[cwd] = result
        return result

    def _resolve(self, cwd: str) -> Optional[dict]:
        """Resolve a directory to GitHub repo info."""
        path = Path(cwd)

        # Find git root by walking up
        git_root = self._find_git_root(path)
        if not git_root:
            return None

        # Check cache for git root (different cwds may share a repo)
        cache_key = str(git_root)
        if cache_key in self._cache:
            return self._cache[cache_key]

        # Parse the remote URL
        remote_info = self._parse_git_remote(git_root)
        if not remote_info:
            self._cache[cache_key] = None
            return None

        owner = remote_info["owner"]
        repo_name = remote_info["repo"]
        full_name = f"{owner}/{repo_name}"
        url = f"https://github.com/{full_name}"

        result = {
            "repo_name": repo_name,
            "full_name": full_name,
            "url": url,
            "owner": owner,
            "readme_summary": None,
        }

        # Fetch README if we have a GitHub client
        if self.github_client:
            try:
                readme = self.github_client.get_readme(repo_name)
                if readme:
                    # Truncate to first ~1500 chars for summary
                    result["readme_summary"] = readme[:1500].strip()
                    if len(readme) > 1500:
                        result["readme_summary"] += "\n..."
            except Exception as e:
                log.debug(f"Could not fetch README for {full_name}: {e}")

        self._cache[cache_key] = result
        return result

    def _find_git_root(self, path: Path) -> Optional[Path]:
        """Walk up from path to find the nearest .git directory."""
        current = path if path.is_dir() else path.parent
        # Walk up at most 10 levels
        for _ in range(10):
            if (current / ".git").exists():
                return current
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def _parse_git_remote(self, git_root: Path) -> Optional[dict]:
        """
        Parse the GitHub owner/repo from git remote.

        Handles:
          - git@github.com:Owner/Repo.git
          - https://github.com/Owner/Repo.git
          - https://github.com/Owner/Repo
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(git_root), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

            url = result.stdout.strip()

            # SSH format: git@github.com:Owner/Repo.git
            ssh_match = re.match(r"git@github\.com:(.+?)/(.+?)(?:\.git)?$", url)
            if ssh_match:
                return {"owner": ssh_match.group(1), "repo": ssh_match.group(2)}

            # HTTPS format: https://github.com/Owner/Repo.git
            https_match = re.match(
                r"https://github\.com/(.+?)/(.+?)(?:\.git)?$", url
            )
            if https_match:
                return {"owner": https_match.group(1), "repo": https_match.group(2)}

            return None

        except Exception as e:
            log.debug(f"Git remote parse error for {git_root}: {e}")
            return None
