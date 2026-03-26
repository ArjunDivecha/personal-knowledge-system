"""
=============================================================================
INGESTION PIPELINE - CONFIGURATION
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Central configuration for the data ingestion pipeline (GitHub, Gmail, etc.)
Shares storage credentials with the distillation pipeline.

INPUT FILES:
- .env file in ingestion folder or parent knowledge-system folder

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
env_path = Path(__file__).parent.parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent.parent / ".env"
if not env_path.exists():
    # Try distillation folder
    env_path = Path(__file__).parent.parent.parent / "distillation" / ".env"
load_dotenv(env_path)


# -----------------------------------------------------------------------------
# PATHS
# -----------------------------------------------------------------------------
# Gmail mbox file location
GMAIL_MBOX_PATH = Path(
    os.getenv(
        "GMAIL_MBOX_PATH",
        "/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/Gmail sent messages.mbox"
    )
)

# Checkpoint directory for resumable runs
CHECKPOINT_DIR = Path(
    os.getenv(
        "CHECKPOINT_DIR",
        str(Path(__file__).parent.parent / "checkpoints")
    )
)
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# UPSTASH CREDENTIALS (shared with distillation)
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
# GITHUB CONFIGURATION
# -----------------------------------------------------------------------------
GITHUB_API_KEY = os.getenv("GITHUB_API_KEY", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "ArjunDivecha")


# -----------------------------------------------------------------------------
# PIPELINE PARAMETERS
# -----------------------------------------------------------------------------
# Embedding model (same as distillation)
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# LLM model for extraction
EXTRACTION_MODEL = "claude-sonnet-4-6"

# Parallel processing
MAX_WORKERS = 8

# GitHub extraction settings
GITHUB_MAX_COMMITS_PER_REPO = 100       # Last N commits to analyze
GITHUB_MAX_CODE_FILES_PER_REPO = 50     # Max code files to scan for comments
GITHUB_CODE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".swift"}

# Gmail extraction settings
GMAIL_MIN_CONTENT_LENGTH = 150          # Skip emails shorter than this
GMAIL_SINCE_YEAR = 2020                 # Only process emails from this year onwards
GMAIL_SKIP_DOMAINS = {
    # Automated/transactional
    "noreply", "no-reply", "notifications", "mailer-daemon", "postmaster",
    # Common services
    "amazon.com", "ebay.com", "paypal.com", "venmo.com", "chase.com",
    "google.com", "apple.com", "microsoft.com",
    # Social
    "facebook.com", "twitter.com", "linkedin.com", "instagram.com",
}


# -----------------------------------------------------------------------------
# VALIDATION
# -----------------------------------------------------------------------------
def validate_config() -> list[str]:
    """Check that all required configuration values are set."""
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
    
    return errors


def validate_github_config() -> list[str]:
    """Check GitHub-specific configuration."""
    errors = validate_config()
    
    if not GITHUB_API_KEY:
        errors.append("GITHUB_API_KEY not set")
    if not GITHUB_USERNAME:
        errors.append("GITHUB_USERNAME not set")
    
    return errors


def validate_gmail_config() -> list[str]:
    """Check Gmail-specific configuration."""
    errors = validate_config()
    
    if not GMAIL_MBOX_PATH.exists():
        errors.append(f"GMAIL_MBOX_PATH does not exist: {GMAIL_MBOX_PATH}")
    
    return errors


if __name__ == "__main__":
    # Quick config check when run directly
    print("=== Ingestion Configuration ===\n")
    
    errors = validate_config()
    if errors:
        print("Base configuration errors:")
        for error in errors:
            print(f"  ✗ {error}")
    else:
        print("✓ Base configuration OK")
    
    print()
    
    gh_errors = validate_github_config()
    gh_specific = [e for e in gh_errors if e not in errors]
    if gh_specific:
        print("GitHub configuration errors:")
        for error in gh_specific:
            print(f"  ✗ {error}")
    else:
        print(f"✓ GitHub configuration OK (user: {GITHUB_USERNAME})")
    
    print()
    
    gm_errors = validate_gmail_config()
    gm_specific = [e for e in gm_errors if e not in errors]
    if gm_specific:
        print("Gmail configuration errors:")
        for error in gm_specific:
            print(f"  ✗ {error}")
    else:
        print(f"✓ Gmail configuration OK ({GMAIL_MBOX_PATH})")

