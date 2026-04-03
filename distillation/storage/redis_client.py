"""
=============================================================================
UPSTASH REDIS CLIENT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Client for reading/writing knowledge and project entries to Upstash Redis.

INPUT FILES:
- Environment variables for Upstash credentials

OUTPUT FILES:
- Data written to Upstash Redis
=============================================================================
"""

import json
from typing import Optional, Any
from datetime import datetime

from upstash_redis import Redis

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
from models.entries import KnowledgeEntry, ProjectEntry
from models.thin_index import ThinIndex


class RedisClient:
    """
    Client for Upstash Redis operations.
    
    Key patterns:
    - knowledge:{id} - Knowledge entry JSON
    - project:{id} - Project entry JSON
    - index:current - Current thin index
    - by_domain:{domain} - Set of entry IDs by domain
    - by_state:{state} - Set of entry IDs by state
    - processed:{conversation_id} - Marker for already-processed conversations
    """
    
    def __init__(self):
        """Initialize the Redis client."""
        self.client = Redis(
            url=UPSTASH_REDIS_REST_URL,
            token=UPSTASH_REDIS_REST_TOKEN,
        )
    
    # -------------------------------------------------------------------------
    # CONNECTION TEST
    # -------------------------------------------------------------------------
    def ping(self) -> bool:
        """Test the connection to Redis."""
        try:
            result = self.client.ping()
            return result == "PONG"
        except Exception:
            return False
    
    def test_connection(self) -> tuple[bool, str]:
        """
        Test connection and return status message.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            self.client.set("_test_", "hello")
            value = self.client.get("_test_")
            self.client.delete("_test_")
            
            if value == "hello":
                return True, "Redis connection OK"
            else:
                return False, f"Unexpected value: {value}"
        except Exception as e:
            return False, f"Redis connection failed: {str(e)}"
    
    # -------------------------------------------------------------------------
    # KNOWLEDGE ENTRIES
    # -------------------------------------------------------------------------
    def get_knowledge_entry(self, entry_id: str) -> Optional[KnowledgeEntry]:
        """Get a knowledge entry by ID."""
        data = self.client.get(f"knowledge:{entry_id}")
        if data is None:
            return None
        
        if isinstance(data, str):
            data = json.loads(data)
        
        return KnowledgeEntry.from_dict(data)
    
    def save_knowledge_entry(self, entry: KnowledgeEntry):
        """Save a knowledge entry."""
        key = f"knowledge:{entry.id}"
        data = json.dumps(entry.to_dict())
        self.client.set(key, data)
        
        # Update secondary indexes
        domain_key = f"by_domain:{entry.domain.lower().replace(' ', '_')}"
        self.client.sadd(domain_key, entry.id)
        
        state_key = f"by_state:{entry.state}"
        self.client.sadd(state_key, entry.id)
        self._sync_classification_pending(entry.id, entry.metadata.classification_status if entry.metadata else None)
    
    def get_all_knowledge_entries(self) -> list[KnowledgeEntry]:
        """Get all knowledge entries."""
        entries = []
        
        # Scan for all knowledge keys
        cursor = 0
        while True:
            cursor, keys = self.client.scan(cursor, match="knowledge:*", count=100)
            
            for key in keys:
                data = self.client.get(key)
                if data:
                    if isinstance(data, str):
                        data = json.loads(data)
                    entries.append(KnowledgeEntry.from_dict(data))
            
            if cursor == 0:
                break
        
        return entries
    
    def delete_knowledge_entry(self, entry_id: str):
        """Delete a knowledge entry."""
        self.client.delete(f"knowledge:{entry_id}")
    
    # -------------------------------------------------------------------------
    # PROJECT ENTRIES
    # -------------------------------------------------------------------------
    def get_project_entry(self, entry_id: str) -> Optional[ProjectEntry]:
        """Get a project entry by ID."""
        data = self.client.get(f"project:{entry_id}")
        if data is None:
            return None
        
        if isinstance(data, str):
            data = json.loads(data)
        
        return ProjectEntry.from_dict(data)
    
    def save_project_entry(self, entry: ProjectEntry):
        """Save a project entry."""
        key = f"project:{entry.id}"
        data = json.dumps(entry.to_dict())
        self.client.set(key, data)
        
        # Update secondary indexes
        name_key = f"by_name:{entry.name.lower().replace(' ', '_')}"
        self.client.sadd(name_key, entry.id)
        
        status_key = f"by_status:{entry.status}"
        self.client.sadd(status_key, entry.id)
        self._sync_classification_pending(entry.id, entry.metadata.classification_status if entry.metadata else None)
    
    def get_all_project_entries(self) -> list[ProjectEntry]:
        """Get all project entries."""
        entries = []
        
        cursor = 0
        while True:
            cursor, keys = self.client.scan(cursor, match="project:*", count=100)
            
            for key in keys:
                data = self.client.get(key)
                if data:
                    if isinstance(data, str):
                        data = json.loads(data)
                    entries.append(ProjectEntry.from_dict(data))
            
            if cursor == 0:
                break
        
        return entries
    
    def delete_project_entry(self, entry_id: str):
        """Delete a project entry."""
        self.client.delete(f"project:{entry_id}")
    
    # -------------------------------------------------------------------------
    # THIN INDEX
    # -------------------------------------------------------------------------
    def get_thin_index(self) -> Optional[ThinIndex]:
        """Get the current thin index."""
        data = self.client.get("index:current")
        if data is None:
            return None
        
        if isinstance(data, str):
            data = json.loads(data)
        
        return ThinIndex.from_dict(data)
    
    def save_thin_index(self, index: ThinIndex):
        """Save the thin index."""
        data = json.dumps(index.to_dict())
        self.client.set("index:current", data)
    
    # -------------------------------------------------------------------------
    # CONVERSATION TRACKING
    # -------------------------------------------------------------------------
    def is_conversation_processed(self, conversation_id: str) -> bool:
        """Check if a conversation has already been processed."""
        return self.client.exists(f"processed:{conversation_id}") > 0
    
    def mark_conversation_processed(self, conversation_id: str):
        """Mark a conversation as processed."""
        self.client.set(f"processed:{conversation_id}", "1")
    
    def get_processed_conversation_ids(self) -> set[str]:
        """Get all processed conversation IDs."""
        ids = set()
        
        cursor = 0
        while True:
            cursor, keys = self.client.scan(cursor, match="processed:*", count=100)
            
            for key in keys:
                # Extract conversation ID from key
                conv_id = key.replace("processed:", "")
                ids.add(conv_id)
            
            if cursor == 0:
                break
        
        return ids
    
    # -------------------------------------------------------------------------
    # GENERIC OPERATIONS
    # -------------------------------------------------------------------------
    def get(self, key: str) -> Optional[Any]:
        """Get a value by key."""
        return self.client.get(key)
    
    def set(self, key: str, value: Any):
        """Set a value by key."""
        self.client.set(key, value)
    
    def delete(self, key: str):
        """Delete a key."""
        self.client.delete(key)
    
    def increment_access_count(self, entry_type: str, entry_id: str):
        """Increment the access count for an entry."""
        key = f"{entry_type}:{entry_id}"
        data = self.client.get(key)
        
        if data:
            if isinstance(data, str):
                data = json.loads(data)
            
            if "metadata" in data and data["metadata"]:
                data["metadata"]["access_count"] = data["metadata"].get("access_count", 0) + 1
                data["metadata"]["last_accessed"] = datetime.utcnow().isoformat()
                self.client.set(key, json.dumps(data))

    def _sync_classification_pending(self, entry_id: str, classification_status: Optional[str]):
        """Keep the migration-time pending-classification set in sync."""
        if self.client.exists("migration:backfill_complete") > 0:
            return

        if classification_status == "pending" or classification_status is None:
            self.client.sadd("classification:pending", entry_id)
            return

        self.client.srem("classification:pending", entry_id)

    def get_set_cardinality(self, key: str) -> int:
        """Return the cardinality of a Redis set."""
        return int(self.client.scard(key) or 0)
