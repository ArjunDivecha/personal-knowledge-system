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
import sys
from typing import Optional, Any
from datetime import datetime
from pathlib import Path

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


REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_ROOT = REPO_ROOT / "distillation"
REDIS_MGET_BATCH_SIZE = 100
VALID_CONTEXT_TYPES = {
    "professional_identity",
    "stated_preference",
    "explicit_save",
    "active_project",
    "recurring_pattern",
    "task_query",
    "passing_reference",
}
DEFAULT_INJECTION_TIER_BY_CONTEXT_TYPE = {
    "professional_identity": 1,
    "stated_preference": 1,
    "explicit_save": 1,
    "active_project": 1,
    "recurring_pattern": 2,
    "task_query": 3,
    "passing_reference": 3,
}


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

    def get_source_metadata(self, source_type: str, source_id: str) -> Optional[dict]:
        """Get stored metadata for a processed source."""
        data = self.redis.get(self._source_key(source_type, source_id))
        if data is None:
            return None
        if isinstance(data, str):
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return None
        if isinstance(data, dict):
            return data
        return None
    
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
        source_conversations = list(meta.get("source_conversations") or [])
        source_messages = list(meta.get("source_messages") or [])
        context_type = meta.get("context_type")
        if context_type not in VALID_CONTEXT_TYPES:
            context_type = None
        mention_count = meta.get("mention_count")
        if mention_count is None:
            mention_count = len(source_conversations) if source_conversations else 1
        mention_count = max(1, int(mention_count or 1))

        meta["created_at"] = created_at
        meta["updated_at"] = updated_at
        meta["source_conversations"] = source_conversations
        meta["source_messages"] = source_messages
        meta["access_count"] = int(meta.get("access_count", 0) or 0)
        meta["last_accessed"] = meta.get("last_accessed")
        meta["schema_version"] = int(meta.get("schema_version", 2) or 2)
        meta["classification_status"] = meta.get("classification_status") or "pending"
        meta["context_type"] = context_type
        meta["mention_count"] = mention_count
        meta["first_seen"] = meta.get("first_seen") or created_at
        meta["last_seen"] = meta.get("last_seen") or updated_at
        meta["auto_inferred"] = meta.get("auto_inferred")
        meta["source_weights"] = dict(meta.get("source_weights")) if isinstance(meta.get("source_weights"), dict) else {}
        # Preserve an already-set injection_tier — Dream may have changed it from the
        # context-type default. Only fall back to the default for entries that have
        # never had a tier explicitly assigned.
        _raw_tier = meta.get("injection_tier")
        meta["injection_tier"] = (
            _raw_tier if _raw_tier is not None
            else (DEFAULT_INJECTION_TIER_BY_CONTEXT_TYPE.get(context_type) if context_type else None)
        )
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
        top_repo = (
            metadata.get("github_repo")
            or next(
                (
                    repo.get("repo")
                    for repo in entry.get("related_repos", [])
                    if isinstance(repo, dict) and repo.get("repo")
                ),
                None,
            )
        )
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
        if top_repo:
            vector_metadata["github_repo"] = top_repo
        if metadata.get("artifact_path"):
            vector_metadata["artifact_path"] = metadata["artifact_path"]

        return vector_metadata

    def _merge_unique_items(self, items: list[Any]) -> list[Any]:
        """Preserve order while removing duplicate JSON-serializable items."""
        seen: set[str] = set()
        merged: list[Any] = []
        for item in items:
            key = json.dumps(item, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _merge_knowledge_entry_data(self, existing: Optional[dict], incoming: dict) -> dict:
        """Merge repeated writes for the same knowledge entry id without losing provenance."""
        if not existing:
            merged = dict(incoming)
            merged["metadata"] = self._normalize_knowledge_metadata(merged.get("metadata"))
            return merged

        existing_meta = self._normalize_knowledge_metadata(existing.get("metadata"))
        incoming_meta = self._normalize_knowledge_metadata(incoming.get("metadata"))

        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        existing_conf = existing.get("confidence", "medium")
        incoming_conf = incoming.get("confidence", "medium")
        merged_confidence = (
            incoming_conf
            if confidence_rank.get(incoming_conf, 1) >= confidence_rank.get(existing_conf, 1)
            else existing_conf
        )

        merged = dict(existing)
        merged.update({
            "domain": incoming.get("domain") or existing.get("domain"),
            "subdomain": incoming.get("subdomain") or existing.get("subdomain"),
            "state": incoming.get("state") or existing.get("state", "active"),
            "detail_level": incoming.get("detail_level") or existing.get("detail_level", "full"),
            "current_view": incoming.get("current_view") or existing.get("current_view", ""),
            "confidence": merged_confidence,
            "full_content_ref": incoming.get("full_content_ref") or existing.get("full_content_ref"),
        })
        merged["positions"] = self._merge_unique_items(
            list(existing.get("positions") or []) + list(incoming.get("positions") or [])
        )
        merged["key_insights"] = self._merge_unique_items(
            list(existing.get("key_insights") or []) + list(incoming.get("key_insights") or [])
        )
        merged["knows_how_to"] = self._merge_unique_items(
            list(existing.get("knows_how_to") or []) + list(incoming.get("knows_how_to") or [])
        )
        merged["open_questions"] = self._merge_unique_items(
            list(existing.get("open_questions") or []) + list(incoming.get("open_questions") or [])
        )
        merged["related_repos"] = self._merge_unique_items(
            list(existing.get("related_repos") or []) + list(incoming.get("related_repos") or [])
        )
        merged["related_knowledge"] = self._merge_unique_items(
            list(existing.get("related_knowledge") or []) + list(incoming.get("related_knowledge") or [])
        )
        merged["evolution"] = self._merge_unique_items(
            list(existing.get("evolution") or []) + list(incoming.get("evolution") or [])
        )
        merged["metadata"] = {
            **existing_meta,
            **incoming_meta,
            "created_at": min(existing_meta["created_at"], incoming_meta["created_at"]),
            "updated_at": max(existing_meta["updated_at"], incoming_meta["updated_at"]),
            "source_conversations": self._merge_unique_items(
                list(existing_meta.get("source_conversations") or [])
                + list(incoming_meta.get("source_conversations") or [])
            ),
            "source_messages": self._merge_unique_items(
                list(existing_meta.get("source_messages") or [])
                + list(incoming_meta.get("source_messages") or [])
            ),
            "access_count": max(
                int(existing_meta.get("access_count", 0) or 0),
                int(incoming_meta.get("access_count", 0) or 0),
            ),
            "consolidation_notes": self._merge_unique_items(
                list(existing_meta.get("consolidation_notes") or [])
                + list(incoming_meta.get("consolidation_notes") or [])
            ),
        }
        # Dream lifecycle flags are sticky from the existing entry. Fresh ingest
        # data always has archived=False (from _normalize_knowledge_metadata:219);
        # without this guard a re-ingested source silently un-archives an entry
        # Dream pruned, defeating forgetting in a loop. Tier, salience_score, and
        # last_consolidated are Dream-computed and must survive re-ingestion too.
        for _sticky in ("injection_quarantine", "injection_tier", "salience_score", "last_consolidated"):
            if existing_meta.get(_sticky) is not None:
                merged["metadata"][_sticky] = existing_meta[_sticky]
        if existing_meta.get("archived"):
            merged["metadata"]["archived"] = True
        return merged
    
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
        entry = self._merge_knowledge_entry_data(self.get_knowledge_entry(entry_id), dict(entry))
        
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
            metadata = entry.get("metadata", {}) or {}
            repo_hint = metadata.get("github_repo")
            base_text = f"{entry['domain']}: {entry.get('current_view', '')}"
            embedding_text = f"{repo_hint}: {base_text}" if repo_hint else base_text
        
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

        merged_by_id: dict[str, dict] = {}
        for entry in entries:
            entry_id = entry["id"]
            current = merged_by_id.get(entry_id)
            if current is not None:
                merged_by_id[entry_id] = self._merge_knowledge_entry_data(current, dict(entry))
                continue
            merged_by_id[entry_id] = self._merge_knowledge_entry_data(
                self.get_knowledge_entry(entry_id),
                dict(entry),
            )
        entries = list(merged_by_id.values())

        # Generate all embeddings first
        if embedding_texts is None:
            embedding_texts = []
            for e in entries:
                metadata = e.get("metadata", {}) or {}
                repo_hint = metadata.get("github_repo")
                base_text = f"{e['domain']}: {e.get('current_view', '')}"
                embedding_texts.append(f"{repo_hint}: {base_text}" if repo_hint else base_text)
        
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
        """Save the thin index, respecting the Worker's rebuild lock.

        3.6 — The Worker acquires index:rebuild:lock before staging and renaming
        the new index.  A plain SET from Python during that window would race
        with the Worker's rename and could lose the Worker's changes.  If the
        lock is held we skip the write and log a warning; the next nightly run
        will write cleanly once the Worker has released the lock.
        """
        import logging as _logging
        lock = self.redis.get("index:rebuild:lock")
        if lock:
            _logging.getLogger(__name__).warning(
                "[storage] save_thin_index skipped: index:rebuild:lock is held by Worker "
                "(run_id=%s); will retry on next nightly cycle",
                lock.get("run_id") if isinstance(lock, dict) else str(lock)[:64],
            )
            return
        self.redis.set("index:current", json.dumps(index))

    def _scan_keys(self, pattern: str) -> list[str]:
        keys = []
        cursor: int | str = 0
        while True:
            cursor, batch = self.redis.scan(cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0 or cursor == "0":
                return keys

    def _iter_json_values(self, pattern: str):
        keys = self._scan_keys(pattern)
        for start in range(0, len(keys), REDIS_MGET_BATCH_SIZE):
            batch = keys[start:start + REDIS_MGET_BATCH_SIZE]
            for value in self.redis.mget(*batch):
                if not value:
                    continue
                if isinstance(value, str):
                    yield json.loads(value)
                else:
                    yield value

    def rebuild_thin_index(self) -> dict:
        """Rebuild the canonical thin index from all Redis entries."""
        if str(DISTILLATION_ROOT) not in sys.path:
            sys.path.insert(0, str(DISTILLATION_ROOT))

        from models.entries import KnowledgeEntry, ProjectEntry
        from pipeline.index import generate_thin_index

        knowledge_entries = [
            KnowledgeEntry.from_dict(entry)
            for entry in self._iter_json_values("knowledge:*")
        ]
        project_entries = [
            ProjectEntry.from_dict(entry)
            for entry in self._iter_json_values("project:*")
        ]
        thin_index = generate_thin_index(knowledge_entries, project_entries)
        thin_index_dict = thin_index.to_dict()
        self.save_thin_index(thin_index_dict)
        return thin_index_dict

    def update_thin_index(self, new_entries: list[dict]):
        """
        Update the thin index with new entries.
        Rebuilds the canonical index from Redis so summary counts cannot drift.

        Args:
            new_entries: List of knowledge entry dicts to add
        """
        if hasattr(self, "redis"):
            self.rebuild_thin_index()
            return

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
        
        # Upsert topics so stable IDs can refresh existing summaries.
        topics_by_id = {t["id"]: t for t in current.get("topics", [])}

        for entry in new_entries:
            metadata = entry.get("metadata", {}) or {}
            top_repo = (
                metadata.get("github_repo")
                or next(
                    (
                        repo.get("repo")
                        for repo in entry.get("related_repos", [])
                        if isinstance(repo, dict) and repo.get("repo")
                    ),
                    None,
                )
            )
            topic_summary = {
                "id": entry["id"],
                "domain": entry["domain"],
                "current_view_summary": entry.get("current_view", "")[:200] + "..." if len(entry.get("current_view", "")) > 200 else entry.get("current_view", ""),
                "state": entry.get("state", "active"),
                "confidence": entry.get("confidence", "medium"),
                "last_updated": metadata.get("updated_at", datetime.utcnow().isoformat()),
                "context_type": metadata.get("context_type"),
                "mention_count": metadata.get("mention_count"),
                "archived": metadata.get("archived", False),
                "top_repo": top_repo,
            }
            topics_by_id[entry["id"]] = topic_summary

        current["topics"] = list(topics_by_id.values())
        
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
