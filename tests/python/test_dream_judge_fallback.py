from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_JUDGE_PATH = REPO_ROOT / "ingestion" / "dream_judge" / "run.py"

spec = importlib.util.spec_from_file_location("dream_judge_run", DREAM_JUDGE_PATH)
assert spec is not None and spec.loader is not None
dream_judge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dream_judge)


class DreamJudgeFallbackTests(unittest.TestCase):
    def test_judge_item_does_not_call_api_when_fallback_disabled(self) -> None:
        with patch.object(dream_judge, "build_prompt", return_value="prompt"):
            with patch.object(dream_judge, "judge_via_claude_cli", return_value=None):
                with patch.object(dream_judge, "judge_via_anthropic_api") as api:
                    self.assertIsNone(
                        dream_judge.judge_item(
                            {"op_id": "op_1"},
                            "claude-opus-4-6",
                            force_api=False,
                            allow_api_fallback=False,
                        )
                    )
        api.assert_not_called()

    def test_force_api_requires_explicit_fallback_permission(self) -> None:
        with patch.object(dream_judge, "build_prompt", return_value="prompt"):
            with patch.object(dream_judge, "judge_via_anthropic_api") as api:
                self.assertIsNone(
                    dream_judge.judge_item(
                        {"op_id": "op_1"},
                        "claude-opus-4-6",
                        force_api=True,
                        allow_api_fallback=False,
                    )
                )
        api.assert_not_called()

    def test_api_fallback_runs_when_explicitly_allowed(self) -> None:
        # judge_via_anthropic_api returns (verdict, reason, synthesis_or_none,
        # source) and receives the item since 29d5c03 (content-bearing insight
        # verdicts need the item payload).
        expected = ("skip", "reason", None, "anthropic_api")
        with patch.object(dream_judge, "build_prompt", return_value="prompt"):
            with patch.object(dream_judge, "judge_via_claude_cli", return_value=None):
                with patch.object(dream_judge, "judge_via_anthropic_api", return_value=expected) as api:
                    result = dream_judge.judge_item(
                        {"op_id": "op_1"},
                        "claude-opus-4-6",
                        force_api=False,
                        allow_api_fallback=True,
                    )
        self.assertEqual(result, expected)
        api.assert_called_once_with("prompt", "claude-opus-4-6", {"op_id": "op_1"})


if __name__ == "__main__":
    unittest.main()
