"""
=============================================================================
UPDATE VECTOR SOURCES
=============================================================================
Re-upload vectors with source metadata for source-based weighting.
This allows the MCP server to downweight email entries.

INPUT FILES:
- Existing knowledge and project entries in Upstash Redis

OUTPUT FILES:
- Updated vectors in Upstash Vector with source metadata
=============================================================================
"""
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from storage.redis_client import RedisClient
from storage.vector_client import VectorClient
from utils.embedding import get_embeddings_batch

def main():
    redis = RedisClient()
    vector = VectorClient()

    print("Loading all entries from Redis...")
    knowledge_entries = redis.get_all_knowledge_entries()
    project_entries = redis.get_all_project_entries()

    print(f"Found {len(knowledge_entries)} knowledge entries and {len(project_entries)} project entries")

    # Process in batches to avoid overwhelming the API
    BATCH_SIZE = 100

    print("\nProcessing knowledge entries...")
    for i in range(0, len(knowledge_entries), BATCH_SIZE):
        batch = knowledge_entries[i:i+BATCH_SIZE]
        
        # Generate texts for embeddings
        texts = [f"{e.domain} {e.current_view}" for e in batch]
        embeddings, _ = get_embeddings_batch(texts)
        
        # Upload with source metadata
        for entry, embedding in zip(batch, embeddings):
            source_convs = entry.metadata.source_conversations if entry.metadata else None
            updated_at = entry.metadata.updated_at if entry.metadata else ""
            
            vector.upsert_entry(
                entry_id=entry.id,
                vector=embedding,
                entry_type="knowledge",
                domain=entry.domain,
                state=entry.state,
                updated_at=updated_at or "",
                source_conversations=source_convs,
            )
        
        print(f"  Processed {min(i+BATCH_SIZE, len(knowledge_entries))}/{len(knowledge_entries)} knowledge entries")

    print("\nProcessing project entries...")
    for i in range(0, len(project_entries), BATCH_SIZE):
        batch = project_entries[i:i+BATCH_SIZE]
        
        # Generate texts for embeddings
        texts = [f"{e.name} {e.goal}" for e in batch]
        embeddings, _ = get_embeddings_batch(texts)
        
        # Upload with source metadata
        for entry, embedding in zip(batch, embeddings):
            source_convs = entry.metadata.source_conversations if entry.metadata else None
            updated_at = entry.metadata.updated_at if entry.metadata else ""
            
            vector.upsert_entry(
                entry_id=entry.id,
                vector=embedding,
                entry_type="project",
                domain=entry.name,
                state=entry.status,
                updated_at=updated_at or "",
                source_conversations=source_convs,
            )
        
        print(f"  Processed {min(i+BATCH_SIZE, len(project_entries))}/{len(project_entries)} project entries")

    print("\nDone! Vectors now include source metadata for weighting.")

if __name__ == "__main__":
    main()

