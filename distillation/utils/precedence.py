"""
=============================================================================
SHARED PRECEDENCE LATTICE — authority-then-durability claim comparator
=============================================================================
Version: 1.0.0
Last Updated: 2026-07-10

PURPOSE:
Pure, dependency-free precedence logic for the contradiction lifecycle
(contract PKS-CONTRADICTION-LIFECYCLE-001). Decides which of two conflicting
claims wins, using an authority-then-durability lattice and recency only as a
final tiebreak — never naive recency. A user assertion always outranks an
assistant assertion regardless of date; a user-stated claim versus a
behavioral (repeated-observation) claim is never auto-resolved and returns
"escalate".

This module is the Python half of a lockstep pair. The TypeScript twin is
cloudflare-mcp/mcp-server/src/precedence.ts and MUST stay semantically
identical; the shared fixture table shared/precedence_fixtures.json is the
proof that both implementations agree.

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/precedence_fixtures.json
    (labeled claim-pair cases; read by evaluate_precedence_fixtures)

OUTPUT FILES:
- None (pure logic; returns values, writes nothing)

USAGE:
    from utils.precedence import compare_claims, derive_asserted_by, authority_rollup
    verdict = compare_claims(claim_a, claim_b)  # "a_wins" | "b_wins" | "escalate"
=============================================================================
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_PATH = REPO_ROOT / "shared" / "precedence_fixtures.json"

_FIXTURES_CACHE: list[dict[str, Any]] | None = None


# Authority ranks (higher wins). user = arjun_explicit; behavioral = repeated
# observed behavior; assistant = assistant-asserted; inferred = extractor
# generalization / unknown provenance.
AUTHORITY_RANK = {"user": 4, "behavioral": 3, "assistant": 2, "inferred": 1}

# Durability ranks (higher wins) within equal authority.
DURABILITY_RANK = {
    "decision": 4,
    "correction": 4,
    "preference": 3,
    "fact": 2,
    "hypothesis": 1,
}


def load_precedence_fixtures() -> list[dict[str, Any]]:
    global _FIXTURES_CACHE
    if _FIXTURES_CACHE is None:
        with FIXTURES_PATH.open() as handle:
            _FIXTURES_CACHE = json.load(handle)
    return _FIXTURES_CACHE


def _coerce_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 as_of into a tz-aware UTC datetime, or None. Mirrors
    the salience.py coercion so the two modules parse timestamps identically."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def derive_asserted_by(
    message_ids: list[str],
    roles_by_id: Optional[dict[str, str]],
) -> str:
    """Derive the asserted_by provenance from the roles of the cited messages.

    - roles_by_id None or message_ids empty -> "inferred"
    - any cited message whose role == "user" -> "user"
    - any cited message id missing from roles_by_id, OR present with a role
      that is neither "user" nor "assistant" (e.g. "system", "tool") ->
      "inferred" (never invent an authority level for an unrecognized role)
    - otherwise (all cited messages found and role == "assistant") -> "assistant"
    """
    if not roles_by_id or not message_ids:
        return "inferred"
    saw_non_assistant = False
    for message_id in message_ids:
        role = roles_by_id.get(message_id)
        if role == "user":
            return "user"
        if role != "assistant":
            saw_non_assistant = True
    if saw_non_assistant:
        return "inferred"
    return "assistant"


def authority_of(asserted_by: str | None, behavioral: bool) -> int:
    """Authority rank for a claim. None -> inferred. behavioral=True upgrades
    ONLY an inferred claim to the behavioral rank (3); it never downgrades a
    user or assistant claim."""
    key = asserted_by or "inferred"
    rank = AUTHORITY_RANK.get(key, AUTHORITY_RANK["inferred"])
    if behavioral and key == "inferred":
        return AUTHORITY_RANK["behavioral"]
    return rank


def _durability_of(assertion_kind: str | None) -> int:
    key = assertion_kind or "hypothesis"
    return DURABILITY_RANK.get(key, DURABILITY_RANK["hypothesis"])


def compare_claims(a: dict, b: dict) -> str:
    """Compare two conflicting claims and return "a_wins" | "b_wins" |
    "escalate". Each claim dict has keys: asserted_by (str|None),
    assertion_kind (str|None), behavioral (bool), as_of (iso-string|None).

    Rules, in order:
      1. Compute authority ranks. If they are {4, 3} (user-stated vs behavioral,
         either order) -> escalate; the behavioral-vs-stated case is never
         auto-resolved.
      2. Unequal authority -> higher wins (regardless of recency).
      3. Equal authority -> higher durability rank wins.
      4. Equal durability -> as_of recency: newer wins; a present as_of beats a
         missing one; both missing -> escalate.
      5. Fully tied -> escalate.
    """
    a_auth = authority_of(a.get("asserted_by"), bool(a.get("behavioral")))
    b_auth = authority_of(b.get("asserted_by"), bool(b.get("behavioral")))

    # Rule 1: user-stated (4) vs behavioral (3) is never auto-resolved.
    if {a_auth, b_auth} == {4, 3}:
        return "escalate"

    # Rule 2: unequal authority -> higher wins, recency irrelevant.
    if a_auth != b_auth:
        return "a_wins" if a_auth > b_auth else "b_wins"

    # Rule 3: equal authority -> durability decides.
    a_dur = _durability_of(a.get("assertion_kind"))
    b_dur = _durability_of(b.get("assertion_kind"))
    if a_dur != b_dur:
        return "a_wins" if a_dur > b_dur else "b_wins"

    # Rule 4: recency as final tiebreak.
    a_date = _coerce_datetime(a.get("as_of"))
    b_date = _coerce_datetime(b.get("as_of"))
    if a_date is not None and b_date is not None:
        if a_date > b_date:
            return "a_wins"
        if b_date > a_date:
            return "b_wins"
        return "escalate"  # identical timestamps -> fully tied
    if a_date is not None:
        return "a_wins"  # present as_of beats missing
    if b_date is not None:
        return "b_wins"
    return "escalate"  # Rule 5: both missing / fully tied


def authority_rollup(entry: dict) -> tuple[str, str]:
    """Entry-level provenance rollup: the (asserted_by, assertion_kind) of the
    single strongest evidence reachable in the entry, where strongest is the
    max by (authority_of(asserted_by, behavioral=False), durability rank).

    Walks positions[].evidence, key_insights[].evidence, knows_how_to[].evidence
    and open_questions[].evidence (when present). Empty / no evidence returns
    ("inferred", "hypothesis"). Missing asserted_by/assertion_kind on the winning
    evidence are normalized to inferred/hypothesis."""
    best_key: tuple[int, int] | None = None
    best_pair = ("inferred", "hypothesis")

    for block in ("positions", "key_insights", "knows_how_to", "open_questions"):
        items = entry.get(block)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            evidence = item.get("evidence")
            if not isinstance(evidence, dict):
                continue
            asserted_by = evidence.get("asserted_by")
            assertion_kind = evidence.get("assertion_kind")
            rank = (
                authority_of(asserted_by, False),
                _durability_of(assertion_kind),
            )
            if best_key is None or rank > best_key:
                best_key = rank
                best_pair = (asserted_by or "inferred", assertion_kind or "hypothesis")

    return best_pair


def evaluate_precedence_fixtures() -> list[dict[str, Any]]:
    """Run the shared fixture table through compare_claims and report per-case
    {name, expected, actual, ok}. Consumed by the lattice unit test and the
    Worker vitest twin (which reads the same JSON)."""
    results = []
    for fixture in load_precedence_fixtures():
        actual = compare_claims(fixture["a"], fixture["b"])
        results.append(
            {
                "name": fixture["name"],
                "expected": fixture["expected"],
                "actual": actual,
                "ok": actual == fixture["expected"],
            }
        )
    return results
