"""
=============================================================================
SALIENCE V2 — five-component additive score (shadow phase, Python twin)
=============================================================================
Version: 1.0.0
Last Updated: 2026-07-10

PURPOSE:
Implements salience_v2 for contract PKS-INJECTION-RANKING-002
(/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/injection-ranking-v2.spec.md),
Phase A (shadow). Replaces v1's multiplicative-clamp scalar (utils/salience.py's
compute_salience) with a weighted sum of five components, each in [0,1],
persisted individually so every ranking decision is inspectable:

    salience_v2 = 0.30*usage + 0.25*evidence + 0.20*recency
                + 0.15*authority + 0.10*corroboration

This module is the Python half of a lockstep pair. The TypeScript twin is
cloudflare-mcp/mcp-server/src/salience_v2.ts and MUST stay semantically
identical; the shared fixture table shared/salience_v2_fixtures.json is the
proof both implementations agree (evaluate_salience_v2_fixtures() here,
replayed by test/salience_v2.test.ts on the Worker side).

Shadow phase only: this module computes and returns values. It does not
write to Redis, does not touch v1's salience_score, and is not consulted by
any live ranking or tiering path until the RANKING_V2 flag is on (Phase B,
out of scope for this module).

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
    (read via utils.salience.load_memory_policy; the "salience_v2" block)
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/salience_v2_fixtures.json
    (read by evaluate_salience_v2_fixtures)

OUTPUT FILES:
- None (pure logic; returns values, writes nothing)

DEPENDENCIES:
- Python 3.14 stdlib only (datetime, math, json via utils.salience)
- utils.salience.load_memory_policy (existing policy loader — reused, not
  reimplemented)
- utils.precedence (AUTHORITY_RANK, authority_of, authority_rollup — the
  precedence lattice from contract PKS-CONTRADICTION-LIFECYCLE-001; reused
  for the authority component rather than reimplementing authority logic)

USAGE:
    from utils.salience_v2 import compute_salience_v2
    score, components = compute_salience_v2(entry_dict, now=datetime.now(timezone.utc))

DESIGN DECISION — distinct_days_seen (documented per contract instruction):
No per-observation-day field exists anywhere on an entry. distinct_days_seen
is computed as the count of unique calendar dates (YYYY-MM-DD, UTC) found
across metadata.first_seen, metadata.last_seen, and every timestamp embedded
in metadata.consolidation_notes entries. This is a deliberate LOWER-BOUND
PROXY for "how many distinct days this entry was meaningfully touched", not
a precise count — an entry seen 10 times in one day still counts once, and
an entry with no consolidation history at all still gets credit for its
first_seen/last_seen span. NOTE: the consolidation-note timestamp format was
verified against the actual formatter
(cloudflare-mcp/mcp-server/src/consolidation.ts's formatConsolidationNote) to
be "<iso-timestamp> | source=... | action=... | detail=..." — a plain
leading ISO timestamp followed by " | ", NOT the "[iso-timestamp] ..."
bracket format an earlier draft of this spec assumed. The parser below
splits on the first " | " and treats the leading segment as the timestamp
candidate, matching the real format.
=============================================================================
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from utils.precedence import AUTHORITY_RANK, authority_of, authority_rollup
from utils.salience import load_memory_policy

# 3.5 (mirrored from utils/salience.py's compute_salience) — when a required
# timestamp is absent, fall back to a conservative 90-day-old value rather
# than "now", so missing-timestamp entries don't read as perpetually fresh.
# Must stay in lockstep with salience_v2.ts.
_MISSING_TIMESTAMP_FALLBACK_DAYS = 90.0

_MAX_AUTHORITY_RANK = max(AUTHORITY_RANK.values())  # 4


def _extract_entry_dict(entry: Any) -> dict[str, Any]:
    """Duplicated (not imported) from utils.salience by the same convention
    precedence.py uses for _coerce_datetime: small, load-bearing helpers are
    mirrored per-module rather than cross-imported as private API."""
    if hasattr(entry, "to_dict"):
        return entry.to_dict()
    if isinstance(entry, dict):
        return entry
    raise TypeError(f"Unsupported entry type for salience_v2 scoring: {type(entry)!r}")


def _coerce_datetime(value: Any) -> datetime | None:
    """Mirrors the coercion in utils.salience / utils.precedence so all three
    modules parse timestamps identically."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _sat(x: float, cap: float) -> float:
    """sat(x, cap) = min(x, cap) / cap. Linear saturation (deliberately NOT
    the log1p saturation utils.salience.py uses for v1's richness lever —
    salience_v2's evidence/corroboration components use a plain linear ramp
    per the locked contract formula)."""
    if cap <= 0:
        return 0.0
    return max(0.0, min(float(x), float(cap))) / float(cap)


def _distinct_days_seen(metadata: dict[str, Any]) -> int:
    days: set[str] = set()
    for key in ("first_seen", "last_seen"):
        dt = _coerce_datetime(metadata.get(key))
        if dt is not None:
            days.add(dt.date().isoformat())
    for note in _coerce_string_list(metadata.get("consolidation_notes")):
        timestamp_candidate = note.split(" | ", 1)[0].strip()
        dt = _coerce_datetime(timestamp_candidate)
        if dt is not None:
            days.add(dt.date().isoformat())
    return len(days)


def _effective_last_seen(metadata: dict[str, Any], now_dt: datetime) -> datetime:
    last_seen_raw = metadata.get("last_seen") or metadata.get("updated_at")
    return _coerce_datetime(last_seen_raw) or (
        now_dt - timedelta(days=_MISSING_TIMESTAMP_FALLBACK_DAYS)
    )


def compute_salience_v2(entry: Any, now: datetime | None = None) -> tuple[float, dict[str, float]]:
    """Compute salience_v2 and its five persisted components.

    Returns (score, components) where score is the rounded (4-decimal)
    weighted sum and components is {usage, evidence, recency, authority,
    corroboration}, each individually clamped to [0,1] and rounded to 4
    decimals for stored consistency with the top-level score. The score
    itself is computed from the unrounded (but still clamped) component
    values, then rounded once at the end.
    """
    policy = load_memory_policy()
    cfg = policy.get("salience_v2")
    if not isinstance(cfg, dict):
        raise ValueError("shared/memory_policy.json is missing the required 'salience_v2' block")

    entry_dict = _extract_entry_dict(entry)
    metadata = entry_dict.get("metadata") or {}
    now_dt = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)

    weights = cfg["weights"]
    usage_half_life_days = float(cfg["usage_half_life_days"])
    recency_half_lives = cfg["recency_half_lives_days"]
    evidence_sat_caps = cfg["evidence_saturation"]
    evidence_component_weights = cfg["evidence_component_weights"]
    corroboration_cap = float(cfg["corroboration_saturation_mention_count"])

    # --- usage: 0.5^(days_since_last_accessed / usage_half_life), 0 if never accessed ---
    last_accessed_dt = _coerce_datetime(metadata.get("last_accessed"))
    if last_accessed_dt is not None:
        days_since_access = max(0.0, (now_dt - last_accessed_dt).total_seconds() / 86400.0)
        usage = 0.5 ** (days_since_access / usage_half_life_days)
    else:
        usage = 0.0

    # --- evidence: saturating blend of source breadth, key insights, distinct days seen ---
    n_source_conversations = len(set(_coerce_string_list(metadata.get("source_conversations"))))
    key_insights_raw = entry_dict.get("key_insights")
    n_key_insights = len(key_insights_raw) if isinstance(key_insights_raw, list) else 0
    distinct_days = _distinct_days_seen(metadata)
    evidence = (
        _sat(n_source_conversations, evidence_sat_caps["source_conversations"])
        * evidence_component_weights["source_conversations"]
        + _sat(n_key_insights, evidence_sat_caps["key_insights"])
        * evidence_component_weights["key_insights"]
        + _sat(distinct_days, evidence_sat_caps["distinct_days_seen"])
        * evidence_component_weights["distinct_days_seen"]
    )

    # --- recency: 0.5^(days_since_last_seen / half_life[context_type]); "infinity" -> 1.0 ---
    context_type = metadata.get("context_type") or "task_query"
    half_life_raw = recency_half_lives.get(context_type, recency_half_lives["task_query"])
    if half_life_raw == "infinity":
        recency = 1.0
    else:
        last_seen_dt = _effective_last_seen(metadata, now_dt)
        days_since_seen = max(0.0, (now_dt - last_seen_dt).total_seconds() / 86400.0)
        recency = 0.5 ** (days_since_seen / float(half_life_raw))

    # --- authority: authority_of(asserted_by, behavioral=False) / max rank (4) ---
    asserted_by, _assertion_kind = authority_rollup(entry_dict)
    authority = authority_of(asserted_by, False) / _MAX_AUTHORITY_RANK

    # --- corroboration: sat(mention_count, cap) ---
    mention_count = max(0, int(metadata.get("mention_count") or 0))
    corroboration = _sat(mention_count, corroboration_cap)

    usage_c, evidence_c, recency_c, authority_c, corroboration_c = (
        _clamp01(usage),
        _clamp01(evidence),
        _clamp01(recency),
        _clamp01(authority),
        _clamp01(corroboration),
    )

    score = round(
        usage_c * weights["usage"]
        + evidence_c * weights["evidence"]
        + recency_c * weights["recency"]
        + authority_c * weights["authority"]
        + corroboration_c * weights["corroboration"],
        4,
    )
    components = {
        "usage": round(usage_c, 4),
        "evidence": round(evidence_c, 4),
        "recency": round(recency_c, 4),
        "authority": round(authority_c, 4),
        "corroboration": round(corroboration_c, 4),
    }
    return score, components


def evidence_count_of(entry_dict: dict[str, Any]) -> int:
    """evidence_count = len(key_insights) + len(positions) + len(knows_how_to),
    used only by tiebreak_key (INV3)."""
    total = 0
    for block in ("key_insights", "positions", "knows_how_to"):
        items = entry_dict.get(block)
        if isinstance(items, list):
            total += len(items)
    return total


def tiebreak_key(entry: Any, salience_v2_score: float) -> tuple[float, float, int, str]:
    """Sort key implementing INV3's tiebreak order:
    (salience_v2 desc, last_seen desc, evidence_count desc, id asc).

    Returns a tuple oriented for a single ascending sort (sorted(..., key=...)
    with no reverse=): the three "desc" fields are negated so ascending order
    reproduces descending semantics, while id is left as-is for ascending
    order. Entries with no parseable last_seen/updated_at sort as oldest
    (epoch 0.0) rather than "now" — unlike compute_salience_v2's decay
    fallback, this is a pure ordering key and must not depend on wall-clock
    time at call time (a "now"-based fallback here would make the same two
    entries compare differently on different sort() invocations).
    """
    entry_dict = _extract_entry_dict(entry)
    metadata = entry_dict.get("metadata") or {}
    last_seen_raw = metadata.get("last_seen") or metadata.get("updated_at")
    last_seen_dt = _coerce_datetime(last_seen_raw)
    last_seen_epoch = last_seen_dt.timestamp() if last_seen_dt is not None else 0.0
    evidence_count = evidence_count_of(entry_dict)
    entry_id = str(entry_dict.get("id") or "")
    return (-float(salience_v2_score), -last_seen_epoch, -evidence_count, entry_id)


def evaluate_salience_v2_fixtures() -> list[dict[str, Any]]:
    """Run the shared fixture table through compute_salience_v2 and report
    per-case {name, expected, actual}. Consumed by the unit test and the
    Worker vitest twin (which reads the same JSON)."""
    import json
    from pathlib import Path

    fixtures_path = Path(__file__).resolve().parents[2] / "shared" / "salience_v2_fixtures.json"
    with fixtures_path.open() as handle:
        fixtures = json.load(handle)

    results = []
    for fixture in fixtures:
        now = _coerce_datetime(fixture.get("now"))
        score, components = compute_salience_v2(fixture["entry"], now=now)
        results.append(
            {
                "name": fixture["name"],
                "expected": fixture["expected"],
                "actual": {"salience_v2": score, "components": components},
            }
        )
    return results
