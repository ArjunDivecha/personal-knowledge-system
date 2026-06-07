#!/usr/bin/env python3
"""Run a tiny real Claude Agent SDK inference to validate local auth."""

from __future__ import annotations

import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from core.sdk_client import resolved_sdk_model, sdk_query  # noqa: E402


def _int_env(name: str, default: int) -> int:
    import os

    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def main() -> int:
    attempts = _int_env("PKS_SDK_PREFLIGHT_ATTEMPTS", 2)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = sdk_query("Reply with exactly: OK", max_tokens=10).strip()
            if result != "OK":
                raise RuntimeError(f"unexpected SDK preflight response: {result!r}")
            print(f"OK model={resolved_sdk_model()} attempt={attempt}")
            return 0
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2)

    print(
        f"Claude Agent SDK preflight failed after {attempts} attempt(s): {last_error}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
