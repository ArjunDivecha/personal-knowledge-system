"""
=============================================================================
INGESTION PIPELINE - AGENT SDK CLIENT
=============================================================================
Version: 1.0.0
Last Updated: 2026-05-15

PURPOSE:
Shared synchronous wrapper around the Claude Agent SDK. Ingestion uses the
Claude subscription/OAuth path by default. Anthropic API fallback is fail-closed
and must be enabled explicitly with PKS_ALLOW_ANTHROPIC_API_FALLBACK=1.

USAGE:
    from core.sdk_client import sdk_query

    text = sdk_query(prompt)  # drop-in for response.content[0].text

NOTES:
- API keys are removed before importing claude_agent_sdk unless fallback is
  explicitly enabled, and also removed from the child CLI env passed to the SDK.
- The venv at ~/agent-sdk-venv must have claude-agent-sdk installed.
  Run: ~/agent-sdk-venv/bin/pip install --upgrade claude-agent-sdk
=============================================================================
"""

import os
import asyncio
import sys


DEFAULT_SDK_MAX_BUDGET_USD = 0.25
DEFAULT_SDK_MAX_TURNS = 4
DEFAULT_SDK_MODEL = "sonnet"
_warned_api_fallback = False


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _allow_api_fallback() -> bool:
    return _truthy(os.getenv("PKS_ALLOW_ANTHROPIC_API_FALLBACK"))


# Force OAuth/subscription billing unless a caller deliberately enables API fallback.
if not _allow_api_fallback():
    os.environ.pop("ANTHROPIC_API_KEY", None)

# SDK import must come AFTER the pop above
from claude_agent_sdk import (  # noqa: E402
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _float_env(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _warn_if_api_fallback_enabled(env: dict[str, str]) -> None:
    global _warned_api_fallback
    if _warned_api_fallback:
        return
    if _allow_api_fallback() and env.get("ANTHROPIC_API_KEY"):
        print(
            "WARNING: PKS_ALLOW_ANTHROPIC_API_FALLBACK=1; Claude Agent SDK may use Anthropic API billing for this process.",
            file=sys.stderr,
        )
        _warned_api_fallback = True


def _build_sdk_env() -> dict[str, str]:
    env = dict(os.environ)
    if not _allow_api_fallback():
        env.pop("ANTHROPIC_API_KEY", None)
    else:
        _warn_if_api_fallback_enabled(env)
    return env


def resolved_sdk_model() -> str:
    return os.getenv("PKS_SDK_MODEL") or DEFAULT_SDK_MODEL


def _assert_allowed_sdk_model(model: str) -> None:
    if "opus" in model.lower() and not _truthy(os.getenv("PKS_ALLOW_OPUS_SDK_MODEL")):
        raise RuntimeError(
            "Refusing to run ingestion SDK with an Opus-class model. "
            "Set PKS_SDK_MODEL=sonnet for normal ingestion, or explicitly set "
            "PKS_ALLOW_OPUS_SDK_MODEL=1 for a deliberate Opus SDK run."
        )


def _build_options() -> ClaudeAgentOptions:
    kwargs: dict[str, object] = {
        "allowed_tools": [],
        "env": _build_sdk_env(),
        "max_turns": _int_env("PKS_SDK_MAX_TURNS", DEFAULT_SDK_MAX_TURNS),
    }
    max_budget = _float_env("PKS_SDK_MAX_BUDGET_USD", DEFAULT_SDK_MAX_BUDGET_USD)
    if max_budget is not None:
        kwargs["max_budget_usd"] = max_budget
    model = resolved_sdk_model()
    _assert_allowed_sdk_model(model)
    kwargs["model"] = model
    return ClaudeAgentOptions(**kwargs)


def _format_result_error(msg: ResultMessage) -> str:
    parts = [f"subtype={msg.subtype}"]
    if msg.api_error_status is not None:
        parts.append(f"api_error_status={msg.api_error_status}")
    if msg.stop_reason:
        parts.append(f"stop_reason={msg.stop_reason}")
    if msg.total_cost_usd is not None:
        parts.append(f"total_cost_usd={msg.total_cost_usd:.6f}")
    if msg.errors:
        parts.append("errors=" + "; ".join(str(error) for error in msg.errors))
    return "Agent SDK returned error result (" + ", ".join(parts) + ")"


def _format_sdk_exception(error: Exception) -> str:
    message = str(error)
    if "Claude Code returned an error result: success" in message:
        return (
            "Agent SDK failed after the Claude CLI reported an error result with subtype=success. "
            "This often means the CLI hit a provider/API error without details; ingestion SDK calls "
            "scrub ANTHROPIC_API_KEY unless PKS_ALLOW_ANTHROPIC_API_FALLBACK=1 is set."
        )
    return f"Agent SDK query failed: {message}"


def sdk_query(prompt: str, max_tokens: int = 4000) -> str:
    """
    Synchronous one-shot Claude inference via the Agent SDK.

    Drop-in replacement for:
        response = client.messages.create(model=..., max_tokens=...,
                       messages=[{"role": "user", "content": prompt}])
        text = response.content[0].text

    The max_tokens parameter is accepted for call-site compatibility but is
    not forwarded to the SDK (the Agent SDK manages its own token budget).

    Raises RuntimeError if the SDK errors or returns an empty response. Fail is
    fail: no silent fallbacks, and no hidden API billing unless explicitly opted in.
    """
    async def _run() -> str:
        output: list[str] = []
        result_text: str | None = None
        try:
            async for msg in query(prompt=prompt, options=_build_options()):
                if isinstance(msg, AssistantMessage):
                    if msg.error:
                        raise RuntimeError(f"Agent SDK assistant error: {msg.error}")
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            output.append(block.text)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        raise RuntimeError(_format_result_error(msg))
                    if msg.result:
                        result_text = msg.result
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(_format_sdk_exception(error)) from error

        text = "".join(output).strip()
        if text:
            return text
        if result_text and result_text.strip():
            return result_text.strip()
        return ""

    result = asyncio.run(_run())
    if not result:
        raise RuntimeError("Agent SDK returned empty response")
    return result
