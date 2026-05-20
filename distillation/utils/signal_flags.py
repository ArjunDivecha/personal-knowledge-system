"""
Deterministic ingestion-time salience signal detection.
"""

from __future__ import annotations

import re

EXPLICIT_SAVE_FLAG = "explicit_save"
CORRECTION_DERIVED_FLAG = "correction_derived"

EXPLICIT_SAVE_PATTERNS = [
    re.compile(
        r"(?:^|[.!?]\s+|\b(?:please|pls|can you|could you)\s+)(remember|save|note|don't forget|file this)\b.{0,30}\b(this|that|for later|to memory|to your memory)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r"\bcommit (this|that) to memory\b", re.IGNORECASE),
    re.compile(r"\bkeep (this|that) in mind\b", re.IGNORECASE),
]


def has_explicit_save_marker(text: str) -> bool:
    """Return true when a user turn contains a high-precision save marker."""
    return any(pattern.search(text) for pattern in EXPLICIT_SAVE_PATTERNS)


def add_signal_flag(existing: list[str] | None, flag: str) -> list[str]:
    """Append a signal flag without duplicating or disturbing existing order."""
    flags = list(existing or [])
    if flag not in flags:
        flags.append(flag)
    return flags
