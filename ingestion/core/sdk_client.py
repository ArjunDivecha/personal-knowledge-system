"""
=============================================================================
INGESTION PIPELINE - AGENT SDK CLIENT
=============================================================================
Version: 1.0.0
Last Updated: 2026-05-15

PURPOSE:
Shared synchronous wrapper around the Claude Agent SDK.
Replaces direct anthropic.messages.create() calls so inference bills against
the Max subscription (OAuth/Keychain) on local Mac runs instead of API credits.

On GitHub Actions CI (where CI=true), the ANTHROPIC_API_KEY remains in the
environment and the SDK falls back to API billing automatically — preserving
the cloud fallback path.

USAGE:
    from core.sdk_client import sdk_query

    text = sdk_query(prompt)  # drop-in for response.content[0].text

NOTES:
- os.environ.pop must happen before claude_agent_sdk is imported (done here
  at module load time so callers never have to think about ordering).
- The venv at ~/agent-sdk-venv must have claude-agent-sdk installed.
  Run: ~/agent-sdk-venv/bin/pip install --upgrade claude-agent-sdk
=============================================================================
"""

import os
import asyncio

# Force OAuth/Max billing on local Mac; leave key intact for CI (GitHub Actions)
if not os.getenv("CI"):
    os.environ.pop("ANTHROPIC_API_KEY", None)

# SDK import must come AFTER the pop above
from claude_agent_sdk import (  # noqa: E402
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
)


def sdk_query(prompt: str, max_tokens: int = 4000) -> str:
    """
    Synchronous one-shot Claude inference via the Agent SDK.

    Drop-in replacement for:
        response = client.messages.create(model=..., max_tokens=...,
                       messages=[{"role": "user", "content": prompt}])
        text = response.content[0].text

    The max_tokens parameter is accepted for call-site compatibility but is
    not forwarded to the SDK (the Agent SDK manages its own token budget).

    Raises RuntimeError if the model returns an empty response — fail is fail,
    no silent fallbacks.
    """
    async def _run() -> str:
        output: list[str] = []
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(allowed_tools=[]),
        ):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        output.append(block.text)
        return "".join(output)

    result = asyncio.run(_run())
    if not result:
        raise RuntimeError("Agent SDK returned empty response")
    return result
