"""
=============================================================================
STAGE 4: MERGE - Merge new entries with existing
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Merge newly extracted entries with existing entries in storage.
Handles update, evolution, and contested state logic.
Never silently overwrites - tracks all changes.

INPUT FILES:
- Candidate KnowledgeEntry and ProjectEntry from extract stage
- Existing entries from Upstash Redis

OUTPUT FILES:
- Updated entries written to Upstash Redis

USAGE:
    from distillation.pipeline.merge import merge_entries
    results = merge_entries(candidates, redis_client, vector_client)
=============================================================================
"""

import argparse
from datetime import datetime
from dataclasses import dataclass
from typing import Literal, Optional

from models import (
    KnowledgeEntry,
    ProjectEntry,
    Evidence,
    Insight,
    Position,
    Evolution,
)
from storage.redis_client import RedisClient
from storage.vector_client import VectorClient
from utils.embedding import get_embedding, cosine_similarity


# -----------------------------------------------------------------------------
# MERGE ACTION
# -----------------------------------------------------------------------------

@dataclass
class MergeAction:
    """Describes what merge operation to perform."""
    action: Literal["create", "update", "evolve", "contest"]
    reason: str
    existing_id: Optional[str] = None
    view_similarity: float = 0.0


@dataclass
class MergeResult:
    """Result of a merge operation."""
    candidate_id: str
    action: str
    reason: str
    existing_id: Optional[str]
    final_id: str
    success: bool
    error: Optional[str] = None


# -----------------------------------------------------------------------------
# MATCHING LOGIC
# -----------------------------------------------------------------------------

def find_matching_entry(
    candidate: KnowledgeEntry,
    existing_entries: list[KnowledgeEntry],
    vector_client: VectorClient,
) -> Optional[tuple[KnowledgeEntry, float]]:
    """
    Find an existing entry that matches the candidate.
    Uses multiple signals: embedding similarity, keyword overlap, shared repos.
    
    Args:
        candidate: The candidate entry
        existing_entries: List of existing entries
        vector_client: Vector client for similarity search
    
    Returns:
        Tuple of (matching_entry, similarity_score) or None
    """
    if not existing_entries:
        return None
    
    # Get candidate embedding
    candidate_text = f"{candidate.domain} {candidate.current_view}"
    candidate_embedding, _ = get_embedding(candidate_text)
    
    best_match = None
    best_score = 0.0
    
    for existing in existing_entries:
        signals = 0
        similarity = 0.0
        
        # Signal 1: High embedding similarity
        existing_text = f"{existing.domain} {existing.current_view}"
        existing_embedding, _ = get_embedding(existing_text)
        similarity = cosine_similarity(candidate_embedding, existing_embedding)
        
        if similarity > 0.85:
            signals += 1
        
        # Signal 2: Keyword overlap in domain
        candidate_keywords = set(candidate.domain.lower().split())
        existing_keywords = set(existing.domain.lower().split())
        
        if len(candidate_keywords & existing_keywords) >= 2:
            signals += 1
        
        # Signal 3: Shared repository
        candidate_repos = {r.repo for r in candidate.related_repos}
        existing_repos = {r.repo for r in existing.related_repos}
        
        if candidate_repos & existing_repos:
            signals += 1
        
        # Require at least 2 signals to match
        if signals >= 2 and similarity > best_score:
            best_score = similarity
            best_match = existing
    
    if best_match:
        return (best_match, best_score)
    
    return None


def find_matching_project(
    candidate: ProjectEntry,
    existing_entries: list[ProjectEntry],
) -> Optional[ProjectEntry]:
    """
    Find an existing project that matches the candidate.
    Projects match primarily by name.
    """
    if not existing_entries:
        return None
    
    candidate_name = candidate.name.lower().strip()
    
    for existing in existing_entries:
        existing_name = existing.name.lower().strip()
        
        # Exact or close name match
        if candidate_name == existing_name:
            return existing
        
        # Check if one contains the other
        if candidate_name in existing_name or existing_name in candidate_name:
            return existing
    
    return None


# -----------------------------------------------------------------------------
# MERGE DECISIONS
# -----------------------------------------------------------------------------

def determine_merge_action(
    candidate: KnowledgeEntry,
    existing: KnowledgeEntry,
) -> MergeAction:
    """
    Determine how to merge candidate with existing entry.
    
    Actions:
    - update: Views align (>85% similar) - merge insights
    - evolve: Views shifted (50-85% similar) - track evolution
    - contest: Views contradict (<50% similar) - create contested state
    """
    # Get embeddings and compare views
    candidate_text = candidate.current_view
    existing_text = existing.current_view
    
    candidate_emb, _ = get_embedding(candidate_text)
    existing_emb, _ = get_embedding(existing_text)
    
    view_similarity = cosine_similarity(candidate_emb, existing_emb)
    
    if view_similarity > 0.85:
        return MergeAction(
            action="update",
            reason="Views aligned",
            existing_id=existing.id,
            view_similarity=view_similarity,
        )
    
    elif view_similarity > 0.50:
        return MergeAction(
            action="evolve",
            reason="View has evolved",
            existing_id=existing.id,
            view_similarity=view_similarity,
        )
    
    else:
        return MergeAction(
            action="contest",
            reason="Views contradict",
            existing_id=existing.id,
            view_similarity=view_similarity,
        )


# -----------------------------------------------------------------------------
# MERGE OPERATIONS
# -----------------------------------------------------------------------------

def merge_insights(
    existing: list[Insight],
    new: list[Insight],
) -> list[Insight]:
    """Merge insights, deduplicating by evidence."""
    merged = list(existing)
    existing_snippets = {i.evidence.snippet.lower().strip()[:50] for i in existing}
    
    for insight in new:
        snippet_key = insight.evidence.snippet.lower().strip()[:50]
        if snippet_key not in existing_snippets:
            merged.append(insight)
            existing_snippets.add(snippet_key)
    
    return merged


def apply_update(
    existing: KnowledgeEntry,
    candidate: KnowledgeEntry,
) -> KnowledgeEntry:
    """Apply update merge: combine insights, update timestamps."""
    now = datetime.utcnow().isoformat()
    
    existing.key_insights = merge_insights(existing.key_insights, candidate.key_insights)
    existing.knows_how_to = list(existing.knows_how_to) + [
        c for c in candidate.knows_how_to
        if c.capability not in [e.capability for e in existing.knows_how_to]
    ]
    
    existing.metadata.updated_at = now
    existing.metadata.source_conversations = list(set(
        existing.metadata.source_conversations +
        candidate.metadata.source_conversations
    ))
    existing.metadata.source_messages = list(set(
        existing.metadata.source_messages +
        candidate.metadata.source_messages
    ))
    
    return existing


def apply_evolution(
    existing: KnowledgeEntry,
    candidate: KnowledgeEntry,
) -> KnowledgeEntry:
    """Apply evolution merge: track the change, update view."""
    now = datetime.utcnow().isoformat()
    
    # Create evolution record
    evolution = Evolution(
        delta=f"View shifted from '{existing.current_view[:50]}...' to '{candidate.current_view[:50]}...'",
        trigger=candidate.metadata.source_conversations[0] if candidate.metadata.source_conversations else "Unknown",
        from_view=existing.current_view,
        to_view=candidate.current_view,
        date=now,
        evidence=candidate.positions[0].evidence if candidate.positions else Evidence(
            conversation_id=candidate.metadata.source_conversations[0] if candidate.metadata.source_conversations else "",
            message_ids=candidate.metadata.source_messages[:3],
            snippet="",
        ),
    )
    existing.evolution.append(evolution)
    
    # Update to new view
    existing.current_view = candidate.current_view
    existing.confidence = candidate.confidence
    
    # Merge insights
    existing.key_insights = merge_insights(existing.key_insights, candidate.key_insights)
    
    existing.metadata.updated_at = now
    existing.metadata.source_conversations = list(set(
        existing.metadata.source_conversations +
        candidate.metadata.source_conversations
    ))
    existing.metadata.source_messages = list(set(
        existing.metadata.source_messages +
        candidate.metadata.source_messages
    ))
    
    return existing


def apply_contest(
    existing: KnowledgeEntry,
    candidate: KnowledgeEntry,
) -> KnowledgeEntry:
    """Apply contest merge: keep both views, mark as contested."""
    now = datetime.utcnow().isoformat()
    
    # Add new position
    new_position = Position(
        view=candidate.current_view,
        confidence=candidate.confidence,
        as_of=now,
        evidence=candidate.positions[0].evidence if candidate.positions else Evidence(
            conversation_id=candidate.metadata.source_conversations[0] if candidate.metadata.source_conversations else "",
            message_ids=candidate.metadata.source_messages[:3],
            snippet="",
        ),
    )
    existing.positions.append(new_position)
    
    # Mark as contested
    existing.state = "contested"
    
    # Update view to newer one (but both are in positions)
    existing.current_view = candidate.current_view
    
    existing.metadata.updated_at = now
    existing.metadata.source_conversations = list(set(
        existing.metadata.source_conversations +
        candidate.metadata.source_conversations
    ))
    
    return existing


# -----------------------------------------------------------------------------
# MAIN MERGE FUNCTION
# -----------------------------------------------------------------------------

def merge_knowledge_entries(
    candidates: list[KnowledgeEntry],
    redis_client: RedisClient,
    vector_client: VectorClient,
) -> list[MergeResult]:
    """
    Merge candidate knowledge entries with existing ones.
    
    Args:
        candidates: New entries to merge
        redis_client: Redis client for storage
        vector_client: Vector client for similarity
    
    Returns:
        List of MergeResult objects
    """
    results = []
    
    # Get existing entries
    existing_entries = redis_client.get_all_knowledge_entries()
    
    for candidate in candidates:
        try:
            # Find matching entry
            match = find_matching_entry(candidate, existing_entries, vector_client)
            
            if match is None:
                # Create new entry
                redis_client.save_knowledge_entry(candidate)
                results.append(MergeResult(
                    candidate_id=candidate.id,
                    action="create",
                    reason="No matching entry found",
                    existing_id=None,
                    final_id=candidate.id,
                    success=True,
                ))
                existing_entries.append(candidate)
            else:
                existing, similarity = match
                action = determine_merge_action(candidate, existing)
                
                if action.action == "update":
                    merged = apply_update(existing, candidate)
                elif action.action == "evolve":
                    merged = apply_evolution(existing, candidate)
                else:  # contest
                    merged = apply_contest(existing, candidate)
                
                redis_client.save_knowledge_entry(merged)
                results.append(MergeResult(
                    candidate_id=candidate.id,
                    action=action.action,
                    reason=action.reason,
                    existing_id=existing.id,
                    final_id=merged.id,
                    success=True,
                ))
        
        except Exception as e:
            results.append(MergeResult(
                candidate_id=candidate.id,
                action="error",
                reason=str(e),
                existing_id=None,
                final_id="",
                success=False,
                error=str(e),
            ))
    
    return results


def merge_project_entries(
    candidates: list[ProjectEntry],
    redis_client: RedisClient,
) -> list[MergeResult]:
    """
    Merge candidate project entries with existing ones.
    Projects are simpler - mainly update or create.
    """
    results = []
    
    existing_entries = redis_client.get_all_project_entries()
    
    for candidate in candidates:
        try:
            match = find_matching_project(candidate, existing_entries)
            
            if match is None:
                # Create new
                redis_client.save_project_entry(candidate)
                results.append(MergeResult(
                    candidate_id=candidate.id,
                    action="create",
                    reason="No matching project found",
                    existing_id=None,
                    final_id=candidate.id,
                    success=True,
                ))
                existing_entries.append(candidate)
            else:
                # Update existing project
                now = datetime.utcnow().isoformat()
                
                # Update phase if different
                if candidate.current_phase and candidate.current_phase != match.current_phase:
                    match.phase_history.append({
                        "phase": candidate.current_phase,
                        "entered_at": now,
                        "evidence": {"conversation_id": candidate.metadata.source_conversations[0] if candidate.metadata.source_conversations else ""},
                    })
                    match.current_phase = candidate.current_phase
                
                # Update blocked_on
                if candidate.blocked_on:
                    match.blocked_on = candidate.blocked_on
                
                # Merge decisions
                existing_decisions = {d.decision for d in match.decisions_made}
                for decision in candidate.decisions_made:
                    if decision.decision not in existing_decisions:
                        match.decisions_made.append(decision)
                
                # Update timestamps
                match.metadata.updated_at = now
                match.metadata.last_touched = now
                match.metadata.source_conversations = list(set(
                    match.metadata.source_conversations +
                    candidate.metadata.source_conversations
                ))
                
                redis_client.save_project_entry(match)
                results.append(MergeResult(
                    candidate_id=candidate.id,
                    action="update",
                    reason="Updated existing project",
                    existing_id=match.id,
                    final_id=match.id,
                    success=True,
                ))
        
        except Exception as e:
            results.append(MergeResult(
                candidate_id=candidate.id,
                action="error",
                reason=str(e),
                existing_id=None,
                final_id="",
                success=False,
                error=str(e),
            ))
    
    return results


# -----------------------------------------------------------------------------
# CLI FOR TESTING
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Merge entries")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    args = parser.parse_args()
    
    print("=" * 60)
    print("STAGE 4: MERGE - Testing merge logic")
    print("=" * 60)
    print()
    print("This stage requires Upstash credentials and extracted entries.")
    print("Run the full pipeline to test merge functionality.")
    print("=" * 60)


if __name__ == "__main__":
    main()

