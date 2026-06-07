#!/usr/bin/env python3
"""
=============================================================================
GITHUB INGESTION RUNNER
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Ingest knowledge from GitHub repositories.
Extracts from READMEs, commits, and code comments.

INPUT FILES:
- GitHub API (via token)

OUTPUT FILES:
- Knowledge entries in Upstash Redis
- Embeddings in Upstash Vector
- Checkpoint files in checkpoints/

USAGE:
    python run.py                    # All repos
    python run.py --repos "A,B,C"    # Specific repos
    python run.py --skip-code        # Skip code comment extraction
    python run.py --dry-run          # Extract but don't save
=============================================================================
"""

import argparse
import json
import pickle
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# Setup path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import validate_github_config, CHECKPOINT_DIR
from core.storage import StorageClient
from core.extractor import Extractor
from github.client import GitHubClient


def check_extractor_error(extractor: Extractor, label: str) -> None:
    """Raise if an extractor swallowed a hard LLM/parse error and returned []."""
    last_error = getattr(extractor, "last_error", None)
    if last_error:
        raise RuntimeError(f"{label} extraction failed: {last_error}")


def save_checkpoint(name: str, data: any):
    """Save checkpoint data to disk."""
    path = CHECKPOINT_DIR / f"github_{name}.pkl"
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"  ✓ Checkpoint saved: {name}")


def load_checkpoint(name: str) -> any:
    """Load checkpoint data from disk."""
    path = CHECKPOINT_DIR / f"github_{name}.pkl"
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    return None


def build_repo_baseline_signature(repo: dict) -> str:
    """Build a stable signature for deciding when repo baseline extraction must rerun."""
    default_branch = repo.get("default_branch") or "main"
    pushed_at = repo.get("pushed_at") or repo.get("updated_at") or "unknown"
    return f"{default_branch}:{pushed_at}"


def should_process_repo_baseline(repo: dict, stored_metadata: Optional[dict]) -> Tuple[bool, str]:
    """
    Decide whether README/commit/code extraction should run for a repo.

    Baselines should rerun whenever the repo's pushed state changes. This keeps
    PKS aligned with repo updates instead of treating README/commit/code
    extraction as a one-time bootstrap.
    """
    if not stored_metadata:
        return True, "new repo"

    current_signature = build_repo_baseline_signature(repo)
    stored_signature = stored_metadata.get("baseline_signature")
    if not stored_signature:
        legacy_branch = stored_metadata.get("default_branch") or repo.get("default_branch") or "main"
        legacy_pushed_at = (
            stored_metadata.get("pushed_at")
            or stored_metadata.get("last_pushed_at")
            or stored_metadata.get("updated_at")
        )
        if legacy_pushed_at:
            stored_signature = f"{legacy_branch}:{legacy_pushed_at}"

    if stored_signature != current_signature:
        return True, "repo pushed since last baseline"

    return False, "unchanged since last push"


def run_github_ingestion(
    repos: list[str] = None,
    skip_code: bool = False,
    skip_commits: bool = False,
    dry_run: bool = False,
    resume: bool = True,
):
    """
    Run GitHub knowledge ingestion.
    
    Args:
        repos: Optional list of specific repo names to process
        skip_code: Skip code comment extraction (faster)
        skip_commits: Skip commit message extraction
        dry_run: Extract but don't save to storage
        resume: Resume from checkpoint if available
    """
    print("=" * 60)
    print("GITHUB KNOWLEDGE INGESTION")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")
    print()
    
    # Validate configuration
    errors = validate_github_config()
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  ✗ {error}")
        raise SystemExit(1)
    
    # Initialize clients
    github = GitHubClient()
    extractor = Extractor()
    storage = StorageClient() if not dry_run else None
    
    # Check GitHub rate limit
    rate = github.get_rate_limit()
    print(f"GitHub API: {rate['remaining']}/{rate['limit']} requests remaining")
    
    if not dry_run:
        ok, msg = storage.test_connection()
        print(f"Storage: {msg}")
        if not ok:
            print("  ✗ Cannot connect to storage, aborting")
            raise SystemExit(1)
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 1: Get list of repositories
    # -------------------------------------------------------------------------
    print("[1/4] FETCHING REPOSITORIES")
    print("-" * 40)
    
    if repos:
        # Use specified repos
        all_repos = []
        for repo_name in repos:
            normalized_name = repo_name.strip()
            repo_info = github.get_repo_info(normalized_name) or {}
            all_repos.append({
                "name": normalized_name,
                "full_name": repo_info.get("full_name", f"ArjunDivecha/{normalized_name}"),
                "description": repo_info.get("description"),
                "language": repo_info.get("language"),
                "stars": repo_info.get("stars", 0),
                "url": repo_info.get("url", f"https://github.com/ArjunDivecha/{normalized_name}"),
                "size": repo_info.get("size", 0),
                "archived": repo_info.get("archived", False),
                "default_branch": repo_info.get("default_branch", "main"),
                "updated_at": repo_info.get("updated_at"),
                "pushed_at": repo_info.get("pushed_at"),
            })
        print(f"Using {len(all_repos)} specified repositories")
    else:
        # Fetch all repos
        all_repos = github.list_repos(include_forks=False)
        print(f"Found {len(all_repos)} repositories (excluding forks)")
    
    # Baseline extraction now reruns whenever a repo has been pushed since the
    # last saved baseline. Agent-context artifacts are still rescanned for
    # every repo and deduplicated by artifact blob SHA instead.
    repo_baseline_decisions: dict[str, tuple[bool, str]] = {}
    processed: set[str] = set()
    if resume and storage:
        processed = set(storage.get_processed_sources("github"))
        repos_to_process = all_repos
        baselines_to_refresh = 0
        for repo in all_repos:
            stored_metadata = storage.get_source_metadata("github", repo["name"])
            decision = should_process_repo_baseline(repo, stored_metadata)
            repo_baseline_decisions[repo["name"]] = decision
            if decision[0]:
                baselines_to_refresh += 1
        print(f"Repo baselines unchanged: {len(all_repos) - baselines_to_refresh}")
        print(f"Repo baselines to refresh: {baselines_to_refresh}")
        print(f"Repos to scan for agent-context artifacts: {len(repos_to_process)}")
    else:
        repos_to_process = all_repos
        for repo in all_repos:
            repo_baseline_decisions[repo["name"]] = (True, "resume disabled")
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 2: Extract knowledge from each repository
    # -------------------------------------------------------------------------
    print("[2/4] EXTRACTING KNOWLEDGE")
    print("-" * 40)
    
    all_entries = []
    stats = {
        "repos_processed": 0,
        "readme_entries": 0,
        "commit_entries": 0,
        "code_entries": 0,
        "markdown_entries": 0,
        "markdown_files": 0,
        "agent_context_entries": 0,
        "agent_context_files": 0,
        "errors": 0,
    }
    
    for i, repo in enumerate(repos_to_process, 1):
        repo_name = repo["name"]
        repo_full_name = repo.get("full_name", f"ArjunDivecha/{repo_name}")
        repo_url = repo.get("url", f"https://github.com/{repo_full_name}")
        should_process_baseline, baseline_reason = repo_baseline_decisions.get(
            repo_name,
            (True, "missing decision"),
        )
        print(f"\n[{i}/{len(repos_to_process)}] {repo_name}")
        
        repo_entries = []
        
        try:
            readme = None
            baseline_entry_count = 0
            baseline_attempted = False
            if resume and not should_process_baseline:
                print(f"  → Repo baseline unchanged ({baseline_reason}), skipping README/commits/code")
            else:
                baseline_attempted = True
                if resume and repo_name in processed:
                    print(f"  → Repo baseline refresh triggered ({baseline_reason})")

                # Extract from README
                print("  → README...", end=" ", flush=True)
                readme = github.get_readme(repo_name)
                if readme:
                    entries = extractor.extract_from_readme(
                        readme_content=readme,
                        repo_name=repo_name,
                        repo_url=repo_url,
                        repo_full_name=repo_full_name,
                    )
                    check_extractor_error(extractor, "README")
                    repo_entries.extend(entries)
                    baseline_entry_count += len(entries)
                    stats["readme_entries"] += len(entries)
                    print(f"{len(entries)} entries")
                else:
                    print("not found")

                # Extract from commits
                if not skip_commits:
                    print("  → Commits...", end=" ", flush=True)
                    commits = github.get_commits(repo_name, max_commits=50)
                    if commits:
                        entries = extractor.extract_from_commits(
                            commits,
                            repo_name,
                            repo_url=repo_url,
                            repo_full_name=repo_full_name,
                        )
                        check_extractor_error(extractor, "commit")
                        repo_entries.extend(entries)
                        baseline_entry_count += len(entries)
                        stats["commit_entries"] += len(entries)
                        print(f"{len(entries)} entries from {len(commits)} commits")
                    else:
                        print("none found")

                # Extract from code comments
                if not skip_code:
                    print("  → Code comments...", end=" ", flush=True)
                    code_files = github.get_code_files(repo_name, max_files=20)
                    if code_files:
                        entries = extractor.extract_from_code_comments(
                            code_files,
                            repo_name,
                            repo_url=repo_url,
                            repo_full_name=repo_full_name,
                        )
                        check_extractor_error(extractor, "code comment")
                        repo_entries.extend(entries)
                        baseline_entry_count += len(entries)
                        stats["code_entries"] += len(entries)
                        print(f"{len(entries)} entries from {len(code_files)} files")
                    else:
                        print("no code files")

                # Extract from markdown documentation files (non-README)
                print("  → Markdown docs...", end=" ", flush=True)
                get_markdown_files = getattr(github, "get_markdown_files", None)
                md_files = get_markdown_files(repo_name) if callable(get_markdown_files) else []
                if md_files:
                    entries = extractor.extract_from_markdown_files(
                        md_files,
                        repo_name,
                        repo_url=repo_url,
                        repo_full_name=repo_full_name,
                    )
                    check_extractor_error(extractor, "markdown")
                    repo_entries.extend(entries)
                    baseline_entry_count += len(entries)
                    stats["markdown_entries"] += len(entries)
                    stats["markdown_files"] += len(md_files)
                    print(f"{len(entries)} entries from {len(md_files)} files")
                else:
                    print("none found")

            print("  → Agent context...", end=" ", flush=True)
            agent_context_files = github.get_agent_context_files(repo_name)
            if agent_context_files:
                processed_artifacts = 0
                skipped_artifacts = 0
                agent_entries = 0
                for artifact in agent_context_files:
                    artifact_source_id = (
                        f"{repo_full_name}:{artifact['path']}:{artifact.get('sha') or 'no-sha'}"
                    )
                    if storage and resume and storage.is_source_processed("github_agent_context", artifact_source_id):
                        skipped_artifacts += 1
                        continue

                    entries = extractor.extract_from_agent_context_artifact(
                        artifact_content=artifact["content"],
                        repo_name=repo_name,
                        repo_full_name=repo_full_name,
                        repo_url=repo_url,
                        artifact_path=artifact["path"],
                        artifact_sha=artifact.get("sha"),
                    )
                    check_extractor_error(extractor, "agent context")
                    repo_entries.extend(entries)
                    agent_entries += len(entries)
                    processed_artifacts += 1

                    if storage:
                        storage.mark_source_processed("github_agent_context", artifact_source_id, {
                            "repo": repo_full_name,
                            "path": artifact["path"],
                            "sha": artifact.get("sha"),
                            "entries_count": len(entries),
                        })

                stats["agent_context_files"] += processed_artifacts
                stats["agent_context_entries"] += agent_entries
                print(
                    f"{agent_entries} entries from {processed_artifacts} files"
                    + (f" ({skipped_artifacts} unchanged)" if skipped_artifacts else "")
                )
            else:
                print("none found")
            
            all_entries.extend(repo_entries)
            stats["repos_processed"] += 1
            
            # Mark as processed
            if storage and baseline_attempted:
                storage.mark_source_processed("github", repo_name, {
                    "entries_count": baseline_entry_count,
                    "has_readme": readme is not None,
                    "repo_full_name": repo_full_name,
                    "default_branch": repo.get("default_branch", "main"),
                    "pushed_at": repo.get("pushed_at"),
                    "updated_at": repo.get("updated_at"),
                    "baseline_signature": build_repo_baseline_signature(repo),
                })
            
            # Checkpoint every 5 repos
            if i % 5 == 0:
                save_checkpoint("entries", all_entries)
                save_checkpoint("stats", stats)
            
        except Exception as e:
            print(f"  ✗ Error: {e}")
            stats["errors"] += 1
    
    print()

    if stats["errors"]:
        save_checkpoint("entries", all_entries)
        save_checkpoint("stats", stats)
        raise RuntimeError(
            f"GitHub ingestion failed for {stats['errors']} repos; "
            "refusing to save partial extraction results."
        )
    
    # -------------------------------------------------------------------------
    # STEP 3: Save to storage
    # -------------------------------------------------------------------------
    print("[3/4] SAVING TO STORAGE")
    print("-" * 40)
    
    if dry_run:
        print("DRY RUN - Not saving to storage")
        print(f"Would save {len(all_entries)} entries")
        
        # Save to file for inspection
        output_path = CHECKPOINT_DIR / "github_dry_run.json"
        with open(output_path, "w") as f:
            json.dump(all_entries, f, indent=2)
        print(f"Saved to {output_path}")
    
    elif all_entries:
        print(f"Saving {len(all_entries)} entries...")
        
        # Batch save
        batch_size = 20
        for i in range(0, len(all_entries), batch_size):
            batch = all_entries[i:i + batch_size]
            storage.save_knowledge_entries_batch(batch)
            print(f"  Saved {min(i + batch_size, len(all_entries))}/{len(all_entries)}")
        
        # Update thin index
        print("Updating thin index...")
        storage.update_thin_index(all_entries)
        print("  ✓ Thin index updated")
    
    else:
        print("No entries to save")
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 4: Summary
    # -------------------------------------------------------------------------
    print("[4/4] SUMMARY")
    print("-" * 40)
    print(f"Repositories processed: {stats['repos_processed']}")
    print(f"Entries from READMEs:   {stats['readme_entries']}")
    print(f"Entries from commits:   {stats['commit_entries']}")
    print(f"Entries from code:      {stats['code_entries']}")
    print(f"Markdown docs files:    {stats['markdown_files']}")
    print(f"Markdown docs entries:  {stats['markdown_entries']}")
    print(f"Agent context files:    {stats['agent_context_files']}")
    print(f"Agent context entries:  {stats['agent_context_entries']}")
    print(f"Total entries:          {len(all_entries)}")
    print(f"Errors:                 {stats['errors']}")
    
    if storage:
        storage_stats = storage.get_stats()
        print()
        print("Storage totals:")
        print(f"  Knowledge entries: {storage_stats['knowledge_entries']}")
        print(f"  Project entries:   {storage_stats['project_entries']}")
        print(f"  Vectors:           {storage_stats['total_vectors']}")
    
    print()
    print(f"Completed: {datetime.now().isoformat()}")
    print("=" * 60)
    
    return all_entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest knowledge from GitHub repositories"
    )
    parser.add_argument(
        "--repos",
        type=str,
        help="Comma-separated list of specific repos to process"
    )
    parser.add_argument(
        "--skip-code",
        action="store_true",
        help="Skip code comment extraction (faster)"
    )
    parser.add_argument(
        "--skip-commits",
        action="store_true",
        help="Skip commit message extraction"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract but don't save to storage"
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Don't resume from checkpoint, process all repos"
    )
    
    args = parser.parse_args()
    
    repos = args.repos.split(",") if args.repos else None
    
    run_github_ingestion(
        repos=repos,
        skip_code=args.skip_code,
        skip_commits=args.skip_commits,
        dry_run=args.dry_run,
        resume=not args.no_resume,
    )
