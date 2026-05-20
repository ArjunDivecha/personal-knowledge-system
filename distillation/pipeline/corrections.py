"""
Correction-event extraction and Dream contest hint generation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from models import Evidence, Insight, KnowledgeEntry, KnowledgeMetadata, NormalizedConversation, Position
from utils.embedding import get_embedding
from utils.llm import call_claude_json
from utils.salience import resolve_stored_tier
from utils.signal_flags import CORRECTION_DERIVED_FLAG, add_signal_flag

CORRECTION_CONFIDENCE_THRESHOLD = 0.7
CORRECTION_MATCH_THRESHOLD = 0.82
CORRECTION_CONTEST_HINT_PREFIX = "dream:contest_hint:"

JsonCall = Callable[..., tuple[Any, int, int]]
EmbeddingFn = Callable[[str], Any]


@dataclass
class CorrectionEvent:
    event_id: str
    conversation_id: str
    message_id: str
    corrected_belief: str
    new_belief: str
    confidence: float
    user_text: str
    source_timestamp: str


@dataclass
class CorrectionDetectionResult:
    events: list[CorrectionEvent] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class CorrectionContestHintResult:
    hints_created: int = 0
    candidates_checked: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)


def _stable_event_id(conversation_id: str, message_id: str, corrected_belief: str, new_belief: str) -> str:
    payload = f"{conversation_id}\n{message_id}\n{corrected_belief}\n{new_belief}".encode()
    return f"ce_{hashlib.sha256(payload).hexdigest()[:16]}"


def _truncate(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def build_correction_classifier_prompt(
    *,
    conversation: NormalizedConversation,
    user_message_index: int,
) -> str:
    user_message = conversation.messages[user_message_index]
    previous_messages = conversation.messages[max(0, user_message_index - 3):user_message_index]
    context_lines = [
        f"[{message.message_id}] {message.role.upper()}: {_truncate(message.content, 1600)}"
        for message in previous_messages
    ]

    return f"""You are classifying whether a user turn corrects an assistant's prior belief.

Return ONLY valid JSON matching this exact schema:
{{
  "is_correction": true,
  "corrected_belief": "the assistant's prior claim that the user is correcting, or null",
  "new_belief": "what the user is asserting instead, or null",
  "confidence": 0.0
}}

Rules:
- A correction must be the user explicitly telling the assistant that a prior claim, assumption, or memory is wrong, stale, incomplete, or pointed at the wrong scope.
- Do not mark ordinary clarifications, follow-up instructions, or new unrelated facts as corrections.
- corrected_belief and new_belief must be concise standalone claims.

Conversation context immediately before the user turn:
{chr(10).join(context_lines) if context_lines else "(none)"}

User turn to classify:
[{user_message.message_id}] USER: {_truncate(user_message.content, 3000)}
"""


def _coerce_confidence(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _event_from_classifier_payload(
    payload: Any,
    *,
    conversation: NormalizedConversation,
    user_message_index: int,
) -> CorrectionEvent | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("is_correction") is not True:
        return None

    corrected_belief = payload.get("corrected_belief")
    new_belief = payload.get("new_belief")
    confidence = _coerce_confidence(payload.get("confidence"))
    if not isinstance(corrected_belief, str) or not isinstance(new_belief, str):
        return None
    corrected_belief = corrected_belief.strip()
    new_belief = new_belief.strip()
    if not corrected_belief or not new_belief or confidence < CORRECTION_CONFIDENCE_THRESHOLD:
        return None

    user_message = conversation.messages[user_message_index]
    return CorrectionEvent(
        event_id=_stable_event_id(conversation.id, user_message.message_id, corrected_belief, new_belief),
        conversation_id=conversation.id,
        message_id=user_message.message_id,
        corrected_belief=corrected_belief,
        new_belief=new_belief,
        confidence=confidence,
        user_text=user_message.content,
        source_timestamp=user_message.created_at or conversation.updated_at,
    )


def detect_correction_events(
    conversation: NormalizedConversation,
    *,
    classifier: JsonCall = call_claude_json,
) -> CorrectionDetectionResult:
    result = CorrectionDetectionResult()

    for index, message in enumerate(conversation.messages):
        if message.role != "user":
            continue

        prompt = build_correction_classifier_prompt(
            conversation=conversation,
            user_message_index=index,
        )
        try:
            payload, input_tokens, output_tokens = classifier(
                prompt,
                max_tokens=700,
                temperature=0.0,
            )
            result.input_tokens += input_tokens
            result.output_tokens += output_tokens
        except Exception as exc:
            result.errors.append(f"Correction classifier failed for {message.message_id}: {exc}")
            continue

        event = _event_from_classifier_payload(
            payload,
            conversation=conversation,
            user_message_index=index,
        )
        if event:
            result.events.append(event)

    return result


def correction_event_to_entry(event: CorrectionEvent) -> KnowledgeEntry:
    created_at = event.source_timestamp or datetime.utcnow().isoformat()
    metadata = KnowledgeMetadata(
        created_at=created_at,
        updated_at=created_at,
        source_conversations=[event.conversation_id],
        source_messages=[event.message_id],
        signal_flags=[CORRECTION_DERIVED_FLAG],
        context_type="recurring_pattern",
        mention_count=1,
        first_seen=created_at,
        last_seen=created_at,
        auto_inferred=False,
        classification_status="complete",
    )
    evidence = Evidence(
        conversation_id=event.conversation_id,
        message_ids=[event.message_id],
        snippet=_truncate(event.user_text, 200),
    )
    return KnowledgeEntry(
        id=f"ke_{event.event_id.removeprefix('ce_')[:12]}",
        domain=_truncate(event.new_belief, 80),
        state="active",
        detail_level="full",
        current_view=event.new_belief,
        confidence="high" if event.confidence >= 0.85 else "medium",
        positions=[
            Position(
                view=event.new_belief,
                confidence="high" if event.confidence >= 0.85 else "medium",
                as_of=created_at,
                evidence=evidence,
            )
        ],
        key_insights=[
            Insight(
                insight=event.new_belief,
                evidence=evidence,
            )
        ],
        metadata=metadata,
    )


def build_correction_entries(events: list[CorrectionEvent]) -> list[KnowledgeEntry]:
    return [correction_event_to_entry(event) for event in events]


def build_contradiction_judge_prompt(
    *,
    prior_view: str,
    corrected_belief: str,
    new_belief: str,
) -> str:
    return f"""Decide whether an existing memory entry contradicts a user correction.

Return ONLY valid JSON matching this schema:
{{
  "contradicts": true,
  "confidence": 0.0,
  "reason": "short reason"
}}

Existing memory current_view:
{_truncate(prior_view, 1600)}

Corrected belief described by classifier:
{_truncate(corrected_belief, 800)}

User's new belief:
{_truncate(new_belief, 800)}
"""


def _judge_says_contradicts(payload: Any) -> tuple[bool, float, str]:
    if not isinstance(payload, dict):
        return False, 0.0, "judge returned non-object payload"
    confidence = _coerce_confidence(payload.get("confidence"))
    reason = payload.get("reason")
    return (
        payload.get("contradicts") is True and confidence >= CORRECTION_CONFIDENCE_THRESHOLD,
        confidence,
        reason.strip() if isinstance(reason, str) and reason.strip() else "correction contradicts prior memory",
    )


def propose_correction_contest_hints(
    *,
    events: list[CorrectionEvent],
    redis_client: Any,
    vector_client: Any,
    judge: JsonCall = call_claude_json,
    embedding_fn: EmbeddingFn = get_embedding,
) -> CorrectionContestHintResult:
    result = CorrectionContestHintResult()
    if not events:
        return result

    for event in events:
        try:
            embedding_result = embedding_fn(event.corrected_belief)
            embedding = embedding_result[0] if isinstance(embedding_result, tuple) else embedding_result
            matches = vector_client.search_by_text(
                embedding,
                top_k=8,
                entry_type="knowledge",
                min_score=CORRECTION_MATCH_THRESHOLD,
            )
        except Exception as exc:
            result.errors.append(f"Correction semantic search failed for {event.event_id}: {exc}")
            continue

        for match in matches:
            result.candidates_checked += 1
            entry_id = match.get("id")
            if not isinstance(entry_id, str):
                continue
            entry = redis_client.get_knowledge_entry(entry_id)
            if not entry or entry.state != "active" or not entry.metadata:
                continue
            if bool(entry.metadata.archived):
                continue
            if resolve_stored_tier(entry) not in (1, 2):
                continue

            prompt = build_contradiction_judge_prompt(
                prior_view=entry.current_view,
                corrected_belief=event.corrected_belief,
                new_belief=event.new_belief,
            )
            try:
                payload, input_tokens, output_tokens = judge(
                    prompt,
                    max_tokens=600,
                    temperature=0.0,
                )
                result.input_tokens += input_tokens
                result.output_tokens += output_tokens
            except Exception as exc:
                result.errors.append(f"Correction judge failed for {event.event_id}/{entry_id}: {exc}")
                continue

            contradicts, judge_confidence, reason = _judge_says_contradicts(payload)
            if not contradicts:
                continue

            hint = {
                "schema_version": 1,
                "proposal_kind": "contest",
                "source": "correction_event",
                "status": "pending",
                "event_id": event.event_id,
                "created_at": datetime.utcnow().isoformat(),
                "conversation_id": event.conversation_id,
                "message_id": event.message_id,
                "target_entry_id": entry.id,
                "target_entry_type": "knowledge",
                "corrected_belief": event.corrected_belief,
                "new_belief": event.new_belief,
                "correction_confidence": event.confidence,
                "judge_confidence": judge_confidence,
                "reason": reason,
                "similarity": match.get("score"),
            }
            key = f"{CORRECTION_CONTEST_HINT_PREFIX}{event.event_id}:{entry.id}"
            redis_client.set(key, json.dumps(hint))
            result.hints_created += 1

    return result
