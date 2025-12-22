"""
=============================================================================
GITHUB API CLIENT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Client for fetching repository data from GitHub API.
Retrieves READMEs, commits, and code files for knowledge extraction.

INPUT FILES:
- GitHub API token from environment

OUTPUT FILES:
- None (fetches data from GitHub API)
=============================================================================
"""

import base64
import time
from typing import Optional
from pathlib import Path

import requests

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import (
    GITHUB_API_KEY,
    GITHUB_USERNAME,
    GITHUB_MAX_COMMITS_PER_REPO,
    GITHUB_MAX_CODE_FILES_PER_REPO,
    GITHUB_CODE_EXTENSIONS,
)


class GitHubClient:
    """
    Client for GitHub API operations.
    
    Handles:
    - Listing repositories
    - Fetching README content
    - Fetching commit history
    - Fetching code files for comment extraction
    """
    
    BASE_URL = "https://api.github.com"
    
    def __init__(self, token: str = None, username: str = None):
        """Initialize with GitHub credentials."""
        self.token = token or GITHUB_API_KEY
        self.username = username or GITHUB_USERNAME
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self._request_count = 0
    
    def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make a rate-limited request to GitHub API."""
        url = f"{self.BASE_URL}{endpoint}"
        
        self._request_count += 1
        
        # Basic rate limiting (5000 requests/hour = ~1.4/sec)
        if self._request_count % 10 == 0:
            time.sleep(0.5)
        
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=30)
            
            if response.status_code == 404:
                return None
            elif response.status_code == 403:
                # Rate limited
                reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
                wait_time = max(0, reset_time - time.time())
                if wait_time > 0 and wait_time < 300:  # Wait up to 5 min
                    print(f"    Rate limited, waiting {wait_time:.0f}s...")
                    time.sleep(wait_time + 1)
                    return self._request(endpoint, params)
                else:
                    print(f"    Rate limited for {wait_time:.0f}s, skipping...")
                    return None
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"    Request error: {e}")
            return None
    
    # -------------------------------------------------------------------------
    # REPOSITORY OPERATIONS
    # -------------------------------------------------------------------------
    def list_repos(self, include_forks: bool = False) -> list[dict]:
        """
        List all repositories for the configured user.
        
        Returns list of {name, full_name, description, language, stars, url, is_fork}
        """
        repos = []
        page = 1
        
        while True:
            data = self._request(
                f"/users/{self.username}/repos",
                params={"per_page": 100, "page": page, "sort": "updated"}
            )
            
            if not data:
                break
            
            for repo in data:
                if repo.get("fork") and not include_forks:
                    continue
                
                repos.append({
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                    "description": repo.get("description"),
                    "language": repo.get("language"),
                    "stars": repo.get("stargazers_count", 0),
                    "url": repo.get("html_url"),
                    "is_fork": repo.get("fork", False),
                    "default_branch": repo.get("default_branch", "main"),
                    "updated_at": repo.get("updated_at"),
                })
            
            if len(data) < 100:
                break
            
            page += 1
        
        return repos
    
    def get_repo_info(self, repo_name: str) -> Optional[dict]:
        """Get detailed information about a repository."""
        data = self._request(f"/repos/{self.username}/{repo_name}")
        
        if not data:
            return None
        
        return {
            "name": data["name"],
            "full_name": data["full_name"],
            "description": data.get("description"),
            "language": data.get("language"),
            "stars": data.get("stargazers_count", 0),
            "url": data.get("html_url"),
            "default_branch": data.get("default_branch", "main"),
            "topics": data.get("topics", []),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        }
    
    # -------------------------------------------------------------------------
    # README OPERATIONS
    # -------------------------------------------------------------------------
    def get_readme(self, repo_name: str) -> Optional[str]:
        """
        Get the README content for a repository.
        
        Returns the decoded README text, or None if not found.
        """
        data = self._request(f"/repos/{self.username}/{repo_name}/readme")
        
        if not data:
            return None
        
        content = data.get("content", "")
        encoding = data.get("encoding", "base64")
        
        if encoding == "base64" and content:
            try:
                return base64.b64decode(content).decode("utf-8")
            except Exception:
                return None
        
        return content if content else None
    
    # -------------------------------------------------------------------------
    # COMMIT OPERATIONS
    # -------------------------------------------------------------------------
    def get_commits(self, repo_name: str, max_commits: int = None) -> list[dict]:
        """
        Get commit history for a repository.
        
        Returns list of {sha, message, date, author, files_changed}
        """
        max_commits = max_commits or GITHUB_MAX_COMMITS_PER_REPO
        commits = []
        page = 1
        
        while len(commits) < max_commits:
            data = self._request(
                f"/repos/{self.username}/{repo_name}/commits",
                params={"per_page": 100, "page": page}
            )
            
            if not data:
                break
            
            for commit in data:
                commit_data = commit.get("commit", {})
                commits.append({
                    "sha": commit.get("sha"),
                    "message": commit_data.get("message", ""),
                    "date": commit_data.get("author", {}).get("date", ""),
                    "author": commit_data.get("author", {}).get("name", ""),
                })
                
                if len(commits) >= max_commits:
                    break
            
            if len(data) < 100:
                break
            
            page += 1
        
        return commits
    
    # -------------------------------------------------------------------------
    # CODE FILE OPERATIONS
    # -------------------------------------------------------------------------
    def get_repo_tree(self, repo_name: str, branch: str = None) -> list[dict]:
        """
        Get the file tree for a repository.
        
        Returns list of {path, type, size, url} for all files.
        """
        if not branch:
            repo_info = self.get_repo_info(repo_name)
            branch = repo_info.get("default_branch", "main") if repo_info else "main"
        
        data = self._request(
            f"/repos/{self.username}/{repo_name}/git/trees/{branch}",
            params={"recursive": "true"}
        )
        
        if not data:
            return []
        
        files = []
        for item in data.get("tree", []):
            if item.get("type") == "blob":
                files.append({
                    "path": item.get("path", ""),
                    "size": item.get("size", 0),
                    "sha": item.get("sha"),
                })
        
        return files
    
    def get_file_content(self, repo_name: str, file_path: str) -> Optional[str]:
        """
        Get the content of a file from a repository.
        
        Returns the decoded file content, or None if not found.
        """
        # URL-encode the path
        encoded_path = file_path.replace("/", "%2F")
        data = self._request(f"/repos/{self.username}/{repo_name}/contents/{file_path}")
        
        if not data:
            return None
        
        content = data.get("content", "")
        encoding = data.get("encoding", "base64")
        
        if encoding == "base64" and content:
            try:
                return base64.b64decode(content).decode("utf-8")
            except Exception:
                return None
        
        return content if content else None
    
    def get_code_files(self, repo_name: str, max_files: int = None) -> list[dict]:
        """
        Get code files suitable for comment extraction.
        
        Returns list of {path, content} for code files.
        """
        max_files = max_files or GITHUB_MAX_CODE_FILES_PER_REPO
        
        tree = self.get_repo_tree(repo_name)
        
        # Filter to code files
        code_files = [
            f for f in tree
            if any(f["path"].endswith(ext) for ext in GITHUB_CODE_EXTENSIONS)
            and f.get("size", 0) < 100000  # Skip very large files
        ]
        
        # Prioritize by likely importance
        def priority(f):
            path = f["path"].lower()
            if "test" in path or "spec" in path:
                return 3
            if "example" in path or "demo" in path:
                return 2
            return 1
        
        code_files.sort(key=priority)
        code_files = code_files[:max_files]
        
        # Fetch content
        files_with_content = []
        for f in code_files:
            content = self.get_file_content(repo_name, f["path"])
            if content:
                files_with_content.append({
                    "path": f["path"],
                    "content": content,
                })
        
        return files_with_content
    
    # -------------------------------------------------------------------------
    # STATISTICS
    # -------------------------------------------------------------------------
    def get_rate_limit(self) -> dict:
        """Get current rate limit status."""
        data = self._request("/rate_limit")
        
        if not data:
            return {"remaining": 0, "limit": 0, "reset": 0}
        
        core = data.get("resources", {}).get("core", {})
        return {
            "remaining": core.get("remaining", 0),
            "limit": core.get("limit", 0),
            "reset": core.get("reset", 0),
        }


if __name__ == "__main__":
    # Quick test when run directly
    client = GitHubClient()
    
    print("=== GitHub Client Test ===\n")
    
    # Check rate limit
    limit = client.get_rate_limit()
    print(f"Rate limit: {limit['remaining']}/{limit['limit']}")
    
    # List repos
    repos = client.list_repos()
    print(f"\nFound {len(repos)} repositories:")
    for repo in repos[:5]:
        print(f"  - {repo['name']} ({repo['language'] or 'N/A'}) ★{repo['stars']}")
    
    if repos:
        # Get README from first repo
        first_repo = repos[0]["name"]
        readme = client.get_readme(first_repo)
        print(f"\nREADME from {first_repo}: {len(readme) if readme else 0} chars")

