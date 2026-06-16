"""Preflight auth-unavailable path + no-browser guard (Phase 1)."""
from orchestrator import preflight as PF


def _deps(**over):
    base = dict(
        env_readable=lambda: (True, "ok"), redis_reachable=lambda: (True, "ok"),
        vector_reachable=lambda: (True, "ok"), cloudflare_health=lambda: (True, "ok"),
        dream_token_present=lambda: (True, "ok"), claude_cli_present=lambda: (True, "ok"),
        sdk_auth_live=lambda: (True, "sdk"), api_fallback_available=lambda: (True, "ok"),
        no_browser_guards=lambda: (True, "ok"))
    base.update(over)
    return PF.PreflightDeps(**base)


def test_auth_unavailable_is_recoverable():
    res = PF.run_preflight(_deps(sdk_auth_live=lambda: (False, "no sdk"),
                                 api_fallback_available=lambda: (False, "no key")))
    assert res["status"] == "failed_recoverable"
    assert res["failure_code"] == "auth_unavailable"
    assert res["next_action"] == PF.AUTH_UNAVAILABLE_NEXT_ACTION
    assert res["auth_route"] is None


def test_sdk_down_but_fallback_available_warns():
    res = PF.run_preflight(_deps(sdk_auth_live=lambda: (False, "no sdk")))
    assert res["status"] == "completed_with_warnings"
    assert res["auth_route"] == "api_fallback"
    assert any("fallback" in w.lower() for w in res["warnings"])


def test_all_green_completes():
    res = PF.run_preflight(_deps())
    assert res["status"] == "completed" and res["auth_route"] == "sdk"


def test_infra_failure_is_recoverable():
    res = PF.run_preflight(_deps(redis_reachable=lambda: (False, "redis down")))
    assert res["status"] == "failed_recoverable"
    assert res["failure_code"] == "preflight_infra_unavailable"
    assert any("redis" in e.lower() for e in res["errors"])


def test_no_browser_guards_present():
    # The unattended auth env must disable the browser and interactive prompts.
    assert PF.NO_BROWSER_ENV["BROWSER"] == "/usr/bin/false"
    assert PF.NO_BROWSER_ENV["CI"] == "1"
    assert PF.NO_BROWSER_ENV["GIT_TERMINAL_PROMPT"] == "0"
    env = PF.hardened_env()
    assert env["BROWSER"] == "/usr/bin/false" and env["CI"] == "1"
    ok, _ = PF._no_browser_guards()
    assert ok is True
