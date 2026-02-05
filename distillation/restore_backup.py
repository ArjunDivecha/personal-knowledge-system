#!/usr/bin/env python3
"""
Restore Upstash Redis and Vector data from a backup.
Use this to rollback if distillation fails or causes issues.
"""

import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from storage.redis_client import RedisClient
from storage.vector_client import VectorClient


def restore_redis(redis: RedisClient, backup_dir: Path) -> bool:
    """Restore Redis data from backup files."""
    print("Restoring Redis data...")
    
    redis_backup = backup_dir / "redis_backup.json"
    if not redis_backup.exists():
        print(f"  ✗ Backup file not found: {redis_backup}")
        return False
    
    with open(redis_backup, "r") as f:
        backup_data = json.load(f)
    
    # Clear existing data (optional - comment out if you want to merge)
    print("  - Clearing existing data...")
    # Note: We'll overwrite keys instead of deleting everything
    # This is safer and allows partial restores
    
    # Restore knowledge entries
    print("  - Restoring knowledge entries...")
    knowledge_count = 0
    for entry in backup_data["knowledge_entries"]:
        entry_id = entry["id"]
        entry_data = entry["data"]
        redis.redis.set(f"knowledge:{entry_id}", json.dumps(entry_data))
        
        # Update indexes
        domain = entry_data.get("domain", "")
        if domain:
            domain_key = f"by_domain:{domain.lower().replace(' ', '_')}"
            redis.redis.sadd(domain_key, entry_id)
        
        state = entry_data.get("state", "active")
        state_key = f"by_state:{state}"
        redis.redis.sadd(state_key, entry_id)
        
        knowledge_count += 1
    
    print(f"    Restored {knowledge_count} knowledge entries")
    
    # Restore project entries
    print("  - Restoring project entries...")
    project_count = 0
    for entry in backup_data["project_entries"]:
        entry_id = entry["id"]
        entry_data = entry["data"]
        redis.redis.set(f"project:{entry_id}", json.dumps(entry_data))
        project_count += 1
    
    print(f"    Restored {project_count} project entries")
    
    # Restore thin index
    print("  - Restoring thin index...")
    if backup_data["thin_index"]:
        redis.redis.set("index:current", json.dumps(backup_data["thin_index"]))
        print("    Restored thin index")
    else:
        print("    No thin index in backup")
    
    # Restore processed conversation tracking
    print("  - Restoring processed conversations...")
    processed_data = backup_data.get("processed_conversations", {})
    processed_ids = processed_data.get("ids", [])
    
    # Clear existing processed tracking
    cursor = 0
    while True:
        cursor, keys = redis.redis.scan(cursor, match="processed:*", count=100)
        for key in keys:
            redis.redis.delete(key)
        if cursor == 0:
            break
    
    # Restore processed IDs
    for conv_id in processed_ids:
        redis.redis.set(f"processed:{conv_id}", "1")
    
    print(f"    Restored {len(processed_ids)} processed conversation IDs")
    
    print("  ✓ Redis restore complete")
    return True


def restore_vector(vector: VectorClient, backup_dir: Path, redis: RedisClient) -> bool:
    """Restore Vector data by re-creating embeddings from Redis data."""
    print("Restoring Vector data...")
    
    vector_backup = backup_dir / "vector_backup.json"
    if not vector_backup.exists():
        print(f"  ✗ Backup file not found: {vector_backup}")
        return False
    
    with open(vector_backup, "r") as f:
        backup_data = json.load(f)
    
    # Note: We can't restore vectors directly from backup
    # We need to re-create them from the Redis data
    
    print("  - Re-creating embeddings from Redis data...")
    
    # Get all knowledge entries from Redis
    knowledge = redis.get_all_knowledge_entries()
    
    if not knowledge:
        print("    No knowledge entries found in Redis")
        return False
    
    # Generate embeddings and upsert
    vectors = []
    for entry in knowledge:
        embedding_text = f"{entry.domain}: {entry.current_view}"
        try:
            embedding = vector.generate_embedding(embedding_text)
            vectors.append({
                "id": entry.id,
                "vector": embedding,
                "metadata": {
                    "type": "knowledge",
                    "domain": entry.domain,
                    "state": entry.state,
                    "updated_at": entry.updated_at
                }
            })
        except Exception as e:
            print(f"    Error generating embedding for {entry.id}: {e}")
    
    if vectors:
        print(f"    Generated {len(vectors)} embeddings")
        
        # Upsert in batches
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            vector.upsert(vectors=batch)
            print(f"    Upserted {min(i + batch_size, len(vectors))}/{len(vectors)} vectors")
        
        print("  ✓ Vector restore complete")
        return True
    else:
        print("    No vectors to restore")
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python restore_backup.py <backup_directory_name>")
        print()
        print("Available backups:")
        backup_root = Path(__file__).parent / "backups"
        if backup_root.exists():
            backups = sorted(backup_root.iterdir(), reverse=True)
            for backup in backups:
                summary_file = backup / "backup_summary.json"
                if summary_file.exists():
                    with open(summary_file) as f:
                        summary = json.load(f)
                    print(f"  {backup.name}")
                    print(f"    Time: {summary['backup_time']}")
                    print(f"    Knowledge: {summary['redis']['knowledge_entries']}")
                    print(f"    Vectors: {summary['vector']['vector_count']}")
                    print()
        return
    
    backup_name = sys.argv[1]
    backup_dir = Path(__file__).parent / "backups" / backup_name
    
    if not backup_dir.exists():
        print(f"✗ Backup not found: {backup_dir}")
        return
    
    print("=" * 80)
    print("UPSTASH RESTORE")
    print("=" * 80)
    print(f"Backup: {backup_dir}")
    print(f"Started: {datetime.now().isoformat()}")
    print()
    
    # Show backup summary
    summary_file = backup_dir / "backup_summary.json"
    if summary_file.exists():
        with open(summary_file) as f:
            summary = json.load(f)
        print("Backup summary:")
        print(f"  Time: {summary['backup_time']}")
        print(f"  Knowledge entries: {summary['redis']['knowledge_entries']}")
        print(f"  Project entries: {summary['redis']['project_entries']}")
        print(f"  Vectors: {summary['vector']['vector_count']}")
        print()
    
    # Confirm restore
    print("⚠️  WARNING: This will overwrite current data!")
    print("Are you sure you want to continue? (yes/no): ", end="", flush=True)
    
    try:
        response = input()
    except EOFError:
        print("\n✗ Restore cancelled")
        return
    
    if response.lower() not in ["yes", "y"]:
        print("✗ Restore cancelled")
        return
    
    print()
    
    # Connect to storage
    try:
        print("Connecting to storage...")
        redis = RedisClient()
        vector = VectorClient()
        
        redis_ok, redis_msg = redis.test_connection()
        vector_ok, vector_msg = vector.test_connection()
        
        if not redis_ok:
            print(f"  ✗ Redis: {redis_msg}")
            return
        if not vector_ok:
            print(f"  ✗ Vector: {vector_msg}")
            return
        
        print(f"  ✓ Redis: {redis_msg}")
        print(f"  ✓ Vector: {vector_msg}")
        print()
        
    except Exception as e:
        print(f"  ✗ Error connecting to storage: {e}")
        return
    
    # Restore Redis
    try:
        if not restore_redis(redis, backup_dir):
            print("✗ Redis restore failed")
            return
    except Exception as e:
        print(f"✗ Error restoring Redis: {e}")
        return
    
    print()
    
    # Restore Vector
    try:
        if not restore_vector(vector, backup_dir, redis):
            print("✗ Vector restore failed")
            return
    except Exception as e:
        print(f"✗ Error restoring Vector: {e}")
        return
    
    print()
    
    print("=" * 80)
    print("RESTORE COMPLETE")
    print("=" * 80)
    print()
    print("Data has been restored from backup.")
    print()


if __name__ == "__main__":
    main()
