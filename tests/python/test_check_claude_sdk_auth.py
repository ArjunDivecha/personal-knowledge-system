from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = REPO_ROOT / "scripts" / "check_claude_sdk_auth.py"

spec = importlib.util.spec_from_file_location("check_claude_sdk_auth", PREFLIGHT_PATH)
assert spec is not None and spec.loader is not None
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class CheckClaudeSdkAuthTests(unittest.TestCase):
    def test_preflight_fails_closed_after_retry_exhaustion(self) -> None:
        with patch.dict("os.environ", {"PKS_SDK_PREFLIGHT_ATTEMPTS": "2"}, clear=True):
            with patch.object(preflight, "sdk_query", side_effect=RuntimeError("auth dead")) as query:
                with patch.object(preflight.time, "sleep") as sleep:
                    self.assertEqual(preflight.main(), 1)

        self.assertEqual(query.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_preflight_succeeds_on_second_attempt(self) -> None:
        with patch.dict("os.environ", {"PKS_SDK_PREFLIGHT_ATTEMPTS": "2"}, clear=True):
            with patch.object(preflight, "sdk_query", side_effect=[RuntimeError("transient"), "OK"]) as query:
                with patch.object(preflight.time, "sleep") as sleep:
                    self.assertEqual(preflight.main(), 0)

        self.assertEqual(query.call_count, 2)
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
