#!/usr/bin/env python3
"""
Export repo-attached AI agent context artifacts under .pks/agent-context/.

Supported surfaces:
- Claude Code
- Codex CLI
- Cursor agent transcripts

Artifacts are committed into the repository so PKS can ingest repo-linked
agent context remotely through GitHub without depending on raw local logs.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
CURSOR_PROJECTS_DIR = Path.home() / ".cursor" / "projects"
DEFAULT_OUTPUT_SUBDIR = ".pks/agent-context"
SURFACE_PREFIXES = {
    "claude_code": "claude-code",
    "codex_cli": "codex-cli",
    "cursor": "cursor",
}
MAX_TRANSCRIPT_CHARS = 24000
TURN_CAP = 800


@dataclass
class SessionArtifact:
    surface: str
    source_path: Path
    session_id: str
    exported_at: str
    export_base_commit_sha: Optional[str]
    github_repo: Optional[str]
    repo_root: Path
    turns: List[Dict]


def parse_claude_code(path: Path, from_offset: int = 0) -> Tuple[List[Dict], int, Dict]:
    """Parse Claude Code user/assistant turns from a JSONL session file."""
    turns: List[Dict] = []
    new_offset = from_offset
    session_meta = {"cwd": None, "project": path.parent.name}

    try:
        with open(path, "rb") as handle:
            handle.seek(from_offset)
            for raw in handle:
                new_offset = handle.tell()
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("cwd") and not session_meta["cwd"]:
                        session_meta["cwd"] = event["cwd"]

                    event_type = event.get("type", "")
                    if event_type not in ("user", "assistant"):
                        continue

                    message = event.get("message", {})
                    content = message.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict) and block.get("type") == "text"
                        )

                    if not content or not content.strip():
                        continue

                    turns.append(
                        {
                            "role": event_type,
                            "content": content.strip()[:TURN_CAP],
                            "timestamp": event.get("timestamp", ""),
                            "session_id": event.get("sessionId", path.stem),
                            "source": "claude_code",
                            "project": session_meta["project"],
                            "cwd": event.get("cwd", ""),
                        }
                    )
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        return [], from_offset, session_meta

    return turns, new_offset, session_meta


def parse_codex(path: Path, from_offset: int = 0) -> Tuple[List[Dict], int, Dict]:
    """Parse Codex CLI user/assistant turns from a rollout JSONL file."""
    turns: List[Dict] = []
    new_offset = from_offset
    session_meta = {"cwd": None, "project": path.parent.name}

    try:
        with open(path, "rb") as handle:
            handle.seek(from_offset)
            for raw in handle:
                new_offset = handle.tell()
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                    payload = event.get("payload", event)
                    event_type = event.get("type", payload.get("type", ""))

                    if event_type == "session_meta":
                        session_meta["cwd"] = payload.get("cwd", "")
                        session_meta["project"] = (
                            Path(payload.get("cwd", "")).name
                            if payload.get("cwd")
                            else path.parent.name
                        )
                        continue

                    if event_type == "turn_context":
                        if payload.get("cwd") and not session_meta["cwd"]:
                            session_meta["cwd"] = payload["cwd"]
                        continue

                    if event_type != "response_item":
                        continue

                    role = payload.get("role", "")
                    if role in ("developer", "user"):
                        role = "user"
                    elif role != "assistant":
                        continue

                    content = payload.get("content", "")
                    if isinstance(content, list):
                        parts = []
                        for block in content:
                            if isinstance(block, dict):
                                parts.append(
                                    block.get("text", "")
                                    or block.get("output_text", "")
                                    or block.get("input_text", "")
                                )
                        content = "\n".join(parts)

                    if not content or not content.strip():
                        continue

                    turns.append(
                        {
                            "role": role,
                            "content": content.strip()[:TURN_CAP],
                            "timestamp": event.get("timestamp", ""),
                            "session_id": payload.get("id", path.stem),
                            "source": "codex_cli",
                            "project": session_meta["project"],
                            "cwd": session_meta.get("cwd", ""),
                        }
                    )
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception:
        return [], from_offset, session_meta

    return turns, new_offset, session_meta


def parse_cursor(path: Path, from_offset: int = 0) -> Tuple[List[Dict], int, Dict]:
    """Parse Cursor agent transcript user/assistant turns from a JSONL file."""
    turns: List[Dict] = []
    new_offset = from_offset
    session_meta = {"cwd": None, "project": path.parents[2].name if len(path.parents) >= 3 else path.parent.name}

    try:
        with open(path, "rb") as handle:
            handle.seek(from_offset)
            for raw in handle:
                new_offset = handle.tell()
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                role = event.get("role", "")
                if role not in {"user", "assistant"}:
                    continue

                message = event.get("message", {})
                content = message.get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                text_parts.append(text)
                    content = "\n".join(text_parts)

                if not content or not content.strip():
                    continue

                turns.append(
                    {
                        "role": role,
                        "content": content.strip()[:TURN_CAP],
                        "timestamp": event.get("timestamp", ""),
                        "session_id": path.stem,
                        "source": "cursor",
                        "project": session_meta["project"],
                        "cwd": session_meta.get("cwd", ""),
                    }
                )
    except Exception:
        return [], from_offset, session_meta

    return turns, new_offset, session_meta


def run_git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_repo_root(repo_dir: Path) -> Path:
    return Path(run_git(repo_dir, "rev-parse", "--show-toplevel"))


def get_repo_identity(repo_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    try:
        remote = run_git(repo_dir, "remote", "get-url", "origin")
    except Exception:
        remote = ""

    github_repo = None
    if remote:
        for pattern in (
            r"git@github\.com:(.+?/.+?)(?:\.git)?$",
            r"https://github\.com/(.+?/.+?)(?:\.git)?$",
        ):
            match = re.match(pattern, remote)
            if match:
                github_repo = match.group(1)
                break

    try:
        export_base_commit_sha = run_git(repo_dir, "rev-parse", "HEAD")
    except Exception:
        export_base_commit_sha = None

    return github_repo, export_base_commit_sha


def redact_text(text: str) -> str:
    redactions = [
        (r"(?im)\b(anthropic|openai|github|twitter|x|upstash)[_-]?(api[_-]?key|token)\s*[:=]\s*['\"]?[^'\"\s]+", r"\1_\2=[REDACTED]"),
        (r"(?im)\b(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*['\"]?[^'\"\s]+", r"\1=[REDACTED]"),
        (r"sk-[A-Za-z0-9_\-]{20,}", "[REDACTED_OPENAI_KEY]"),
        (r"(?i)Bearer\s+[A-Za-z0-9%._\-]{20,}", "Bearer [REDACTED]"),
        (r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", "[REDACTED_GITHUB_TOKEN]"),
    ]
    redacted = text
    for pattern, replacement in redactions:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def slugify_fragment(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-._")
    return (slug or fallback)[:80]


def build_output_path(output_dir: Path, artifact: SessionArtifact) -> Path:
    session_slug = slugify_fragment(
        artifact.session_id or artifact.source_path.stem,
        fallback=artifact.source_path.stem,
    )
    prefix = SURFACE_PREFIXES[artifact.surface]
    return output_dir / f"{prefix}-{session_slug}.md"


def render_markdown(artifact: SessionArtifact) -> str:
    lines = [
        "---",
        "schema_version: 1",
        "artifact_type: repo_agent_context",
        f"surface: {artifact.surface}",
        f"repo_name: {artifact.repo_root.name}",
        f"github_repo: {artifact.github_repo or ''}",
        f"session_id: {artifact.session_id}",
        f"source_file: {artifact.source_path.name}",
        f"exported_at: {artifact.exported_at}",
        f"export_base_commit_sha: {artifact.export_base_commit_sha or ''}",
        "redacted: true",
        "---",
        "",
        f"# Repo Agent Context: {artifact.repo_root.name}",
        "",
        f"_Surface:_ `{artifact.surface}`  ",
        f"_Session:_ `{artifact.session_id}`",
        "",
        "## Transcript",
        "",
    ]

    chars = 0
    for turn in artifact.turns:
        role = "User" if turn.get("role") == "user" else "Assistant"
        content = redact_text((turn.get("content") or "").strip())
        if not content:
            continue
        block = f"**{role}:** {content}\n"
        if chars + len(block) > MAX_TRANSCRIPT_CHARS:
            lines.append("_Transcript truncated for commit-sized artifact._")
            break
        lines.append(block)
        lines.append("")
        chars += len(block)

    return "\n".join(lines).rstrip() + "\n"


def write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else None
    if existing == content:
        return False
    path.write_text(content)
    return True


def normalize_turn_cwd(turn: Dict) -> Optional[Path]:
    cwd = turn.get("cwd")
    if not cwd:
        return None
    try:
        return Path(cwd).resolve()
    except Exception:
        return None


def path_matches_repo(candidate_path: str, repo_root: Path) -> bool:
    try:
        resolved = Path(candidate_path).resolve()
        repo_resolved = repo_root.resolve()
    except Exception:
        return False
    return resolved == repo_resolved or repo_resolved in resolved.parents


def codex_jsonl_mentions_repo(path: Path, repo_root: Path) -> bool:
    """Check raw rollout events for tool workdirs or absolute file paths in the repo."""
    repo_root_str = str(repo_root)
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                payload: Dict[str, Any] = event.get("payload", event)
                event_type = event.get("type", payload.get("type", ""))

                if event_type in {"session_meta", "turn_context"}:
                    cwd_value = payload.get("cwd")
                    if isinstance(cwd_value, str) and path_matches_repo(cwd_value, repo_root):
                        return True

                if event_type == "response_item":
                    item_type = payload.get("type")
                    if item_type == "function_call":
                        arguments = payload.get("arguments")
                        if isinstance(arguments, str):
                            try:
                                parsed_args = json.loads(arguments)
                            except json.JSONDecodeError:
                                parsed_args = {}
                            workdir = parsed_args.get("workdir")
                            if isinstance(workdir, str) and path_matches_repo(workdir, repo_root):
                                return True
                            cmd = parsed_args.get("cmd")
                            if isinstance(cmd, str) and repo_root_str in cmd:
                                return True
                    elif item_type == "custom_tool_call":
                        tool_input = payload.get("input")
                        if isinstance(tool_input, str) and repo_root_str in tool_input:
                            return True
                    elif item_type == "custom_tool_call_output":
                        output = payload.get("output")
                        if isinstance(output, str) and repo_root_str in output:
                            return True
    except Exception:
        return False

    return False


def cursor_jsonl_mentions_repo(path: Path, repo_root: Path) -> bool:
    """Check Cursor transcript content and tool inputs for repo paths."""
    repo_root_str = str(repo_root)
    try:
        with open(path, "rb") as handle:
            for raw in handle:
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                message = event.get("message", {})
                content = message.get("content", [])
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        if isinstance(text, str) and repo_root_str in text:
                            return True
                    elif block.get("type") == "tool_use":
                        tool_input = block.get("input")
                        serialized = json.dumps(tool_input, sort_keys=True) if isinstance(tool_input, (dict, list)) else str(tool_input or "")
                        if repo_root_str in serialized:
                            return True
    except Exception:
        return False

    return False


def find_claude_session_file(repo_root: Path) -> Optional[Path]:
    if not CLAUDE_PROJECTS_DIR.exists():
        return None

    repo_path = str(repo_root)
    normalized = repo_path.strip("/")
    candidate_names = {
        "-" + normalized.replace("/", "-"),
        "-" + normalized.replace("/", "-").replace(" ", "-"),
        "-" + normalized.replace("/", "-").replace(" ", "_"),
    }

    candidate_dirs: List[Tuple[int, Path]] = []
    for child in CLAUDE_PROJECTS_DIR.iterdir():
        if not child.is_dir():
            continue
        score = 0
        if child.name in candidate_names:
            score += 100
        if repo_root.name.lower() in child.name.lower():
            score += 20
        if normalized.lower().replace("/", "-") in child.name.lower():
            score += 50
        if score > 0:
            candidate_dirs.append((score, child))

    for _score, session_dir in sorted(candidate_dirs, key=lambda item: (-item[0], item[1].name)):
        jsonls = sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if jsonls:
            return jsonls[0]
    return None


def find_codex_session_file(repo_root: Path, max_candidates: int = 120) -> Optional[Path]:
    if not CODEX_SESSIONS_DIR.exists():
        return None

    candidates = sorted(
        CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:max_candidates]

    for candidate in candidates:
        turns, _offset, session_meta = parse_codex(candidate, from_offset=0)
        cwd = session_meta.get("cwd")
        if not cwd:
            continue
        try:
            cwd_path = Path(cwd).resolve()
        except Exception:
            continue
        if cwd_path == repo_root or repo_root in cwd_path.parents:
            return candidate

        for turn in turns[:5]:
            turn_cwd = normalize_turn_cwd(turn)
            if turn_cwd and (turn_cwd == repo_root or repo_root in turn_cwd.parents):
                return candidate

        if codex_jsonl_mentions_repo(candidate, repo_root):
            return candidate
    return None


def find_cursor_session_file(repo_root: Path, max_candidates: int = 160) -> Optional[Path]:
    if not CURSOR_PROJECTS_DIR.exists():
        return None

    candidates = sorted(
        CURSOR_PROJECTS_DIR.glob("**/agent-transcripts/**/*.jsonl"),
        key=lambda p: ("/subagents/" in str(p), -p.stat().st_mtime),
    )[:max_candidates]

    for candidate in candidates:
        if cursor_jsonl_mentions_repo(candidate, repo_root):
            return candidate
    return None


def load_session_artifact(
    repo_root: Path,
    surface: str,
) -> Optional[SessionArtifact]:
    github_repo, export_base_commit_sha = get_repo_identity(repo_root)
    exported_at = datetime.now(timezone.utc).isoformat()

    if surface == "claude_code":
        source_path = find_claude_session_file(repo_root)
        if not source_path:
            return None
        turns, _offset, _meta = parse_claude_code(source_path, from_offset=0)
    elif surface == "codex_cli":
        source_path = find_codex_session_file(repo_root)
        if not source_path:
            return None
        turns, _offset, _meta = parse_codex(source_path, from_offset=0)
    elif surface == "cursor":
        source_path = find_cursor_session_file(repo_root)
        if not source_path:
            return None
        turns, _offset, _meta = parse_cursor(source_path, from_offset=0)
    else:
        raise ValueError(f"Unsupported surface: {surface}")

    if not turns:
        return None

    return SessionArtifact(
        surface=surface,
        source_path=source_path,
        session_id=turns[0].get("session_id", source_path.stem),
        exported_at=exported_at,
        export_base_commit_sha=export_base_commit_sha,
        github_repo=github_repo,
        repo_root=repo_root,
        turns=turns,
    )


def export_surface(repo_root: Path, output_dir: Path, surface: str, stage: bool) -> bool:
    artifact = load_session_artifact(repo_root, surface)
    if artifact is None:
        print(f"[agent-context] No {surface} session found for {repo_root.name}, skipping.")
        return False

    output_path = build_output_path(output_dir, artifact)
    changed = write_if_changed(output_path, render_markdown(artifact))
    if changed:
        print(f"[agent-context] Wrote {output_path}")
        if stage:
            stage_artifact(repo_root, output_path)
    else:
        print(f"[agent-context] No changes for {output_path.relative_to(repo_root)}")
    return changed


def stage_artifact(repo_root: Path, output_path: Path) -> bool:
    """Stage an exported artifact, respecting the repo's .gitignore.

    A repo that ignores .pks/ has opted out of committing context artifacts,
    so the file is written but not forced into the index (a plain `git add`
    on an ignored path exits 1, which used to crash the pre-commit hook).
    Returns True only when the artifact was actually staged.
    """
    ignored = (
        subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(output_path)],
        ).returncode
        == 0
    )
    if ignored:
        print(
            f"[agent-context] {output_path.relative_to(repo_root)} is gitignored "
            "in this repo; wrote it without staging."
        )
        return False
    add = subprocess.run(["git", "-C", str(repo_root), "add", str(output_path)])
    if add.returncode == 0:
        print(f"[agent-context] Staged {output_path.relative_to(repo_root)}")
        return True
    print(
        f"[agent-context] git add exited {add.returncode} for "
        f"{output_path.relative_to(repo_root)}; continuing without staging."
    )
    return False


def has_github_origin(repo_root: Path) -> bool:
    github_repo, _commit_sha = get_repo_identity(repo_root)
    return bool(github_repo)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export repo-attached AI agent context artifacts.")
    parser.add_argument("--repo-dir", default=".", help="Repository directory to export for.")
    parser.add_argument(
        "--surface",
        choices=["claude_code", "codex_cli", "cursor", "all"],
        default="all",
        help="Surface to export.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory. Defaults to <repo>/.pks/agent-context",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage changed artifacts with git add.",
    )
    parser.add_argument(
        "--require-github-origin",
        action="store_true",
        help="Skip cleanly unless the repo's origin remote resolves to GitHub.",
    )
    args = parser.parse_args()

    repo_root = get_repo_root(Path(args.repo_dir).resolve())
    if args.require_github_origin and not has_github_origin(repo_root):
        print(f"[agent-context] {repo_root.name} has no GitHub origin, skipping.")
        return 0

    output_dir = Path(args.output_dir).resolve() if args.output_dir else repo_root / DEFAULT_OUTPUT_SUBDIR

    surfaces = ["claude_code", "codex_cli", "cursor"] if args.surface == "all" else [args.surface]
    changed_any = False
    for surface in surfaces:
        changed_any = export_surface(repo_root, output_dir, surface, args.stage) or changed_any

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
