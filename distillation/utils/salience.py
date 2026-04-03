"""
Shared salience/tier policy loader and scorer.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "shared" / "memory_policy.json"
FIXTURES_PATH = REPO_ROOT / "shared" / "salience_fixtures.json"

_POLICY_CACHE: dict[str, Any] | None = None
_FIXTURES_CACHE: list[dict[str, Any]] | None = None


def load_memory_policy() -> dict[str, Any]:
    global _POLICY_CACHE
    if _POLICY_CACHE is None:
        with POLICY_PATH.open() as handle:
            _POLICY_CACHE = json.load(handle)
    return _POLICY_CACHE


def load_salience_fixtures() -> list[dict[str, Any]]:
    global _FIXTURES_CACHE
    if _FIXTURES_CACHE is None:
        with FIXTURES_PATH.open() as handle:
            _FIXTURES_CACHE = json.load(handle)
    return _FIXTURES_CACHE


def _coerce_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_entry_dict(entry: Any) -> dict[str, Any]:
    if hasattr(entry, "to_dict"):
        return entry.to_dict()
    if isinstance(entry, dict):
        return entry
    raise TypeError(f"Unsupported entry type for salience scoring: {type(entry)!r}")


def default_injection_tier(context_type: str | None) -> int:
    policy = load_memory_policy()
    mapping = policy["default_injection_tier_by_context_type"]
    return int(mapping.get(context_type or "task_query", 3))


def compute_salience(entry: Any, now: datetime | None = None) -> float:
    policy = load_memory_policy()
    entry_dict = _extract_entry_dict(entry)
    metadata = entry_dict.get("metadata") or {}
    context_type = metadata.get("context_type") or "task_query"
    mention_count = int(metadata.get("mention_count") or 1)
    last_seen = metadata.get("last_seen") or metadata.get("updated_at")
    last_accessed = metadata.get("last_accessed")
    confidence_raw = entry_dict.get("confidence", "medium")
    confidence_map = policy["confidence_map"]
    confidence = float(confidence_map.get(confidence_raw, confidence_map["medium"]))

    half_life_raw = policy["half_lives_days"].get(context_type, policy["half_lives_days"]["task_query"])
    last_seen_dt = _coerce_datetime(last_seen) or datetime.now(timezone.utc)
    now_dt = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)

    if half_life_raw == "infinity":
        decay = 1.0
    else:
        half_life = float(half_life_raw)
        days_since = max(0.0, (now_dt - last_seen_dt).total_seconds() / 86400.0)
        decay = 0.5 ** (days_since / half_life)

    freq_boost = min(1.0, math.log1p(max(1, mention_count)) / math.log1p(20))
    type_multiplier = float(policy["type_multipliers"].get(context_type, policy["type_multipliers"]["task_query"]))

    retrieval_boost = 0.0
    last_accessed_dt = _coerce_datetime(last_accessed)
    if last_accessed_dt is not None:
        days_since_retrieved = max(0.0, (now_dt - last_accessed_dt).total_seconds() / 86400.0)
        retrieval_boost = 0.15 * (0.5 ** (days_since_retrieved / 60.0))

    raw = confidence * decay * type_multiplier * freq_boost + retrieval_boost
    return round(min(1.0, raw), 4)


def resolve_stored_tier(entry: Any) -> int:
    entry_dict = _extract_entry_dict(entry)
    metadata = entry_dict.get("metadata") or {}
    raw_tier = metadata.get("injection_tier")
    if isinstance(raw_tier, int) and raw_tier in (1, 2, 3):
        return raw_tier
    return default_injection_tier(metadata.get("context_type"))


def evaluate_salience_fixtures() -> list[dict[str, Any]]:
    results = []
    for fixture in load_salience_fixtures():
        now = _coerce_datetime(fixture.get("now"))
        score = compute_salience(fixture["entry"], now=now)
        tier = resolve_stored_tier(fixture["entry"])
        results.append(
            {
                "name": fixture["name"],
                "expected": fixture["expected"],
                "actual": {
                    "salience_score": score,
                    "stored_tier": tier,
                },
            }
        )
    return results
