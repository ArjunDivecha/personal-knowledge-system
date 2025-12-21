"""
=============================================================================
EMBEDDING UTILITIES
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Wrapper functions for OpenAI embedding API calls.
Uses text-embedding-3-small (1536 dimensions) for compatibility with Upstash Vector.

INPUT FILES:
- None

OUTPUT FILES:
- None
=============================================================================
"""

from typing import Optional

import openai

from config import OPENAI_API_KEY, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS


# Initialize client
_openai_client: Optional[openai.OpenAI] = None


def get_openai_client() -> openai.OpenAI:
    """Get or create the OpenAI client."""
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def get_embedding(text: str) -> tuple[list[float], int]:
    """
    Get embedding vector for a single text.
    
    Args:
        text: Text to embed
    
    Returns:
        Tuple of (embedding_vector, token_count)
    """
    client = get_openai_client()
    
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text,
        dimensions=EMBEDDING_DIMENSIONS,
    )
    
    embedding = response.data[0].embedding
    token_count = response.usage.total_tokens
    
    return embedding, token_count


def get_embeddings_batch(texts: list[str], batch_size: int = 100) -> tuple[list[list[float]], int]:
    """
    Get embedding vectors for multiple texts in batches.
    
    Args:
        texts: List of texts to embed
        batch_size: Number of texts per API call (max 2048)
    
    Returns:
        Tuple of (list_of_embeddings, total_token_count)
    """
    client = get_openai_client()
    
    all_embeddings = []
    total_tokens = 0
    
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=batch,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        
        # Sort by index to maintain order
        batch_embeddings = sorted(response.data, key=lambda x: x.index)
        all_embeddings.extend([e.embedding for e in batch_embeddings])
        total_tokens += response.usage.total_tokens
    
    return all_embeddings, total_tokens


def cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """
    Calculate cosine similarity between two vectors.
    
    Args:
        vec1: First vector
        vec2: Second vector
    
    Returns:
        Cosine similarity (0.0 to 1.0)
    """
    if len(vec1) != len(vec2):
        raise ValueError("Vectors must have same length")
    
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = sum(a * a for a in vec1) ** 0.5
    norm2 = sum(b * b for b in vec2) ** 0.5
    
    if norm1 == 0 or norm2 == 0:
        return 0.0
    
    return dot_product / (norm1 * norm2)

