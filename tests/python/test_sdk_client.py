from __future__ import annotations

import json
import sys
import tempfile
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
    def setUp(self) -> None:
        sdk_client._api_fallback_calls = 0
        sdk_client._api_fallback_total_cost_usd = 0.0
        sdk_client._warned_api_fallback = False

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

    def test_api_fallback_uses_same_model_turn_and_budget_guards(self) -> None:
        captured = {}

        def fake_query(prompt, options):
            captured["env"] = options.env
            captured["model"] = options.model
            captured["max_turns"] = options.max_turns
            captured["max_budget_usd"] = options.max_budget_usd

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=0.01)

            return messages()

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                "PKS_SDK_MODEL": "sonnet",
                "PKS_SDK_MAX_TURNS": "3",
                "PKS_SDK_MAX_BUDGET_USD": "0.12",
                "PKS_API_FALLBACK_RUN_MAX_BUDGET_USD": "1.00",
            },
            clear=True,
        ):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("hello"), "ok")

        self.assertEqual(captured["env"].get("ANTHROPIC_API_KEY"), "sk-test")
        self.assertEqual(captured["model"], "sonnet")
        self.assertEqual(captured["max_turns"], 3)
        self.assertEqual(captured["max_budget_usd"], 0.12)

    def test_api_fallback_refuses_opus_model_without_explicit_override(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                "PKS_SDK_MODEL": "claude-opus-4-8",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "Opus-class model"):
                sdk_client.sdk_query("hello")

    def test_api_fallback_run_budget_allows_paid_result_then_blocks_next_call(self) -> None:
        call_count = 0

        def fake_query(prompt, options):
            nonlocal call_count
            call_count += 1

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=0.60 if call_count == 1 else 0.01)

            return messages()

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                "PKS_API_FALLBACK_RUN_MAX_BUDGET_USD": "0.50",
            },
            clear=True,
        ):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("first"), "ok")
                with self.assertRaisesRegex(RuntimeError, "would be exceeded by the next call"):
                    sdk_client.sdk_query("second")

        self.assertEqual(call_count, 1)

    def test_api_fallback_call_cap_blocks_second_call(self) -> None:
        call_count = 0

        def fake_query(prompt, options):
            nonlocal call_count
            call_count += 1

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=0.01)

            return messages()

        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                "PKS_API_FALLBACK_MAX_CALLS": "1",
            },
            clear=True,
        ):
            with patch.object(sdk_client, "query", fake_query):
                self.assertEqual(sdk_client.sdk_query("first"), "ok")
                with self.assertRaisesRegex(RuntimeError, "call cap exceeded"):
                    sdk_client.sdk_query("second")

        self.assertEqual(call_count, 1)

    def test_api_fallback_budget_file_blocks_next_call_across_client_state(self) -> None:
        costs = iter([0.30, 0.25])
        call_count = 0

        def fake_query(prompt, options):
            nonlocal call_count
            call_count += 1
            cost = next(costs)

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=cost)

            return messages()

        with tempfile.TemporaryDirectory() as tmp_dir:
            budget_file = Path(tmp_dir) / "fallback-budget.json"
            with patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "sk-test",
                    "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                    "PKS_API_FALLBACK_RUN_MAX_BUDGET_USD": "0.50",
                    "PKS_API_FALLBACK_BUDGET_FILE": str(budget_file),
                },
                clear=True,
            ):
                with patch.object(sdk_client, "query", fake_query):
                    self.assertEqual(sdk_client.sdk_query("first"), "ok")
                    sdk_client._api_fallback_calls = 0
                    sdk_client._api_fallback_total_cost_usd = 0.0
                    with self.assertRaisesRegex(RuntimeError, "would be exceeded by the next call"):
                        sdk_client.sdk_query("second")

            state = json.loads(budget_file.read_text(encoding="utf-8"))
            self.assertEqual(call_count, 1)
            self.assertEqual(state["calls"], 1)
            self.assertAlmostEqual(state["total_cost_usd"], 0.30)

    def test_api_fallback_budget_file_releases_pre_result_failure(self) -> None:
        attempts = 0

        def fake_query(prompt, options):
            nonlocal attempts
            attempts += 1

            async def messages():
                if attempts == 1:
                    raise RuntimeError("network died before result")
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=0.01)

            return messages()

        with tempfile.TemporaryDirectory() as tmp_dir:
            budget_file = Path(tmp_dir) / "fallback-budget.json"
            with patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "sk-test",
                    "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                    "PKS_API_FALLBACK_MAX_CALLS": "1",
                    "PKS_API_FALLBACK_BUDGET_FILE": str(budget_file),
                },
                clear=True,
            ):
                with patch.object(sdk_client, "query", fake_query):
                    with self.assertRaisesRegex(RuntimeError, "network died before result"):
                        sdk_client.sdk_query("first")
                    self.assertEqual(sdk_client.sdk_query("second"), "ok")

            state = json.loads(budget_file.read_text(encoding="utf-8"))
            self.assertEqual(attempts, 2)
            self.assertEqual(state["calls"], 1)
            self.assertAlmostEqual(state["total_cost_usd"], 0.01)

    def test_api_fallback_budget_file_keeps_paid_overbudget_result(self) -> None:
        attempts = 0

        def fake_query(prompt, options):
            nonlocal attempts
            attempts += 1

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=0.60 if attempts == 1 else 0.01)

            return messages()

        with tempfile.TemporaryDirectory() as tmp_dir:
            budget_file = Path(tmp_dir) / "fallback-budget.json"
            with patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "sk-test",
                    "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                    "PKS_API_FALLBACK_RUN_MAX_BUDGET_USD": "0.50",
                    "PKS_API_FALLBACK_BUDGET_FILE": str(budget_file),
                },
                clear=True,
            ):
                with patch.object(sdk_client, "query", fake_query):
                    self.assertEqual(sdk_client.sdk_query("first"), "ok")
                    sdk_client._api_fallback_calls = 0
                    sdk_client._api_fallback_total_cost_usd = 0.0
                    with self.assertRaisesRegex(RuntimeError, "would be exceeded by the next call"):
                        sdk_client.sdk_query("second")

            state = json.loads(budget_file.read_text(encoding="utf-8"))
            self.assertEqual(attempts, 1)
            self.assertEqual(state["calls"], 1)
            self.assertAlmostEqual(state["total_cost_usd"], 0.60)

    def test_api_fallback_budget_file_allows_exact_budget_then_blocks_next(self) -> None:
        attempts = 0

        def fake_query(prompt, options):
            nonlocal attempts
            attempts += 1

            async def messages():
                yield AssistantMessage(
                    content=[TextBlock("ok")],
                    model="test-model",
                )
                yield _result_message(total_cost_usd=0.25)

            return messages()

        with tempfile.TemporaryDirectory() as tmp_dir:
            budget_file = Path(tmp_dir) / "fallback-budget.json"
            with patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "sk-test",
                    "PKS_ALLOW_ANTHROPIC_API_FALLBACK": "1",
                    "PKS_API_FALLBACK_RUN_MAX_BUDGET_USD": "0.50",
                    "PKS_API_FALLBACK_RESERVE_USD": "0.25",
                    "PKS_API_FALLBACK_BUDGET_FILE": str(budget_file),
                },
                clear=True,
            ):
                with patch.object(sdk_client, "query", fake_query):
                    self.assertEqual(sdk_client.sdk_query("first"), "ok")
                    self.assertEqual(sdk_client.sdk_query("second"), "ok")
                    with self.assertRaisesRegex(RuntimeError, "would be exceeded by the next call"):
                        sdk_client.sdk_query("third")

            state = json.loads(budget_file.read_text(encoding="utf-8"))
            self.assertEqual(attempts, 2)
            self.assertEqual(state["calls"], 2)
            self.assertAlmostEqual(state["total_cost_usd"], 0.50)

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
