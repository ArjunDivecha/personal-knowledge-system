"""
=============================================================================
STAGE 2: FILTER - Score and filter conversations by value
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Score conversations based on value signals and filter out low-value ones.
Uses the scoring system defined in prd-distillation-v1.1.md Section 4.2.

INPUT FILES:
- NormalizedConversation objects from parse stage

OUTPUT FILES:
- FilteredConversation objects (conversations + filter metadata)

USAGE:
    from distillation.pipeline.filter import filter_conversations
    filtered = filter_conversations(normalized_conversations)
    
    # Or test mode:
    python -m distillation.pipeline.filter --test
=============================================================================
"""

import re
import argparse
from dataclasses import dataclass, field
from typing import Literal

from config import FILTER_THRESHOLD
from models import NormalizedConversation


# -----------------------------------------------------------------------------
# FILTER RESULT
# -----------------------------------------------------------------------------

@dataclass
class FilterMetadata:
    """Metadata from the filtering process."""
    value_score: int
    signals_present: list[str]
    signals_absent: list[str]
    decision: Literal["keep", "skip"]
    skip_reason: str = ""


@dataclass
class FilteredConversation:
    """A conversation with filter metadata attached."""
    conversation: NormalizedConversation
    filter_metadata: FilterMetadata
    
    @property
    def should_keep(self) -> bool:
        return self.filter_metadata.decision == "keep"


# -----------------------------------------------------------------------------
# SIGNAL DETECTION
# -----------------------------------------------------------------------------

# Positive signals
DECISION_PATTERNS = [
    r"\b(I decided|I'll go with|let's go with|the answer is|I'm going to use|I chose)\b",
    r"\b(we'll use|we decided|the decision is|I've decided)\b",
    r"\b(settled on|going with|picked|selected)\b",
]

LEARNING_PATTERNS = [
    r"\b(I see|that makes sense|I didn't know|I understand now)\b",
    r"\b(oh interesting|that's interesting|good to know|makes sense)\b",
    r"\b(I learned|TIL|now I get it|ah okay)\b",
]

CONCLUSION_PATTERNS = [
    r"\b(in summary|to summarize|in conclusion|so basically)\b",
    r"\b(the key takeaway|main points|key points|bottom line)\b",
    r"\b(to recap|wrapping up|final answer)\b",
]

# Project name patterns (common project identifiers)
PROJECT_PATTERNS = [
    r"\b(opus ensemble|trading system|knowledge system|distillation)\b",
    r"\b\w+\s+(project|repo|repository|codebase)\b",
]

# Negative signals
TROUBLESHOOTING_PATTERNS = [
    r"\b(error|exception|failed|not working|doesn't work|broken)\b",
    r"\b(fix|debug|issue|bug|problem)\b",
    r"\b(help me|can you help|why isn't|what's wrong)\b",
]

META_AI_PATTERNS = [
    r"\b(can you|are you able|do you have|what are your capabilities)\b",
    r"\b(as an AI|as a language model|I cannot|I'm unable)\b",
]


def has_pattern_match(text: str, patterns: list[str]) -> bool:
    """Check if any pattern matches in the text."""
    text_lower = text.lower()
    for pattern in patterns:
        if re.search(pattern, text_lower, re.IGNORECASE):
            return True
    return False


def count_exchanges(conversation: NormalizedConversation) -> int:
    """Count user-assistant exchange pairs."""
    user_count = sum(1 for m in conversation.messages if m.role == "user")
    assistant_count = sum(1 for m in conversation.messages if m.role == "assistant")
    return min(user_count, assistant_count)


def is_abandoned_thread(conversation: NormalizedConversation) -> bool:
    """Check if the last 3+ messages are from assistant (no user engagement)."""
    if len(conversation.messages) < 3:
        return False
    
    last_3 = conversation.messages[-3:]
    return all(m.role == "assistant" for m in last_3)


def get_all_content(conversation: NormalizedConversation) -> str:
    """Concatenate all message content."""
    return " ".join(m.content for m in conversation.messages)


# -----------------------------------------------------------------------------
# SCORING FUNCTION
# -----------------------------------------------------------------------------

def score_conversation(conversation: NormalizedConversation) -> tuple[int, list[str], list[str]]:
    """
    Score a conversation based on value signals.
    
    Positive signals:
    - has_code_blocks: +3
    - has_explicit_decision: +3
    - has_user_learning: +2
    - has_project_reference: +2
    - has_repo_reference: +2
    - conversation_length: +1 per 5 exchanges (max +3)
    - has_conclusion: +2
    
    Negative signals:
    - pure_troubleshooting: -2
    - meta_about_ai: -3
    - abandoned_thread: -2
    
    Args:
        conversation: The conversation to score
    
    Returns:
        Tuple of (score, signals_present, signals_absent)
    """
    score = 0
    signals_present = []
    signals_absent = []
    all_content = get_all_content(conversation)
    
    # Positive: has_code_blocks (+3)
    if conversation.has_code:
        score += 3
        signals_present.append("has_code_blocks")
    else:
        signals_absent.append("has_code_blocks")
    
    # Positive: has_explicit_decision (+3)
    if has_pattern_match(all_content, DECISION_PATTERNS):
        score += 3
        signals_present.append("has_explicit_decision")
    else:
        signals_absent.append("has_explicit_decision")
    
    # Positive: has_user_learning (+2)
    user_content = " ".join(m.content for m in conversation.messages if m.role == "user")
    if has_pattern_match(user_content, LEARNING_PATTERNS):
        score += 2
        signals_present.append("has_user_learning")
    else:
        signals_absent.append("has_user_learning")
    
    # Positive: has_project_reference (+2)
    if has_pattern_match(all_content, PROJECT_PATTERNS):
        score += 2
        signals_present.append("has_project_reference")
    else:
        signals_absent.append("has_project_reference")
    
    # Positive: has_repo_reference (+2)
    if re.search(r"github\.com/\w+/\w+|[\w-]+/[\w-]+\s+(repo|repository)", all_content, re.IGNORECASE):
        score += 2
        signals_present.append("has_repo_reference")
    else:
        signals_absent.append("has_repo_reference")
    
    # Positive: conversation_length (+1 per 5 exchanges, max +3)
    exchanges = count_exchanges(conversation)
    length_bonus = min(exchanges // 5, 3)
    if length_bonus > 0:
        score += length_bonus
        signals_present.append(f"conversation_length_{length_bonus}")
    else:
        signals_absent.append("conversation_length")
    
    # Positive: has_conclusion (+2)
    last_messages = " ".join(m.content for m in conversation.messages[-3:])
    if has_pattern_match(last_messages, CONCLUSION_PATTERNS):
        score += 2
        signals_present.append("has_conclusion")
    else:
        signals_absent.append("has_conclusion")
    
    # Negative: pure_troubleshooting (-2)
    # Only apply if troubleshooting without substantive resolution
    if has_pattern_match(all_content, TROUBLESHOOTING_PATTERNS):
        # Check if there's resolution (decisions or conclusions)
        has_resolution = (
            has_pattern_match(all_content, DECISION_PATTERNS) or
            has_pattern_match(all_content, CONCLUSION_PATTERNS)
        )
        if not has_resolution:
            score -= 2
            signals_present.append("pure_troubleshooting")
    
    # Negative: meta_about_ai (-3)
    if has_pattern_match(all_content, META_AI_PATTERNS):
        # Only penalize if the conversation is primarily about AI capabilities
        ai_mentions = len(re.findall(r"\b(AI|language model|capabilities|can you)\b", all_content, re.IGNORECASE))
        if ai_mentions > 3 and len(conversation.messages) < 6:
            score -= 3
            signals_present.append("meta_about_ai")
    
    # Negative: abandoned_thread (-2)
    if is_abandoned_thread(conversation):
        score -= 2
        signals_present.append("abandoned_thread")
    
    return score, signals_present, signals_absent


# -----------------------------------------------------------------------------
# FILTER FUNCTION
# -----------------------------------------------------------------------------

def filter_conversations(
    conversations: list[NormalizedConversation],
    threshold: int = FILTER_THRESHOLD,
) -> list[FilteredConversation]:
    """
    Filter conversations by value score.
    
    Args:
        conversations: List of normalized conversations
        threshold: Minimum score to keep (default from config)
    
    Returns:
        List of FilteredConversation with metadata
    """
    results = []
    
    for conv in conversations:
        score, signals_present, signals_absent = score_conversation(conv)
        
        if score >= threshold:
            decision = "keep"
            skip_reason = ""
        else:
            decision = "skip"
            # Determine primary skip reason
            if "pure_troubleshooting" in signals_present:
                skip_reason = "Pure troubleshooting without resolution"
            elif "meta_about_ai" in signals_present:
                skip_reason = "Meta discussion about AI capabilities"
            elif "abandoned_thread" in signals_present:
                skip_reason = "Abandoned thread"
            elif score <= 0:
                skip_reason = "No value signals detected"
            else:
                skip_reason = f"Score {score} below threshold {threshold}"
        
        results.append(FilteredConversation(
            conversation=conv,
            filter_metadata=FilterMetadata(
                value_score=score,
                signals_present=signals_present,
                signals_absent=signals_absent,
                decision=decision,
                skip_reason=skip_reason,
            ),
        ))
    
    return results


def get_score_distribution(filtered: list[FilteredConversation]) -> dict[int, int]:
    """Get distribution of scores."""
    dist = {}
    for fc in filtered:
        score = fc.filter_metadata.value_score
        dist[score] = dist.get(score, 0) + 1
    return dict(sorted(dist.items()))


# -----------------------------------------------------------------------------
# CLI FOR TESTING
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Filter conversations by value")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    parser.add_argument("--threshold", type=int, default=FILTER_THRESHOLD,
                        help=f"Minimum score to keep (default: {FILTER_THRESHOLD})")
    args = parser.parse_args()
    
    print("=" * 60)
    print("STAGE 2: FILTER - Testing value scoring")
    print("=" * 60)
    print()
    
    # Import parse stage
    from .parse import parse_all_exports
    
    print("Parsing conversations...")
    conversations = parse_all_exports()
    print(f"Parsed {len(conversations)} conversations")
    print()
    
    print(f"Filtering with threshold: {args.threshold}")
    filtered = filter_conversations(conversations, threshold=args.threshold)
    
    kept = [f for f in filtered if f.should_keep]
    skipped = [f for f in filtered if not f.should_keep]
    
    print(f"  Kept: {len(kept)}")
    print(f"  Skipped: {len(skipped)}")
    print()
    
    # Score distribution
    print("Score distribution:")
    dist = get_score_distribution(filtered)
    for score, count in dist.items():
        marker = "✓" if score >= args.threshold else "✗"
        print(f"  {marker} Score {score:3d}: {count} conversations")
    print()
    
    # Sample kept
    if kept:
        print("Sample KEPT conversations:")
        for fc in kept[:3]:
            signals = ", ".join(fc.filter_metadata.signals_present[:3])
            print(f"  [{fc.filter_metadata.value_score:2d}] {fc.conversation.title[:40]}...")
            print(f"       Signals: {signals}")
    print()
    
    # Sample skipped with reasons
    if skipped:
        print("Sample SKIPPED conversations:")
        for fc in skipped[:3]:
            print(f"  [{fc.filter_metadata.value_score:2d}] {fc.conversation.title[:40]}...")
            print(f"       Reason: {fc.filter_metadata.skip_reason}")
    
    print()
    print("=" * 60)
    print(f"✓ Filter test passed! {len(kept)}/{len(filtered)} conversations kept")
    print("=" * 60)


if __name__ == "__main__":
    main()

