# Personal Knowledge System

## Overview

This skill provides access to my personal knowledge base - a distilled collection of insights, decisions, and learnings from past conversations. Use it to maintain consistency across conversations and avoid re-explaining past decisions.

## Available Tools

### 1. `get_index`
Returns a compressed overview of all topics and projects.

**When to use:**
- At the start of technical conversations
- When asked "what do I know about X?"
- When asked "what are my active projects?"

**Example triggers:**
- "What are my current projects?"
- "What topics have we discussed?"
- "Show me my knowledge index"

### 2. `get_context(topic)`
Returns the current view and key insights for a specific topic.

**When to use:**
- When discussing a specific technical topic
- When I mention a project by name
- When you need my established position on something

**Example triggers:**
- "What's my view on MLX?"
- "Tell me about the trading system project"
- "What do I think about React state management?"

### 3. `get_deep(id)`
Returns the full entry with all evidence and evolution history.

**When to use:**
- When asked about how my thinking evolved
- When provenance/sources matter
- When there's a contested entry needing resolution

**Example triggers:**
- "How did my view on X evolve?"
- "Walk me through the decisions on project Y"
- "What evidence supports this view?"

### 4. `search(query)`
Semantic search across all knowledge.

**When to use:**
- When topic/project name is unclear
- When looking for related discussions
- When asked "have we discussed X?"

**Example triggers:**
- "Have we talked about volatility trading?"
- "Find anything related to model fine-tuning"
- "What do I know about Python packaging?"

## Routing Logic

```
User message mentions specific topic/project
  → get_context(topic) first, get_deep(id) if details needed

User asks about general knowledge state
  → get_index()

User asks about evolution or provenance
  → get_context(topic) then get_deep(id)

User asks if we've discussed something
  → search(query)

Entry is contested (shown in index)
  → get_deep(id) to show both positions
```

## Important Behaviors

1. **Contested entries** have multiple positions - present BOTH views, don't pick one
2. **Compressed entries** have `has_full_content: false` - mention that archive exists
3. **Evolution history** shows how thinking changed - useful for explaining past shifts
4. **Evidence** links back to specific conversations - use for credibility

## Response Format

When referencing knowledge:
- Cite the entry domain/name naturally
- Mention confidence level if relevant
- Flag if entry is contested or stale
- Offer to get_deep if user wants provenance

## Example Session

**User:** What's my current view on MLX layer selection?

**Assistant:** *calls get_context("MLX layer selection")*

Based on your knowledge base, your current view on MLX layer selection is:

> "For LoRA fine-tuning on MLX, select layers based on task specificity - earlier layers for domain adaptation, later layers for output format changes. The default all-layers approach often over-fits."

Key insights:
- Layer selection significantly impacts training efficiency
- You've had success with selective layer training on code models

This is a **high confidence** active entry. Want me to pull the full evolution history?

