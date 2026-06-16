"""
=============================================================================
MODULE: orchestrator/preflight.py
=============================================================================

DESCRIPTION:
The orchestrator preflight (spec "Preflight And Auth Failure"). Verifies the
environment before any stage runs and classifies auth availability. Auth
unavailable is a RECOVERABLE terminal state (failure_code auth_unavailable),
never a crash, and preflight MUST NEVER open a browser.

Checks (all):
  - repo .env readable
  - Upstash Redis reachable
  - Upstash Vector reachable
  - Cloudflare /health reachable
  - DREAM_OPERATOR_TOKEN present
  - Claude CLI present
  - non-interactive Claude SDK or CLI auth live
  - API fallback available and capped (if SDK auth is unavailable)
  - no-browser guards set for unattended auth checks

Every check is dependency-injected (PreflightDeps) so the auth-unavailable and
no-browser-guard paths are unit-testable without network or subprocesses.

INPUT FILES:
- knowledge-system/.env (existence/readability check only)
- scripts/check_claude_sdk_auth_noninteractive.py (subprocess, default SDK check)

OUTPUT FILES: none (returns a structured result the engine folds into PREFLIGHT).
=============================================================================
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable

from . import config

Check = Callable[[], "tuple[bool, str]"]

AUTH_UNAVAILABLE_NEXT_ACTION = (
    "Reauthenticate Claude CLI/SDK on M4 or enable capped API fallback."
)

# Hardened, no-browser env for any unattended auth probe (spec: "no-browser
# guards are set", "Preflight must never open a browser").
NO_BROWSER_ENV = {
    "CI": "1",
    "BROWSER": "/usr/bin/false",
    "GIT_TERMINAL_PROMPT": "0",
}


def hardened_env() -> dict:
    env = dict(os.environ)
    env.update(NO_BROWSER_ENV)
    return env


# ── default (real) check implementations ─────────────────────────────────────
def _env_readable() -> tuple[bool, str]:
    for c in (config.REPO_ROOT / ".env", config.REPO_ROOT / "ingestion" / ".env"):
        if c.exists() and os.access(c, os.R_OK):
            return True, str(c)
    return False, "no readable .env found"


def _redis_reachable() -> tuple[bool, str]:
    try:
        from .backends import redis_lua_backend_from_env
        b = redis_lua_backend_from_env()
        b.set("pks:orchestrator:__preflight__", "1", ex=60)
        return (b.get("pks:orchestrator:__preflight__") == "1"), "Upstash Redis OK"
    except Exception as exc:
        return False, f"Redis unreachable: {exc.__class__.__name__}: {exc}"


def _vector_reachable() -> tuple[bool, str]:
    try:
        from upstash_vector import Index
        idx = Index(url=os.environ["UPSTASH_VECTOR_REST_URL"],
                    token=os.environ["UPSTASH_VECTOR_REST_TOKEN"])
        info = idx.info()
        return True, f"Vector OK ({getattr(info, 'vector_count', '?')} vectors)"
    except Exception as exc:
        return False, f"Vector unreachable: {exc.__class__.__name__}: {exc}"


def _cloudflare_health() -> tuple[bool, str]:
    try:
        import requests
        url = config.dream_base_url() + "/health"
        resp = requests.get(url, timeout=15)
        return (resp.status_code == 200), f"/health {resp.status_code}"
    except Exception as exc:
        return False, f"/health unreachable: {exc.__class__.__name__}"


def _dream_token_present() -> tuple[bool, str]:
    return (bool(config.env("DREAM_OPERATOR_TOKEN")),
            "DREAM_OPERATOR_TOKEN " + ("present" if config.env("DREAM_OPERATOR_TOKEN") else "MISSING"))


def _claude_cli_present() -> tuple[bool, str]:
    path = shutil.which("claude")
    return (path is not None), (path or "claude CLI not on PATH")


def _sdk_auth_live() -> tuple[bool, str]:
    """Run the existing non-interactive SDK auth probe with no-browser env."""
    probe = config.REPO_ROOT / "scripts" / "check_claude_sdk_auth_noninteractive.py"
    if not probe.exists():
        return False, "auth probe script missing"
    try:
        proc = subprocess.run(
            [sys.executable, str(probe)],
            env=hardened_env(), start_new_session=True,
            capture_output=True, text=True, timeout=90)
        return (proc.returncode == 0), (proc.stdout.strip() or proc.stderr.strip())[:200]
    except Exception as exc:
        return False, f"auth probe error: {exc.__class__.__name__}"


def _api_fallback_available() -> tuple[bool, str]:
    has = bool(config.env("ANTHROPIC_API_KEY"))
    cap = config.env("PKS_API_FALLBACK_RUN_MAX_BUDGET_USD", "5.00")
    return has, (f"API fallback available (cap ${cap})" if has
                 else "no ANTHROPIC_API_KEY for fallback")


def _no_browser_guards() -> tuple[bool, str]:
    # Confirm we will run unattended auth with browser/OAuth disabled.
    ok = all(NO_BROWSER_ENV[k] for k in NO_BROWSER_ENV)
    return ok, "no-browser guards configured (CI=1, BROWSER=/usr/bin/false, GIT_TERMINAL_PROMPT=0)"


@dataclass
class PreflightDeps:
    env_readable: Check = _env_readable
    redis_reachable: Check = _redis_reachable
    vector_reachable: Check = _vector_reachable
    cloudflare_health: Check = _cloudflare_health
    dream_token_present: Check = _dream_token_present
    claude_cli_present: Check = _claude_cli_present
    sdk_auth_live: Check = _sdk_auth_live
    api_fallback_available: Check = _api_fallback_available
    no_browser_guards: Check = _no_browser_guards


def run_preflight(deps: PreflightDeps | None = None) -> dict:
    """Run all checks; classify auth; return a structured PREFLIGHT result.

    Auth resolution: SDK live -> ok (sdk route). Else API fallback present ->
    ok with a warning (fallback route). Else failed_recoverable auth_unavailable.
    Non-auth hard checks (env/redis/vector/cloudflare/token/cli) failing ->
    failed_recoverable too (recoverable: fix infra and resume).
    """
    deps = deps or PreflightDeps()
    checks: list[dict] = []

    def record(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:  # a check must never crash preflight
            ok, detail = False, f"check error: {exc.__class__.__name__}: {exc}"
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    env_ok = record("env_readable", deps.env_readable)
    redis_ok = record("redis_reachable", deps.redis_reachable)
    vector_ok = record("vector_reachable", deps.vector_reachable)
    cf_ok = record("cloudflare_health", deps.cloudflare_health)
    token_ok = record("dream_token_present", deps.dream_token_present)
    cli_ok = record("claude_cli_present", deps.claude_cli_present)
    guards_ok = record("no_browser_guards", deps.no_browser_guards)
    sdk_ok = record("sdk_auth_live", deps.sdk_auth_live)
    fallback_ok = record("api_fallback_available", deps.api_fallback_available)

    warnings: list[str] = []
    errors: list[str] = []

    # Auth classification.
    if sdk_ok:
        auth_route = "sdk"
    elif fallback_ok:
        auth_route = "api_fallback"
        warnings.append("SDK auth unavailable; using capped Anthropic API fallback.")
    else:
        return {
            "status": "failed_recoverable",
            "failure_code": "auth_unavailable",
            "next_action": AUTH_UNAVAILABLE_NEXT_ACTION,
            "checks": checks,
            "warnings": warnings,
            "errors": ["Claude SDK auth unavailable and no API fallback."],
            "auth_route": None,
        }

    infra_ok = env_ok and redis_ok and vector_ok and cf_ok and token_ok and cli_ok and guards_ok
    if not infra_ok:
        for c in checks:
            if not c["ok"] and c["name"] not in ("sdk_auth_live", "api_fallback_available"):
                errors.append(f"{c['name']}: {c['detail']}")
        return {
            "status": "failed_recoverable",
            "failure_code": "preflight_infra_unavailable",
            "next_action": "Restore the failing dependency above on M4, then resume.",
            "checks": checks,
            "warnings": warnings,
            "errors": errors,
            "auth_route": auth_route,
        }

    return {
        "status": "completed_with_warnings" if warnings else "completed",
        "failure_code": None,
        "next_action": None,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "auth_route": auth_route,
    }
