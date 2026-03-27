#!/usr/bin/env python3
"""
Backup Upstash Redis and Vector data to local files.
Creates a snapshot that can be restored if distillation fails.
"""

import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from storage.redis_client import RedisClient
from storage.vector_client import VectorClient


def backup_redis(redis: RedisClient, backup_dir: Path) -> dict:
    """Backup all Redis data to JSON files."""
    print("Backing up Redis data...")
    
    backup_data = {
        "knowledge_entries": [],
        "project_entries": [],
        "thin_index": None,
        "processed_conversations": {},
        "metadata": {
            "backup_time": datetime.now().isoformat(),
            "source": "redis"
        }
    }
    
    # Backup knowledge entries
    print("  - Knowledge entries...")
    knowledge = redis.get_all_knowledge_entries()
    backup_data["knowledge_entries"] = [
        {
            "id": k.id,
            "data": k.to_dict()
        }
        for k in knowledge
    ]
    print(f"    Backed up {len(knowledge)} entries")
    
    # Backup project entries
    print("  - Project entries...")
    projects = redis.get_all_project_entries()
    backup_data["project_entries"] = [
        {
            "id": p.id,
            "data": p.to_dict()
        }
        for p in projects
    ]
    print(f"    Backed up {len(projects)} entries")
    
    # Backup thin index
    print("  - Thin index...")
    thin_index = redis.get_thin_index()
    if thin_index:
        backup_data["thin_index"] = thin_index.to_dict()
        print("    Backed up thin index")
    else:
        print("    No thin index found")
    
    # Backup processed conversation tracking
    print("  - Processed conversations...")
    processed_ids = redis.get_processed_conversation_ids()
    backup_data["processed_conversations"] = {
        "count": len(processed_ids),
        "ids": list(processed_ids)
    }
    print(f"    Backed up {len(processed_ids)} processed IDs")
    
    # Save to file
    redis_backup = backup_dir / "redis_backup.json"
    with open(redis_backup, "w") as f:
        json.dump(backup_data, f, indent=2)
    
    print(f"  ✓ Redis backup saved to {redis_backup}")
    
    return backup_data


def backup_vector(vector: VectorClient, backup_dir: Path) -> dict:
    """Backup Vector index metadata (can't backup actual vectors efficiently)."""
    print("Backing up Vector data...")
    
    backup_data = {
        "metadata": {
            "backup_time": datetime.now().isoformat(),
            "source": "vector",
            "note": "Full vector backup not supported - only metadata"
        },
        "index_info": None
    }
    
    # Get index info
    print("  - Index info...")
    try:
        info = vector.get_info()
        backup_data["index_info"] = {
            "vector_count": info["vector_count"],
            "dimension": info["dimension"],
            "metric": info["similarity_function"],
        }
        print(f"    Index has {info['vector_count']} vectors ({info['dimension']} dimensions)")
    except Exception as e:
        print(f"    Error getting index info: {e}")
    
    # Note: We can't efficiently backup all vectors from Upstash Vector
    # The vectors would need to be re-created from the Redis backup
    
    # Save to file
    vector_backup = backup_dir / "vector_backup.json"
    with open(vector_backup, "w") as f:
        json.dump(backup_data, f, indent=2)
    
    print(f"  ✓ Vector metadata saved to {vector_backup}")
    print(f"  ⚠ Note: Full vector backup requires re-creating from Redis data")
    
    return backup_data


def main():
    print("=" * 80)
    print("UPSTASH BACKUP")
    print("=" * 80)
    print(f"Started: {datetime.now().isoformat()}")
    print()
    
    # Create backup directory
    backup_dir = Path(__file__).parent / "backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Backup directory: {backup_dir}")
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
    
    # Backup Redis
    try:
        redis_data = backup_redis(redis, backup_dir)
    except Exception as e:
        print(f"  ✗ Error backing up Redis: {e}")
        return
    
    print()
    
    # Backup Vector
    try:
        vector_data = backup_vector(vector, backup_dir)
    except Exception as e:
        print(f"  ✗ Error backing up Vector: {e}")
        return
    
    print()
    
    # Save summary
    summary = {
        "backup_time": datetime.now().isoformat(),
        "backup_dir": str(backup_dir),
        "redis": {
            "knowledge_entries": len(redis_data["knowledge_entries"]),
            "project_entries": len(redis_data["project_entries"]),
            "thin_index": redis_data["thin_index"] is not None,
            "processed_conversations": redis_data["processed_conversations"]["count"]
        },
        "vector": {
            "vector_count": vector_data["index_info"]["vector_count"] if vector_data["index_info"] else 0,
            "dimension": vector_data["index_info"]["dimension"] if vector_data["index_info"] else 0
        }
    }
    
    summary_file = backup_dir / "backup_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    
    print("=" * 80)
    print("BACKUP COMPLETE")
    print("=" * 80)
    print()
    print(f"Backup location: {backup_dir}")
    print(f"Knowledge entries: {summary['redis']['knowledge_entries']}")
    print(f"Project entries: {summary['redis']['project_entries']}")
    print(f"Vectors: {summary['vector']['vector_count']}")
    print()
    print("To restore this backup, run:")
    print(f"  python restore_backup.py {backup_dir.name}")
    print()


if __name__ == "__main__":
    main()
