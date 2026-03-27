"""
=============================================================================
STAGE 6: INDEX - Write to storage and generate thin index
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Write entries to Upstash Redis/Vector and generate the thin index
for fast context injection.

INPUT FILES:
- Entries from merge stage

OUTPUT FILES:
- Entries written to Upstash Redis
- Embeddings written to Upstash Vector
- Thin index at Redis key "index:current"

USAGE:
    from distillation.pipeline.index import update_index
    result = update_index(redis_client, vector_client)
=============================================================================
"""

import argparse
from datetime import datetime, timedelta
from dataclasses import dataclass

from config import THIN_INDEX_MAX_TOKENS
from models import (
    KnowledgeEntry,
    ProjectEntry,
    ThinIndex,
    ThinIndexTopic,
    ThinIndexProject,
    ThinIndexEvolution,
)
from storage.redis_client import RedisClient
from storage.vector_client import VectorClient
from utils.embedding import get_embedding, get_embeddings_batch
from utils.llm import count_tokens
from utils.salience import compute_salience, resolve_stored_tier


# -----------------------------------------------------------------------------
# INDEX RESULT
# -----------------------------------------------------------------------------

@dataclass
class IndexResult:
    """Result from indexing operation."""
    entries_indexed: int
    vectors_upserted: int
    thin_index_token_count: int
    embedding_tokens: int
    success: bool
    error: str = ""


# -----------------------------------------------------------------------------
# TRUNCATION
# -----------------------------------------------------------------------------

def truncate(text: str, max_length: int) -> str:
    """Truncate text to max length with ellipsis."""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def sort_timestamp(value: str | None) -> float:
    """Convert ISO timestamp to a descending-sortable numeric key."""
    if not value:
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


# -----------------------------------------------------------------------------
# THIN INDEX GENERATION
# -----------------------------------------------------------------------------

def generate_thin_index(
    knowledge_entries: list[KnowledgeEntry],
    project_entries: list[ProjectEntry],
) -> ThinIndex:
    """
    Generate the thin index from all entries.
    
    Constraint: Must fit within THIN_INDEX_MAX_TOKENS (~3000).
    """
    now = datetime.utcnow().isoformat()
    
    index = ThinIndex(
        generated_at=now,
        token_count=0,
        topics=[],
        projects=[],
        recent_evolutions=[],
        contested_count=0,
        tier_1_count=0,
        tier_2_count=0,
        tier_3_count=0,
        archived_count=0,
    )

    ranked_topics = []
    for entry in knowledge_entries:
        if entry.state == "deprecated":
            continue
        salience_score = compute_salience(entry)
        stored_tier = resolve_stored_tier(entry)
        archived = bool(entry.metadata.archived) if entry.metadata else False
        if archived:
            index.archived_count += 1
            continue
        if stored_tier == 1:
            index.tier_1_count += 1
        elif stored_tier == 2:
            index.tier_2_count += 1
        else:
            index.tier_3_count += 1

        ranked_topics.append(
            (
                stored_tier,
                salience_score,
                entry.metadata.updated_at if entry.metadata else now,
                entry,
            )
        )
        if entry.state == "contested":
            index.contested_count += 1

    ranked_topics.sort(
        key=lambda item: (
            item[0],
            -item[1],
            -sort_timestamp(item[2]),
        )
    )

    for stored_tier, salience_score, _, entry in ranked_topics:
        index.topics.append(ThinIndexTopic(
            id=entry.id,
            domain=entry.domain,
            current_view_summary=truncate(entry.current_view, 80),
            state=entry.state if entry.state in ("active", "contested", "stale") else "active",
            confidence=entry.confidence,
            last_updated=entry.metadata.updated_at if entry.metadata else now,
            top_repo=entry.related_repos[0].repo if entry.related_repos else None,
            context_type=entry.metadata.context_type if entry.metadata else None,
            injection_tier=stored_tier,
            salience_score=salience_score,
            mention_count=entry.metadata.mention_count if entry.metadata else None,
            archived=False,
        ))

    ranked_projects = []
    for entry in project_entries:
        salience_score = compute_salience(entry)
        stored_tier = resolve_stored_tier(entry)
        archived = bool(entry.metadata.archived) if entry.metadata else False
        if archived:
            index.archived_count += 1
            continue
        if stored_tier == 1:
            index.tier_1_count += 1
        elif stored_tier == 2:
            index.tier_2_count += 1
        else:
            index.tier_3_count += 1
        ranked_projects.append(
            (
                stored_tier,
                salience_score,
                entry.metadata.last_touched if entry.metadata else now,
                entry,
            )
        )

    ranked_projects.sort(
        key=lambda item: (
            item[0],
            -item[1],
            -sort_timestamp(item[2]),
        )
    )

    for stored_tier, salience_score, _, entry in ranked_projects:
        primary_repo = None
        for repo in entry.related_repos:
            if getattr(repo, "is_primary", False):
                primary_repo = repo.repo
                break
        if not primary_repo and entry.related_repos:
            primary_repo = entry.related_repos[0].repo
        
        index.projects.append(ThinIndexProject(
            id=entry.id,
            name=entry.name,
            status=entry.status,
            goal_summary=truncate(entry.goal, 80),
            current_phase=entry.current_phase,
            blocked_on=entry.blocked_on,
            last_touched=entry.metadata.last_touched if entry.metadata else now,
            primary_repo=primary_repo,
            context_type=entry.metadata.context_type if entry.metadata else None,
            injection_tier=stored_tier,
            salience_score=salience_score,
            mention_count=entry.metadata.mention_count if entry.metadata else None,
            archived=False,
        ))

    index.total_topic_count = len(index.topics)
    index.total_project_count = len(index.projects)
    
    # Collect recent evolutions (last 30 days)
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    all_evolutions = []
    
    for entry in knowledge_entries:
        for evo in entry.evolution:
            try:
                evo_date = datetime.fromisoformat(evo.date.replace("Z", "+00:00"))
                if evo_date.replace(tzinfo=None) > thirty_days_ago:
                    all_evolutions.append(ThinIndexEvolution(
                        entry_id=entry.id,
                        entry_type="knowledge",
                        domain_or_name=entry.domain,
                        delta_summary=truncate(evo.delta, 60),
                        date=evo.date,
                    ))
            except:
                continue
    
    # Sort and limit evolutions
    all_evolutions.sort(key=lambda e: e.date, reverse=True)
    index.recent_evolutions = all_evolutions[:10]
    
    # Enforce token budget
    index = enforce_token_budget(index)
    index.token_count = count_tokens(str(index.to_dict()))
    
    return index


def enforce_token_budget(index: ThinIndex) -> ThinIndex:
    """Trim index to fit within token budget."""
    import json
    
    max_tokens = THIN_INDEX_MAX_TOKENS
    
    while count_tokens(json.dumps(index.to_dict())) > max_tokens:
        # Remove items in order of priority - progressively trim
        if len(index.topics) > 100:
            index.topics = index.topics[:int(len(index.topics) * 0.8)]  # Remove 20%
        elif len(index.projects) > 50:
            index.projects = index.projects[:int(len(index.projects) * 0.8)]
        elif len(index.recent_evolutions) > 10:
            index.recent_evolutions = index.recent_evolutions[:10]
        else:
            # Truncate summaries further
            for topic in index.topics:
                topic.current_view_summary = truncate(topic.current_view_summary, 50)
            for project in index.projects:
                project.goal_summary = truncate(project.goal_summary, 50)
            break
    
    return index


# -----------------------------------------------------------------------------
# VECTOR INDEXING
# -----------------------------------------------------------------------------

def update_vectors(
    entries: list[KnowledgeEntry | ProjectEntry],
    vector_client: VectorClient,
) -> tuple[int, int]:
    """
    Update vector embeddings for entries.
    
    Returns:
        Tuple of (vectors_upserted, tokens_used)
    """
    if not entries:
        return 0, 0
    
    # Prepare texts for embedding
    texts = []
    for entry in entries:
        if hasattr(entry, "domain"):
            # Knowledge entry
            text = f"{entry.domain} {entry.current_view} " + " ".join(
                i.insight for i in entry.key_insights[:3]
            )
        else:
            # Project entry
            text = f"{entry.name} {entry.goal} {entry.current_phase}"
        texts.append(text)
    
    # Get embeddings in batch
    embeddings, total_tokens = get_embeddings_batch(texts)
    
    # Prepare for upsert
    vector_entries = []
    for entry, embedding in zip(entries, embeddings):
        vector_entries.append({
            "id": entry.id,
            "vector": embedding,
            "type": entry.type,
            "domain": getattr(entry, "domain", entry.name),
            "state": getattr(entry, "state", entry.status),
            "updated_at": entry.metadata.updated_at if entry.metadata else datetime.utcnow().isoformat(),
            "source_conversations": entry.metadata.source_conversations if entry.metadata else [],
            "classification_status": entry.metadata.classification_status if entry.metadata else "pending",
            "context_type": entry.metadata.context_type if entry.metadata else None,
            "injection_tier": entry.metadata.injection_tier if entry.metadata else None,
            "salience_score": entry.metadata.salience_score if entry.metadata else None,
            "archived": entry.metadata.archived if entry.metadata else False,
        })
    
    # Upsert
    vector_client.upsert_entries_batch(vector_entries)
    
    return len(vector_entries), total_tokens


# -----------------------------------------------------------------------------
# MAIN INDEX FUNCTION
# -----------------------------------------------------------------------------

def update_index(
    redis_client: RedisClient,
    vector_client: VectorClient,
) -> IndexResult:
    """
    Update the full index: vectors + thin index.
    
    Args:
        redis_client: Redis client
        vector_client: Vector client
    
    Returns:
        IndexResult with metrics
    """
    try:
        # Get all entries
        knowledge_entries = redis_client.get_all_knowledge_entries()
        project_entries = redis_client.get_all_project_entries()
        
        total_entries = len(knowledge_entries) + len(project_entries)
        
        # Update vectors
        all_entries = knowledge_entries + project_entries
        vectors_upserted, embedding_tokens = update_vectors(all_entries, vector_client)
        
        # Generate and save thin index
        thin_index = generate_thin_index(knowledge_entries, project_entries)
        redis_client.save_thin_index(thin_index)
        
        return IndexResult(
            entries_indexed=total_entries,
            vectors_upserted=vectors_upserted,
            thin_index_token_count=thin_index.token_count,
            embedding_tokens=embedding_tokens,
            success=True,
        )
    
    except Exception as e:
        return IndexResult(
            entries_indexed=0,
            vectors_upserted=0,
            thin_index_token_count=0,
            embedding_tokens=0,
            success=False,
            error=str(e),
        )


# -----------------------------------------------------------------------------
# CLI FOR TESTING
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Update index")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    args = parser.parse_args()
    
    print("=" * 60)
    print("STAGE 6: INDEX - Testing index generation")
    print("=" * 60)
    print()
    print("This stage requires Upstash credentials and stored entries.")
    print("Run the full pipeline to test index functionality.")
    print("=" * 60)


if __name__ == "__main__":
    main()
