"""
=============================================================================
CHECKPOINT 1: TEST UPSTASH CONNECTION
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Verify that Upstash Redis and Vector connections are working.
Run this after setting up credentials in .env file.

USAGE:
    cd knowledge-system/distillation
    python test_connection.py

EXPECTED OUTPUT:
    ✓ Redis connection OK
    ✓ Vector connection OK (dimensions: 1536, vectors: 0)
=============================================================================
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

# Get credentials
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
UPSTASH_VECTOR_REST_URL = os.getenv("UPSTASH_VECTOR_REST_URL", "")
UPSTASH_VECTOR_REST_TOKEN = os.getenv("UPSTASH_VECTOR_REST_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")


def check_config() -> list[str]:
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
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_KEY_HERE":
        errors.append("ANTHROPIC_API_KEY not set (still placeholder)")
    if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_KEY_HERE":
        errors.append("OPENAI_API_KEY not set (still placeholder)")
    
    return errors


def test_redis() -> tuple[bool, str]:
    """Test Redis connection."""
    try:
        from upstash_redis import Redis
        
        redis = Redis(
            url=UPSTASH_REDIS_REST_URL,
            token=UPSTASH_REDIS_REST_TOKEN,
        )
        
        # Test set/get
        redis.set("_test_connection_", "hello")
        value = redis.get("_test_connection_")
        redis.delete("_test_connection_")
        
        if value == "hello":
            return True, "Redis connection OK"
        else:
            return False, f"Unexpected value: {value}"
    
    except Exception as e:
        return False, f"Redis error: {e}"


def test_vector() -> tuple[bool, str]:
    """Test Vector connection."""
    try:
        from upstash_vector import Index
        
        vector = Index(
            url=UPSTASH_VECTOR_REST_URL,
            token=UPSTASH_VECTOR_REST_TOKEN,
        )
        
        info = vector.info()
        return True, f"Vector connection OK (dimensions: {info.dimension}, vectors: {info.vector_count})"
    
    except Exception as e:
        return False, f"Vector error: {e}"


def main():
    print("=" * 60)
    print("CHECKPOINT 1: Testing Upstash Connections")
    print("=" * 60)
    print()
    
    # First check configuration
    print("Checking configuration...")
    errors = check_config()
    if errors:
        print("\n❌ Configuration errors:")
        for error in errors:
            print(f"   - {error}")
        print("\nPlease update the .env file with your credentials.")
        return False
    
    print("✓ Configuration OK")
    print()
    
    # Test Redis
    print("Testing Redis connection...")
    success, message = test_redis()
    if success:
        print(f"✓ {message}")
    else:
        print(f"❌ {message}")
        return False
    
    print()
    
    # Test Vector
    print("Testing Vector connection...")
    success, message = test_vector()
    if success:
        print(f"✓ {message}")
    else:
        print(f"❌ {message}")
        return False
    
    print()
    print("=" * 60)
    print("✓ All connections successful!")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
