"""
=============================================================================
MODULE: orchestrator/tests/conftest.py
=============================================================================

DESCRIPTION:
Pytest fixtures for the orchestrator Phase-1 tests. Provides a hermetic
environment: repo root on sys.path, temp ledger/report dirs (so tests never
touch real checkpoints/reports), an InMemoryBackend, a controllable fake clock,
all-green preflight deps, and a factory for a fully-injected Orchestrator.

No network is used; the production Lua path is covered separately by the opt-in
test_lock_live.py.

INPUT/OUTPUT FILES: tests write only under pytest's tmp_path.
=============================================================================
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator import config  # noqa: E402
from orchestrator import preflight as PF  # noqa: E402
from orchestrator.backends import InMemoryBackend  # noqa: E402


@pytest.fixture(autouse=True)
def temp_dirs(tmp_path, monkeypatch):
    """Redirect ledger + report output into tmp_path for every test."""
    led = tmp_path / "ledger"
    rep = tmp_path / "reports"
    monkeypatch.setattr(config, "LEDGER_DIR", led)
    monkeypatch.setattr(config, "REPORTS_DIR", rep)
    led.mkdir(parents=True, exist_ok=True)
    rep.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def clock():
    """Controllable clock: clock.t is the epoch; returns (iso, epoch)."""
    class Clock:
        t = 1000.0
        def __call__(self):
            return ("2026-06-16T23:00:00-07:00", self.t)
    return Clock()


@pytest.fixture
def green_deps():
    return PF.PreflightDeps(
        env_readable=lambda: (True, "ok"),
        redis_reachable=lambda: (True, "ok"),
        vector_reachable=lambda: (True, "ok"),
        cloudflare_health=lambda: (True, "ok"),
        dream_token_present=lambda: (True, "ok"),
        claude_cli_present=lambda: (True, "ok"),
        sdk_auth_live=lambda: (True, "sdk"),
        api_fallback_available=lambda: (True, "ok"),
        no_browser_guards=lambda: (True, "ok"))


@pytest.fixture
def make_orch(backend, clock, green_deps):
    from orchestrator.engine import Orchestrator

    def _make(**overrides):
        kwargs = dict(backend=backend, preflight_deps=green_deps, clock=clock,
                      sleep=lambda s: None, monotonic=lambda: clock.t,
                      owner_host="m4max-base", suffix="ab12cd34",
                      dream_poll_interval=1, dream_timeout=10)
        kwargs.update(overrides)
        return Orchestrator(**kwargs)
    return _make
