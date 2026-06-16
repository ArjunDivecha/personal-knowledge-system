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
import fcntl
import json
from pathlib import Path


DEFAULT_SDK_MAX_BUDGET_USD = 0.25
DEFAULT_SDK_MAX_TURNS = 4
DEFAULT_SDK_MODEL = "sonnet"
DEFAULT_API_FALLBACK_RUN_MAX_BUDGET_USD = 5.0
DEFAULT_API_FALLBACK_MAX_CALLS = 200
_warned_api_fallback = False
_api_fallback_total_cost_usd = 0.0
_api_fallback_calls = 0


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


def _api_fallback_active(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return _allow_api_fallback() and bool(source.get("ANTHROPIC_API_KEY"))


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


def _api_fallback_run_max_budget_usd() -> float | None:
    return _float_env(
        "PKS_API_FALLBACK_RUN_MAX_BUDGET_USD",
        DEFAULT_API_FALLBACK_RUN_MAX_BUDGET_USD,
    )


def _api_fallback_max_calls() -> int:
    return _int_env("PKS_API_FALLBACK_MAX_CALLS", DEFAULT_API_FALLBACK_MAX_CALLS)


def _api_fallback_reserve_usd() -> float:
    fallback = _float_env("PKS_SDK_MAX_BUDGET_USD", DEFAULT_SDK_MAX_BUDGET_USD)
    value = _float_env("PKS_API_FALLBACK_RESERVE_USD", fallback)
    return max(0.0, float(value or 0.0))


def _api_fallback_budget_file() -> Path | None:
    raw = os.getenv("PKS_API_FALLBACK_BUDGET_FILE")
    if not raw:
        return None
    return Path(raw).expanduser()


def _load_api_fallback_budget_state(path: Path) -> dict[str, float | int]:
    if not path.exists():
        return {"total_cost_usd": 0.0, "calls": 0}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"total_cost_usd": 0.0, "calls": 0}
    return {
        "total_cost_usd": max(0.0, float(raw.get("total_cost_usd", 0.0) or 0.0)),
        "calls": max(0, int(raw.get("calls", 0) or 0)),
    }


def _write_api_fallback_budget_state(path: Path, state: dict[str, float | int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _mutate_api_fallback_budget_file(mutator) -> str | None:
    path = _api_fallback_budget_file()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            state = _load_api_fallback_budget_state(path)
            next_state, error = mutator(state)
            _write_api_fallback_budget_state(path, next_state)
            return error
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _reserve_api_fallback_call(env: dict[str, str]) -> bool:
    global _api_fallback_calls
    if not _api_fallback_active(env):
        return False
    max_calls = _api_fallback_max_calls()
    budget = _api_fallback_run_max_budget_usd()
    reserve_usd = _api_fallback_reserve_usd()
    if _api_fallback_budget_file() is not None:
        def mutate(state: dict[str, float | int]) -> tuple[dict[str, float | int], str | None]:
            calls = int(state.get("calls", 0) or 0)
            total_cost = float(state.get("total_cost_usd", 0.0) or 0.0)
            if calls >= max_calls:
                return state, (
                    "Anthropic API fallback call cap exceeded "
                    f"({max_calls} calls). Refusing additional ingestion SDK calls."
                )
            if budget is not None and total_cost + reserve_usd > budget:
                return state, (
                    "Anthropic API fallback run budget would be exceeded by the next call "
                    f"(${total_cost:.4f} + ${reserve_usd:.4f} > ${budget:.4f})."
                )
            state["calls"] = calls + 1
            return state, None

        error = _mutate_api_fallback_budget_file(mutate)
        if error:
            raise RuntimeError(error)
        return True

    if _api_fallback_calls >= max_calls:
        raise RuntimeError(
            "Anthropic API fallback call cap exceeded "
            f"({max_calls} calls). Refusing additional ingestion SDK calls."
        )
    if budget is not None and _api_fallback_total_cost_usd + reserve_usd > budget:
        raise RuntimeError(
            "Anthropic API fallback run budget would be exceeded by the next call "
            f"(${_api_fallback_total_cost_usd:.4f} + ${reserve_usd:.4f} > ${budget:.4f})."
        )
    _api_fallback_calls += 1
    return True


def _release_api_fallback_call(env: dict[str, str]) -> None:
    global _api_fallback_calls
    if not _api_fallback_active(env):
        return
    if _api_fallback_budget_file() is not None:
        def mutate(state: dict[str, float | int]) -> tuple[dict[str, float | int], str | None]:
            calls = int(state.get("calls", 0) or 0)
            state["calls"] = max(0, calls - 1)
            return state, None

        _mutate_api_fallback_budget_file(mutate)
        return
    _api_fallback_calls = max(0, _api_fallback_calls - 1)


def _record_api_fallback_cost(total_cost_usd: float | None, env: dict[str, str]) -> None:
    global _api_fallback_total_cost_usd
    if total_cost_usd is None or not _api_fallback_active(env):
        return
    cost = max(0.0, float(total_cost_usd))
    budget = _api_fallback_run_max_budget_usd()
    if _api_fallback_budget_file() is not None:
        def mutate(state: dict[str, float | int]) -> tuple[dict[str, float | int], str | None]:
            total_cost = float(state.get("total_cost_usd", 0.0) or 0.0) + cost
            state["total_cost_usd"] = total_cost
            if budget is not None and total_cost > budget:
                print(
                    "WARNING: Anthropic API fallback run budget exceeded "
                    f"(${total_cost:.4f} > ${budget:.4f}); the next fallback call will be blocked.",
                    file=sys.stderr,
                )
            return state, None

        error = _mutate_api_fallback_budget_file(mutate)
        if error:
            raise RuntimeError(error)
        return

    _api_fallback_total_cost_usd += cost
    if budget is not None and _api_fallback_total_cost_usd > budget:
        print(
            "WARNING: Anthropic API fallback run budget exceeded "
            f"(${_api_fallback_total_cost_usd:.4f} > ${budget:.4f}); the next fallback call will be blocked.",
            file=sys.stderr,
        )


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
        options = _build_options()
        reserved_api_fallback_call = _reserve_api_fallback_call(options.env)
        saw_result_message = False
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    if msg.error:
                        raise RuntimeError(f"Agent SDK assistant error: {msg.error}")
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            output.append(block.text)
                elif isinstance(msg, ResultMessage):
                    saw_result_message = True
                    # Error result messages can still represent a billed API call,
                    # so record reported cost before raising on msg.is_error.
                    _record_api_fallback_cost(msg.total_cost_usd, options.env)
                    if msg.is_error:
                        raise RuntimeError(_format_result_error(msg))
                    if msg.result:
                        result_text = msg.result
        except RuntimeError:
            if reserved_api_fallback_call and not saw_result_message:
                _release_api_fallback_call(options.env)
            raise
        except Exception as error:
            if reserved_api_fallback_call and not saw_result_message:
                _release_api_fallback_call(options.env)
            raise RuntimeError(_format_sdk_exception(error)) from error

        text = "".join(output).strip()
        if text:
            return text
        if result_text and result_text.strip():
            return result_text.strip()
        return ""

    # 4.3 — Wall-clock timeout so a stalled Agent SDK call never blocks the
    # nightly pipeline indefinitely.  300 s matches a generous tool-call chain;
    # raise promptly on TimeoutError so the stage is marked FAILED, not hung.
    SDK_QUERY_TIMEOUT_SECONDS = _int_env("PKS_SDK_QUERY_TIMEOUT_SECONDS", 300)
    try:
        result = asyncio.run(asyncio.wait_for(_run(), timeout=SDK_QUERY_TIMEOUT_SECONDS))
    except TimeoutError as exc:
        raise RuntimeError(
            f"sdk_query timed out after {SDK_QUERY_TIMEOUT_SECONDS}s — "
            "check Agent SDK auth or increase SDK_QUERY_TIMEOUT_SECONDS"
        ) from exc
    if not result:
        raise RuntimeError("Agent SDK returned empty response")
    return result
