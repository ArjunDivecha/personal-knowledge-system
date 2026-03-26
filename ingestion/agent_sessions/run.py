#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: run.py
=============================================================================

INPUT FILES:
- ~/.claude/projects/**/*.jsonl: Claude Code session files
- ~/.codex/sessions/**/*.jsonl: Codex CLI rollout files
- ingestion/checkpoints/agent_sessions_state.json: Processing state (byte offsets)
- ingestion/.env: Environment variables (Upstash, Anthropic, OpenAI, GitHub)

OUTPUT FILES:
- Knowledge entries written to Upstash Redis + Vector (via StorageClient)
- ingestion/checkpoints/agent_sessions_state.json: Updated processing state
- ingestion/logs/agent_sessions.log: Processing log

VERSION: 1.0
LAST UPDATED: 2026-03-25
AUTHOR: Arjun Divecha

DESCRIPTION:
Scans Claude Code and Codex CLI session files for new conversation turns,
distills durable knowledge using Claude API, links to GitHub repos when
the session was working in a git repository, and saves entries to the
knowledge system via StorageClient.

Designed to run daily (or every few hours) via launchd. Uses byte-offset
tracking so each run only processes new data since the last run.

DEPENDENCIES:
- anthropic
- upstash_redis, upstash_vector, openai (via StorageClient)
- python-dotenv

USAGE:
    # Process all new sessions since last run
    python agent_sessions/run.py

    # Full backfill of all history (first-time setup)
    python agent_sessions/run.py --backfill

    # Dry run: parse and distill but don't save to storage
    python agent_sessions/run.py --dry-run

    # Process only Claude Code (skip Codex)
    python agent_sessions/run.py --source claude_code

    # Process only Codex (skip Claude Code)
    python agent_sessions/run.py --source codex_cli

NOTES:
- Uses claude-sonnet-4-5-20250929 for distillation (cost-effective)
- Deterministic entry IDs prevent duplicates on re-run
- Rate-limits API calls with 0.5s sleep between sessions
- GitHub README fetching is cached per repo (no repeated API calls)
=============================================================================
"""

import sys
import os
import json
import time
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Bootstrap: add ingestion/ to path and load .env
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import anthropic
from core.storage import StorageClient
from github.client import GitHubClient
from agent_sessions.parsers import parse_claude_code, parse_codex
from agent_sessions.github_linker import GitHubLinker

# ── Configuration ─────────────────────────────────────────────────────────────

CLAUDE_CODE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
STATE_FILE = Path(__file__).parent.parent / "checkpoints" / "agent_sessions_state.json"
LOG_FILE = Path(__file__).parent.parent / "logs" / "agent_sessions.log"

# Filtering thresholds
MIN_USER_CHARS = 300    # Skip trivial sessions (just cd/ls)
MIN_TURNS = 4           # Skip sessions with too few back-and-forth turns

# Distillation model
DISTILL_MODEL = "claude-sonnet-4-5-20250929"

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── State Management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load processing state (byte offsets per file)."""
    STATE_FILE.parent.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"files": {}, "last_run": None, "stats": {"total_saved": 0, "total_skipped": 0}}


def save_state(state: dict):
    """Persist processing state atomically."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


# ── Distillation ──────────────────────────────────────────────────────────────

DISTILL_PROMPT = """You are extracting durable personal knowledge from an AI coding agent session ({source}: Claude Code or Codex CLI).

The user was working in: {project}
{github_context}

Focus on:
- Technical decisions and WHY they were made
- Problems solved and the approach taken
- Project architecture or design insights
- Preferences expressed (libraries, patterns, approaches)
- Lessons learned or errors diagnosed
- New capabilities built or discovered

Skip: mechanical code generation without insight, simple file reads, trivial commands, tool call boilerplate.

Return ONLY a JSON array (no preamble, no markdown fences):
[
  {{
    "domain": "concise domain label (e.g. 'MLX LoRA layer selection', 'Python async error handling')",
    "current_view": "1-3 sentence distillation of the insight, decision, or lesson learned",
    "confidence": "high|medium|low",
    "project_context": "project name or directory if discernible"
  }}
]

If no durable knowledge is present, return: []

Session conversation:
{conversation}"""


def distill(
    turns: list[dict],
    client: anthropic.Anthropic,
    github_info: dict | None = None,
) -> list[dict]:
    """
    Call Claude to extract knowledge entries from a session's turns.

    Args:
        turns: Parsed conversation turns
        client: Anthropic API client
        github_info: Optional GitHub repo info from GitHubLinker

    Returns:
        List of extracted knowledge entry dicts
    """
    if len(turns) < MIN_TURNS:
        return []

    user_chars = sum(len(t["content"]) for t in turns if t["role"] == "user")
    if user_chars < MIN_USER_CHARS:
        return []

    # Build conversation text, cap at ~6k chars
    conv = ""
    for t in turns[:50]:
        label = "User" if t["role"] == "user" else "Agent"
        conv += f"\n{label}: {t['content']}\n"
        if len(conv) > 6000:
            break

    source = turns[0].get("source", "agent")
    project = turns[0].get("project", "unknown")

    # Build GitHub context string
    github_context = ""
    if github_info:
        github_context = f"GitHub repo: {github_info['url']}"
        if github_info.get("readme_summary"):
            # Include first 500 chars of README for context
            readme_excerpt = github_info["readme_summary"][:500]
            github_context += f"\nREADME excerpt: {readme_excerpt}"

    try:
        resp = client.messages.create(
            model=DISTILL_MODEL,
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": DISTILL_PROMPT.format(
                    source=source,
                    project=project,
                    github_context=github_context,
                    conversation=conv,
                ),
            }],
        )
        raw = resp.content[0].text.strip()

        # Strip accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        entries = json.loads(raw.strip())

        if not isinstance(entries, list):
            return []
        return entries

    except Exception as e:
        log.warning(f"Distillation error: {e}")
        return []


# ── Storage ───────────────────────────────────────────────────────────────────

def save_entries(
    entries: list[dict],
    turns: list[dict],
    storage: StorageClient,
    github_info: dict | None = None,
    dry_run: bool = False,
) -> int:
    """
    Convert distilled insights to knowledge entries and save via StorageClient.

    Args:
        entries: Distilled knowledge dicts from Claude
        turns: Original conversation turns (for metadata)
        storage: StorageClient instance
        github_info: Optional GitHub repo info
        dry_run: If True, log but don't save

    Returns:
        Number of entries saved
    """
    saved = 0
    source = turns[0].get("source", "agent") if turns else "agent"
    session_id = turns[0].get("session_id", "unknown") if turns else "unknown"
    project = turns[0].get("project", "") if turns else ""

    for e in entries:
        if not e.get("domain") or not e.get("current_view"):
            continue

        # Stable ID: hash of source + session_id + domain (prevents duplicates)
        hash_input = f"{source}:{session_id}:{e['domain']}"
        entry_id = "ke_" + hashlib.md5(hash_input.encode()).hexdigest()[:12]

        if dry_run:
            log.info(f"  [DRY RUN] Would save: [{entry_id}] {e['domain']}")
            log.info(f"            View: {e['current_view'][:120]}...")
            saved += 1
            continue

        # Check if already exists
        existing = storage.get_knowledge_entry(entry_id)
        if existing:
            log.debug(f"  Skip (exists): {e['domain']}")
            continue

        # Build metadata
        metadata = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sources": [f"{source}:{session_id}"],
            "project": project or e.get("project_context", ""),
            "source_type": source,
        }

        # Attach GitHub info if available
        if github_info:
            metadata["github_repo"] = github_info["full_name"]
            metadata["github_url"] = github_info["url"]
            if github_info.get("readme_summary"):
                metadata["readme_summary"] = github_info["readme_summary"][:500]

        entry = {
            "id": entry_id,
            "domain": e["domain"],
            "current_view": e["current_view"],
            "state": "active",
            "confidence": e.get("confidence", "medium"),
            "detail_level": "full",
            "metadata": metadata,
        }

        try:
            storage.save_knowledge_entry(entry)
            saved += 1
            log.info(f"  Saved: [{entry_id}] {e['domain']}")
        except Exception as ex:
            log.warning(f"  Storage error for {e['domain']}: {ex}")

    # Update thin index with newly saved entries (skip in dry run)
    if saved > 0 and not dry_run:
        try:
            new_entry_ids = []
            for e in entries:
                if e.get("domain"):
                    hash_input = f"{source}:{session_id}:{e['domain']}"
                    eid = "ke_" + hashlib.md5(hash_input.encode()).hexdigest()[:12]
                    new_entry_ids.append(eid)

            fetched = [storage.get_knowledge_entry(eid) for eid in new_entry_ids]
            fetched = [x for x in fetched if x]
            if fetched:
                storage.update_thin_index(fetched)
        except Exception as ex:
            log.warning(f"Thin index update error: {ex}")

    return saved


# ── File Discovery ────────────────────────────────────────────────────────────

def discover_claude_code_files() -> list[Path]:
    """
    Find all Claude Code session JSONL files.

    Filters to UUID-shaped filenames (session files) and excludes
    index files and non-JSONL files.
    """
    if not CLAUDE_CODE_DIR.exists():
        return []

    files = sorted(
        CLAUDE_CODE_DIR.glob("**/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    # Filter to UUID-shaped session files (e.g., a1b2c3d4-e5f6-...)
    return [f for f in files if len(f.stem) > 20 and "-" in f.stem]


def discover_codex_files() -> list[Path]:
    """Find all Codex CLI rollout JSONL files."""
    if not CODEX_DIR.exists():
        return []

    return sorted(
        CODEX_DIR.glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )


# ── Processing ────────────────────────────────────────────────────────────────

def process_file(
    path: Path,
    source_type: str,
    state: dict,
    anthropic_client: anthropic.Anthropic,
    storage: StorageClient,
    linker: GitHubLinker,
    dry_run: bool = False,
) -> int:
    """
    Process a single session file: parse turns, distill, save.

    Args:
        path: Path to the JSONL file
        source_type: "claude_code" or "codex_cli"
        state: Processing state dict (modified in place)
        anthropic_client: Anthropic API client
        storage: StorageClient instance
        linker: GitHubLinker for repo detection
        dry_run: If True, don't save to storage

    Returns:
        Number of entries saved
    """
    if not path.exists():
        return 0

    state_key = str(path)
    file_state = state["files"].get(state_key, {"offset": 0, "mtime": 0})
    current_mtime = path.stat().st_mtime

    # Skip if file hasn't been modified since last processing
    if current_mtime <= file_state.get("mtime", 0):
        return 0

    offset = file_state.get("offset", 0)

    # Parse turns
    if source_type == "claude_code":
        turns, new_offset, session_meta = parse_claude_code(path, offset)
    else:
        turns, new_offset, session_meta = parse_codex(path, offset)

    if not turns:
        # Update state even if no turns (file was read)
        state["files"][state_key] = {"offset": new_offset, "mtime": current_mtime}
        save_state(state)
        return 0

    log.info(f"[{source_type}] {len(turns)} new turns from {path.name}")

    # Resolve GitHub repo from session cwd
    cwd = session_meta.get("cwd") or (turns[0].get("cwd") if turns else "")
    github_info = linker.get_repo_info(cwd) if cwd else None
    if github_info:
        log.info(f"  Linked to GitHub: {github_info['url']}")

    # Distill knowledge
    entries = distill(turns, anthropic_client, github_info)

    saved = 0
    if entries:
        saved = save_entries(entries, turns, storage, github_info, dry_run)
        log.info(f"  -> {saved}/{len(entries)} entries saved")

    # Update state
    state["files"][state_key] = {"offset": new_offset, "mtime": current_mtime}
    save_state(state)

    return saved


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingest Claude Code + Codex CLI sessions into the knowledge system"
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process all existing history (resets state file)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and distill but don't save to storage",
    )
    parser.add_argument(
        "--source",
        choices=["claude_code", "codex_cli"],
        default=None,
        help="Process only one source type (default: both)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of files to process (useful for testing)",
    )
    args = parser.parse_args()

    # Load or reset state
    if args.backfill:
        state = {"files": {}, "last_run": None, "stats": {"total_saved": 0, "total_skipped": 0}}
        log.info("Backfill mode: processing all history")
    else:
        state = load_state()

    # Initialize clients
    anthropic_client = anthropic.Anthropic()  # Uses ANTHROPIC_API_KEY from env
    storage = StorageClient()

    if not args.dry_run:
        ok, msg = storage.test_connection()
        if not ok:
            log.error(f"Storage connection failed: {msg}")
            sys.exit(1)
        log.info(f"Storage: {msg}")

    # GitHub linker (reuses existing GitHubClient)
    try:
        github_client = GitHubClient()
        linker = GitHubLinker(github_client)
        log.info("GitHub linker: enabled")
    except Exception as e:
        log.warning(f"GitHub linker disabled: {e}")
        linker = GitHubLinker(None)

    # Discover files
    files_to_process = []

    if args.source != "codex_cli":
        cc_files = discover_claude_code_files()
        files_to_process.extend([(f, "claude_code") for f in cc_files])
        log.info(f"Claude Code: {len(cc_files)} session files found")

    if args.source != "claude_code":
        codex_files = discover_codex_files()
        files_to_process.extend([(f, "codex_cli") for f in codex_files])
        log.info(f"Codex CLI: {len(codex_files)} rollout files found")

    if args.limit:
        files_to_process = files_to_process[:args.limit]
        log.info(f"Limited to {args.limit} files")

    # Process
    total_saved = 0
    total_files_processed = 0
    start_time = time.time()

    for i, (path, source_type) in enumerate(files_to_process):
        try:
            saved = process_file(
                path, source_type, state, anthropic_client, storage, linker, args.dry_run
            )
            total_saved += saved
            if saved > 0:
                total_files_processed += 1

            # Rate limiting between sessions
            if saved > 0:
                time.sleep(0.5)

        except Exception as e:
            log.error(f"Error processing {path.name}: {e}")
            continue

        # Progress update every 50 files
        if (i + 1) % 50 == 0:
            elapsed = time.time() - start_time
            log.info(
                f"Progress: {i + 1}/{len(files_to_process)} files, "
                f"{total_saved} entries saved, {elapsed:.0f}s elapsed"
            )

    # Update state metadata
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["stats"]["total_saved"] = state["stats"].get("total_saved", 0) + total_saved
    save_state(state)

    elapsed = time.time() - start_time
    log.info(
        f"Done: {total_files_processed} files yielded {total_saved} entries "
        f"in {elapsed:.0f}s"
    )


if __name__ == "__main__":
    main()
