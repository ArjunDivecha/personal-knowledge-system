"""
Phase 8 offline hybrid retrieval helpers.

This module proves the Phase 7 read path before live MCP search wiring changes.
It ranks over compiled current claims, unexpired provisional claims, memory
blocks, and observations when the query asks for evidence or history.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

from .phase7 import Phase7CompiledClaim, Phase7Observation
from .phase7b import enrich_observations_phase7b, extract_entity_mentions
from .phase7c import Phase7CurrentView, generate_compiled_current_view
from .phase7d import Phase7MemoryBlock, build_phase7d_memory_blocks

PHASE8_SCHEMA_VERSION = 1

Phase8QueryKind = Literal[
    "current_answer",
    "evidence_history",
    "point_in_time",
    "procedural_policy",
]
Phase8TemporalMode = Literal["current", "history", "point_in_time", "policy"]
Phase8CandidateSource = Literal[
    "current_claim",
    "provisional_claim",
    "observation",
    "memory_block",
]

QUERY_KINDS = {
    "current_answer",
    "evidence_history",
    "point_in_time",
    "procedural_policy",
}
TEMPORAL_MODES = {"current", "history", "point_in_time", "policy"}
CANDIDATE_SOURCES = {
    "current_claim",
    "provisional_claim",
    "observation",
    "memory_block",
}

STOPWORDS = {
    "a",
    "about",
    "and",
    "are",
    "as",
    "can",
    "did",
    "do",
    "does",
    "for",
    "from",
    "have",
    "how",
    "in",
    "is",
    "it",
    "now",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "this",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "why",
}

EVIDENCE_TERMS = {
    "audit",
    "auditable",
    "changed",
    "evidence",
    "history",
    "origin",
    "source",
    "sources",
    "support",
    "why",
}
POLICY_TERMS = {
    "agents.md",
    "guardrail",
    "policy",
    "procedure",
    "procedural",
    "rule",
    "rules",
}
POINT_IN_TIME_TERMS = {
    "as of",
    "back then",
    "previously",
    "used to",
    "was true",
}


def _validate_allowed(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")


def _validate_non_empty_string(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return _parse_datetime(value).date()


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(float(value), 1.0)), 6)


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


@dataclass
class Phase8QueryIntent:
    intent: Phase8QueryKind
    temporal_mode: Phase8TemporalMode
    matched_terms: list[str] = field(default_factory=list)
    as_of: str | None = None
    schema_version: int = PHASE8_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_allowed("intent", self.intent, QUERY_KINDS)
        _validate_allowed("temporal_mode", self.temporal_mode, TEMPORAL_MODES)
        if self.schema_version != PHASE8_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE8_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase8QueryIntent":
        return cls(
            intent=data["intent"],
            temporal_mode=data["temporal_mode"],
            matched_terms=list(data.get("matched_terms") or []),
            as_of=data.get("as_of"),
            schema_version=data.get("schema_version", PHASE8_SCHEMA_VERSION),
        )


@dataclass
class Phase8RetrievalCandidate:
    candidate_id: str
    source_type: Phase8CandidateSource
    text: str
    source_id: str
    source_label: str
    memory_lane: str
    status: str
    entity_ids: list[str] = field(default_factory=list)
    source_priority: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
    source_observation_ids: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)
    schema_version: int = PHASE8_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("candidate_id", "source_type", "text", "source_id", "source_label"):
            _validate_non_empty_string(name, getattr(self, name))
        _validate_allowed("source_type", self.source_type, CANDIDATE_SOURCES)
        self.source_priority = _clamp_score(self.source_priority)
        if self.schema_version != PHASE8_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE8_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase8RetrievalCandidate":
        return cls(
            candidate_id=data["candidate_id"],
            source_type=data["source_type"],
            text=data["text"],
            source_id=data["source_id"],
            source_label=data["source_label"],
            memory_lane=data["memory_lane"],
            status=data["status"],
            entity_ids=list(data.get("entity_ids") or []),
            source_priority=float(data.get("source_priority", 0.0)),
            metadata=dict(data.get("metadata") or {}),
            source_observation_ids=list(data.get("source_observation_ids") or []),
            source_paths=list(data.get("source_paths") or []),
            schema_version=data.get("schema_version", PHASE8_SCHEMA_VERSION),
        )


@dataclass
class Phase8RetrievalResult:
    candidate: Phase8RetrievalCandidate
    final_score: float
    lexical_score: float
    entity_score: float
    vector_score: float
    temporal_score: float
    lane_score: float
    source_priority_score: float
    reasons: list[str] = field(default_factory=list)
    schema_version: int = PHASE8_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.final_score = _clamp_score(self.final_score)
        self.lexical_score = _clamp_score(self.lexical_score)
        self.entity_score = _clamp_score(self.entity_score)
        self.vector_score = _clamp_score(self.vector_score)
        self.temporal_score = _clamp_score(self.temporal_score)
        self.lane_score = _clamp_score(self.lane_score)
        self.source_priority_score = _clamp_score(self.source_priority_score)
        if self.schema_version != PHASE8_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {PHASE8_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "final_score": self.final_score,
            "lexical_score": self.lexical_score,
            "entity_score": self.entity_score,
            "vector_score": self.vector_score,
            "temporal_score": self.temporal_score,
            "lane_score": self.lane_score,
            "source_priority_score": self.source_priority_score,
            "reasons": list(self.reasons),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase8RetrievalResult":
        return cls(
            candidate=Phase8RetrievalCandidate.from_dict(data["candidate"]),
            final_score=float(data["final_score"]),
            lexical_score=float(data["lexical_score"]),
            entity_score=float(data["entity_score"]),
            vector_score=float(data["vector_score"]),
            temporal_score=float(data["temporal_score"]),
            lane_score=float(data["lane_score"]),
            source_priority_score=float(data["source_priority_score"]),
            reasons=list(data.get("reasons") or []),
            schema_version=data.get("schema_version", PHASE8_SCHEMA_VERSION),
        )


@dataclass
class Phase8RetrievalReport:
    query: str
    intent: Phase8QueryIntent
    results: list[Phase8RetrievalResult]
    evaluated_at: str
    schema_version: int = PHASE8_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "intent": self.intent.to_dict(),
            "results": [result.to_dict() for result in self.results],
            "evaluated_at": self.evaluated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase8RetrievalReport":
        return cls(
            query=data["query"],
            intent=Phase8QueryIntent.from_dict(data["intent"]),
            results=[
                Phase8RetrievalResult.from_dict(item)
                for item in data.get("results", [])
            ],
            evaluated_at=data["evaluated_at"],
            schema_version=data.get("schema_version", PHASE8_SCHEMA_VERSION),
        )


@dataclass
class Phase8EvalCheck:
    check_id: str
    query: str
    passed: bool
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    schema_version: int = PHASE8_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase8EvalCheck":
        return cls(
            check_id=data["check_id"],
            query=data["query"],
            passed=bool(data["passed"]),
            expected=dict(data.get("expected") or {}),
            actual=dict(data.get("actual") or {}),
            issues=list(data.get("issues") or []),
            schema_version=data.get("schema_version", PHASE8_SCHEMA_VERSION),
        )


@dataclass
class Phase8EvalReport:
    generated_at: str
    passed: bool
    check_count: int
    failure_count: int
    checks: list[Phase8EvalCheck]
    retrieval_reports: list[Phase8RetrievalReport]
    schema_version: int = PHASE8_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "passed": self.passed,
            "check_count": self.check_count,
            "failure_count": self.failure_count,
            "checks": [check.to_dict() for check in self.checks],
            "retrieval_reports": [report.to_dict() for report in self.retrieval_reports],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Phase8EvalReport":
        return cls(
            generated_at=data["generated_at"],
            passed=bool(data["passed"]),
            check_count=int(data["check_count"]),
            failure_count=int(data["failure_count"]),
            checks=[
                Phase8EvalCheck.from_dict(item)
                for item in data.get("checks", [])
            ],
            retrieval_reports=[
                Phase8RetrievalReport.from_dict(item)
                for item in data.get("retrieval_reports", [])
            ],
            schema_version=data.get("schema_version", PHASE8_SCHEMA_VERSION),
        )


def classify_phase8_query(query: str) -> Phase8QueryIntent:
    lowered = query.casefold()
    matched_terms: list[str] = []
    as_of = _extract_as_of_date(lowered)

    if as_of or _contains_phrase(lowered, POINT_IN_TIME_TERMS):
        if as_of:
            matched_terms.append(f"as_of:{as_of}")
        matched_terms.extend(_matched_terms(lowered, POINT_IN_TIME_TERMS))
        return Phase8QueryIntent(
            intent="point_in_time",
            temporal_mode="point_in_time",
            matched_terms=_dedupe(matched_terms),
            as_of=as_of,
        )

    policy_matches = _matched_terms(lowered, POLICY_TERMS)
    if policy_matches:
        return Phase8QueryIntent(
            intent="procedural_policy",
            temporal_mode="policy",
            matched_terms=policy_matches,
        )

    evidence_matches = _matched_terms(lowered, EVIDENCE_TERMS)
    if evidence_matches:
        return Phase8QueryIntent(
            intent="evidence_history",
            temporal_mode="history",
            matched_terms=evidence_matches,
        )

    return Phase8QueryIntent(intent="current_answer", temporal_mode="current")


def build_phase8_candidates(
    current_view: Phase7CurrentView,
    observations: list[Phase7Observation],
    memory_blocks: list[Phase7MemoryBlock],
    *,
    now_utc: str,
    query_intent: Phase8QueryIntent | None = None,
) -> list[Phase8RetrievalCandidate]:
    intent = query_intent or Phase8QueryIntent(intent="current_answer", temporal_mode="current")
    observations_by_id = {observation.observation_id: observation for observation in observations}
    enriched_observations = enrich_observations_phase7b(observations, now_utc=now_utc)
    source_paths_by_observation_id = {
        observation.observation_id: observation.source_path
        for observation in observations
        if observation.source_path
    }
    candidates: list[Phase8RetrievalCandidate] = []

    for claim in current_view.current_claims:
        candidates.append(
            _candidate_from_claim(
                claim,
                source_type="current_claim",
                source_label="compiled_current_claim",
                source_priority=1.0,
                source_paths_by_observation_id=source_paths_by_observation_id,
            )
        )

    for claim in current_view.provisional_claims:
        candidates.append(
            _candidate_from_claim(
                claim,
                source_type="provisional_claim",
                source_label="provisional_claim",
                source_priority=0.78,
                source_paths_by_observation_id=source_paths_by_observation_id,
            )
        )

    for block in memory_blocks:
        candidates.append(_candidate_from_memory_block(block))

    if intent.intent in {"evidence_history", "point_in_time"}:
        for observation in enriched_observations:
            candidates.append(_candidate_from_observation(observation, observations_by_id))

    return sorted(candidates, key=lambda item: (item.source_type, item.candidate_id))


def retrieve_phase8(
    query: str,
    *,
    current_view: Phase7CurrentView,
    observations: list[Phase7Observation],
    memory_blocks: list[Phase7MemoryBlock],
    now_utc: str,
    vector_scores: dict[str, float] | None = None,
    limit: int = 5,
) -> Phase8RetrievalReport:
    intent = classify_phase8_query(query)
    candidates = build_phase8_candidates(
        current_view,
        observations,
        memory_blocks,
        now_utc=now_utc,
        query_intent=intent,
    )
    query_tokens = _tokenize(query)
    query_entities = _entity_ids_for_text(query)
    scored = [
        _score_candidate(
            query_tokens,
            query_entities,
            intent,
            candidate,
            vector_scores=dict(vector_scores or {}),
        )
        for candidate in candidates
    ]
    scored.sort(
        key=lambda item: (
            -item.final_score,
            -item.source_priority_score,
            -item.lexical_score,
            item.candidate.candidate_id,
        )
    )
    return Phase8RetrievalReport(
        query=query,
        intent=intent,
        results=scored[:limit],
        evaluated_at=now_utc,
    )


def evaluate_phase8_retrieval_probes(
    probes: list[dict[str, Any]],
    *,
    current_view: Phase7CurrentView,
    observations: list[Phase7Observation],
    memory_blocks: list[Phase7MemoryBlock],
    now_utc: str,
    vector_scores: dict[str, float] | None = None,
) -> Phase8EvalReport:
    checks: list[Phase8EvalCheck] = []
    reports: list[Phase8RetrievalReport] = []
    for probe in probes:
        report = retrieve_phase8(
            probe["query"],
            current_view=current_view,
            observations=observations,
            memory_blocks=memory_blocks,
            now_utc=now_utc,
            vector_scores=vector_scores,
            limit=int(probe.get("limit", probe.get("top_k", 5))),
        )
        reports.append(report)
        checks.append(_evaluate_probe(probe, report))

    failure_count = sum(1 for check in checks if not check.passed)
    return Phase8EvalReport(
        generated_at=now_utc,
        passed=failure_count == 0,
        check_count=len(checks),
        failure_count=failure_count,
        checks=checks,
        retrieval_reports=reports,
    )


def run_phase8_retrieval_fixture(fixture: dict[str, Any]) -> Phase8EvalReport:
    metadata = fixture.get("metadata") or {}
    now_utc = metadata["now_utc"]
    observations = [
        Phase7Observation.from_dict(item)
        for item in fixture.get("observations", [])
    ]
    claims = [
        Phase7CompiledClaim.from_dict(item)
        for item in fixture.get("claims", [])
    ]
    current_view = generate_compiled_current_view(
        observations,
        claims,
        now_utc=now_utc,
    )
    memory_blocks = build_phase7d_memory_blocks(
        current_view,
        observations,
        generated_at=now_utc,
        policy_source_paths=list(fixture.get("policy_source_paths") or []),
        project_scope_ref=fixture.get("project_scope_ref"),
    )
    return evaluate_phase8_retrieval_probes(
        list(fixture.get("probes") or []),
        current_view=current_view,
        observations=observations,
        memory_blocks=memory_blocks,
        now_utc=now_utc,
        vector_scores=dict(fixture.get("vector_scores") or {}),
    )


def _extract_as_of_date(lowered_query: str) -> str | None:
    match = re.search(
        r"\b(?:as\s+of|on|at)\s+(20\d{2}-\d{2}-\d{2})\b",
        lowered_query,
    )
    return match.group(1) if match else None


def _contains_phrase(text: str, phrases: set[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _matched_terms(text: str, terms: set[str]) -> list[str]:
    return sorted(term for term in terms if term in text)


def _tokenize(text: str) -> list[str]:
    raw_tokens = re.findall(r"[a-z0-9]+(?:[-_/.:][a-z0-9]+)*", text.casefold())
    tokens: list[str] = []
    for raw in raw_tokens:
        parts = [part for part in re.split(r"[-_/.:]+", raw) if part]
        for token in [raw, *parts]:
            if token not in STOPWORDS and len(token) > 1 and token not in tokens:
                tokens.append(token)
    return tokens


def _token_overlap(query_tokens: list[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    candidate_tokens = set(_tokenize(text))
    if not candidate_tokens:
        return 0.0
    matches = [token for token in query_tokens if token in candidate_tokens]
    return _clamp_score(len(matches) / len(query_tokens))


def _entity_ids_for_text(text: str, explicit_ids: list[str] | None = None) -> list[str]:
    ids = list(explicit_ids or [])
    ids.extend(mention.entity_id for mention in extract_entity_mentions(text))
    return _dedupe(ids)


def _entity_overlap(query_entities: list[str], candidate_entities: list[str]) -> float:
    if not query_entities:
        return 0.0
    candidate_set = set(candidate_entities)
    matches = [entity_id for entity_id in query_entities if entity_id in candidate_set]
    return _clamp_score(len(matches) / len(query_entities))


def _candidate_from_claim(
    claim: Phase7CompiledClaim,
    *,
    source_type: Phase8CandidateSource,
    source_label: str,
    source_priority: float,
    source_paths_by_observation_id: dict[str, str],
) -> Phase8RetrievalCandidate:
    return Phase8RetrievalCandidate(
        candidate_id=claim.claim_id,
        source_type=source_type,
        text=claim.compiled_text,
        source_id=claim.claim_id,
        source_label=source_label,
        memory_lane=claim.memory_lane,
        status=claim.status,
        entity_ids=_entity_ids_for_text(claim.compiled_text),
        source_priority=source_priority,
        metadata={
            "subject_id": claim.subject_id,
            "primary_source_authority": claim.primary_source_authority,
            "temporal_status": claim.temporal_status,
            "valid_from": claim.valid_from,
            "valid_to": claim.valid_to,
            "ttl_expires_at": claim.ttl_expires_at,
            "scope": dict(claim.scope),
        },
        source_observation_ids=list(claim.support_observation_ids),
        source_paths=_dedupe(
            [
                source_paths_by_observation_id[observation_id]
                for observation_id in claim.support_observation_ids
                if observation_id in source_paths_by_observation_id
            ]
        ),
    )


def _candidate_from_observation(
    observation: Phase7Observation,
    raw_observations_by_id: dict[str, Phase7Observation],
) -> Phase8RetrievalCandidate:
    raw_observation = raw_observations_by_id.get(observation.observation_id, observation)
    return Phase8RetrievalCandidate(
        candidate_id=observation.observation_id,
        source_type="observation",
        text=observation.claim_text,
        source_id=observation.source_id,
        source_label=observation.source_type,
        memory_lane=observation.memory_lane,
        status="observed",
        entity_ids=_entity_ids_for_text(
            observation.claim_text,
            explicit_ids=list(observation.entity_mentions or []),
        ),
        source_priority=0.56,
        metadata={
            "subject_id": observation.subject_id,
            "source_authority": observation.source_authority,
            "observed_at": observation.observed_at,
            "learned_at": observation.learned_at,
            "valid_from": observation.valid_from,
            "valid_to": observation.valid_to,
            "invalidated_at": observation.invalidated_at,
            "scope": dict(observation.scope),
        },
        source_observation_ids=[observation.observation_id],
        source_paths=[raw_observation.source_path] if raw_observation.source_path else [],
    )


def _candidate_from_memory_block(block: Phase7MemoryBlock) -> Phase8RetrievalCandidate:
    source_label = block.label
    memory_lane = "procedural" if block.label == "policy_pointer" else "semantic"
    source_priority = 0.92 if block.label == "policy_pointer" else 0.74
    return Phase8RetrievalCandidate(
        candidate_id=f"block:{block.label}:{block.scope_ref or block.scope}",
        source_type="memory_block",
        text=f"{block.description}\n{block.value}",
        source_id=block.block_id,
        source_label=source_label,
        memory_lane=memory_lane,
        status="current",
        entity_ids=_entity_ids_for_text(f"{block.description}\n{block.value}"),
        source_priority=source_priority,
        metadata={
            "block_id": block.block_id,
            "scope": block.scope,
            "scope_ref": block.scope_ref,
            "read_only": block.read_only,
            "chars_limit": block.chars_limit,
        },
        source_observation_ids=list(block.source_observation_ids),
        source_paths=list(block.source_paths),
    )


def _score_candidate(
    query_tokens: list[str],
    query_entities: list[str],
    intent: Phase8QueryIntent,
    candidate: Phase8RetrievalCandidate,
    *,
    vector_scores: dict[str, float],
) -> Phase8RetrievalResult:
    lexical_score = _token_overlap(query_tokens, candidate.text)
    entity_score = _entity_overlap(query_entities, candidate.entity_ids)
    vector_score = _vector_score_for(candidate, vector_scores)
    temporal_score = _temporal_score_for(intent, candidate)
    lane_score = _lane_score_for(intent, candidate)
    source_priority_score = _source_priority_for(intent, candidate)
    weights = _weights_for(intent)
    final_score = (
        weights["lexical"] * lexical_score
        + weights["entity"] * entity_score
        + weights["vector"] * vector_score
        + weights["temporal"] * temporal_score
        + weights["lane"] * lane_score
        + weights["source"] * source_priority_score
    )
    reasons = _score_reasons(
        candidate,
        intent,
        lexical_score=lexical_score,
        entity_score=entity_score,
        vector_score=vector_score,
        temporal_score=temporal_score,
        lane_score=lane_score,
        source_priority_score=source_priority_score,
    )
    return Phase8RetrievalResult(
        candidate=candidate,
        final_score=final_score,
        lexical_score=lexical_score,
        entity_score=entity_score,
        vector_score=vector_score,
        temporal_score=temporal_score,
        lane_score=lane_score,
        source_priority_score=source_priority_score,
        reasons=reasons,
    )


def _weights_for(intent: Phase8QueryIntent) -> dict[str, float]:
    if intent.intent == "point_in_time":
        return {
            "lexical": 0.25,
            "entity": 0.10,
            "vector": 0.10,
            "temporal": 0.30,
            "lane": 0.05,
            "source": 0.20,
        }
    if intent.intent == "evidence_history":
        return {
            "lexical": 0.30,
            "entity": 0.10,
            "vector": 0.15,
            "temporal": 0.15,
            "lane": 0.05,
            "source": 0.25,
        }
    if intent.intent == "procedural_policy":
        return {
            "lexical": 0.25,
            "entity": 0.10,
            "vector": 0.10,
            "temporal": 0.15,
            "lane": 0.15,
            "source": 0.25,
        }
    return {
        "lexical": 0.35,
        "entity": 0.15,
        "vector": 0.15,
        "temporal": 0.10,
        "lane": 0.05,
        "source": 0.20,
    }


def _vector_score_for(
    candidate: Phase8RetrievalCandidate,
    vector_scores: dict[str, float],
) -> float:
    for key in (candidate.candidate_id, candidate.source_id):
        if key in vector_scores:
            return _clamp_score(float(vector_scores[key]))
    return 0.0


def _temporal_score_for(
    intent: Phase8QueryIntent,
    candidate: Phase8RetrievalCandidate,
) -> float:
    temporal_status = str(candidate.metadata.get("temporal_status") or "unknown")
    if intent.intent == "current_answer":
        if candidate.source_type == "current_claim":
            return 1.0 if temporal_status not in {"expired", "historical"} else 0.05
        if candidate.source_type == "provisional_claim":
            return 0.82
        if candidate.source_type == "memory_block":
            return 0.70
        return 0.25

    if intent.intent == "evidence_history":
        if candidate.source_type == "observation":
            return 1.0
        if candidate.source_type == "current_claim":
            return 0.75
        if candidate.source_type == "provisional_claim":
            return 0.60
        return 0.45

    if intent.intent == "point_in_time":
        if _candidate_valid_at(candidate, intent.as_of):
            return 1.0
        if candidate.source_type == "observation":
            return 0.72
        if temporal_status in {"historical", "expired"}:
            return 0.65
        if candidate.source_type == "current_claim":
            return 0.42
        return 0.30

    if candidate.source_type == "memory_block" and candidate.source_label == "policy_pointer":
        return 1.0
    if candidate.source_type == "memory_block":
        return 0.65
    if candidate.memory_lane == "procedural":
        return 0.70
    return 0.38


def _candidate_valid_at(candidate: Phase8RetrievalCandidate, as_of: str | None) -> bool:
    if not as_of:
        return False
    target = _parse_date(as_of)
    if not target:
        return False
    valid_from = _parse_date(candidate.metadata.get("valid_from"))
    valid_to = _parse_date(candidate.metadata.get("valid_to"))
    if not valid_from and not valid_to:
        return False
    lower = valid_from or valid_to
    upper = valid_to or valid_from
    if not lower or not upper:
        return False
    return lower <= target <= upper


def _lane_score_for(
    intent: Phase8QueryIntent,
    candidate: Phase8RetrievalCandidate,
) -> float:
    if intent.intent == "procedural_policy":
        if candidate.memory_lane == "procedural":
            return 1.0
        if candidate.source_type == "memory_block":
            return 0.75
        return 0.45
    if intent.intent in {"evidence_history", "point_in_time"}:
        if candidate.source_type == "observation":
            return 0.90
        if candidate.memory_lane == "semantic":
            return 0.75
        return 0.50
    if candidate.memory_lane == "semantic":
        return 1.0
    if candidate.memory_lane == "procedural":
        return 0.60
    return 0.35


def _source_priority_for(
    intent: Phase8QueryIntent,
    candidate: Phase8RetrievalCandidate,
) -> float:
    if intent.intent == "current_answer":
        if candidate.source_type == "current_claim":
            return 1.0
        if candidate.source_type == "provisional_claim":
            return 0.85
        if candidate.source_type == "memory_block":
            return 0.70
        return 0.35
    if intent.intent in {"evidence_history", "point_in_time"}:
        if candidate.source_type == "observation":
            return 1.0
        if candidate.source_type == "current_claim":
            return 0.75
        if candidate.source_type == "provisional_claim":
            return 0.70
        return 0.55
    if candidate.source_type == "memory_block" and candidate.source_label == "policy_pointer":
        return 1.0
    if candidate.source_type == "memory_block":
        return 0.85
    if candidate.memory_lane == "procedural":
        return 0.65
    return 0.45


def _score_reasons(
    candidate: Phase8RetrievalCandidate,
    intent: Phase8QueryIntent,
    *,
    lexical_score: float,
    entity_score: float,
    vector_score: float,
    temporal_score: float,
    lane_score: float,
    source_priority_score: float,
) -> list[str]:
    reasons: list[str] = []
    if lexical_score > 0:
        reasons.append("lexical_match")
    if entity_score > 0:
        reasons.append("entity_match")
    if vector_score > 0:
        reasons.append("vector_score")
    if temporal_score >= 0.9:
        reasons.append(f"{intent.temporal_mode}_temporal_fit")
    if lane_score >= 0.9:
        reasons.append(f"{candidate.memory_lane}_lane_fit")
    if source_priority_score >= 0.85:
        reasons.append(_source_reason(candidate, intent))
    return _dedupe(reasons)


def _source_reason(
    candidate: Phase8RetrievalCandidate,
    intent: Phase8QueryIntent,
) -> str:
    if candidate.source_type == "current_claim":
        return "compiled_current_preferred"
    if candidate.source_type == "provisional_claim":
        return "provisional_allowed"
    if candidate.source_type == "observation":
        return "observation_for_history"
    if candidate.source_label == "policy_pointer":
        return "policy_pointer_preferred"
    return f"{intent.intent}_source_fit"


def _evaluate_probe(
    probe: dict[str, Any],
    report: Phase8RetrievalReport,
) -> Phase8EvalCheck:
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    issues: list[str] = []
    result_ids = [result.candidate.candidate_id for result in report.results]
    result_source_types = [result.candidate.source_type for result in report.results]
    result_labels = [result.candidate.source_label for result in report.results]

    expected_intent = probe.get("expected_intent")
    if expected_intent:
        expected["intent"] = expected_intent
        actual["intent"] = report.intent.intent
        if report.intent.intent != expected_intent:
            issues.append(f"intent mismatch: expected {expected_intent}, got {report.intent.intent}")

    expected_top = probe.get("expected_top_candidate_id")
    if expected_top:
        expected["top_candidate_id"] = expected_top
        actual["top_candidate_id"] = result_ids[0] if result_ids else None
        if not result_ids or result_ids[0] != expected_top:
            issues.append(f"top result mismatch: expected {expected_top}, got {actual['top_candidate_id']}")

    expected_ids = list(probe.get("expected_candidate_ids") or [])
    if expected_ids:
        expected["candidate_ids"] = expected_ids
        actual["candidate_ids"] = result_ids
        for candidate_id in expected_ids:
            if candidate_id not in result_ids:
                issues.append(f"missing candidate: {candidate_id}")

    excluded_ids = list(probe.get("excluded_candidate_ids") or [])
    if excluded_ids:
        expected["excluded_candidate_ids"] = excluded_ids
        actual["candidate_ids"] = result_ids
        for candidate_id in excluded_ids:
            if candidate_id in result_ids:
                issues.append(f"unexpected candidate: {candidate_id}")

    expected_sources = list(probe.get("expected_source_types") or [])
    if expected_sources:
        expected["source_types"] = expected_sources
        actual["source_types"] = result_source_types
        for source_type in expected_sources:
            if source_type not in result_source_types:
                issues.append(f"missing source type: {source_type}")

    expected_labels = list(probe.get("expected_labels") or [])
    if expected_labels:
        expected["labels"] = expected_labels
        actual["labels"] = result_labels
        for label in expected_labels:
            if label not in result_labels:
                issues.append(f"missing label: {label}")

    min_results = probe.get("min_results")
    if min_results is not None:
        expected["min_results"] = int(min_results)
        actual["result_count"] = len(result_ids)
        if len(result_ids) < int(min_results):
            issues.append(f"result count below minimum: expected {min_results}, got {len(result_ids)}")

    actual.setdefault("candidate_ids", result_ids)
    actual.setdefault("source_types", result_source_types)
    actual.setdefault("labels", result_labels)
    actual["scores"] = {
        result.candidate.candidate_id: result.final_score
        for result in report.results
    }
    return Phase8EvalCheck(
        check_id=probe["id"],
        query=report.query,
        passed=not issues,
        expected=expected,
        actual=actual,
        issues=issues,
    )
