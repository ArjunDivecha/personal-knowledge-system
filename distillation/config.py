"""
=============================================================================
KNOWLEDGE DISTILLATION PIPELINE - CONFIGURATION
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Central configuration for the distillation pipeline. Loads environment
variables and defines paths, API settings, and pipeline parameters.

INPUT FILES:
- .env file in project root with API keys and credentials

OUTPUT FILES:
- None (configuration only)
=============================================================================
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# LOAD ENVIRONMENT VARIABLES
# -----------------------------------------------------------------------------
# Look for .env in the distillation folder or parent knowledge-system folder
env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


# -----------------------------------------------------------------------------
# PATHS - Source Data Locations
# -----------------------------------------------------------------------------
# Where Claude and GPT exports are stored (Dropbox)
CLAUDE_EXPORT_PATH = Path(
    os.getenv(
        "CLAUDE_EXPORT_PATH",
        "/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/Anthropic"
    )
)

GPT_EXPORT_PATH = Path(
    os.getenv(
        "GPT_EXPORT_PATH",
        "/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/ChatGPT"
    )
)

# Where to archive compressed entries
ARCHIVE_PATH = Path(
    os.getenv(
        "ARCHIVE_PATH",
        "/Users/macbook2024/Library/CloudStorage/Dropbox/AAA Backup/A Working/Memory/knowledge-system/archive"
    )
)

# Create archive directory if it doesn't exist
ARCHIVE_PATH.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# UPSTASH CREDENTIALS
# -----------------------------------------------------------------------------
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

UPSTASH_VECTOR_REST_URL = os.getenv("UPSTASH_VECTOR_REST_URL", "")
UPSTASH_VECTOR_REST_TOKEN = os.getenv("UPSTASH_VECTOR_REST_TOKEN", "")


# -----------------------------------------------------------------------------
# LLM API KEYS
# -----------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


# -----------------------------------------------------------------------------
# PIPELINE PARAMETERS
# -----------------------------------------------------------------------------
# Filter stage: minimum score to keep a conversation
FILTER_THRESHOLD = 3

# Extract stage: max tokens before chunking
MAX_CONVERSATION_TOKENS = 8000
CHUNK_OVERLAP_TOKENS = 500

# Extract stage: parallel processing
MAX_EXTRACTION_WORKERS = 8  # Adjust based on API rate limits

# Compress stage: criteria for compression
COMPRESS_AFTER_DAYS = 90
COMPRESS_IF_ACCESS_COUNT_BELOW = 3

# Index stage: thin index token budget
THIN_INDEX_MAX_TOKENS = 8000  # Balance between comprehensiveness and context size

# Embedding model
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# LLM model for extraction
EXTRACTION_MODEL = "claude-sonnet-4-5-20250929"


# -----------------------------------------------------------------------------
# VALIDATION
# -----------------------------------------------------------------------------
def validate_config() -> list[str]:
    """
    Check that all required configuration values are set.
    Returns a list of missing/invalid configuration items.
    """
    errors = []
    
    if not UPSTASH_REDIS_REST_URL:
        errors.append("UPSTASH_REDIS_REST_URL not set")
    if not UPSTASH_REDIS_REST_TOKEN:
        errors.append("UPSTASH_REDIS_REST_TOKEN not set")
    if not UPSTASH_VECTOR_REST_URL:
        errors.append("UPSTASH_VECTOR_REST_URL not set")
    if not UPSTASH_VECTOR_REST_TOKEN:
        errors.append("UPSTASH_VECTOR_REST_TOKEN not set")
    if not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY not set")
    if not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY not set")
    
    if not CLAUDE_EXPORT_PATH.exists():
        errors.append(f"CLAUDE_EXPORT_PATH does not exist: {CLAUDE_EXPORT_PATH}")
    if not GPT_EXPORT_PATH.exists():
        errors.append(f"GPT_EXPORT_PATH does not exist: {GPT_EXPORT_PATH}")
    
    return errors


if __name__ == "__main__":
    # Quick config check when run directly
    errors = validate_config()
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
    else:
        print("Configuration OK")
        print(f"  Claude exports: {CLAUDE_EXPORT_PATH}")
        print(f"  GPT exports: {GPT_EXPORT_PATH}")
        print(f"  Archive: {ARCHIVE_PATH}")

