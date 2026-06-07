from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = REPO_ROOT / "scripts" / "check_claude_sdk_auth_noninteractive.py"

spec = importlib.util.spec_from_file_location("check_claude_sdk_auth_noninteractive", WRAPPER_PATH)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class _FakeProc:
    """Minimal Popen stand-in for the sandboxed probe subprocess."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "", timeout: bool = False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._timeout_pending = timeout
        self.pid = 4242

    def communicate(self, timeout=None):
        if self._timeout_pending:
            # First call (the real wait) raises; after a kill the wrapper calls
            # communicate() again to reap, which must return cleanly.
            self._timeout_pending = False
            raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):  # pragma: no cover - fallback path only
        self.returncode = -9


class NonInteractivePreflightTests(unittest.TestCase):
    def test_hardened_env_sets_no_browser_guards_and_scrubs_key(self) -> None:
        with patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-should-be-removed", "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1"},
            clear=True,
        ):
            env = wrapper._hardened_env()

        self.assertEqual(env["CI"], "1")
        self.assertEqual(env["BROWSER"], "/usr/bin/false")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["PKS_ALLOW_ANTHROPIC_API_FALLBACK"], "0")
        self.assertEqual(env["PKS_SDK_PREFLIGHT_ATTEMPTS"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_success_reports_sdk_source_and_passes_hardened_sandboxed_env(self) -> None:
        captured: dict[str, object] = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            captured["start_new_session"] = kwargs.get("start_new_session")
            captured["stdin"] = kwargs.get("stdin")
            return _FakeProc(returncode=0, stdout="OK model=sonnet attempt=1\n")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test", "PKS_SDK_MODEL": "sonnet"}, clear=True):
            with patch.object(wrapper.subprocess, "Popen", side_effect=fake_popen):
                with patch("builtins.print") as mock_print:
                    rc = wrapper.main()

        self.assertEqual(rc, 0)
        # Probe runs in its own session so the group can be killed on timeout.
        self.assertIs(captured["start_new_session"], True)
        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        # The probe sees a scrubbed, no-browser environment.
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
        self.assertEqual(captured["env"]["CI"], "1")
        self.assertEqual(captured["env"]["BROWSER"], "/usr/bin/false")
        printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list if c.args)
        self.assertIn("source=sdk", printed)

    def test_auth_failure_returns_nonzero_and_does_not_skip(self) -> None:
        fake = _FakeProc(
            returncode=1,
            stderr="Claude Agent SDK preflight failed after 1 attempt(s): auth dead\n",
        )
        with patch.object(wrapper.subprocess, "Popen", return_value=fake):
            rc = wrapper.main()
        # Nonzero == route to API fallback. There is no "skip" exit code.
        self.assertEqual(rc, 1)

    def test_timeout_kills_process_group_and_returns_nonzero(self) -> None:
        fake = _FakeProc(timeout=True)
        with patch.object(wrapper.subprocess, "Popen", return_value=fake):
            with patch.object(wrapper.os, "killpg") as killpg:
                with patch.object(wrapper.os, "getpgid", return_value=4242):
                    rc = wrapper.main()

        self.assertEqual(rc, 1)
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 4242)


if __name__ == "__main__":
    unittest.main()
