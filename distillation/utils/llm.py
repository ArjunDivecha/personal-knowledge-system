"""
=============================================================================
LLM UTILITIES
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
Wrapper functions for Claude API calls and token counting.

INPUT FILES:
- None

OUTPUT FILES:
- None
=============================================================================
"""

import json
from typing import Any, Optional

import anthropic
import tiktoken

from config import ANTHROPIC_API_KEY, EXTRACTION_MODEL


# Initialize clients
_anthropic_client: Optional[anthropic.Anthropic] = None
_tokenizer: Optional[tiktoken.Encoding] = None


def get_anthropic_client() -> anthropic.Anthropic:
    """Get or create the Anthropic client."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def get_tokenizer() -> tiktoken.Encoding:
    """Get or create the tokenizer (cl100k_base for compatibility)."""
    global _tokenizer
    if _tokenizer is None:
        _tokenizer = tiktoken.get_encoding("cl100k_base")
    return _tokenizer


def count_tokens(text: str) -> int:
    """
    Count tokens in text using cl100k_base tokenizer.
    This is the standard for token budget calculations.
    
    Args:
        text: Text to count tokens for
    
    Returns:
        Number of tokens
    """
    tokenizer = get_tokenizer()
    return len(tokenizer.encode(text))


def call_claude(
    prompt: str,
    system: Optional[str] = None,
    model: str = EXTRACTION_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> tuple[str, int, int]:
    """
    Call Claude API with the given prompt.
    
    Args:
        prompt: User message content
        system: Optional system prompt
        model: Model to use (defaults to EXTRACTION_MODEL from config)
        max_tokens: Maximum tokens in response
        temperature: Sampling temperature (0.0 for deterministic)
    
    Returns:
        Tuple of (response_text, input_tokens, output_tokens)
    
    Raises:
        anthropic.APIError: If API call fails
    """
    client = get_anthropic_client()
    
    messages = [{"role": "user", "content": prompt}]
    
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": messages,
    }
    
    if system:
        kwargs["system"] = system
    
    response = client.messages.create(**kwargs)
    
    # Extract text from response
    response_text = ""
    for block in response.content:
        if block.type == "text":
            response_text += block.text
    
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    
    return response_text, input_tokens, output_tokens


def call_claude_json(
    prompt: str,
    system: Optional[str] = None,
    model: str = EXTRACTION_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.0,
) -> tuple[Any, int, int]:
    """
    Call Claude API and parse the response as JSON.
    
    Args:
        prompt: User message content (should request JSON output)
        system: Optional system prompt
        model: Model to use
        max_tokens: Maximum tokens in response
        temperature: Sampling temperature
    
    Returns:
        Tuple of (parsed_json, input_tokens, output_tokens)
    
    Raises:
        json.JSONDecodeError: If response is not valid JSON
        anthropic.APIError: If API call fails
    """
    response_text, input_tokens, output_tokens = call_claude(
        prompt=prompt,
        system=system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    
    # Try to extract JSON from the response
    # Claude sometimes wraps JSON in markdown code blocks
    text = response_text.strip()
    
    # Remove markdown code block if present
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    
    if text.endswith("```"):
        text = text[:-3]
    
    text = text.strip()
    
    parsed = json.loads(text)
    
    return parsed, input_tokens, output_tokens


def chunk_text(text: str, max_tokens: int, overlap_tokens: int = 500) -> list[str]:
    """
    Split text into chunks that fit within token limit.
    
    Args:
        text: Text to split
        max_tokens: Maximum tokens per chunk
        overlap_tokens: Token overlap between chunks for context
    
    Returns:
        List of text chunks
    """
    tokenizer = get_tokenizer()
    tokens = tokenizer.encode(text)
    
    if len(tokens) <= max_tokens:
        return [text]
    
    chunks = []
    start = 0
    
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = tokenizer.decode(chunk_tokens)
        chunks.append(chunk_text)
        
        # Move start forward, keeping overlap
        start = end - overlap_tokens
        if start >= len(tokens):
            break
    
    return chunks

