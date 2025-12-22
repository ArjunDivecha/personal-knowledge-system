"""GitHub ingestion module."""

from .client import GitHubClient
from .run import run_github_ingestion

__all__ = ["GitHubClient", "run_github_ingestion"]

