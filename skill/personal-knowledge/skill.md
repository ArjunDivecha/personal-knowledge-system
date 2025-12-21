---
name: personal-knowledge-system
description: Access and use my personal knowledge base from past AI conversations. Use when discussing my views, projects, technical decisions, or when I ask what I know about something.
---

## Overview

This skill teaches you how to effectively use my Personal Knowledge MCP connector to maintain consistency across conversations and leverage my established knowledge, preferences, and project context.

## When to Use This Skill

**Always check my knowledge base when:**
- I ask "what do I know about X?" or "what's my view on X?"
- I mention a project by name (check if it exists in my knowledge)
- We're discussing technical decisions where I may have established positions
- I ask "have we discussed X?" or "what are my active projects?"
- Starting a technical conversation where past context would help

**Don't use when:**
- Simple factual questions unrelated to my personal knowledge
- I explicitly say "forget what you know" or want a fresh perspective

## Tool Usage Guide

### 1. `get_index` - Start Here for Context

**Use this to:** Get an overview of all my knowledge topics and active projects.

**Best for:**
- Beginning of technical conversations
- When I ask about my projects or knowledge areas
- When you're unsure if I have relevant prior knowledge

**Example triggers:**
- "What are my current projects?"
- "What topics have we discussed?"
- "Show me my knowledge index"

### 2. `get_context(topic)` - Get Specific Knowledge

**Use this to:** Retrieve my current view, key insights, and confidence level on a specific topic or project.

**Best for:**
- When I mention a specific technical domain
- When you need my established position on something
- When discussing a named project

**Example triggers:**
- "What's my view on factor timing?"
- "Tell me about the ngods-stocks project"
- "What do I think about LoRA fine-tuning?"

### 3. `get_deep(id)` - Full Provenance

**Use this to:** Get complete entry with all evidence, evolution history, and source references.

**Best for:**
- When I ask how my thinking evolved
- When provenance or sources matter
- When there's a contested entry that needs resolution
- When I want to understand the basis for a view

**Example triggers:**
- "How did my view on X evolve?"
- "Walk me through the decisions on this project"
- "What evidence supports this view?"

### 4. `search(query)` - Find Related Knowledge

**Use this to:** Semantic search when you're not sure of the exact topic name.

**Best for:**
- Fuzzy queries or partial topic names
- Finding related discussions
- Checking if we've covered something before

**Example triggers:**
- "Have we talked about volatility trading?"
- "Find anything related to model fine-tuning"
- "What do I know about Docker?"

## Response Guidelines

### Citing Knowledge

When referencing my knowledge base:
- Mention the topic/project name naturally: *"Based on your knowledge base entry on Factor Timing..."*
- Include confidence level when relevant: *"This is a high-confidence view..."*
- Flag if entry is contested: *"Note: You have conflicting positions on this..."*
- Offer deeper context: *"Want me to pull the full evolution history?"*

### Handling Special Cases

**Contested Entries:**
If an entry has `contested` state, present BOTH positions without picking one. Ask me to clarify which view is current.

**Compressed Entries:**
If `has_full_content: false`, mention that an archive with full details exists and offer to retrieve it.

**No Results:**
If search returns nothing relevant, say so clearly rather than guessing. I may not have discussed that topic yet.

### Example Response Format

> Based on your knowledge base, your current view on **MLX layer selection** is:
> 
> *"For LoRA fine-tuning on MLX, select layers based on task specificity - earlier layers for domain adaptation, later layers for output format changes."*
> 
> **Key insights:**
> - Layer selection significantly impacts training efficiency
> - You've had success with selective layer training on code models
> 
> This is a **high confidence** active entry from December 2024. Want me to pull the full evolution history?

## My Known Preferences (from knowledge base patterns)

Based on the knowledge system's content, these patterns apply:
- I work on quantitative finance and trading systems
- I'm skeptical of bitcoin as corporate treasury strategy
- I prefer explicit error handling over silent failures
- I use Claude Code extensively and have optimized workflows
- I value provenance and evidence-based reasoning

## Integration with MCP Connector

This skill works with the **Personal Knowledge** MCP connector at:
`https://personal-knowledge-mcp.arjun-divecha.workers.dev/sse`

Ensure this connector is enabled in your Claude settings for this skill to function.

