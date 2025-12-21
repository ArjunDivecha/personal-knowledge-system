#!/usr/bin/env python3
"""
Simple runner script that sets up the path correctly before importing main.

Includes checkpointing to save intermediate results and avoid re-processing.
"""
import sys
import os
import json
import pickle
from pathlib import Path
from datetime import datetime

# Set up the Python path so all imports work
distillation_dir = Path(__file__).parent
sys.path.insert(0, str(distillation_dir))

# Change to the distillation directory
os.chdir(distillation_dir)

# Checkpoint directory
CHECKPOINT_DIR = distillation_dir / "checkpoints"
CHECKPOINT_DIR.mkdir(exist_ok=True)

def save_checkpoint(name: str, data):
    """Save checkpoint data to disk."""
    checkpoint_file = CHECKPOINT_DIR / f"{name}.pkl"
    with open(checkpoint_file, "wb") as f:
        pickle.dump(data, f)
    print(f"  [Checkpoint saved: {name}]")

def load_checkpoint(name: str):
    """Load checkpoint data from disk if it exists."""
    checkpoint_file = CHECKPOINT_DIR / f"{name}.pkl"
    if checkpoint_file.exists():
        with open(checkpoint_file, "rb") as f:
            data = pickle.load(f)
        print(f"  [Checkpoint loaded: {name}]")
        return data
    return None

def clear_checkpoints():
    """Clear all checkpoints for a fresh run."""
    for f in CHECKPOINT_DIR.glob("*.pkl"):
        f.unlink()
    print("  [All checkpoints cleared]")

# Now import and run
if __name__ == "__main__":
    # Import main module components directly
    from config import validate_config, ARCHIVE_PATH, CLAUDE_EXPORT_PATH, GPT_EXPORT_PATH
    
    # Check config first
    errors = validate_config()
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    
    print("Configuration OK")
    print(f"  Claude exports: {CLAUDE_EXPORT_PATH}")
    print(f"  GPT exports: {GPT_EXPORT_PATH}")
    print()
    
    # Import the rest
    from storage.redis_client import RedisClient
    from storage.vector_client import VectorClient
    from pipeline.parse import parse_all_exports
    from pipeline.filter import filter_conversations
    from pipeline.extract import extract_entries
    from pipeline.merge import merge_knowledge_entries, merge_project_entries
    from pipeline.compress import compress_eligible_entries
    from pipeline.index import update_index
    
    print("All modules imported successfully!")
    print()
    
    # Run the full pipeline
    print("=" * 60)
    print("STARTING FULL PIPELINE RUN")
    print(f"  Started at: {datetime.now().isoformat()}")
    print("=" * 60)
    
    # Initialize clients
    redis = RedisClient()
    vector = VectorClient()
    
    # Stage 1: Parse
    print("\n[1/6] PARSING exports...")
    conversations = load_checkpoint("parsed_conversations")
    if conversations is None:
        conversations = parse_all_exports()
        save_checkpoint("parsed_conversations", conversations)
    print(f"  Parsed {len(conversations)} conversations")
    
    # Stage 2: Filter
    print("\n[2/6] FILTERING conversations...")
    filtered = load_checkpoint("filtered_conversations")
    if filtered is None:
        filtered = filter_conversations(conversations)
        save_checkpoint("filtered_conversations", filtered)
    print(f"  Filtered to {len(filtered)} valuable conversations")
    
    # Stage 3: Extract (most expensive - save per batch)
    print("\n[3/6] EXTRACTING knowledge entries...")
    extraction_results = load_checkpoint("extraction_results")
    if extraction_results is None:
        extraction_results = extract_entries(filtered)
        save_checkpoint("extraction_results", extraction_results)
    
    # Separate knowledge and project entries
    knowledge_entries = []
    project_entries = []
    for result in extraction_results:
        knowledge_entries.extend(result.knowledge_entries)
        project_entries.extend(result.project_entries)
    
    print(f"  Extracted {len(knowledge_entries)} knowledge entries")
    print(f"  Extracted {len(project_entries)} project entries")
    
    # Stage 4: Store entries (first run - direct storage, skip merge)
    print("\n[4/6] STORING entries...")
    from utils.embedding import get_embeddings_batch
    from datetime import datetime
    
    # Clear existing entries for fresh start
    print("  Clearing existing entries...")
    existing_k = redis.get_all_knowledge_entries()
    for e in existing_k:
        redis.delete_knowledge_entry(e.id)
    existing_p = redis.get_all_project_entries()
    for e in existing_p:
        redis.delete_project_entry(e.id)
    
    # Batch process knowledge entries
    print(f"  Processing {len(knowledge_entries)} knowledge entries...")
    k_texts = [f"{e.domain} {e.current_view}" for e in knowledge_entries]
    k_embeddings, _ = get_embeddings_batch(k_texts)
    
    for i, (entry, embedding) in enumerate(zip(knowledge_entries, k_embeddings)):
        redis.save_knowledge_entry(entry)
        vector.upsert_entry(
            entry_id=entry.id,
            vector=embedding,
            entry_type="knowledge",
            domain=entry.domain,
            state=entry.state,
            updated_at=datetime.now().isoformat(),
        )
        if (i + 1) % 200 == 0:
            print(f"    Saved {i + 1}/{len(knowledge_entries)} knowledge entries...")
    
    # Batch process project entries
    print(f"  Processing {len(project_entries)} project entries...")
    p_texts = [f"{e.name} {e.goal}" for e in project_entries]
    p_embeddings, _ = get_embeddings_batch(p_texts)
    
    for i, (entry, embedding) in enumerate(zip(project_entries, p_embeddings)):
        redis.save_project_entry(entry)
        vector.upsert_entry(
            entry_id=entry.id,
            vector=embedding,
            entry_type="project",
            domain=entry.name,
            state=entry.status,
            updated_at=datetime.now().isoformat(),
        )
        if (i + 1) % 100 == 0:
            print(f"    Saved {i + 1}/{len(project_entries)} project entries...")
    
    print(f"  Stored {len(knowledge_entries)} knowledge entries")
    print(f"  Stored {len(project_entries)} project entries")
    
    # Stage 5: Compress
    print("\n[5/6] COMPRESSING old entries...")
    compressed_count = compress_eligible_entries(redis)
    print(f"  Compressed {compressed_count} entries")
    
    # Stage 6: Index
    print("\n[6/6] UPDATING thin index...")
    update_index(redis, vector)
    print("  Index updated")
    
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)
    
    # Show final stats
    all_knowledge = redis.get_all_knowledge_entries()
    all_projects = redis.get_all_project_entries()
    print(f"\nFinal counts:")
    print(f"  Knowledge entries: {len(all_knowledge)}")
    print(f"  Project entries: {len(all_projects)}")

