"""
=============================================================================
UPSTASH VECTOR CLIENT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Client for storing and searching embeddings in Upstash Vector.
Used for semantic search across knowledge entries.

INPUT FILES:
- Environment variables for Upstash credentials

OUTPUT FILES:
- Vectors stored in Upstash Vector index
=============================================================================
"""

from typing import Optional

from upstash_vector import Index

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import UPSTASH_VECTOR_REST_URL, UPSTASH_VECTOR_REST_TOKEN


class VectorClient:
    """
    Client for Upstash Vector operations.
    
    Each entry gets one vector with metadata containing:
    - type: "knowledge" or "project"
    - domain: Topic domain (for knowledge) or name (for project)
    - state: Current state
    - updated_at: Last update timestamp
    """
    
    def __init__(self):
        """Initialize the Vector client."""
        self.index = Index(
            url=UPSTASH_VECTOR_REST_URL,
            token=UPSTASH_VECTOR_REST_TOKEN,
        )
    
    # -------------------------------------------------------------------------
    # CONNECTION TEST
    # -------------------------------------------------------------------------
    def test_connection(self) -> tuple[bool, str]:
        """
        Test connection and return status message.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            info = self.index.info()
            return True, f"Vector connection OK (dimensions: {info.dimension}, vectors: {info.vector_count})"
        except Exception as e:
            return False, f"Vector connection failed: {str(e)}"
    
    # -------------------------------------------------------------------------
    # UPSERT OPERATIONS
    # -------------------------------------------------------------------------
    def upsert_entry(
        self,
        entry_id: str,
        vector: list[float],
        entry_type: str,
        domain: str,
        state: str,
        updated_at: str,
    ):
        """
        Upsert a single entry's embedding.
        
        Args:
            entry_id: Entry ID (ke_xxx or pe_xxx)
            vector: Embedding vector (1536 dimensions)
            entry_type: "knowledge" or "project"
            domain: Topic domain or project name
            state: Current state
            updated_at: ISO8601 timestamp
        """
        self.index.upsert(
            vectors=[{
                "id": entry_id,
                "vector": vector,
                "metadata": {
                    "type": entry_type,
                    "domain": domain,
                    "state": state,
                    "updated_at": updated_at,
                }
            }]
        )
    
    def upsert_entries_batch(
        self,
        entries: list[dict],
    ):
        """
        Upsert multiple entries in a batch.
        
        Args:
            entries: List of dicts with id, vector, type, domain, state, updated_at
        """
        vectors = []
        for entry in entries:
            vectors.append({
                "id": entry["id"],
                "vector": entry["vector"],
                "metadata": {
                    "type": entry["type"],
                    "domain": entry["domain"],
                    "state": entry["state"],
                    "updated_at": entry["updated_at"],
                }
            })
        
        # Upstash Vector has batch limits, process in chunks
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            self.index.upsert(vectors=batch)
    
    # -------------------------------------------------------------------------
    # QUERY OPERATIONS
    # -------------------------------------------------------------------------
    def query(
        self,
        vector: list[float],
        top_k: int = 5,
        filter_metadata: Optional[dict] = None,
        include_metadata: bool = True,
    ) -> list[dict]:
        """
        Query for similar vectors.
        
        Args:
            vector: Query vector
            top_k: Number of results to return
            filter_metadata: Optional metadata filter
            include_metadata: Whether to include metadata in results
        
        Returns:
            List of results with id, score, and optionally metadata
        """
        kwargs = {
            "vector": vector,
            "top_k": top_k,
            "include_metadata": include_metadata,
        }
        
        if filter_metadata:
            kwargs["filter"] = filter_metadata
        
        results = self.index.query(**kwargs)
        
        # Convert to list of dicts
        output = []
        for result in results:
            item = {
                "id": result.id,
                "score": result.score,
            }
            if include_metadata and result.metadata:
                item["metadata"] = result.metadata
            output.append(item)
        
        return output
    
    def search_by_text(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        entry_type: Optional[str] = None,
        min_score: float = 0.5,
    ) -> list[dict]:
        """
        Search for entries similar to a query.
        
        Args:
            query_embedding: Embedding of the search query
            top_k: Number of results
            entry_type: Optional filter by "knowledge" or "project"
            min_score: Minimum similarity score to include
        
        Returns:
            List of matching entries with scores
        """
        filter_metadata = None
        if entry_type:
            filter_metadata = {"type": entry_type}
        
        results = self.query(
            vector=query_embedding,
            top_k=top_k,
            filter_metadata=filter_metadata,
            include_metadata=True,
        )
        
        # Filter by minimum score
        return [r for r in results if r["score"] >= min_score]
    
    # -------------------------------------------------------------------------
    # DELETE OPERATIONS
    # -------------------------------------------------------------------------
    def delete_entry(self, entry_id: str):
        """Delete an entry's vector."""
        self.index.delete(ids=[entry_id])
    
    def delete_entries_batch(self, entry_ids: list[str]):
        """Delete multiple entries' vectors."""
        if entry_ids:
            self.index.delete(ids=entry_ids)
    
    # -------------------------------------------------------------------------
    # INFO
    # -------------------------------------------------------------------------
    def get_info(self) -> dict:
        """Get information about the vector index."""
        info = self.index.info()
        return {
            "dimension": info.dimension,
            "vector_count": info.vector_count,
            "pending_vector_count": info.pending_vector_count,
            "similarity_function": info.similarity_function,
        }

