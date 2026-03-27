"""
Utility functions for the distillation pipeline.
"""

from .embedding import get_embedding, get_embeddings_batch
from .llm import call_claude, count_tokens
from .logging import setup_logger, log_run_report
from .salience import compute_salience, default_injection_tier, evaluate_salience_fixtures, load_memory_policy

__all__ = [
    "get_embedding",
    "get_embeddings_batch",
    "call_claude",
    "count_tokens",
    "setup_logger",
    "log_run_report",
    "compute_salience",
    "default_injection_tier",
    "evaluate_salience_fixtures",
    "load_memory_policy",
]
