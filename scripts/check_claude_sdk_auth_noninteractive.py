#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: check_claude_sdk_auth_noninteractive.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/check_claude_sdk_auth.py
  (spawned as a hardened, sandboxed subprocess to perform the real one-shot SDK probe)

OUTPUT FILES:
- None. This wrapper only writes status lines to stdout/stderr and returns an
  exit code. It does NOT read or write any data files.

VERSION: 1.0
LAST UPDATED: 2026-06-07
AUTHOR: Claude

DESCRIPTION:
No-browser, non-interactive Claude Agent SDK auth preflight.

Background: the ingestion workflows and the local nightly wrapper choose a
billing route by asking "does the Claude Agent SDK subscription auth work in
this process?". The previous preflight (scripts/check_claude_sdk_auth.py) ran
the real SDK probe directly. In a service/CI/self-hosted-runner context with no
valid subscription auth, the underlying Claude CLI can launch an interactive
OAuth login *browser* flow (the localhost:.../oauth/callback windows) and wait
for a callback instead of failing cleanly. Running several workflows at once
multiplied this into a browser/OAuth "session storm".

This wrapper makes route detection safe:
  1. It hardens the child environment so an interactive browser/login flow is
     suppressed (CI=1, BROWSER=/usr/bin/false, GIT_TERMINAL_PROMPT=0).
  2. It scrubs ANTHROPIC_API_KEY for the probe so the test proves SUBSCRIPTION /
     OAuth auth specifically and never silently "succeeds" via API billing.
  3. It runs the real probe in its OWN process session (start_new_session=True)
     under a hard timeout. If the probe stalls (e.g. a login flow waiting for an
     OAuth callback), the ENTIRE probe process group is SIGKILLed, so nothing
     loops and no browser/OAuth callback processes survive.

Architecture preserved: SDK primary, Anthropic API fallback, never skip.
  - Exit 0   -> SDK subscription auth proven non-interactively.
               Prints: "OK model=<model> source=sdk"
  - Exit !=0 -> SDK not available non-interactively (or probe stalled/timed out).
               Prints: "SDK unavailable non-interactively; use API fallback"
               The CALLER must route to Anthropic API fallback, NOT skip.

A browser must NOT open in either case.

DEPENDENCIES:
- Python 3 standard library only (os, signal, subprocess, sys, pathlib).

USAGE:
    python scripts/check_claude_sdk_auth_noninteractive.py

ENVIRONMENT:
- PKS_SDK_MODEL                      Model label echoed on success (default: sonnet).
- PKS_SDK_PREFLIGHT_TIMEOUT_SECONDS  Hard timeout for the probe (default: 60).

NOTES:
- This wrapper is intentionally side-effect free except for the sandboxed probe
  subprocess; it can be imported and unit-tested without launching the SDK.
=============================================================================
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_SCRIPT = REPO_ROOT / "scripts" / "check_claude_sdk_auth.py"

FALLBACK_MESSAGE = "SDK unavailable non-interactively; use API fallback"
DEFAULT_TIMEOUT_SECONDS = 60


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _resolved_model() -> str:
    return os.getenv("PKS_SDK_MODEL") or "sonnet"


def _hardened_env() -> dict[str, str]:
    """Build a child environment that cannot start a browser/OAuth login flow."""
    env = dict(os.environ)
    # Suppress any interactive browser / login the SDK or Claude CLI might try.
    env["CI"] = "1"
    env["BROWSER"] = "/usr/bin/false"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Route detection must prove SUBSCRIPTION auth only -- never API billing.
    env["PKS_ALLOW_ANTHROPIC_API_FALLBACK"] = "0"
    env.pop("ANTHROPIC_API_KEY", None)
    # Single shot: the wrapper's own hard timeout bounds total wall time, so the
    # probe should not retry-loop (which could re-trigger a login attempt).
    env["PKS_SDK_PREFLIGHT_ATTEMPTS"] = "1"
    return env


def _kill_process_group(proc: "subprocess.Popen[str]") -> None:
    """SIGKILL the probe's whole process group so a stalled login cannot survive."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def main() -> int:
    if not PROBE_SCRIPT.exists():
        print(
            f"{FALLBACK_MESSAGE} (probe script missing: {PROBE_SCRIPT})",
            file=sys.stderr,
        )
        return 1

    timeout = _int_env("PKS_SDK_PREFLIGHT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    env = _hardened_env()

    # start_new_session=True puts the probe in its own session/process group,
    # detached from this wrapper. On timeout we can then SIGKILL that whole group
    # without touching the wrapper or the user's interactive sessions.
    proc = subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        print(
            f"{FALLBACK_MESSAGE} (SDK preflight timed out after {timeout}s; "
            "probe process group killed)",
            file=sys.stderr,
        )
        return 1

    if proc.returncode == 0:
        print(f"OK model={_resolved_model()} source=sdk")
        return 0

    detail = (stderr or stdout or "").strip().splitlines()
    if detail:
        print(detail[-1], file=sys.stderr)
    print(FALLBACK_MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
