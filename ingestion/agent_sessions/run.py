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
LAST UPDATED: 2026-03-26
AUTHOR: Arjun Divecha

DESCRIPTION:
Scans Claude Code and Codex CLI session files for new conversation turns,
distills durable knowledge using Claude API, links to GitHub repos when
the session was working in a git repository, and saves entries to the
knowledge system via StorageClient.

Designed for manual or scheduled local ingestion. A daily local launcher runs
this pipeline one hour before the remote Dream job, and byte-offset tracking
ensures each run only processes new data since the last run.

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
- Uses claude-sonnet-4-6 for distillation (cost-effective)
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
from typing import Optional

# Bootstrap: add ingestion/ to path and load .env
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from upstash_redis import Redis
from core.storage import StorageClient
from github.client import GitHubClient
from agent_sessions.parsers import parse_claude_code, parse_codex
from agent_sessions.github_linker import GitHubLinker
from core.config import UPSTASH_REDIS_REST_TOKEN, UPSTASH_REDIS_REST_URL
from core.sdk_client import sdk_query

# ── Configuration ─────────────────────────────────────────────────────────────

CLAUDE_CODE_DIR = Path.home() / ".claude" / "projects"
CODEX_DIR = Path.home() / ".codex" / "sessions"
STATE_FILE = Path(__file__).parent.parent / "checkpoints" / "agent_sessions_state.json"
LOG_FILE = Path(__file__).parent.parent / "logs" / "agent_sessions.log"
STATE_REDIS_KEY = "ingestion:agent_sessions:state"
_state_redis_client: Optional[Redis] = None
_redis_write_failed = False
DEFAULT_DISTILL_FAILURE_RETRY_LIMIT = 2

# Filtering thresholds
MIN_USER_CHARS = 300    # Skip trivial sessions (just cd/ls)
MIN_TURNS = 4           # Skip sessions with too few back-and-forth turns

# Distillation model
DISTILL_MODEL = "claude-sonnet-4-6"

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── State Management ─────────────────────────────────────────────────────────


class DistillationFailure(RuntimeError):
    """Raised when a session should be retried instead of checkpointed forward."""


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _distill_failure_retry_limit() -> int:
    return _int_env(
        "PKS_AGENT_SESSION_DISTILL_RETRY_LIMIT",
        DEFAULT_DISTILL_FAILURE_RETRY_LIMIT,
    )


def _default_state() -> dict:
    """Default processing state for byte-offset tracking."""
    return {"files": {}, "last_run": None, "stats": {"total_saved": 0, "total_skipped": 0}}


def _normalize_state(state: Optional[dict] = None) -> dict:
    """Normalize checkpoint payloads loaded from disk or Redis."""
    merged = _default_state()
    if not isinstance(state, dict):
        return merged

    files = state.get("files")
    if isinstance(files, dict):
        merged["files"] = files

    merged["last_run"] = state.get("last_run")

    stats = state.get("stats")
    if isinstance(stats, dict):
        merged["stats"]["total_saved"] = int(stats.get("total_saved", 0) or 0)
        merged["stats"]["total_skipped"] = int(stats.get("total_skipped", 0) or 0)

    return merged


def _state_progress_score(state: dict) -> tuple[int, int, float]:
    """Return a comparable checkpoint progress score."""
    normalized = _normalize_state(state)
    files = normalized.get("files", {})
    offset_total = 0
    max_mtime = 0.0

    for file_state in files.values():
        if not isinstance(file_state, dict):
            continue
        offset_total += int(file_state.get("offset", 0) or 0)
        max_mtime = max(max_mtime, float(file_state.get("mtime", 0) or 0))

    return (len(files), offset_total, max_mtime)


def _parse_json_array_response(raw_text: str) -> list[dict]:
    """Parse a model response that should contain a JSON array."""
    candidate = raw_text.strip()

    # Strip accidental markdown fences.
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1]
        if candidate.startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start == -1 or end == -1 or end < start:
            raise

        parsed = json.loads(candidate[start:end + 1])
        return parsed if isinstance(parsed, list) else []


def _get_state_redis_client() -> Optional[Redis]:
    """Create a Redis client for mirrored checkpoint state when configured."""
    global _state_redis_client
    if _state_redis_client is not None:
        return _state_redis_client

    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None

    _state_redis_client = Redis(
        url=UPSTASH_REDIS_REST_URL,
        token=UPSTASH_REDIS_REST_TOKEN,
    )
    return _state_redis_client


def load_state() -> tuple[dict, str]:
    """Load processing state from Redis when available, otherwise local disk."""
    STATE_FILE.parent.mkdir(exist_ok=True)
    file_state = None
    if STATE_FILE.exists():
        file_state = _normalize_state(json.loads(STATE_FILE.read_text()))

    redis_client = _get_state_redis_client()
    if redis_client is not None:
        try:
            raw_state = redis_client.get(STATE_REDIS_KEY)
            if raw_state:
                redis_state = None
                if isinstance(raw_state, str):
                    redis_state = _normalize_state(json.loads(raw_state))
                elif isinstance(raw_state, dict):
                    redis_state = _normalize_state(raw_state)
                if redis_state is None:
                    raise ValueError(f"Unexpected Redis state payload: {type(raw_state).__name__}")
                if file_state and _state_progress_score(file_state) > _state_progress_score(redis_state):
                    log.warning(
                        "Local agent session checkpoint is ahead of Redis; "
                        "using local checkpoint and re-syncing Redis."
                    )
                    save_state(file_state)
                    return file_state, "file_ahead_of_redis"
                return redis_state, "redis"
        except Exception as exc:
            log.warning(f"Could not load agent session state from Redis: {exc}")

    if file_state:
        return file_state, "file"
    return _default_state(), "default"


def save_state(state: dict):
    """Persist processing state atomically to disk and best-effort to Redis."""
    global _redis_write_failed
    normalized = _normalize_state(state)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(normalized, indent=2))
    tmp.rename(STATE_FILE)

    redis_client = _get_state_redis_client()
    if redis_client is not None:
        try:
            redis_client.set(STATE_REDIS_KEY, json.dumps(normalized))
        except Exception as exc:
            _redis_write_failed = True
            log.warning(f"Could not save agent session state to Redis: {exc}")


def write_run_status(
    *,
    state_source: str,
    total_saved: int,
    total_files_processed: int,
    elapsed_seconds: float,
) -> None:
    """Write optional machine-readable run status for the local overnight wrapper."""
    status_path_raw = os.getenv("PKS_AGENT_SESSION_STATUS_FILE")
    if not status_path_raw:
        return

    status_path = Path(status_path_raw).expanduser()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "state_source": state_source,
        "redis_write_failed": _redis_write_failed,
        "total_saved": total_saved,
        "files_yielded_entries": total_files_processed,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    tmp = status_path.with_suffix(status_path.suffix + ".tmp")
    tmp.write_text(json.dumps(status, indent=2))
    tmp.rename(status_path)


def record_distillation_failure(
    state: dict,
    state_key: str,
    file_state: dict,
    *,
    offset: int,
    new_offset: int,
    current_mtime: float,
    exc: DistillationFailure,
) -> bool:
    """Record a failed distillation attempt; return True if checkpoint advanced."""
    attempts = int(file_state.get("distill_failures", 0) or 0) + 1
    retry_limit = _distill_failure_retry_limit()
    now = datetime.now(timezone.utc).isoformat()
    failure_metadata = {
        "distill_failures": attempts,
        "last_distill_error": str(exc)[:500],
        "last_distill_failed_at": now,
        "last_failed_offset": offset,
        "last_failed_target_offset": new_offset,
    }

    if attempts >= retry_limit:
        state["files"][state_key] = {
            "offset": new_offset,
            "mtime": current_mtime,
            "distill_failure_exhausted": True,
            **failure_metadata,
        }
        save_state(state)
        return True

    state["files"][state_key] = {
        **file_state,
        "offset": offset,
        "mtime": float(file_state.get("mtime", 0) or 0),
        **failure_metadata,
    }
    save_state(state)
    return False


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
    github_info: Optional[dict] = None,
) -> list[dict]:
    """
    Call Claude to extract knowledge entries from a session's turns.

    Args:
        turns: Parsed conversation turns
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
        raw = sdk_query(
            DISTILL_PROMPT.format(
                source=source,
                project=project,
                github_context=github_context,
                conversation=conv,
            ),
            max_tokens=1500,
        )
    except Exception as e:
        raise DistillationFailure(f"SDK request failed: {e}") from e

    try:
        return _parse_json_array_response(raw)
    except Exception as e:
        raise DistillationFailure(f"JSON parse failed: {e}") from e


# ── Storage ───────────────────────────────────────────────────────────────────

def save_entries(
    entries: list[dict],
    turns: list[dict],
    storage: StorageClient,
    github_info: Optional[dict] = None,
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
        if not dry_run:
            # Update state only for real runs; dry-runs must not advance checkpoints.
            state["files"][state_key] = {"offset": new_offset, "mtime": current_mtime}
            save_state(state)
        return 0

    log.info(f"[{source_type}] {len(turns)} new turns from {path.name}")

    # Resolve GitHub repo from session cwd
    cwd = session_meta.get("cwd") or (turns[0].get("cwd") if turns else "")
    github_info = linker.get_repo_info(cwd) if cwd else None
    if github_info:
        log.info(f"  Linked to GitHub: {github_info['url']}")

    # Distill knowledge. If distillation itself fails, leave the checkpoint
    # unchanged so the next run can retry the same session window.
    try:
        entries = distill(turns, github_info)
    except DistillationFailure as exc:
        advanced = False
        if not dry_run:
            advanced = record_distillation_failure(
                state,
                state_key,
                file_state,
                offset=offset,
                new_offset=new_offset,
                current_mtime=current_mtime,
                exc=exc,
            )
        retry_limit = _distill_failure_retry_limit()
        action = (
            f"advanced checkpoint after {retry_limit} failed attempt(s); window skipped"
            if advanced
            else "leaving checkpoint window eligible for bounded retry"
        )
        log.warning("  Distillation failed; %s: %s", action, exc)
        return 0

    saved = 0
    if entries:
        saved = save_entries(entries, turns, storage, github_info, dry_run)
        log.info(f"  -> {saved}/{len(entries)} entries saved")

    if not dry_run:
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
    parser.add_argument(
        "--require-redis-state",
        action="store_true",
        help="Abort unless the effective checkpoint was loaded from Redis.",
    )
    parser.add_argument(
        "--sync-state-only",
        action="store_true",
        help="Mirror the current checkpoint state to Redis/file and exit.",
    )
    args = parser.parse_args()

    # Load or reset state
    if args.backfill:
        state = _default_state()
        state_source = "reset"
        log.info("Backfill mode: processing all history")
    else:
        state, state_source = load_state()

    log.info(f"Agent session checkpoint source: {state_source}")

    if args.sync_state_only:
        save_state(state)
        tracked_files = len(state.get("files", {}))
        log.info(
            "Synced agent session checkpoint to configured backends "
            f"(source={state_source}, tracked_files={tracked_files})"
        )
        return

    if args.require_redis_state and not args.backfill and state_source != "redis":
        log.error("Remote agent-session runs require Redis-backed checkpoint state.")
        log.error("Refusing to fall back to local/default state on this runner.")
        sys.exit(1)

    # Initialize storage (inference handled via sdk_client / Max subscription)
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

    if args.limit is not None:
        files_to_process = files_to_process[:args.limit]
        log.info(f"Limited to {args.limit} files")

    # Process
    total_saved = 0
    total_files_processed = 0
    start_time = time.time()

    for i, (path, source_type) in enumerate(files_to_process):
        try:
            saved = process_file(
                path, source_type, state, storage, linker, args.dry_run
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

    if not args.dry_run:
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["stats"]["total_saved"] = state["stats"].get("total_saved", 0) + total_saved
        save_state(state)

    elapsed = time.time() - start_time
    write_run_status(
        state_source=state_source,
        total_saved=total_saved,
        total_files_processed=total_files_processed,
        elapsed_seconds=elapsed,
    )
    log.info(
        f"Done: {total_files_processed} files yielded {total_saved} entries "
        f"in {elapsed:.0f}s"
    )


if __name__ == "__main__":
    main()
