"""
Shared salience/tier policy loader and scorer.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
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


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


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
    now_dt = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    # 3.5 — When last_seen is absent use a conservative 90-day-old fallback rather
    # than now; defaulting to now gave missing-timestamp entries zero decay and
    # made them appear perpetually fresh.  Must stay in lockstep with salience.ts.
    _MISSING_TIMESTAMP_FALLBACK_DAYS = 90.0
    _last_seen_dt_raw = _coerce_datetime(last_seen)
    last_seen_dt = _last_seen_dt_raw or (now_dt - timedelta(days=_MISSING_TIMESTAMP_FALLBACK_DAYS))

    if half_life_raw == "infinity":
        decay = 1.0
    else:
        half_life = float(half_life_raw)
        days_since = max(0.0, (now_dt - last_seen_dt).total_seconds() / 86400.0)
        decay = 0.5 ** (days_since / half_life)

    freq_boost = min(1.0, math.log1p(max(1, mention_count)) / math.log1p(20))
    type_multiplier = float(policy["type_multipliers"].get(context_type, policy["type_multipliers"]["task_query"]))
    signal_multiplier = 1.0
    signal_multipliers = policy.get("signal_flag_multipliers", {})
    for flag in _coerce_string_list(metadata.get("signal_flags")):
        signal_multiplier *= float(signal_multipliers.get(flag, 1.0))
    max_combined_multiplier = float(policy.get("max_combined_salience_multiplier", 3.0))
    combined_multiplier = min(max_combined_multiplier, type_multiplier * signal_multiplier)

    retrieval_boost = 0.0
    last_accessed_dt = _coerce_datetime(last_accessed)
    if last_accessed_dt is not None:
        days_since_retrieved = max(0.0, (now_dt - last_accessed_dt).total_seconds() / 86400.0)
        retrieval_boost = 0.15 * (0.5 ** (days_since_retrieved / 60.0))

    updated_age_days = max(0.0, (now_dt - last_seen_dt).total_seconds() / 86400.0)
    # Phase 2: MULTIPLICATIVE continuous lever (see memory_policy.json note +
    # computeSalience in salience.ts — must stay in lockstep).
    richness = _compute_richness(entry_dict, metadata, policy, updated_age_days)
    richness_weight = _richness_weight(policy)
    base = confidence * decay * combined_multiplier * freq_boost

    raw = base * (1.0 + richness_weight * richness) + retrieval_boost
    score = round(min(1.0, raw), 4)

    # 3.5 — deprecated entries must rank below stale entries. Apply a fixed
    # post-factor penalty so deprecated < any active/stale score for the same
    # entry content. Must stay in lockstep with salience.ts.
    _DEPRECATED_PENALTY = 0.15
    if entry_dict.get("state") == "deprecated":
        score = round(score * _DEPRECATED_PENALTY, 4)

    return score


def _richness_weight(policy: dict[str, Any]) -> float:
    cfg = policy.get("salience_continuous")
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return 0.0
    return float(cfg.get("weight") or 0.0)


def _saturating(n: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    capped = max(0.0, min(float(n), float(cap)))
    return math.log1p(capped) / math.log1p(cap)


def _compute_richness(
    entry_dict: dict[str, Any],
    metadata: dict[str, Any],
    policy: dict[str, Any],
    updated_age_days: float = 0.0,
) -> float:
    """Phase 2 (PRD R2.3) richness scalar in [0,1]. Multiplicative lever
    applies _richness_weight(policy) * richness as a (1+...) factor. Must stay
    in lockstep with computeRichness in cloudflare-mcp/.../salience.ts.
    Constants live in memory_policy.json (salience_continuous)."""
    cfg = policy.get("salience_continuous")
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return 0.0
    components = cfg.get("components") or {}

    source_breadth = len({s for s in _coerce_string_list(metadata.get("source_conversations"))})
    key_insights = len(entry_dict.get("key_insights") or []) if isinstance(entry_dict.get("key_insights"), list) else 0
    related_links = (
        (len(entry_dict.get("related_knowledge") or []) if isinstance(entry_dict.get("related_knowledge"), list) else 0)
        + (len(entry_dict.get("related_repos") or []) if isinstance(entry_dict.get("related_repos"), list) else 0)
    )

    def part(name: str, value: float) -> float:
        c = components.get(name)
        if not isinstance(c, dict):
            return 0.0
        return float(c.get("weight") or 0.0) * _saturating(value, float(c.get("cap") or 1.0))

    recency_cfg = components.get("recency_tiebreaker")
    recency_part = 0.0
    if isinstance(recency_cfg, dict):
        w = float(recency_cfg.get("weight") or 0.0)
        hl = float(recency_cfg.get("half_life_days") or 180.0)
        recency_value = (0.5 ** (updated_age_days / hl)) if hl > 0 else 0.0
        recency_part = w * recency_value

    richness = (
        part("source_breadth", source_breadth)
        + part("key_insights", key_insights)
        + part("related_links", related_links)
        + recency_part
    )
    return max(0.0, min(1.0, richness))


def assign_tiers_by_percentile(
    salience_by_id: dict[str, float],
    context_type_by_id: dict[str, str | None],
    policy: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Phase 3 (PRD R3.1). Given salience + context_type for the active corpus,
    assign tiers by salience percentile: the top tier_1_top_pct -> Tier 1, the
    next tier_2_next_pct -> Tier 2, the remainder -> Tier 3. Entries whose
    context_type is in identity_floor_context_types never fall below Tier 2.

    Pure: takes precomputed salience, returns {id: tier}. Must stay in lockstep
    with assignTierByPercentile in salience.ts.
    """
    policy = policy or load_memory_policy()
    cfg = policy.get("tier_percentiles", {}) or {}
    top_pct = float(cfg.get("tier_1_top_pct", 0.15))
    next_pct = float(cfg.get("tier_2_next_pct", 0.25))
    floor_types = set(cfg.get("identity_floor_context_types", []) or [])

    ids = list(salience_by_id.keys())
    n = len(ids)
    if n == 0:
        return {}

    # Rank by salience descending; ties broken by id for determinism.
    ordered = sorted(ids, key=lambda i: (-salience_by_id.get(i, 0.0), i))
    tier1_cut = max(0, int(round(top_pct * n)))
    tier2_cut = tier1_cut + max(0, int(round(next_pct * n)))

    tiers: dict[str, int] = {}
    for rank, eid in enumerate(ordered):
        if rank < tier1_cut:
            tier = 1
        elif rank < tier2_cut:
            tier = 2
        else:
            tier = 3
        if (context_type_by_id.get(eid) in floor_types) and tier > 2:
            tier = 2  # identity floor: never below Tier 2
        tiers[eid] = tier
    return tiers


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
        if "entry" not in fixture:
            continue
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
