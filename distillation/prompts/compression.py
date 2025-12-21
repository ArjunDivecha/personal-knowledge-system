"""
=============================================================================
COMPRESSION PROMPT
=============================================================================
Version: 1.0.0
Last Updated: December 2024

PURPOSE:
LLM prompt template for compressing knowledge entries while
preserving the most important information.
=============================================================================
"""

import json
from models import KnowledgeEntry


COMPRESSION_PROMPT = '''Compress this knowledge entry while preserving what matters most.

## Priority (what to KEEP):
1. Core insight or conclusion - the "so what"
2. Non-obvious learnings that would be hard to re-derive
3. Pointers to code/repos that still exist
4. Summary of how thinking evolved (if applicable)

## What to DROP:
1. Reasoning steps that led to obvious conclusions
2. Failed approaches (unless the failure mode itself is instructive)
3. Context that can be recovered from linked repos
4. Verbose explanations of well-known concepts

## Original Entry:
{entry_json}

## Output Requirements:
Return a compressed version with:
- current_view: Max 2 sentences
- key_insights: Max 3 items, each max 1 sentence
- knows_how_to: Max 2 items
- evolution: Summarize multiple evolutions to 1 sentence if applicable

IMPORTANT: Preserve ALL evidence fields exactly - just make the content shorter.

Return ONLY the JSON object matching the original schema, no additional text.'''


def build_compression_prompt(entry: KnowledgeEntry) -> str:
    """
    Build the compression prompt for an entry.
    
    Args:
        entry: The knowledge entry to compress
    
    Returns:
        Complete prompt string
    """
    entry_json = json.dumps(entry.to_dict(), indent=2)
    return COMPRESSION_PROMPT.replace("{entry_json}", entry_json)

