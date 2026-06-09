#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: nightly_health_monitor.py
=============================================================================

INPUT FILES (read):
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/.env
  (loaded indirectly via core.config for Upstash/OpenAI credentials)
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/twitter_state.json
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/agent_sessions_state.json
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/agent_sessions_last_run.json
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/nightly_ingestion_success.json
- The nightly log file passed via --log (e.g. ingestion/logs/nightly/YYYY-MM-DD.log)
- Snapshot JSON files passed via --before / --after (produced by this same script)
- Live Upstash Redis + Vector (via StorageClient: get_stats, test_connection, get_processed_sources)

OUTPUT FILES (write):
- The snapshot JSON path passed via --out in `snapshot` mode
- The report .json and .md paths passed via --out in `verify` mode
- (no other side effects; this script never writes to Upstash)

VERSION: 1.0
LAST UPDATED: 2026-06-07
AUTHOR: Claude

DESCRIPTION:
End-to-end health monitor for the knowledge-system nightly ingestion run. It does
three jobs, selected by sub-mode:

  preflight  -> Non-destructive readiness checks before a run: Redis/Vector
                connectivity, required env keys present, source directories exist,
                claude CLI + agent-sdk import, and the NO-BROWSER SDK preflight
                wrapper (scripts/check_claude_sdk_auth_noninteractive.py). Never
                opens a browser. Prints a PASS/FAIL table; exits nonzero if any
                hard check fails.

  snapshot   -> Captures the current observable state of the system to a JSON file:
                storage counts (knowledge entries / project entries / vectors),
                per-source dedup-marker counts (twitter/github), and checkpoint
                state (twitter cursor, agent-sessions file/offset stats, last-run
                status, nightly success marker). Run once BEFORE and once AFTER
                the nightly run to measure what every pipeline actually did.

  verify     -> Compares a before snapshot and an after snapshot, reads the nightly
                log and success marker, and produces a per-pipeline PASS/WARN/FAIL
                report (JSON + Markdown). This is the "did every piece work?" check.

This monitor is READ-ONLY against production storage. It is safe to run anytime.

DEPENDENCIES:
- Python 3 (ingestion/.venv) with: upstash_redis, upstash_vector, openai, python-dotenv
- Must be run with the ingestion venv:
    "<repo>/ingestion/.venv/bin/python" scripts/nightly_health_monitor.py ...

USAGE:
    python scripts/nightly_health_monitor.py preflight
    python scripts/nightly_health_monitor.py snapshot --label before --out /tmp/before.json
    python scripts/nightly_health_monitor.py snapshot --label after  --out /tmp/after.json
    python scripts/nightly_health_monitor.py verify --before /tmp/before.json \
        --after /tmp/after.json --log ingestion/logs/nightly/2026-06-07.log \
        --out /tmp/nightly_report

NOTES:
- The pure comparison/verification logic (build_verification, summarize_preflight)
  takes plain dicts so it can be unit-tested without touching the network.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INGESTION_DIR = REPO_ROOT / "ingestion"
CHECKPOINT_DIR = INGESTION_DIR / "checkpoints"
NONINTERACTIVE_PREFLIGHT = REPO_ROOT / "scripts" / "check_claude_sdk_auth_noninteractive.py"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))


# ---------------------------------------------------------------------------
# Helpers (pure-ish)
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _checkpoint_state() -> dict:
    """Summarize the on-disk checkpoint files without importing pickle data."""
    out: dict = {}

    tw = _read_json(CHECKPOINT_DIR / "twitter_state.json")
    out["twitter_state"] = (
        {
            "last_seen_id": tw.get("last_seen_id"),
            "run_count": tw.get("run_count"),
            "last_run_at": tw.get("last_run_at"),
        }
        if isinstance(tw, dict)
        else None
    )

    ag = _read_json(CHECKPOINT_DIR / "agent_sessions_state.json")
    if isinstance(ag, dict):
        files = ag.get("files", {}) or {}
        stats = ag.get("stats", {}) or {}
        out["agent_sessions_state"] = {
            "file_count": len(files),
            "total_saved": stats.get("total_saved"),
            "total_skipped": stats.get("total_skipped"),
            "last_run": ag.get("last_run"),
        }
    else:
        out["agent_sessions_state"] = None

    out["agent_sessions_last_run"] = _read_json(CHECKPOINT_DIR / "agent_sessions_last_run.json")
    out["nightly_success_marker"] = _read_json(CHECKPOINT_DIR / "nightly_ingestion_success.json")
    return out


# ---------------------------------------------------------------------------
# SNAPSHOT
# ---------------------------------------------------------------------------
def capture_snapshot(label: str) -> dict:
    """Capture the live, observable state of the system (READ-ONLY)."""
    from core.storage import StorageClient

    storage = StorageClient()
    ok, message = storage.test_connection()

    snapshot: dict = {
        "label": label,
        "captured_at": _now_iso(),
        "connection_ok": ok,
        "connection_message": message,
        "storage": None,
        "processed_sources": {},
        "checkpoints": _checkpoint_state(),
    }

    if ok:
        try:
            snapshot["storage"] = storage.get_stats()
        except Exception as exc:  # surface, do not mask
            snapshot["storage_error"] = str(exc)
        for source in ("twitter", "github"):
            try:
                snapshot["processed_sources"][source] = len(storage.get_processed_sources(source))
            except Exception as exc:
                snapshot["processed_sources"][source] = f"ERROR: {exc}"

    return snapshot


# ---------------------------------------------------------------------------
# PREFLIGHT
# ---------------------------------------------------------------------------
REQUIRED_ENV = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "UPSTASH_REDIS_REST_URL",
    "UPSTASH_REDIS_REST_TOKEN",
    "UPSTASH_VECTOR_REST_URL",
    "UPSTASH_VECTOR_REST_TOKEN",
    "TWITTER_BEARER_TOKEN",
    "GITHUB_API_KEY",
    "DREAM_OPERATOR_TOKEN",
]


def _env_file_values() -> dict[str, str]:
    """
    Read the .env file the nightly wrapper actually sources, as the source of
    truth for credential presence.

    NOTE: we must NOT use os.getenv here. Importing core.* pulls in
    core.sdk_client, which deliberately pops ANTHROPIC_API_KEY from the live
    process env to force Claude subscription billing. So os.getenv would falsely
    report ANTHROPIC_API_KEY missing. The nightly wrapper `source`s repo/.env,
    and the pipelines load ingestion/.env via python-dotenv, so the union of
    both files reflects what every pipeline will actually see.
    """
    from dotenv import dotenv_values

    merged: dict[str, str] = {}
    for candidate in (INGESTION_DIR / ".env", REPO_ROOT / ".env"):
        if candidate.exists():
            for k, v in dotenv_values(candidate).items():
                if v:
                    merged.setdefault(k, v)
    return merged


def run_preflight() -> dict:
    """Non-destructive readiness checks. Returns a dict of check -> (ok, detail)."""
    checks: dict[str, dict] = {}

    # 1. Required credentials present in the .env files the run sources.
    env_vals = _env_file_values()
    missing = [k for k in REQUIRED_ENV if not env_vals.get(k)]
    checks["env_keys"] = {
        "ok": not missing,
        "hard": True,
        "detail": "all present in .env" if not missing else f"MISSING from .env: {', '.join(missing)}",
    }

    # 2. Source directories exist
    claude_dir = Path(os.getenv("CLAUDE_CODE_DIR", str(Path.home() / ".claude" / "projects")))
    codex_dir = Path(os.getenv("CODEX_DIR", str(Path.home() / ".codex" / "sessions")))
    checks["source_dirs"] = {
        "ok": claude_dir.exists() or codex_dir.exists(),
        "hard": False,
        "detail": f"claude_projects={'yes' if claude_dir.exists() else 'no'} "
        f"codex_sessions={'yes' if codex_dir.exists() else 'no'}",
    }

    # 3. claude CLI on PATH
    claude_path = subprocess.run(
        ["bash", "-lc", "command -v claude"], capture_output=True, text=True
    )
    checks["claude_cli"] = {
        "ok": claude_path.returncode == 0,
        "hard": True,
        "detail": (claude_path.stdout or claude_path.stderr).strip() or "not found",
    }

    # 4. agent-sdk import
    sdk_import = subprocess.run(
        [str(Path.home() / "agent-sdk-venv" / "bin" / "python3"), "-c", "from claude_agent_sdk import query"],
        capture_output=True,
        text=True,
    )
    checks["agent_sdk_import"] = {
        "ok": sdk_import.returncode == 0,
        "hard": True,
        "detail": "import OK" if sdk_import.returncode == 0 else sdk_import.stderr.strip()[-200:],
    }

    # 5. Redis + Vector connectivity (READ-ONLY test)
    try:
        from core.storage import StorageClient

        ok, message = StorageClient().test_connection()
        checks["storage_connectivity"] = {"ok": ok, "hard": True, "detail": message}
    except Exception as exc:
        checks["storage_connectivity"] = {"ok": False, "hard": True, "detail": str(exc)}

    # 6. NO-BROWSER SDK preflight wrapper (the storm-prevention path).
    #    A 'fail' here is NOT a hard failure: it just means the run will route to
    #    API fallback. We assert ONLY that it returns quickly and opens no browser.
    sdk_pre = subprocess.run(
        [sys.executable, str(NONINTERACTIVE_PREFLIGHT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    sdk_out = (sdk_pre.stdout + sdk_pre.stderr).strip().splitlines()
    checks["no_browser_sdk_preflight"] = {
        "ok": True,  # informational: 0 => SDK route, !=0 => API fallback route
        "hard": False,
        "detail": (
            f"exit={sdk_pre.returncode} "
            f"({'SDK route' if sdk_pre.returncode == 0 else 'API fallback route'}); "
            f"{sdk_out[-1] if sdk_out else 'no output'}"
        ),
    }

    return checks


def summarize_preflight(checks: dict) -> tuple[bool, list[str]]:
    """Pure: turn a preflight checks dict into (all_hard_ok, lines)."""
    lines: list[str] = []
    all_hard_ok = True
    for name, c in checks.items():
        mark = "PASS" if c["ok"] else ("FAIL" if c.get("hard") else "WARN")
        if not c["ok"] and c.get("hard"):
            all_hard_ok = False
        lines.append(f"[{mark}] {name}: {c['detail']}")
    return all_hard_ok, lines


# ---------------------------------------------------------------------------
# VERIFY  (pure — takes snapshots + log text, returns a report dict)
# ---------------------------------------------------------------------------
def _delta(before: dict | None, after: dict | None, key: str) -> int | None:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    b, a = before.get(key), after.get(key)
    if isinstance(b, int) and isinstance(a, int):
        return a - b
    return None


RUN_START_MARKER = "=== Nightly ingestion started ==="


def scope_log_to_last_run(log_text: str) -> str:
    """
    The nightly wrapper appends to a per-DAY log file, so a single file can hold
    several runs. Verification must only consider the MOST RECENT run, otherwise a
    failure from an earlier run today would wrongly fail the current one. Return
    the text from the last run-start marker onward (or the whole text if absent).
    """
    idx = log_text.rfind(RUN_START_MARKER)
    return log_text[idx:] if idx != -1 else log_text


def build_verification(before: dict, after: dict, log_text: str) -> dict:
    """
    Pure verification logic. Returns a structured report:
      { overall, pipelines: {name: {status, reasons[]}}, deltas, log_errors, marker }
    status is one of: PASS, WARN, FAIL.
    """
    report: dict = {
        "generated_at": _now_iso(),
        "before_label": before.get("label"),
        "after_label": after.get("label"),
        "deltas": {},
        "pipelines": {},
        "log": {},
        "marker": {},
    }

    b_store = before.get("storage") or {}
    a_store = after.get("storage") or {}
    d_knowledge = _delta(b_store, a_store, "knowledge_entries")
    d_project = _delta(b_store, a_store, "project_entries")
    d_vectors = _delta(b_store, a_store, "total_vectors")
    d_tw = _delta(before.get("processed_sources") or {}, after.get("processed_sources") or {}, "twitter")
    d_gh = _delta(before.get("processed_sources") or {}, after.get("processed_sources") or {}, "github")
    report["deltas"] = {
        "knowledge_entries": d_knowledge,
        "project_entries": d_project,
        "total_vectors": d_vectors,
        "twitter_sources": d_tw,
        "github_sources": d_gh,
    }

    # --- Log scan ---
    error_lines = [
        ln for ln in log_text.splitlines()
        if re.search(r"\b(FATAL|Traceback|ERROR|Refusing to run|call cap exceeded|run budget would be exceeded)\b", ln)
    ]
    started = "Nightly ingestion started" in log_text
    completed = "Nightly ingestion complete" in log_text
    pipeline_markers = {
        "twitter": ("Twitter ingestion starting", "Twitter ingestion done"),
        "github": ("GitHub ingestion starting", "GitHub ingestion done"),
        "agent_sessions": ("Agent sessions ingestion starting", "Agent sessions ingestion done"),
        "dream_judge": ("Dream judge starting", "Dream judge done"),
    }
    report["log"] = {
        "started": started,
        "completed": completed,
        "error_line_count": len(error_lines),
        "error_lines": error_lines[:50],
    }

    # --- Browser-storm guard: nothing should mention oauth callback / port 18043 ---
    storm_hits = [ln for ln in log_text.splitlines() if re.search(r"oauth/callback|localhost:18043|18043", ln)]
    report["log"]["browser_storm_hits"] = storm_hits[:20]

    # --- Success marker ---
    marker = (after.get("checkpoints") or {}).get("nightly_success_marker")
    report["marker"] = marker if isinstance(marker, dict) else None

    # --- Per-pipeline verdicts ---
    def verdict(name: str, ran: tuple[bool, bool], reasons: list[str], data_signal: int | None) -> dict:
        start_seen, done_seen = ran
        status = "PASS"
        rs = list(reasons)
        if not start_seen or not done_seen:
            status = "FAIL"
            rs.append(f"log markers start={start_seen} done={done_seen}")
        if data_signal is None:
            rs.append("no before/after count available")
        return {"status": status, "reasons": rs, "data_signal": data_signal}

    # Twitter: ran + (new sources OR knowledge growth). New data is a bonus, not required
    # (an incremental run can legitimately find nothing new), so absence => WARN not FAIL.
    tw_ran = (pipeline_markers["twitter"][0] in log_text, pipeline_markers["twitter"][1] in log_text)
    tw = verdict("twitter", tw_ran, [], d_tw)
    if tw["status"] == "PASS" and not (d_tw and d_tw > 0) and not (d_knowledge and d_knowledge > 0):
        tw["status"] = "WARN"
        tw["reasons"].append("ran cleanly but no new twitter sources/knowledge this run (OK if nothing new)")
    report["pipelines"]["twitter"] = tw

    gh_ran = (pipeline_markers["github"][0] in log_text, pipeline_markers["github"][1] in log_text)
    gh = verdict("github", gh_ran, [], d_gh)
    if gh["status"] == "PASS" and not (d_gh and d_gh > 0):
        gh["status"] = "WARN"
        gh["reasons"].append("ran cleanly but no new github sources this run (OK if nothing pushed)")
    report["pipelines"]["github"] = gh

    ag_ran = (
        pipeline_markers["agent_sessions"][0] in log_text,
        pipeline_markers["agent_sessions"][1] in log_text,
    )
    ag = verdict("agent_sessions", ag_ran, [], _delta(
        (before.get("checkpoints") or {}).get("agent_sessions_state") or {},
        (after.get("checkpoints") or {}).get("agent_sessions_state") or {},
        "file_count",
    ))
    last_run = (after.get("checkpoints") or {}).get("agent_sessions_last_run")
    if isinstance(last_run, dict):
        ag["reasons"].append(
            f"last_run total_saved={last_run.get('total_saved')} "
            f"redis_write_failed={last_run.get('redis_write_failed')}"
        )
        if last_run.get("redis_write_failed") is True:
            # The local disk checkpoint is authoritative and re-syncs Redis on the
            # next run, so a mirror write failure is a WARN (surfaced), not a hard
            # FAIL — unless something else already failed this pipeline.
            if ag["status"] != "FAIL":
                ag["status"] = "WARN"
            ag["reasons"].append(
                "agent_sessions Redis mirror write failed (disk checkpoint OK; "
                "re-syncs next run)"
            )
    if ag["status"] == "PASS" and not (ag["data_signal"] and ag["data_signal"] > 0):
        ag["status"] = "WARN"
        ag["reasons"].append("ran cleanly but no new session files tracked (OK if already up to date)")
    report["pipelines"]["agent_sessions"] = ag

    dj_ran = (pipeline_markers["dream_judge"][0] in log_text, pipeline_markers["dream_judge"][1] in log_text)
    dj = verdict("dream_judge", dj_ran, [], None)
    # Dream judge is non-fatal by design; a non-zero exit is logged but tolerated.
    if "Dream judge exited with non-zero status" in log_text:
        dj["status"] = "WARN"
        dj["reasons"].append("dream judge exited non-zero (tolerated by nightly wrapper)")
    report["pipelines"]["dream_judge"] = dj

    # --- Success-marker self-report (the wrapper records its own verdict) ---
    marker_ok = None
    if isinstance(marker, dict) and "ok" in marker:
        marker_ok = bool(marker.get("ok"))
        report["log"]["marker_ok"] = marker_ok
        if marker.get("failed_stages"):
            report["log"]["marker_failed_stages"] = marker["failed_stages"]

    # --- Overall ---
    statuses = [p["status"] for p in report["pipelines"].values()]
    overall = "PASS"
    if (
        "FAIL" in statuses
        or not completed
        or len(error_lines) > 0
        or storm_hits
        or marker_ok is False
    ):
        overall = "FAIL"
    elif "WARN" in statuses:
        overall = "WARN"
    report["overall"] = overall
    return report


def render_markdown(report: dict, before: dict, after: dict) -> str:
    emoji = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    lines = [
        "# Nightly Ingestion Health Report",
        "",
        f"Generated: {report.get('generated_at')}",
        "",
        f"## Overall: {emoji.get(report['overall'], '')} {report['overall']}",
        "",
        "## Storage deltas (after − before)",
        "",
        "| Metric | Before | After | Δ |",
        "| --- | ---: | ---: | ---: |",
    ]
    b_store = before.get("storage") or {}
    a_store = after.get("storage") or {}
    for key, label in [
        ("knowledge_entries", "Knowledge entries"),
        ("project_entries", "Project entries"),
        ("total_vectors", "Vectors"),
    ]:
        lines.append(
            f"| {label} | {b_store.get(key)} | {a_store.get(key)} | {report['deltas'].get(key)} |"
        )
    ps_b = before.get("processed_sources") or {}
    ps_a = after.get("processed_sources") or {}
    lines.append(f"| Twitter sources | {ps_b.get('twitter')} | {ps_a.get('twitter')} | {report['deltas'].get('twitter_sources')} |")
    lines.append(f"| GitHub sources | {ps_b.get('github')} | {ps_a.get('github')} | {report['deltas'].get('github_sources')} |")

    lines += ["", "## Pipelines", "", "| Pipeline | Status | Notes |", "| --- | --- | --- |"]
    for name, p in report["pipelines"].items():
        notes = "; ".join(p["reasons"]) if p["reasons"] else "ran clean, data changed"
        lines.append(f"| {name} | {emoji.get(p['status'], '')} {p['status']} | {notes} |")

    log = report["log"]
    lines += [
        "",
        "## Run log",
        "",
        f"- Started: {log['started']}  ·  Completed: {log['completed']}",
        f"- Error lines: {log['error_line_count']}",
        f"- Browser/OAuth storm hits in log: {len(log['browser_storm_hits'])} (must be 0)",
    ]
    if log["error_lines"]:
        lines += ["", "### Error lines (first 50)", "", "```"] + log["error_lines"] + ["```"]
    if log["browser_storm_hits"]:
        lines += ["", "### ⚠️ Browser-storm references in log", "", "```"] + log["browser_storm_hits"] + ["```"]

    marker = report.get("marker")
    lines += ["", "## Success marker", ""]
    if marker:
        lines.append("```json")
        lines.append(json.dumps(marker, indent=2))
        lines.append("```")
    else:
        lines.append("❌ No nightly_ingestion_success.json marker found.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly ingestion health monitor")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_snap = sub.add_parser("snapshot")
    p_snap.add_argument("--label", required=True)
    p_snap.add_argument("--out", required=True)

    sub.add_parser("preflight")

    p_ver = sub.add_parser("verify")
    p_ver.add_argument("--before", required=True)
    p_ver.add_argument("--after", required=True)
    p_ver.add_argument("--log", required=True)
    p_ver.add_argument("--out", required=True, help="output path stem (.json and .md are written)")

    args = parser.parse_args(argv)

    if args.mode == "snapshot":
        snap = capture_snapshot(args.label)
        Path(args.out).write_text(json.dumps(snap, indent=2) + "\n", encoding="utf-8")
        print(f"snapshot[{args.label}] -> {args.out}")
        print(json.dumps(snap.get("storage") or {"connection": snap.get("connection_message")}, indent=2))
        return 0 if snap.get("connection_ok") else 1

    if args.mode == "preflight":
        checks = run_preflight()
        ok, lines = summarize_preflight(checks)
        print("\n".join(lines))
        print("\nPREFLIGHT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1

    if args.mode == "verify":
        before = _read_json(Path(args.before)) or {}
        after = _read_json(Path(args.after)) or {}
        log_text = ""
        try:
            log_text = Path(args.log).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            print(f"WARNING: could not read log {args.log}: {exc}", file=sys.stderr)
        report = build_verification(before, after, scope_log_to_last_run(log_text))
        out_stem = args.out
        Path(out_stem + ".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        Path(out_stem + ".md").write_text(render_markdown(report, before, after), encoding="utf-8")
        print(f"report -> {out_stem}.json / {out_stem}.md")
        print("OVERALL:", report["overall"])
        return 0 if report["overall"] != "FAIL" else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
