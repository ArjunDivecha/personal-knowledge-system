"""
=============================================================================
STAGE 3: EXTRACT - LLM extraction with evidence validation
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Extract knowledge and project entries from filtered conversations using
Claude API. Validates that all insights have proper evidence.

INPUT FILES:
- FilteredConversation objects from filter stage

OUTPUT FILES:
- CandidateEntry objects (KnowledgeEntry, ProjectEntry candidates)

USAGE:
    from distillation.pipeline.extract import extract_entries
    entries = extract_entries(filtered_conversations)
    
    # Or test mode:
    python -m distillation.pipeline.extract --test --limit 3
=============================================================================
"""

import json
import uuid
import argparse
from datetime import datetime
from dataclasses import dataclass
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import MAX_CONVERSATION_TOKENS, CHUNK_OVERLAP_TOKENS, MAX_EXTRACTION_WORKERS
from models import (
    KnowledgeEntry,
    ProjectEntry,
    Evidence,
    Insight,
    Capability,
    OpenQuestion,
    Decision,
    Position,
    KnowledgeMetadata,
    ProjectMetadata,
    NormalizedConversation,
)
from prompts.extraction import build_extraction_prompt
from utils.llm import call_claude_json, count_tokens, chunk_text
from .filter import FilteredConversation


# -----------------------------------------------------------------------------
# EXTRACTION RESULT
# -----------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    """Result from extracting a single conversation."""
    conversation_id: str
    knowledge_entries: list[KnowledgeEntry]
    project_entries: list[ProjectEntry]
    input_tokens: int
    output_tokens: int
    validation_errors: list[str]
    success: bool
    error: Optional[str] = None


# -----------------------------------------------------------------------------
# VALIDATION
# -----------------------------------------------------------------------------

def validate_message_ids(
    conversation: NormalizedConversation,
    message_ids: list[str],
) -> list[str]:
    """
    Validate that message IDs exist in the conversation.
    Returns list of invalid IDs.
    """
    valid_ids = {m.message_id for m in conversation.messages}
    return [mid for mid in message_ids if mid not in valid_ids]


def validate_extraction(
    data: dict,
    conversation: NormalizedConversation,
) -> list[str]:
    """
    Validate extracted data.
    
    Checks:
    - All insights have evidence with message_ids
    - Message IDs exist in the conversation
    - Domain is specific enough
    
    Returns list of validation errors.
    """
    errors = []
    
    # Validate knowledge entries
    for i, entry in enumerate(data.get("knowledge_entries", [])):
        # Check domain specificity
        domain = entry.get("domain", "")
        if len(domain.split()) < 2:
            errors.append(f"Knowledge entry {i}: Domain too generic: '{domain}'")
        
        # Check key insights have evidence
        for j, insight in enumerate(entry.get("key_insights", [])):
            evidence = insight.get("evidence", {})
            message_ids = evidence.get("message_ids", [])
            
            if not message_ids:
                errors.append(f"Knowledge entry {i}, insight {j}: Missing message_ids")
            else:
                invalid = validate_message_ids(conversation, message_ids)
                if invalid:
                    errors.append(f"Knowledge entry {i}, insight {j}: Invalid message_ids: {invalid}")
        
        # Check capabilities have evidence
        for j, cap in enumerate(entry.get("knows_how_to", [])):
            evidence = cap.get("evidence", {})
            message_ids = evidence.get("message_ids", [])
            
            if not message_ids:
                errors.append(f"Knowledge entry {i}, capability {j}: Missing message_ids")
    
    # Validate project entries
    for i, entry in enumerate(data.get("project_entries", [])):
        # Check decisions have evidence
        for j, decision in enumerate(entry.get("decisions_made", [])):
            evidence = decision.get("evidence", {})
            message_ids = evidence.get("message_ids", [])
            
            if not message_ids:
                errors.append(f"Project entry {i}, decision {j}: Missing message_ids")
            else:
                invalid = validate_message_ids(conversation, message_ids)
                if invalid:
                    errors.append(f"Project entry {i}, decision {j}: Invalid message_ids: {invalid}")
    
    return errors


# -----------------------------------------------------------------------------
# CONVERSION
# -----------------------------------------------------------------------------

def convert_to_knowledge_entry(
    data: dict,
    conversation_id: str,
) -> KnowledgeEntry:
    """Convert extracted JSON to KnowledgeEntry object."""
    entry_id = f"ke_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()
    
    # Collect all message IDs from evidence
    all_message_ids = set()
    
    # Build key insights
    key_insights = []
    for item in data.get("key_insights", []):
        ev = item.get("evidence", {})
        msg_ids = ev.get("message_ids", [])
        all_message_ids.update(msg_ids)
        
        key_insights.append(Insight(
            insight=item.get("insight", ""),
            evidence=Evidence(
                conversation_id=conversation_id,
                message_ids=msg_ids,
                snippet=ev.get("snippet", "")[:200],
            ),
        ))
    
    # Build capabilities
    knows_how_to = []
    for item in data.get("knows_how_to", []):
        ev = item.get("evidence", {})
        msg_ids = ev.get("message_ids", [])
        all_message_ids.update(msg_ids)
        
        knows_how_to.append(Capability(
            capability=item.get("capability", ""),
            evidence=Evidence(
                conversation_id=conversation_id,
                message_ids=msg_ids,
                snippet=ev.get("snippet", "")[:200] if ev.get("snippet") else "",
            ),
        ))
    
    # Build open questions
    open_questions = []
    for item in data.get("open_questions", []):
        ev = item.get("evidence", {})
        msg_ids = ev.get("message_ids", [])
        all_message_ids.update(msg_ids)
        
        open_questions.append(OpenQuestion(
            question=item.get("question", ""),
            context=item.get("context"),
            evidence=Evidence(
                conversation_id=conversation_id,
                message_ids=msg_ids,
                snippet=ev.get("snippet", "") if ev.get("snippet") else "",
            ) if msg_ids else None,
        ))
    
    # Build initial position
    positions = []
    if data.get("current_view"):
        # Use first insight's evidence if available
        if key_insights:
            pos_evidence = key_insights[0].evidence
        else:
            pos_evidence = Evidence(
                conversation_id=conversation_id,
                message_ids=list(all_message_ids)[:3],
                snippet="",
            )
        
        positions.append(Position(
            view=data.get("current_view", ""),
            confidence=data.get("confidence", "medium"),
            as_of=now,
            evidence=pos_evidence,
        ))
    
    return KnowledgeEntry(
        id=entry_id,
        type="knowledge",
        domain=data.get("domain", ""),
        state="active",
        detail_level="full",
        current_view=data.get("current_view", ""),
        confidence=data.get("confidence", "medium"),
        positions=positions,
        key_insights=key_insights,
        knows_how_to=knows_how_to,
        open_questions=open_questions,
        metadata=KnowledgeMetadata(
            created_at=now,
            updated_at=now,
            source_conversations=[conversation_id],
            source_messages=list(all_message_ids),
            access_count=0,
        ),
    )


def convert_to_project_entry(
    data: dict,
    conversation_id: str,
) -> ProjectEntry:
    """Convert extracted JSON to ProjectEntry object."""
    entry_id = f"pe_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat()
    
    # Collect all message IDs
    all_message_ids = set()
    
    # Build decisions
    decisions_made = []
    for item in data.get("decisions_made", []):
        ev = item.get("evidence", {})
        msg_ids = ev.get("message_ids", [])
        all_message_ids.update(msg_ids)
        
        decisions_made.append(Decision(
            decision=item.get("decision", ""),
            rationale=item.get("rationale"),
            date=now,
            evidence=Evidence(
                conversation_id=conversation_id,
                message_ids=msg_ids,
                snippet=ev.get("snippet", "")[:200] if ev.get("snippet") else "",
            ) if msg_ids else None,
        ))
    
    return ProjectEntry(
        id=entry_id,
        type="project",
        name=data.get("name", ""),
        status="active",
        detail_level="full",
        goal=data.get("goal", ""),
        current_phase=data.get("current_phase", ""),
        blocked_on=data.get("blocked_on"),
        decisions_made=decisions_made,
        tech_stack=data.get("tech_stack", []),
        phase_history=[{
            "phase": data.get("current_phase", ""),
            "entered_at": now,
            "evidence": {"conversation_id": conversation_id},
        }] if data.get("current_phase") else [],
        metadata=ProjectMetadata(
            created_at=now,
            updated_at=now,
            source_conversations=[conversation_id],
            source_messages=list(all_message_ids),
            last_touched=now,
        ),
    )


# -----------------------------------------------------------------------------
# EXTRACTION
# -----------------------------------------------------------------------------

def extract_from_conversation(
    conversation: NormalizedConversation,
) -> ExtractionResult:
    """
    Extract knowledge and project entries from a single conversation.
    
    Args:
        conversation: The normalized conversation
    
    Returns:
        ExtractionResult with entries and metrics
    """
    try:
        # Build prompt
        prompt = build_extraction_prompt(conversation)
        prompt_tokens = count_tokens(prompt)
        
        # Check if chunking is needed
        if prompt_tokens > MAX_CONVERSATION_TOKENS:
            # For now, just truncate - chunking with consolidation is complex
            # Future: implement chunking with overlap and consolidation
            pass
        
        # Call Claude
        data, input_tokens, output_tokens = call_claude_json(prompt)
        
        # Validate extraction
        validation_errors = validate_extraction(data, conversation)
        
        # Convert to entries (even with validation errors, keep valid parts)
        knowledge_entries = []
        for entry_data in data.get("knowledge_entries", []):
            try:
                entry = convert_to_knowledge_entry(entry_data, conversation.id)
                if entry.key_insights:  # Only keep if has at least one insight
                    knowledge_entries.append(entry)
            except Exception as e:
                validation_errors.append(f"Failed to convert knowledge entry: {e}")
        
        project_entries = []
        for entry_data in data.get("project_entries", []):
            try:
                entry = convert_to_project_entry(entry_data, conversation.id)
                if entry.name:  # Only keep if has a name
                    project_entries.append(entry)
            except Exception as e:
                validation_errors.append(f"Failed to convert project entry: {e}")
        
        return ExtractionResult(
            conversation_id=conversation.id,
            knowledge_entries=knowledge_entries,
            project_entries=project_entries,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            validation_errors=validation_errors,
            success=True,
        )
        
    except json.JSONDecodeError as e:
        return ExtractionResult(
            conversation_id=conversation.id,
            knowledge_entries=[],
            project_entries=[],
            input_tokens=0,
            output_tokens=0,
            validation_errors=[],
            success=False,
            error=f"JSON decode error: {e}",
        )
    except Exception as e:
        return ExtractionResult(
            conversation_id=conversation.id,
            knowledge_entries=[],
            project_entries=[],
            input_tokens=0,
            output_tokens=0,
            validation_errors=[],
            success=False,
            error=str(e),
        )


def extract_entries(
    filtered_conversations: list[FilteredConversation],
    max_workers: int = MAX_EXTRACTION_WORKERS,
    progress_callback=None,
) -> list[ExtractionResult]:
    """
    Extract entries from multiple conversations in parallel.
    
    Args:
        filtered_conversations: List of filtered conversations (only keeps are processed)
        max_workers: Number of parallel extraction workers
        progress_callback: Optional callback(completed, total) for progress tracking
    
    Returns:
        List of ExtractionResult objects
    """
    # Filter to only conversations that should be kept
    to_process = [fc.conversation for fc in filtered_conversations if fc.should_keep]
    
    if not to_process:
        return []
    
    results = []
    
    # Use ThreadPoolExecutor for parallel processing
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(extract_from_conversation, conv): conv
            for conv in to_process
        }
        
        for i, future in enumerate(as_completed(futures)):
            result = future.result()
            results.append(result)
            
            if progress_callback:
                progress_callback(i + 1, len(to_process))
    
    return results


# -----------------------------------------------------------------------------
# CLI FOR TESTING
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract knowledge from conversations")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    parser.add_argument("--limit", type=int, default=3, help="Max conversations to process")
    args = parser.parse_args()
    
    print("=" * 60)
    print("STAGE 3: EXTRACT - Testing LLM extraction")
    print("=" * 60)
    print()
    
    # Import earlier stages
    from .parse import parse_all_exports
    from .filter import filter_conversations
    
    print("Parsing and filtering conversations...")
    conversations = parse_all_exports()
    filtered = filter_conversations(conversations)
    kept = [f for f in filtered if f.should_keep]
    
    print(f"Found {len(kept)} conversations to process")
    print(f"Processing up to {args.limit} for testing...")
    print()
    
    # Limit for testing
    test_filtered = [FilteredConversation(
        conversation=fc.conversation,
        filter_metadata=fc.filter_metadata,
    ) for fc in kept[:args.limit]]
    
    # Extract
    total_input_tokens = 0
    total_output_tokens = 0
    
    for i, fc in enumerate(test_filtered):
        print(f"[{i+1}/{len(test_filtered)}] Extracting: {fc.conversation.title[:50]}...")
        
        result = extract_from_conversation(fc.conversation)
        
        if result.success:
            total_input_tokens += result.input_tokens
            total_output_tokens += result.output_tokens
            
            print(f"  ✓ {len(result.knowledge_entries)} knowledge, {len(result.project_entries)} project entries")
            print(f"  Tokens: {result.input_tokens} in, {result.output_tokens} out")
            
            if result.validation_errors:
                print(f"  ⚠ {len(result.validation_errors)} validation warnings")
            
            # Show sample entry
            if result.knowledge_entries:
                entry = result.knowledge_entries[0]
                print(f"  Sample: [{entry.domain}] {entry.current_view[:60]}...")
        else:
            print(f"  ✗ Error: {result.error}")
        
        print()
    
    # Cost estimate
    input_cost = (total_input_tokens / 1_000_000) * 3.00
    output_cost = (total_output_tokens / 1_000_000) * 15.00
    total_cost = input_cost + output_cost
    
    print("=" * 60)
    print(f"Total tokens: {total_input_tokens} in, {total_output_tokens} out")
    print(f"Estimated cost: ${total_cost:.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()

