"""
LLM prompt templates for extraction and compression.
"""

from .extraction import EXTRACTION_PROMPT, format_conversation_for_extraction
from .compression import COMPRESSION_PROMPT

__all__ = [
    "EXTRACTION_PROMPT",
    "format_conversation_for_extraction",
    "COMPRESSION_PROMPT",
]

