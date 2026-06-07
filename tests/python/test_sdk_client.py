from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
import core.sdk_client as sdk_client


def _result_message(**overrides) -> ResultMessage:
    values = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "test-session",
    }
    values.update(overrides)
    return ResultMessage(**values)


class SdkClientTests(unittest.TestCase):
    def test_sdk_query_scrubs_anthropic_api_key_by_default(self) -> None:
        captured = {}

        def fake_query(prompt, options):
            captured["env"] = options.env

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message()

            return messages()

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("hello"), "ok")

        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])

    def test_sdk_query_allows_explicit_api_fallback_env(self) -> None:
        captured = {}

        def fake_query(prompt, options):
            captured["env"] = options.env

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message()

            return messages()

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
            },
            clear=False,
        ):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("hello"), "ok")

        self.assertEqual(captured["env"].get("ANTHROPIC_API_KEY"), "sk-test")

    def test_sdk_query_pins_default_model_and_turn_cap(self) -> None:
        captured = {}

        def fake_query(prompt, options):
            captured["model"] = options.model
            captured["max_turns"] = options.max_turns

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message()

            return messages()

        with patch.dict("os.environ", {}, clear=True):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("hello"), "ok")

        self.assertEqual(captured["model"], "sonnet")
        self.assertEqual(captured["max_turns"], 4)

    def test_sdk_query_refuses_opus_model_without_explicit_override(self) -> None:
        with patch.dict("os.environ", {"PKS_SDK_MODEL": "claude-opus-4-8"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Opus-class model"):
                sdk_client.sdk_query("hello")

    def test_sdk_query_allows_opus_model_with_explicit_override(self) -> None:
        captured = {}

        def fake_query(prompt, options):
            captured["model"] = options.model

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message()

            return messages()

        with patch.dict(
            "os.environ",
            {
                "PKS_SDK_MODEL": "claude-opus-4-8",
                "PKS_ALLOW_OPUS_SDK_MODEL": "1",
            },
            clear=True,
        ):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("hello"), "ok")

        self.assertEqual(captured["model"], "claude-opus-4-8")

    def test_sdk_query_uses_result_text_when_assistant_text_is_empty(self) -> None:
        def fake_query(prompt, options):
            async def messages():
                yield _result_message(result="done from result")

            return messages()

        with patch.object(sdk_client, "query", fake_query):
            self.assertEqual(sdk_client.sdk_query("hello"), "done from result")

    def test_sdk_query_raises_detailed_result_error(self) -> None:
        def fake_query(prompt, options):
            async def messages():
                yield _result_message(
                    is_error=True,
                    api_error_status=529,
                    errors=["overloaded"],
                    total_cost_usd=0.0123,
                )

            return messages()

        with patch.object(sdk_client, "query", fake_query):
            with self.assertRaisesRegex(RuntimeError, "api_error_status=529"):
                sdk_client.sdk_query("hello")


if __name__ == "__main__":
    unittest.main()
