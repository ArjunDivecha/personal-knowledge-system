"""
Phase 7B offline temporal normalization and entity linking helpers.

This module enriches Phase 7A observations and compiled claims without touching
storage, retrieval, Dream, or ingestion write paths.
"""

from __future__ import annotations

import calendar
import hashlib
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .phase7 import (
    Phase7CompiledClaim,
    Phase7Observation,
)

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
MONTH_NAMES = {name.casefold() for name in MONTHS}
ENTITY_STOPWORDS = MONTH_NAMES | {
    "a",
    "an",
    "and",
    "as",
    "current",
    "future",
    "historical",
    "in",
    "next",
    "phase",
    "the",
    "this",
    "today",
    "tomorrow",
    "yesterday",
}
FUTURE_MARKERS = ("going to", "will", "planned", "upcoming", "next", "tomorrow")
HISTORICAL_MARKERS = ("completed", "finished", "shipped", "was", "previously")


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reference_date(*, observed_at: str | None = None, now_utc: str) -> date:
    return (_parse_datetime(observed_at) or _parse_datetime(now_utc) or datetime.now(timezone.utc)).date()


def _date_string(value: date) -> str:
    return value.isoformat()


@dataclass
class Phase7TemporalResolution:
    temporal_status: str
    valid_from: str | None = None
    valid_to: str | None = None
    matched_texts: list[str] = field(default_factory=list)
    resolution_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7TemporalResolution":
        return cls(
            temporal_status=data["temporal_status"],
            valid_from=data.get("valid_from"),
            valid_to=data.get("valid_to"),
            matched_texts=list(data.get("matched_texts") or []),
            resolution_notes=list(data.get("resolution_notes") or []),
        )


@dataclass
class Phase7EntityMention:
    entity_id: str
    canonical_name: str
    normalized_name: str
    entity_type: str
    source_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7EntityMention":
        return cls(
            entity_id=data["entity_id"],
            canonical_name=data["canonical_name"],
            normalized_name=data["normalized_name"],
            entity_type=data["entity_type"],
            source_text=data["source_text"],
        )


@dataclass
class Phase7EntityIndexEntry:
    entity_id: str
    canonical_name: str
    normalized_name: str
    entity_type: str
    aliases: list[str] = field(default_factory=list)
    source_observation_ids: list[str] = field(default_factory=list)
    source_claim_ids: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase7EntityIndexEntry":
        return cls(
            entity_id=data["entity_id"],
            canonical_name=data["canonical_name"],
            normalized_name=data["normalized_name"],
            entity_type=data["entity_type"],
            aliases=list(data.get("aliases") or []),
            source_observation_ids=list(data.get("source_observation_ids") or []),
            source_claim_ids=list(data.get("source_claim_ids") or []),
            source_paths=list(data.get("source_paths") or []),
        )


def normalize_entity_name(name: str) -> str:
    return " ".join(name.casefold().strip().split())


def stable_entity_id(name: str) -> str:
    normalized = normalize_entity_name(name)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"ent_{digest[:16]}"


def _append_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _entity_type_for(source_text: str, *, backticked: bool = False) -> str:
    if backticked:
        return "artifact"
    if source_text.isupper():
        return "acronym"
    return "concept"


def _add_mention(
    mentions: list[Phase7EntityMention],
    seen: set[str],
    text: str,
    *,
    backticked: bool = False,
) -> None:
    candidate = text.strip(" ,.;:()[]{}")
    if not candidate:
        return
    normalized = normalize_entity_name(candidate)
    if normalized in ENTITY_STOPWORDS or len(normalized) < 2:
        return
    entity_id = stable_entity_id(candidate)
    if entity_id in seen:
        return
    seen.add(entity_id)
    mentions.append(
        Phase7EntityMention(
            entity_id=entity_id,
            canonical_name=candidate,
            normalized_name=normalized,
            entity_type=_entity_type_for(candidate, backticked=backticked),
            source_text=candidate,
        )
    )


def extract_entity_mentions(text: str) -> list[Phase7EntityMention]:
    """Extract deterministic lightweight entity mentions from text."""
    mentions: list[Phase7EntityMention] = []
    seen: set[str] = set()

    for match in re.finditer(r"`([^`]+)`", text):
        _add_mention(mentions, seen, match.group(1), backticked=True)

    for match in re.finditer(r"\b[A-Z][A-Z0-9]{1,9}\b", text):
        _add_mention(mentions, seen, match.group(0))

    for match in re.finditer(r"\b[A-Z][a-z]+(?:\s+\d+[A-Z]?)?(?:\s+[A-Z][a-z]+){0,3}\b", text):
        _add_mention(mentions, seen, match.group(0))

    return mentions


def _month_window(month: int, year: int) -> tuple[str, str]:
    last_day = calendar.monthrange(year, month)[1]
    return _date_string(date(year, month, 1)), _date_string(date(year, month, last_day))


def _status_for_window(
    *,
    valid_from: str | None,
    valid_to: str | None,
    reference: date,
    text: str,
) -> str:
    lowered = text.casefold()
    if any(marker in lowered for marker in HISTORICAL_MARKERS):
        return "historical"
    if not valid_from and not valid_to:
        return "unknown"
    start = date.fromisoformat(valid_from or valid_to)
    end = date.fromisoformat(valid_to or valid_from)
    if end < reference:
        return "expired"
    if start > reference:
        return "future"
    return "current"


def normalize_temporal_text(
    text: str,
    *,
    observed_at: str | None = None,
    now_utc: str,
) -> Phase7TemporalResolution:
    reference = _reference_date(observed_at=observed_at, now_utc=now_utc)
    lowered = text.casefold()
    matched: list[str] = []
    notes: list[str] = []
    valid_from: str | None = None
    valid_to: str | None = None

    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if iso_match:
        matched.append(iso_match.group(1))
        valid_from = valid_to = iso_match.group(1)
        notes.append("resolved iso date")
    elif "tomorrow" in lowered:
        target = reference + timedelta(days=1)
        matched.append("tomorrow")
        valid_from = valid_to = _date_string(target)
        notes.append("resolved relative day")
    elif "yesterday" in lowered:
        target = reference - timedelta(days=1)
        matched.append("yesterday")
        valid_from = valid_to = _date_string(target)
        notes.append("resolved relative day")
    elif "today" in lowered:
        matched.append("today")
        valid_from = valid_to = _date_string(reference)
        notes.append("resolved relative day")
    else:
        month_match = re.search(
            r"\b(?:in\s+)?("
            + "|".join(MONTHS)
            + r")\s*(20\d{2})?\b",
            lowered,
        )
        if month_match:
            month_name = month_match.group(1)
            explicit_year = month_match.group(2)
            year = int(explicit_year) if explicit_year else reference.year
            if not explicit_year and MONTHS[month_name] < reference.month:
                year += 1
            valid_from, valid_to = _month_window(MONTHS[month_name], year)
            matched.append(month_match.group(0).strip())
            notes.append("resolved month window")

    if not matched:
        return Phase7TemporalResolution(
            temporal_status="unknown",
            resolution_notes=["no supported temporal expression"],
        )

    status = _status_for_window(
        valid_from=valid_from,
        valid_to=valid_to,
        reference=reference,
        text=text,
    )
    if status == "future" and not any(marker in lowered for marker in FUTURE_MARKERS):
        notes.append("future inferred from resolved date")

    return Phase7TemporalResolution(
        temporal_status=status,
        valid_from=valid_from,
        valid_to=valid_to,
        matched_texts=matched,
        resolution_notes=notes,
    )


def enrich_observation_temporal(
    observation: Phase7Observation,
    *,
    now_utc: str,
) -> Phase7Observation:
    resolution = normalize_temporal_text(
        observation.claim_text,
        observed_at=observation.observed_at,
        now_utc=now_utc,
    )
    if resolution.temporal_status == "unknown":
        return observation
    return replace(
        observation,
        valid_from=resolution.valid_from,
        valid_to=resolution.valid_to,
    )


def enrich_claim_temporal(
    claim: Phase7CompiledClaim,
    observations_by_id: dict[str, Phase7Observation],
    *,
    now_utc: str,
) -> Phase7CompiledClaim:
    resolution = normalize_temporal_text(claim.compiled_text, now_utc=now_utc)
    if resolution.temporal_status == "unknown":
        for observation_id in claim.support_observation_ids:
            observation = observations_by_id.get(observation_id)
            if observation and (observation.valid_from or observation.valid_to):
                resolution = Phase7TemporalResolution(
                    temporal_status=_status_for_window(
                        valid_from=observation.valid_from,
                        valid_to=observation.valid_to,
                        reference=_reference_date(now_utc=now_utc),
                        text=observation.claim_text,
                    ),
                    valid_from=observation.valid_from,
                    valid_to=observation.valid_to,
                    matched_texts=[],
                    resolution_notes=["copied from support observation"],
                )
                break

    if resolution.temporal_status == "unknown":
        return claim
    return replace(
        claim,
        temporal_status=resolution.temporal_status,
        valid_from=resolution.valid_from,
        valid_to=resolution.valid_to,
    )


def enrich_observations_phase7b(
    observations: list[Phase7Observation],
    *,
    now_utc: str,
) -> list[Phase7Observation]:
    enriched: list[Phase7Observation] = []
    for observation in observations:
        temporal = enrich_observation_temporal(observation, now_utc=now_utc)
        entity_ids = [mention.entity_id for mention in extract_entity_mentions(temporal.claim_text)]
        enriched.append(replace(temporal, entity_mentions=entity_ids))
    return enriched


def enrich_claims_phase7b(
    claims: list[Phase7CompiledClaim],
    observations: list[Phase7Observation],
    *,
    now_utc: str,
) -> list[Phase7CompiledClaim]:
    observations_by_id = {observation.observation_id: observation for observation in observations}
    return [
        enrich_claim_temporal(claim, observations_by_id, now_utc=now_utc)
        for claim in claims
    ]


def build_entity_index(
    observations: list[Phase7Observation],
    claims: list[Phase7CompiledClaim] | None = None,
) -> list[Phase7EntityIndexEntry]:
    index: dict[str, Phase7EntityIndexEntry] = {}

    def ensure_entry(mention: Phase7EntityMention) -> Phase7EntityIndexEntry:
        if mention.entity_id not in index:
            index[mention.entity_id] = Phase7EntityIndexEntry(
                entity_id=mention.entity_id,
                canonical_name=mention.canonical_name,
                normalized_name=mention.normalized_name,
                entity_type=mention.entity_type,
                aliases=[mention.canonical_name],
            )
        else:
            _append_unique(index[mention.entity_id].aliases, mention.canonical_name)
        return index[mention.entity_id]

    for observation in observations:
        for mention in extract_entity_mentions(observation.claim_text):
            entry = ensure_entry(mention)
            _append_unique(entry.source_observation_ids, observation.observation_id)
            _append_unique(entry.source_paths, observation.source_path)

    for claim in claims or []:
        for mention in extract_entity_mentions(claim.compiled_text):
            entry = ensure_entry(mention)
            _append_unique(entry.source_claim_ids, claim.claim_id)

    return sorted(index.values(), key=lambda item: item.normalized_name)


def evaluate_phase7b_temporal_probe(
    probe: dict[str, Any],
    *,
    now_utc: str | None = None,
) -> dict[str, Any]:
    effective_now = now_utc or probe["now_utc"]
    resolution = normalize_temporal_text(
        probe["text"],
        observed_at=probe.get("observed_at"),
        now_utc=effective_now,
    )
    failures: list[str] = []
    for field_name in ("temporal_status", "valid_from", "valid_to"):
        expected = probe.get(f"expected_{field_name}")
        actual = getattr(resolution, field_name)
        if expected is not None and actual != expected:
            failures.append(f"{field_name}: expected {expected}, got {actual}")
    return {
        "id": probe.get("id"),
        "passed": not failures,
        "failures": failures,
        "resolution": resolution.to_dict(),
    }
