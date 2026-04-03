"""
=============================================================================
INGESTION PIPELINE - STORAGE CLIENT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Unified client for Upstash Redis and Vector storage.
Handles saving knowledge entries and embeddings.

INPUT FILES:
- Environment variables for Upstash credentials

OUTPUT FILES:
- Data written to Upstash Redis and Vector
=============================================================================
"""

import json
import hashlib
from typing import Optional, Any
from datetime import datetime

from upstash_redis import Redis
from upstash_vector import Index
from openai import OpenAI

from .config import (
    UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN,
    UPSTASH_VECTOR_REST_URL,
    UPSTASH_VECTOR_REST_TOKEN,
    OPENAI_API_KEY,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONS,
)


class StorageClient:
    """
    Unified storage client for ingestion pipelines.
    
    Handles:
    - Redis read/write for knowledge and project entries
    - Vector embeddings for semantic search
    - Deduplication via source tracking
    """
    
    def __init__(self):
        """Initialize Redis, Vector, and OpenAI clients."""
        self.redis = Redis(
            url=UPSTASH_REDIS_REST_URL,
            token=UPSTASH_REDIS_REST_TOKEN,
        )
        self.vector = Index(
            url=UPSTASH_VECTOR_REST_URL,
            token=UPSTASH_VECTOR_REST_TOKEN,
        )
        self.openai = OpenAI(api_key=OPENAI_API_KEY)
    
    # -------------------------------------------------------------------------
    # CONNECTION TEST
    # -------------------------------------------------------------------------
    def test_connection(self) -> tuple[bool, str]:
        """Test connections to all services."""
        try:
            # Test Redis
            self.redis.set("_test_", "hello")
            value = self.redis.get("_test_")
            self.redis.delete("_test_")
            if value != "hello":
                return False, f"Redis test failed: {value}"
            
            # Test Vector
            info = self.vector.info()
            
            return True, f"Connected: Redis OK, Vector OK ({info.vector_count} vectors)"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"
    
    # -------------------------------------------------------------------------
    # SOURCE TRACKING (for deduplication)
    # -------------------------------------------------------------------------
    def _source_key(self, source_type: str, source_id: str) -> str:
        """Generate a key for tracking processed sources."""
        return f"ingested:{source_type}:{source_id}"
    
    def is_source_processed(self, source_type: str, source_id: str) -> bool:
        """Check if a source (repo, email, etc.) has already been processed."""
        return self.redis.exists(self._source_key(source_type, source_id)) > 0
    
    def mark_source_processed(self, source_type: str, source_id: str, metadata: dict = None):
        """Mark a source as processed with optional metadata."""
        key = self._source_key(source_type, source_id)
        data = {
            "processed_at": datetime.utcnow().isoformat(),
            **(metadata or {})
        }
        self.redis.set(key, json.dumps(data))
    
    def get_processed_sources(self, source_type: str) -> list[str]:
        """Get all processed source IDs for a given type."""
        sources = []
        cursor = 0
        pattern = f"ingested:{source_type}:*"
        
        while True:
            cursor, keys = self.redis.scan(cursor, match=pattern, count=100)
            for key in keys:
                # Extract source_id from key
                source_id = key.replace(f"ingested:{source_type}:", "")
                sources.append(source_id)
            if cursor == 0:
                break
        
        return sources
    
    # -------------------------------------------------------------------------
    # EMBEDDING GENERATION
    # -------------------------------------------------------------------------
    def generate_embedding(self, text: str) -> list[float]:
        """Generate an embedding for text using OpenAI."""
        response = self.openai.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        return response.data[0].embedding
    
    def generate_embeddings_batch(self, texts: list[str], batch_size: int = 100) -> list[list[float]]:
        """Generate embeddings for multiple texts in batches."""
        all_embeddings = []
        
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            response = self.openai.embeddings.create(
                model=EMBEDDING_MODEL,
                input=batch,
                dimensions=EMBEDDING_DIMENSIONS,
            )
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)
        
        return all_embeddings

    def _normalize_knowledge_metadata(self, metadata: Optional[dict]) -> dict:
        """Apply Phase 1 metadata defaults for ingestion-created knowledge entries."""
        meta = dict(metadata or {})
        updated_at = meta.get("updated_at") or meta.get("created_at") or datetime.utcnow().isoformat()
        created_at = meta.get("created_at") or updated_at

        meta["created_at"] = created_at
        meta["updated_at"] = updated_at
        meta["source_conversations"] = list(meta.get("source_conversations") or [])
        meta["source_messages"] = list(meta.get("source_messages") or [])
        meta["access_count"] = int(meta.get("access_count", 0) or 0)
        meta["last_accessed"] = meta.get("last_accessed")
        meta["schema_version"] = int(meta.get("schema_version", 2) or 2)
        meta["classification_status"] = meta.get("classification_status") or "pending"
        meta["context_type"] = meta.get("context_type")
        meta["mention_count"] = meta.get("mention_count")
        meta["first_seen"] = meta.get("first_seen")
        meta["last_seen"] = meta.get("last_seen")
        meta["auto_inferred"] = meta.get("auto_inferred")
        meta["source_weights"] = dict(meta.get("source_weights")) if isinstance(meta.get("source_weights"), dict) else {}
        meta["injection_tier"] = meta.get("injection_tier")
        meta["salience_score"] = meta.get("salience_score")
        meta["last_consolidated"] = meta.get("last_consolidated")
        meta["consolidation_notes"] = list(meta.get("consolidation_notes") or [])
        meta["archived"] = bool(meta.get("archived", False))
        return meta

    def _sync_classification_pending(self, entry_id: str, metadata: Optional[dict]):
        """Keep the migration-time pending-classification set in sync."""
        if self.redis.exists("migration:backfill_complete") > 0:
            return

        status = (metadata or {}).get("classification_status")
        if status == "pending" or status is None:
            self.redis.sadd("classification:pending", entry_id)
            return

        self.redis.srem("classification:pending", entry_id)

    def _build_vector_metadata(self, entry: dict) -> dict:
        """Build Phase 1-safe vector metadata for new ingestion writes."""
        metadata = entry.get("metadata", {}) or {}
        vector_metadata = {
            "type": "knowledge",
            "domain": entry["domain"],
            "state": entry.get("state", "active"),
            "updated_at": metadata.get("updated_at", datetime.utcnow().isoformat()),
            "classification_status": metadata.get("classification_status", "pending"),
            "archived": metadata.get("archived", False),
        }

        source_conversations = metadata.get("source_conversations") or []
        if source_conversations:
            vector_metadata["source"] = source_conversations[0] if len(source_conversations) == 1 else ",".join(source_conversations[:3])
        if metadata.get("context_type"):
            vector_metadata["context_type"] = metadata["context_type"]
        if metadata.get("injection_tier") is not None:
            vector_metadata["injection_tier"] = metadata["injection_tier"]
        if metadata.get("salience_score") is not None:
            vector_metadata["salience_score"] = metadata["salience_score"]

        return vector_metadata
    
    # -------------------------------------------------------------------------
    # KNOWLEDGE ENTRY OPERATIONS
    # -------------------------------------------------------------------------
    def save_knowledge_entry(self, entry: dict, embedding_text: str = None):
        """
        Save a knowledge entry to Redis and Vector.
        
        Args:
            entry: Dictionary with knowledge entry data (must have 'id', 'domain', etc.)
            embedding_text: Text to embed (defaults to domain + current_view)
        """
        entry_id = entry["id"]
        entry = dict(entry)
        entry["metadata"] = self._normalize_knowledge_metadata(entry.get("metadata"))
        
        # Save to Redis
        key = f"knowledge:{entry_id}"
        self.redis.set(key, json.dumps(entry))
        
        # Update secondary indexes
        domain_key = f"by_domain:{entry['domain'].lower().replace(' ', '_')}"
        self.redis.sadd(domain_key, entry_id)
        
        state = entry.get("state", "active")
        state_key = f"by_state:{state}"
        self.redis.sadd(state_key, entry_id)
        self._sync_classification_pending(entry_id, entry.get("metadata"))
        
        # Generate and save embedding
        if embedding_text is None:
            embedding_text = f"{entry['domain']}: {entry.get('current_view', '')}"
        
        embedding = self.generate_embedding(embedding_text)
        
        self.vector.upsert(
            vectors=[{
                "id": entry_id,
                "vector": embedding,
                "metadata": self._build_vector_metadata(entry)
            }]
        )
    
    def save_knowledge_entries_batch(self, entries: list[dict], embedding_texts: list[str] = None):
        """
        Save multiple knowledge entries in batch.
        
        Args:
            entries: List of knowledge entry dicts
            embedding_texts: Optional list of texts to embed (parallel to entries)
        """
        if not entries:
            return

        entries = [dict(entry) for entry in entries]
        for entry in entries:
            entry["metadata"] = self._normalize_knowledge_metadata(entry.get("metadata"))
        
        # Generate all embeddings first
        if embedding_texts is None:
            embedding_texts = [
                f"{e['domain']}: {e.get('current_view', '')}"
                for e in entries
            ]
        
        embeddings = self.generate_embeddings_batch(embedding_texts)
        
        # Save to Redis (one by one - Upstash doesn't have mset for complex values)
        for entry in entries:
            key = f"knowledge:{entry['id']}"
            self.redis.set(key, json.dumps(entry))
            
            # Update indexes
            domain_key = f"by_domain:{entry['domain'].lower().replace(' ', '_')}"
            self.redis.sadd(domain_key, entry["id"])
            
            state = entry.get("state", "active")
            self.redis.sadd(f"by_state:{state}", entry["id"])
            self._sync_classification_pending(entry["id"], entry.get("metadata"))
        
        # Save to Vector in batches
        vectors = []
        for entry, embedding in zip(entries, embeddings):
            vectors.append({
                "id": entry["id"],
                "vector": embedding,
                "metadata": self._build_vector_metadata(entry)
            })
        
        # Upstash Vector batch limit
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            self.vector.upsert(vectors=batch)
    
    def get_knowledge_entry(self, entry_id: str) -> Optional[dict]:
        """Get a knowledge entry by ID."""
        data = self.redis.get(f"knowledge:{entry_id}")
        if data is None:
            return None
        if isinstance(data, str):
            return json.loads(data)
        return data
    
    # -------------------------------------------------------------------------
    # THIN INDEX OPERATIONS
    # -------------------------------------------------------------------------
    def get_thin_index(self) -> Optional[dict]:
        """Get the current thin index."""
        data = self.redis.get("index:current")
        if data is None:
            return None
        if isinstance(data, str):
            return json.loads(data)
        return data
    
    def save_thin_index(self, index: dict):
        """Save the thin index."""
        self.redis.set("index:current", json.dumps(index))
    
    def update_thin_index(self, new_entries: list[dict]):
        """
        Update the thin index with new entries.
        Adds new entries to the existing index.
        
        Args:
            new_entries: List of knowledge entry dicts to add
        """
        current = self.get_thin_index()
        
        if current is None:
            # Create new index
            current = {
                "generated_at": datetime.utcnow().isoformat(),
                "token_count": 0,
                "topics": [],
                "projects": [],
                "recent_evolutions": [],
                "contested_count": 0,
            }
        
        # Add new topics
        existing_ids = {t["id"] for t in current.get("topics", [])}
        
        for entry in new_entries:
            if entry["id"] not in existing_ids:
                topic_summary = {
                    "id": entry["id"],
                    "domain": entry["domain"],
                    "current_view_summary": entry.get("current_view", "")[:200] + "..." if len(entry.get("current_view", "")) > 200 else entry.get("current_view", ""),
                    "state": entry.get("state", "active"),
                    "confidence": entry.get("confidence", "medium"),
                    "last_updated": entry.get("metadata", {}).get("updated_at", datetime.utcnow().isoformat()),
                    "top_repo": None,
                }
                current["topics"].append(topic_summary)
        
        # Update metadata
        current["generated_at"] = datetime.utcnow().isoformat()
        
        # Rough token estimate (4 chars per token)
        current["token_count"] = len(json.dumps(current)) // 4
        
        self.save_thin_index(current)
    
    # -------------------------------------------------------------------------
    # SEMANTIC SEARCH
    # -------------------------------------------------------------------------
    def search(self, query: str, top_k: int = 5, min_score: float = 0.5) -> list[dict]:
        """
        Semantic search for knowledge entries.
        
        Args:
            query: Search query text
            top_k: Number of results to return
            min_score: Minimum similarity score
        
        Returns:
            List of matching entries with scores
        """
        query_embedding = self.generate_embedding(query)
        
        results = self.vector.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True,
        )
        
        # Filter by score and fetch full entries
        matches = []
        for result in results:
            if result.score >= min_score:
                entry = self.get_knowledge_entry(result.id)
                if entry:
                    matches.append({
                        "entry": entry,
                        "score": result.score,
                    })
        
        return matches
    
    # -------------------------------------------------------------------------
    # STATISTICS
    # -------------------------------------------------------------------------
    def get_stats(self) -> dict:
        """Get storage statistics."""
        vector_info = self.vector.info()
        
        # Count entries by type
        knowledge_count = 0
        project_count = 0
        
        cursor = 0
        while True:
            cursor, keys = self.redis.scan(cursor, match="knowledge:*", count=100)
            knowledge_count += len(keys)
            if cursor == 0:
                break
        
        cursor = 0
        while True:
            cursor, keys = self.redis.scan(cursor, match="project:*", count=100)
            project_count += len(keys)
            if cursor == 0:
                break
        
        return {
            "knowledge_entries": knowledge_count,
            "project_entries": project_count,
            "total_vectors": vector_info.vector_count,
            "vector_dimensions": vector_info.dimension,
        }
