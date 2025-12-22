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

# Setup path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import validate_github_config, CHECKPOINT_DIR
from core.storage import StorageClient
from core.extractor import Extractor
from github.client import GitHubClient


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
        return
    
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
            return
    
    print()
    
    # -------------------------------------------------------------------------
    # STEP 1: Get list of repositories
    # -------------------------------------------------------------------------
    print("[1/4] FETCHING REPOSITORIES")
    print("-" * 40)
    
    if repos:
        # Use specified repos
        all_repos = [{"name": r.strip(), "full_name": f"ArjunDivecha/{r.strip()}"} for r in repos]
        print(f"Using {len(all_repos)} specified repositories")
    else:
        # Fetch all repos
        all_repos = github.list_repos(include_forks=False)
        print(f"Found {len(all_repos)} repositories (excluding forks)")
    
    # Check for already processed repos
    if resume and storage:
        processed = set(storage.get_processed_sources("github"))
        repos_to_process = [r for r in all_repos if r["name"] not in processed]
        print(f"Already processed: {len(all_repos) - len(repos_to_process)}")
        print(f"To process: {len(repos_to_process)}")
    else:
        repos_to_process = all_repos
        processed = set()
    
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
        "errors": 0,
    }
    
    for i, repo in enumerate(repos_to_process, 1):
        repo_name = repo["name"]
        print(f"\n[{i}/{len(repos_to_process)}] {repo_name}")
        
        repo_entries = []
        
        try:
            # Extract from README
            print("  → README...", end=" ", flush=True)
            readme = github.get_readme(repo_name)
            if readme:
                entries = extractor.extract_from_readme(
                    readme_content=readme,
                    repo_name=repo_name,
                    repo_url=repo.get("url", f"https://github.com/ArjunDivecha/{repo_name}")
                )
                repo_entries.extend(entries)
                stats["readme_entries"] += len(entries)
                print(f"{len(entries)} entries")
            else:
                print("not found")
            
            # Extract from commits
            if not skip_commits:
                print("  → Commits...", end=" ", flush=True)
                commits = github.get_commits(repo_name, max_commits=50)
                if commits:
                    entries = extractor.extract_from_commits(commits, repo_name)
                    repo_entries.extend(entries)
                    stats["commit_entries"] += len(entries)
                    print(f"{len(entries)} entries from {len(commits)} commits")
                else:
                    print("none found")
            
            # Extract from code comments
            if not skip_code:
                print("  → Code comments...", end=" ", flush=True)
                code_files = github.get_code_files(repo_name, max_files=20)
                if code_files:
                    entries = extractor.extract_from_code_comments(code_files, repo_name)
                    repo_entries.extend(entries)
                    stats["code_entries"] += len(entries)
                    print(f"{len(entries)} entries from {len(code_files)} files")
                else:
                    print("no code files")
            
            all_entries.extend(repo_entries)
            stats["repos_processed"] += 1
            
            # Mark as processed
            if storage and repo_entries:
                storage.mark_source_processed("github", repo_name, {
                    "entries_count": len(repo_entries),
                    "has_readme": readme is not None,
                })
            
            # Checkpoint every 5 repos
            if i % 5 == 0:
                save_checkpoint("entries", all_entries)
                save_checkpoint("stats", stats)
            
        except Exception as e:
            print(f"  ✗ Error: {e}")
            stats["errors"] += 1
    
    print()
    
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

