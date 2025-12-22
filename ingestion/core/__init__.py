"""Core ingestion utilities."""

from .config import (
    UPSTASH_REDIS_REST_URL,
    UPSTASH_REDIS_REST_TOKEN,
    UPSTASH_VECTOR_REST_URL,
    UPSTASH_VECTOR_REST_TOKEN,
    ANTHROPIC_API_KEY,
    OPENAI_API_KEY,
    GITHUB_API_KEY,
    GITHUB_USERNAME,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONS,
    EXTRACTION_MODEL,
    validate_config,
    validate_github_config,
    validate_gmail_config,
)

from .storage import StorageClient
from .extractor import Extractor

__all__ = [
    "StorageClient",
    "Extractor",
    "validate_config",
    "validate_github_config",
    "validate_gmail_config",
]

