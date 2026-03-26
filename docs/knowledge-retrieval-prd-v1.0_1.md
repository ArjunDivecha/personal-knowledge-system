# Product Requirements Document: Knowledge Retrieval Layer

**Version:** 1.0
**Author:** Claude + Arjun
**Date:** December 2024 (updated March 2026)
**Status:** Implemented
**Depends On:** Knowledge Distillation Pipeline PRD v1.1
**Note:** MCP server is deployed on Cloudflare Workers. Now serves entries from 5+ sources including auto-ingested Claude Code and Codex CLI sessions.

---

## 1. Overview

### 1.1 Purpose

The Knowledge Retrieval Layer makes distilled personal knowledge accessible during Claude conversations. It consists of two components:

1. **MCP Server**: A remote API exposing tools that retrieve knowledge from Upstash Redis/Vector
2. **Claude Skill**: Routing logic that determines when and how to call MCP tools

Together, they enable Claude to "remember" past conversations, understand your current projects, and provide contextually-aware responses without you re-explaining background every time.

### 1.2 User Experience Goal

When you ask Claude:
- "What's my current thinking on volatility trading?" → Claude retrieves your knowledge entry on that topic
- "Continue where we left off on Opus Ensemble" → Claude knows the project status and blockers
- "How does X relate to what we discussed about Y?" → Claude searches across your knowledge base

This should feel like talking to someone who actually knows your work, not an LLM with amnesia.

### 1.3 Scope

This PRD covers:
- MCP server implementation and tool definitions
- Claude Skill definition (SKILL.md)
- Integration with Claude clients (web, desktop, mobile)
- Profile Preferences configuration

Out of scope:
- Distillation pipeline (see separate PRD)
- Storage layer implementation details
- Admin/management UI

### 1.4 Success Criteria

| Metric | Target |
|--------|--------|
| Tool response latency (p50) | <100ms |
| Tool response latency (p99) | <200ms |
| Successful retrieval rate | >95% (when relevant content exists) |
| False positive rate | <10% (retrieving irrelevant content) |
| User satisfaction | "Feels like Claude knows my work" |

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Claude Conversation                      │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Profile Preferences                        │
│  (~800 chars: identity, domains, communication style)        │
│  Always injected. Triggers Skill when relevant.              │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      Claude Skill                            │
│  SKILL.md: Routing logic for MCP tool selection              │
│  Loaded when relevant topics detected in conversation        │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      MCP Server                              │
│  Vercel Edge Functions @ knowledge.yourdomain.com            │
│                                                              │
│  Tools:                                                      │
│  ├── get_index()      → Topic map + active projects          │
│  ├── get_context()    → Current view + key insights          │
│  ├── get_deep()       → Full content with evidence           │
│  └── search()         → Semantic search across all           │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Storage Layer                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐      │
│  │ Upstash     │    │ Upstash     │    │ Dropbox     │      │
│  │ Redis       │    │ Vector      │    │ (Archive)   │      │
│  │ Entries     │    │ Embeddings  │    │ Full content│      │
│  └─────────────┘    └─────────────┘    └─────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### 2.1 Layer Responsibilities

| Layer | Responsibility | Latency Impact |
|-------|----------------|----------------|
| Profile Preferences | Always-on identity context | +0ms |
| Skill | Route to correct tool based on query type | +0ms (loaded) |
| MCP Server | Retrieve and format knowledge | +50-150ms |
| Storage | Serve data | Included above |

---

## 3. Profile Preferences

### 3.1 Purpose

Profile Preferences provide always-on context that:
1. Establishes identity (role, domains, style)
2. Triggers Skill loading when relevant topics appear
3. Stays within character limits (~1500 usable chars)

### 3.2 Configuration

Location: Claude Settings → Profile → "What would you like Claude to know about you?"

```markdown
## Identity
Investment analyst at GMO focused on systematic strategies. Building AI tools for trading and research workflows.

## Active Domains
- Volatility trading (VIX regimes, mean reversion)
- MLX fine-tuning (local LLMs, LoRA techniques)
- Agentic coding (SWE-Bench, multi-agent systems)
- Personal knowledge systems

## Projects
- Opus Ensemble: Multi-agent SWE-Bench solver
- Trading systems: Volatility reversal strategies
- Knowledge distillation: This system

## Style
Direct communication. Skip disclaimers. Show reasoning. Code in Python unless specified.

## Knowledge System
I have a personal knowledge system accessible via MCP. When I reference past work, my current thinking on topics, or ongoing projects, use the knowledge tools to retrieve context rather than asking me to re-explain.
```

**Character count:** ~850 characters

### 3.3 Trigger Phrases

The Profile Preferences prime Claude to recognize when the Skill should activate:

| Phrase Pattern | Expected Action |
|----------------|-----------------|
| "What do I think about X" | get_context(X) |
| "Continue on [project]" | get_deep(project) |
| "My current view on" | get_context(topic) |
| "What we discussed about" | search(topic) |
| "How does X relate to Y" | search() then get_context() |
| Mentions of listed domains | get_index() then targeted retrieval |

---

## 4. Claude Skill Definition

### 4.1 What is a Skill?

A Claude Skill is a capability extension uploaded as a ZIP file containing:
- `SKILL.md`: Instructions and routing logic
- Optional supporting files (examples, schemas)

Skills are loaded contextually when Claude determines they're relevant to the conversation.

### 4.2 Skill Package Structure

```
personal-knowledge-skill/
├── SKILL.md           # Main instruction file
├── examples/
│   ├── retrieval-examples.md
│   └── edge-cases.md
└── schemas/
    └── entry-schemas.md
```

### 4.3 SKILL.md

```markdown
# Personal Knowledge System Skill

## Purpose

You have access to a personal knowledge system that contains distilled insights from past conversations. This system helps you understand what the user already knows, their current positions on topics, and their active projects—so conversations can pick up where they left off.

## When to Use

Activate this skill when the user:
- References past conversations or decisions
- Asks about their current thinking/view on a topic
- Mentions an active project by name
- Uses phrases like "what do I think about", "my position on", "where we left off"
- Discusses topics in their known domains (volatility trading, MLX, agentic coding)
- Seems to expect you to have context you don't have in the conversation

Do NOT use when:
- The user is asking general knowledge questions unrelated to their work
- The user is teaching you something new (no prior knowledge to retrieve)
- The conversation has sufficient context already
- The user explicitly says "without checking my notes" or similar

## Available Tools

### get_index()
**Purpose:** Get a map of all topics and projects in the knowledge system.  
**When to use:** 
- Start of relevant conversations to orient yourself
- When user asks "what are my active projects" or "what topics do we have notes on"
- When you need to find the right entry to retrieve

**Returns:** Thin index with topics, projects, recent evolutions (~1-2k tokens)

**Example usage:**
```
User: "Let's pick up where we left off"
Action: Call get_index() to see what's active, then ask which project/topic to continue
```

### get_context(topic: string)
**Purpose:** Get the current view and key insights for a specific topic.  
**When to use:**
- User asks "what's my thinking on X"
- You need to understand their position before responding
- Quick context on a known domain

**Returns:** Current view, confidence, key insights, related repos (~500 tokens)

**Example usage:**
```
User: "What's my current view on volatility regime detection?"
Action: Call get_context("volatility regime detection")
Response: Share their documented position with evidence
```

### get_deep(topic: string)
**Purpose:** Get full entry including evidence, evolution history, and all details.  
**When to use:**
- User wants to review how their thinking evolved
- You need code references or specific evidence
- User asks for the "full picture" on something
- Project status with all decisions and blockers

**Returns:** Complete entry with provenance (~2k tokens)

**Example usage:**
```
User: "Walk me through how my thinking on MLX fine-tuning has evolved"
Action: Call get_deep("MLX fine-tuning")
Response: Trace through the evolution array, citing evidence
```

### search(query: string, limit: int = 5)
**Purpose:** Semantic search across all knowledge entries.  
**When to use:**
- User asks how topics relate to each other
- Looking for something but don't know exact domain name
- Cross-referencing across multiple entries

**Returns:** Ranked list of relevant entries with summaries (~1k tokens)

**Example usage:**
```
User: "Have we discussed anything about learning rate scheduling?"
Action: Call search("learning rate scheduling")
Response: Summarize what was found or note if nothing relevant exists
```

## Response Guidelines

### When Retrieving Knowledge

1. **Cite provenance when relevant**: If sharing an insight, mention it came from a past conversation
   - Good: "Based on your November discussion about LoRA, you concluded that..."
   - Avoid: Presenting retrieved knowledge as if you independently deduced it

2. **Acknowledge contested states**: If an entry is marked "contested", present both positions
   - "You have two views on this: [older view] vs [newer view]. Which is current?"

3. **Surface evolution**: When relevant, show how thinking changed
   - "You initially thought X, but after [trigger], shifted to Y"

4. **Handle missing context**: If retrieval returns nothing relevant
   - "I don't have any notes on that topic. Would you like to explain your current thinking?"

5. **Don't over-retrieve**: One targeted retrieval is usually better than multiple
   - Start with get_index() only if truly orienting
   - Go directly to get_context() if you know the topic

### Formatting Retrieved Content

When presenting retrieved knowledge:

```markdown
**Your current view on [topic]:** [current_view]

Key insights from past discussions:
- [insight 1] (from [date/context])
- [insight 2]

Related work: [repo links if present]
```

For projects:

```markdown
**[Project Name]** - [status]

Goal: [goal]
Current phase: [phase]
Blocked on: [blocker or "nothing currently"]

Recent decisions:
- [decision] — [rationale]
```

## Edge Cases

### Multiple Matches
If search returns multiple relevant entries, summarize the top 2-3 and ask which to explore:
"I found notes on [topic A], [topic B], and [topic C]. Which would you like to discuss?"

### Stale Content
If entry hasn't been updated in 90+ days and is marked "stale":
"Your last notes on this are from [date]. Want to review and update?"

### Compressed Entries
If entry is compressed and user needs full detail:
"I have a summary of this. The full notes are archived—want me to share what I have, or should we reconstruct from memory?"

### No Relevant Knowledge
Be direct: "I don't have notes on [topic]. This might be new, or not yet distilled from recent conversations."

## Anti-Patterns

❌ **Don't retrieve for every message** — Only when genuinely useful  
❌ **Don't dump full entries** — Synthesize and summarize  
❌ **Don't claim certainty from old notes** — Acknowledge when things may have changed  
❌ **Don't ignore the user's current statement** — Retrieved knowledge supplements, doesn't override  
❌ **Don't make up provenance** — If you don't have evidence, don't fabricate sources  

## Integration with Native Memory

Claude has its own memory system that extracts facts from conversations. This skill complements that:
- Claude Memory: Discrete facts, preferences, biographical info
- Knowledge System: Structured positions, project states, evolved thinking

Don't duplicate what Claude Memory already handles. Focus on:
- Complex, nuanced positions (not one-line facts)
- Project context with decisions and rationale
- Evolution of thinking over time
- Connections to code/repos
```

### 4.4 Supporting Files

**examples/retrieval-examples.md:**
```markdown
# Retrieval Examples

## Example 1: Topic Context

User: "What's my take on using ensemble methods for SWE-Bench?"

Claude's process:
1. Recognize this is asking for user's position on a known topic
2. Call get_context("ensemble methods SWE-Bench") or get_context("Opus Ensemble")
3. Present the retrieved current_view
4. Offer to go deeper if they want evolution/evidence

Response:
"Your current approach is parallel generation with voting—you found sequential refinement 
too slow for the accuracy gains. Key insight: generating 5+ attempts in parallel and 
selecting the best outperforms iterative improvement on a single attempt. This is 
documented in your Opus Ensemble project notes from early December."

## Example 2: Project Status

User: "Where are we on the trading system?"

Claude's process:
1. Call get_deep("trading systems") or get_index() if unsure of exact project name
2. Return project status with current phase and blockers

Response:
"Your volatility reversal trading project is in backtesting phase. Last decision: 
focusing on VIX 20-30 regime where mean reversion is strongest. Currently blocked 
on 'regime detection latency'—you were exploring whether to use rolling stats or 
a trained classifier. Want to pick up there?"

## Example 3: Cross-Reference

User: "Does anything in my notes connect MLX work to the trading systems?"

Claude's process:
1. Call search("MLX trading") 
2. If no direct hits, call get_context for each and look for links

Response:
"Looking at your notes... You haven't explicitly connected these, but there's a 
potential link: you've noted that small local models (MLX) could run regime 
classifiers with lower latency than API calls. That connects to your trading 
system's 'regime detection latency' blocker. Want to explore that?"

## Example 4: Nothing Found

User: "What do I think about using Rust for this?"

Claude's process:
1. Call search("Rust") or get_context("Rust")
2. No results or low relevance results

Response:
"I don't have any notes on Rust in your knowledge system. If you've discussed it 
recently, it might not be distilled yet (updates run weekly). What's your 
thinking—are you considering Rust for the trading system or something else?"
```

**schemas/entry-schemas.md:**
```markdown
# Entry Schemas

## Knowledge Entry (Summarized)

When get_context() returns:

```yaml
id: "ke_xxx"
domain: "topic name"
state: "active" | "contested" | "stale"
current_view: "1-3 sentence summary"
confidence: "high" | "medium" | "low"
key_insights:
  - insight: "the learning"
    evidence_date: "when"
related_repos:
  - repo: "owner/name"
    confidence: 0.95
last_updated: "ISO date"
```

## Project Entry (Summarized)

When get_context() returns for a project:

```yaml
id: "pe_xxx"
name: "Project Name"
status: "active" | "paused" | "completed"
goal: "what it achieves"
current_phase: "where in the work"
blocked_on: "current obstacle or null"
recent_decisions:
  - decision: "what was decided"
    date: "when"
last_touched: "ISO date"
```

## Full Entry (get_deep)

Includes all above plus:
- Complete evidence blocks with message snippets
- Full evolution history with from_view/to_view
- All related knowledge entries
- All capabilities (knows_how_to)
- Open questions
```

---

## 5. MCP Server Implementation

### 5.1 Infrastructure

**Platform:** Vercel Edge Functions  
**Domain:** `knowledge-mcp.yourdomain.com` (or Vercel subdomain)  
**Runtime:** Edge Runtime (not Node.js) for lowest latency

### 5.2 Project Structure

```
knowledge-mcp/
├── vercel.json
├── package.json
├── src/
│   ├── index.ts              # MCP server entry point
│   ├── tools/
│   │   ├── get-index.ts
│   │   ├── get-context.ts
│   │   ├── get-deep.ts
│   │   └── search.ts
│   ├── storage/
│   │   ├── redis.ts          # Upstash Redis client
│   │   └── vector.ts         # Upstash Vector client
│   ├── types/
│   │   └── entries.ts        # TypeScript types for entries
│   └── utils/
│       ├── formatting.ts     # Response formatting
│       └── auth.ts           # Authentication
└── .env.example
```

### 5.3 vercel.json

```json
{
  "version": 2,
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "functions": {
    "src/index.ts": {
      "runtime": "edge",
      "maxDuration": 10
    }
  },
  "routes": [
    {
      "src": "/mcp/(.*)",
      "dest": "/src/index.ts"
    }
  ],
  "env": {
    "UPSTASH_REDIS_REST_URL": "@upstash_redis_url",
    "UPSTASH_REDIS_REST_TOKEN": "@upstash_redis_token",
    "UPSTASH_VECTOR_REST_URL": "@upstash_vector_url",
    "UPSTASH_VECTOR_REST_TOKEN": "@upstash_vector_token",
    "MCP_AUTH_TOKEN": "@mcp_auth_token"
  }
}
```

### 5.4 Tool Specifications

#### get_index

```typescript
// src/tools/get-index.ts

import { redis } from '../storage/redis';
import { MCPToolResult } from '../types';

export const getIndexTool = {
  name: 'get_index',
  description: 'Get the thin index of all topics and active projects',
  inputSchema: {
    type: 'object',
    properties: {},
    required: []
  },
  
  async execute(): Promise<MCPToolResult> {
    const startTime = Date.now();
    
    try {
      // Fetch thin index from Redis
      const index = await redis.get('index:current');
      
      if (!index) {
        return {
          success: true,
          data: {
            topics: [],
            projects: [],
            message: 'Knowledge system is empty. No entries have been distilled yet.'
          },
          latency_ms: Date.now() - startTime
        };
      }
      
      const parsed = JSON.parse(index);
      
      return {
        success: true,
        data: {
          generated_at: parsed.generated_at,
          topics: parsed.topics,
          projects: parsed.projects,
          recent_evolutions: parsed.recent_evolutions,
          contested_count: parsed.contested_count,
          summary: `${parsed.topics.length} topics, ${parsed.projects.length} projects`
        },
        latency_ms: Date.now() - startTime
      };
      
    } catch (error) {
      return {
        success: false,
        error: `Failed to retrieve index: ${error.message}`,
        latency_ms: Date.now() - startTime
      };
    }
  }
};
```

**Response format:**
```yaml
success: true
data:
  generated_at: "2024-12-15T10:00:00Z"
  topics:
    - id: "ke_abc123"
      domain: "MLX fine-tuning"
      current_view_summary: "All layers > attention-only (LoRA Without Regret)"
      state: "active"
      confidence: "high"
      last_updated: "2024-12-10"
      top_repo: "mlx-experiments"
  projects:
    - id: "pe_xyz789"
      name: "Opus Ensemble"
      status: "active"
      goal_summary: "72%+ SWE-Bench via parallel generation"
      current_phase: "architecture_iteration"
      blocked_on: "Voting mechanism design"
      last_touched: "2024-12-15"
  recent_evolutions:
    - entry_id: "ke_abc123"
      domain_or_name: "MLX fine-tuning"
      delta_summary: "Switched to all-layer training"
      date: "2024-11-15"
  contested_count: 1
  summary: "12 topics, 3 projects"
latency_ms: 45
```

#### get_context

```typescript
// src/tools/get-context.ts

import { redis } from '../storage/redis';
import { vector } from '../storage/vector';
import { formatContextResponse } from '../utils/formatting';
import { MCPToolResult } from '../types';

export const getContextTool = {
  name: 'get_context',
  description: 'Get current view and key insights for a specific topic or project',
  inputSchema: {
    type: 'object',
    properties: {
      topic: {
        type: 'string',
        description: 'Topic domain or project name to retrieve'
      }
    },
    required: ['topic']
  },
  
  async execute({ topic }: { topic: string }): Promise<MCPToolResult> {
    const startTime = Date.now();
    
    try {
      // First, try exact match on domain/name
      let entry = await findExactMatch(topic);
      
      // If no exact match, use semantic search
      if (!entry) {
        const searchResults = await vector.query({
          vector: await embed(topic),
          topK: 1,
          includeMetadata: true
        });
        
        if (searchResults.length > 0 && searchResults[0].score > 0.75) {
          const entryId = searchResults[0].id;
          entry = await redis.get(`knowledge:${entryId}`) || 
                  await redis.get(`project:${entryId}`);
          entry = entry ? JSON.parse(entry) : null;
        }
      }
      
      if (!entry) {
        return {
          success: true,
          data: {
            found: false,
            message: `No knowledge entry found for "${topic}". This topic may not have been discussed yet, or recent conversations haven't been distilled.`,
            suggestion: 'Try get_index() to see available topics, or search() for related content.'
          },
          latency_ms: Date.now() - startTime
        };
      }
      
      // Update access count (fire and forget)
      incrementAccessCount(entry.id);
      
      // Format response based on entry type
      const formatted = formatContextResponse(entry);
      
      return {
        success: true,
        data: {
          found: true,
          ...formatted
        },
        latency_ms: Date.now() - startTime
      };
      
    } catch (error) {
      return {
        success: false,
        error: `Failed to retrieve context: ${error.message}`,
        latency_ms: Date.now() - startTime
      };
    }
  }
};

async function findExactMatch(topic: string) {
  const normalized = topic.toLowerCase().trim();
  
  // Check knowledge entries
  const knowledgeIds = await redis.smembers(`by_domain:${normalized}`);
  if (knowledgeIds.length > 0) {
    const entry = await redis.get(`knowledge:${knowledgeIds[0]}`);
    return entry ? JSON.parse(entry) : null;
  }
  
  // Check project entries
  const projectIds = await redis.smembers(`by_name:${normalized}`);
  if (projectIds.length > 0) {
    const entry = await redis.get(`project:${projectIds[0]}`);
    return entry ? JSON.parse(entry) : null;
  }
  
  return null;
}
```

**Response format (knowledge entry):**
```yaml
success: true
data:
  found: true
  type: "knowledge"
  id: "ke_abc123"
  domain: "MLX fine-tuning"
  state: "active"
  
  current_view: "Train all layers, not just attention blocks. The 'LoRA Without Regret' paper shows full-layer fine-tuning outperforms attention-only on small models when you have sufficient compute."
  confidence: "high"
  
  key_insights:
    - insight: "Small models benefit more from full fine-tuning than large models"
      evidence_summary: "From November discussion about LoRA paper"
    - insight: "Learning rate scheduling matters more than layer selection"
      evidence_summary: "From December MLX experiments"
  
  related_repos:
    - repo: "arjun/mlx-experiments"
      path: "/fine-tuning"
  
  open_questions:
    - "Optimal batch size for 8GB unified memory"
  
  last_updated: "2024-12-10"
  access_count: 4
latency_ms: 62
```

**Response format (project entry):**
```yaml
success: true
data:
  found: true
  type: "project"
  id: "pe_xyz789"
  name: "Opus Ensemble"
  status: "active"
  
  goal: "Achieve 72%+ on SWE-Bench Verified through parallel generation and intelligent voting"
  current_phase: "architecture_iteration"
  blocked_on: "Voting mechanism design—exploring majority vote vs. LLM-as-judge"
  
  recent_decisions:
    - decision: "Parallel generation over sequential refinement"
      rationale: "Higher throughput, easier to scale"
      date: "2024-12-01"
    - decision: "5 parallel attempts as baseline"
      rationale: "Diminishing returns beyond 5 for most issues"
      date: "2024-12-08"
  
  tech_stack: ["Python", "MLX", "FastAPI"]
  
  related_repos:
    - repo: "arjun/opus-ensemble"
      is_primary: true
  
  last_touched: "2024-12-15"
latency_ms: 58
```

#### get_deep

```typescript
// src/tools/get-deep.ts

import { redis } from '../storage/redis';
import { vector } from '../storage/vector';
import { MCPToolResult } from '../types';

export const getDeepTool = {
  name: 'get_deep',
  description: 'Get full entry with complete evidence, evolution history, and all details',
  inputSchema: {
    type: 'object',
    properties: {
      topic: {
        type: 'string',
        description: 'Topic domain or project name to retrieve in full'
      }
    },
    required: ['topic']
  },
  
  async execute({ topic }: { topic: string }): Promise<MCPToolResult> {
    const startTime = Date.now();
    
    try {
      // Find entry (same logic as get_context)
      let entry = await findEntry(topic);
      
      if (!entry) {
        return {
          success: true,
          data: {
            found: false,
            message: `No entry found for "${topic}".`
          },
          latency_ms: Date.now() - startTime
        };
      }
      
      // Update access count
      incrementAccessCount(entry.id);
      
      // For compressed entries, note that full content is archived
      if (entry.detail_level === 'compressed') {
        return {
          success: true,
          data: {
            found: true,
            compressed: true,
            message: 'This entry has been compressed. Showing available summary.',
            entry: entry,
            full_content_available: !!entry.full_content_ref,
            archive_ref: entry.full_content_ref
          },
          latency_ms: Date.now() - startTime
        };
      }
      
      // Return full entry
      return {
        success: true,
        data: {
          found: true,
          compressed: false,
          entry: entry
        },
        latency_ms: Date.now() - startTime
      };
      
    } catch (error) {
      return {
        success: false,
        error: `Failed to retrieve full entry: ${error.message}`,
        latency_ms: Date.now() - startTime
      };
    }
  }
};
```

**Response format (full entry):**
```yaml
success: true
data:
  found: true
  compressed: false
  entry:
    id: "ke_abc123"
    type: "knowledge"
    domain: "MLX fine-tuning"
    state: "active"
    detail_level: "full"
    
    current_view: "Train all layers, not just attention blocks..."
    confidence: "high"
    
    positions:
      - view: "Train all layers for small models"
        confidence: "high"
        as_of: "2024-12-10"
        evidence:
          conversation_id: "claude_xyz"
          message_ids: ["msg_123", "msg_125"]
          snippet: "The LoRA Without Regret paper shows..."
    
    key_insights:
      - insight: "Small models benefit more from full fine-tuning"
        evidence:
          conversation_id: "claude_xyz"
          message_ids: ["msg_127"]
          snippet: "Interestingly, the effect is stronger for..."
    
    knows_how_to:
      - capability: "Set up MLX training pipeline with custom datasets"
        evidence:
          conversation_id: "claude_abc"
          message_ids: ["msg_45"]
    
    open_questions:
      - question: "Optimal batch size for 8GB unified memory"
        evidence:
          conversation_id: "claude_xyz"
          message_ids: ["msg_130"]
    
    related_repos:
      - repo: "arjun/mlx-experiments"
        path: "/fine-tuning"
        link_type: "explicit"
        confidence: 1.0
    
    evolution:
      - delta: "Changed from attention-only to all-layer training"
        trigger: "LoRA Without Regret paper discussion"
        from_view: "Focus on attention layers only for efficiency"
        to_view: "Train all layers when compute allows"
        date: "2024-11-15"
        evidence:
          conversation_id: "claude_xyz"
          message_ids: ["msg_120", "msg_123"]
    
    metadata:
      created_at: "2024-11-01"
      updated_at: "2024-12-10"
      source_conversations: ["claude_abc", "claude_xyz"]
      source_messages: ["msg_45", "msg_120", "msg_123", "msg_125", "msg_127", "msg_130"]
      access_count: 5
latency_ms: 78
```

#### search

```typescript
// src/tools/search.ts

import { vector } from '../storage/vector';
import { redis } from '../storage/redis';
import { MCPToolResult } from '../types';

export const searchTool = {
  name: 'search',
  description: 'Semantic search across all knowledge entries',
  inputSchema: {
    type: 'object',
    properties: {
      query: {
        type: 'string',
        description: 'Search query'
      },
      limit: {
        type: 'number',
        description: 'Maximum results to return',
        default: 5
      }
    },
    required: ['query']
  },
  
  async execute({ query, limit = 5 }: { query: string; limit?: number }): Promise<MCPToolResult> {
    const startTime = Date.now();
    
    try {
      // Generate embedding for query
      const queryEmbedding = await embed(query);
      
      // Search vector store
      const results = await vector.query({
        vector: queryEmbedding,
        topK: limit,
        includeMetadata: true
      });
      
      if (results.length === 0) {
        return {
          success: true,
          data: {
            found: false,
            results: [],
            message: `No relevant entries found for "${query}".`
          },
          latency_ms: Date.now() - startTime
        };
      }
      
      // Fetch entry summaries for results
      const entries = await Promise.all(
        results.map(async (result) => {
          const entryKey = result.metadata.type === 'knowledge' 
            ? `knowledge:${result.id}` 
            : `project:${result.id}`;
          const entry = await redis.get(entryKey);
          
          if (!entry) return null;
          
          const parsed = JSON.parse(entry);
          return {
            id: result.id,
            type: result.metadata.type,
            domain_or_name: parsed.domain || parsed.name,
            current_view_summary: truncate(parsed.current_view || parsed.goal, 100),
            state: parsed.state || parsed.status,
            relevance_score: result.score,
            last_updated: parsed.metadata?.updated_at || parsed.metadata?.last_touched
          };
        })
      );
      
      const validEntries = entries.filter(Boolean);
      
      return {
        success: true,
        data: {
          found: true,
          query: query,
          results: validEntries,
          message: `Found ${validEntries.length} relevant entries.`
        },
        latency_ms: Date.now() - startTime
      };
      
    } catch (error) {
      return {
        success: false,
        error: `Search failed: ${error.message}`,
        latency_ms: Date.now() - startTime
      };
    }
  }
};
```

**Response format:**
```yaml
success: true
data:
  found: true
  query: "learning rate scheduling"
  results:
    - id: "ke_abc123"
      type: "knowledge"
      domain_or_name: "MLX fine-tuning"
      current_view_summary: "Train all layers, not just attention blocks. Learning rate scheduling matters more than..."
      state: "active"
      relevance_score: 0.87
      last_updated: "2024-12-10"
    - id: "ke_def456"
      type: "knowledge"
      domain_or_name: "Neural network training basics"
      current_view_summary: "Standard practices for training small models, including warmup and cosine decay..."
      state: "stale"
      relevance_score: 0.72
      last_updated: "2024-09-15"
  message: "Found 2 relevant entries."
latency_ms: 95
```

### 5.5 MCP Server Entry Point

```typescript
// src/index.ts

import { MCPServer } from '@anthropic/mcp-server';
import { getIndexTool } from './tools/get-index';
import { getContextTool } from './tools/get-context';
import { getDeepTool } from './tools/get-deep';
import { searchTool } from './tools/search';
import { authenticateRequest } from './utils/auth';

const server = new MCPServer({
  name: 'personal-knowledge',
  version: '1.0.0',
  description: 'Personal knowledge system for retrieving distilled conversation insights'
});

// Register tools
server.addTool(getIndexTool);
server.addTool(getContextTool);
server.addTool(getDeepTool);
server.addTool(searchTool);

// Export handler for Vercel Edge
export default async function handler(request: Request): Promise<Response> {
  // Authenticate
  const authResult = await authenticateRequest(request);
  if (!authResult.success) {
    return new Response(JSON.stringify({ error: 'Unauthorized' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' }
    });
  }
  
  // Handle MCP request
  return server.handleRequest(request);
}

export const config = {
  runtime: 'edge'
};
```

### 5.6 Authentication

```typescript
// src/utils/auth.ts

export async function authenticateRequest(request: Request): Promise<{ success: boolean }> {
  // MCP uses bearer token authentication
  const authHeader = request.headers.get('Authorization');
  
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    return { success: false };
  }
  
  const token = authHeader.slice(7);
  const expectedToken = process.env.MCP_AUTH_TOKEN;
  
  if (!expectedToken || token !== expectedToken) {
    return { success: false };
  }
  
  return { success: true };
}
```

---

## 6. Integration & Configuration

### 6.1 Claude MCP Connector Setup

**Location:** claude.ai → Settings → Connectors → Add MCP Server

**Configuration:**
```yaml
name: "Personal Knowledge"
url: "https://knowledge-mcp.yourdomain.com/mcp"
authentication:
  type: "bearer"
  token: "<your-mcp-auth-token>"
```

**Platform availability:**
- ✅ Claude.ai (web)
- ✅ Claude Desktop (synced from web)
- ✅ Claude iOS (synced from web)
- ✅ Claude Android (synced from web)

**Note:** MCP connectors can only be added via the web interface, but once added they sync to all clients automatically.

### 6.2 Skill Upload

**Location:** claude.ai → Settings → Skills → Upload Skill

**Package:** ZIP file containing SKILL.md and supporting files

**Requirements:**
- Claude Pro, Max, Team, or Enterprise plan
- Skill files must be under size limit (check current docs)

### 6.3 Environment Variables

Required environment variables for MCP server:

```bash
# Upstash Redis
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxx

# Upstash Vector
UPSTASH_VECTOR_REST_URL=https://xxx.upstash.io
UPSTASH_VECTOR_REST_TOKEN=xxx

# MCP Authentication
MCP_AUTH_TOKEN=<generate-secure-token>

# Optional: Embedding API (if generating embeddings in MCP server)
OPENAI_API_KEY=sk-xxx
```

---

## 7. Latency Budget

### 7.1 Target Latencies

| Tool | Target p50 | Target p99 | Budget Breakdown |
|------|------------|------------|------------------|
| get_index | 50ms | 100ms | Redis read: 5ms, Network: 40ms |
| get_context | 75ms | 150ms | Vector query: 20ms, Redis: 5ms, Network: 40ms |
| get_deep | 75ms | 150ms | Redis read: 10ms, Network: 40ms |
| search | 100ms | 200ms | Embed: 30ms, Vector: 30ms, Redis: 20ms, Network: 40ms |

### 7.2 Optimization Strategies

1. **Edge deployment**: Vercel Edge puts compute close to user
2. **Connection reuse**: Maintain warm connections to Upstash
3. **Thin responses**: Only return what's needed (context vs deep)
4. **Cached embeddings**: Pre-compute query embeddings for common terms
5. **Index caching**: Consider caching thin index at edge (TTL: 5 min)

### 7.3 Monitoring

Track via Vercel Analytics + custom logging:

```typescript
// Log every tool call
console.log(JSON.stringify({
  tool: toolName,
  latency_ms: endTime - startTime,
  success: result.success,
  cache_hit: cacheHit,
  timestamp: new Date().toISOString()
}));
```

---

## 8. Error Handling

### 8.1 Graceful Degradation

| Error Type | Handling |
|------------|----------|
| Redis unavailable | Return cached thin index if available, else error message |
| Vector unavailable | Fall back to keyword matching on domain names |
| Timeout | Return partial results with warning |
| Entry not found | Clear message, suggest alternatives |
| Auth failure | 401 response, Claude handles gracefully |

### 8.2 Error Response Format

```yaml
success: false
error: "Human-readable error message"
error_code: "REDIS_UNAVAILABLE" | "VECTOR_TIMEOUT" | "NOT_FOUND" | etc
suggestion: "Try get_index() to see available topics"
latency_ms: 150
```

### 8.3 Skill Fallback Behavior

When MCP tools fail, the Skill should instruct Claude to:

```markdown
## When Tools Fail

If a knowledge tool returns an error:
1. Acknowledge the issue briefly: "I couldn't retrieve your notes on that topic."
2. Offer to proceed without stored knowledge: "Want to tell me your current thinking?"
3. Don't repeatedly retry—one attempt is enough.
4. Never pretend to have knowledge you couldn't retrieve.
```

---

## 9. Testing

### 9.1 Tool Tests

```typescript
// tests/tools.test.ts

describe('get_index', () => {
  it('returns thin index when data exists', async () => {
    // Setup: seed Redis with test index
    await redis.set('index:current', JSON.stringify(testIndex));
    
    const result = await getIndexTool.execute();
    
    expect(result.success).toBe(true);
    expect(result.data.topics.length).toBeGreaterThan(0);
    expect(result.latency_ms).toBeLessThan(100);
  });
  
  it('returns empty state when no data', async () => {
    // Setup: ensure no index exists
    await redis.del('index:current');
    
    const result = await getIndexTool.execute();
    
    expect(result.success).toBe(true);
    expect(result.data.topics).toEqual([]);
    expect(result.data.message).toContain('empty');
  });
});

describe('get_context', () => {
  it('finds exact domain match', async () => {
    // Setup: seed entry
    await seedKnowledgeEntry('MLX fine-tuning');
    
    const result = await getContextTool.execute({ topic: 'MLX fine-tuning' });
    
    expect(result.success).toBe(true);
    expect(result.data.found).toBe(true);
    expect(result.data.domain).toBe('MLX fine-tuning');
  });
  
  it('finds via semantic search when no exact match', async () => {
    await seedKnowledgeEntry('MLX fine-tuning');
    
    const result = await getContextTool.execute({ topic: 'local LLM training' });
    
    expect(result.success).toBe(true);
    expect(result.data.found).toBe(true);
    // Should find MLX entry via semantic similarity
  });
  
  it('handles not found gracefully', async () => {
    const result = await getContextTool.execute({ topic: 'quantum computing' });
    
    expect(result.success).toBe(true);
    expect(result.data.found).toBe(false);
    expect(result.data.message).toContain('No knowledge entry found');
  });
});

describe('search', () => {
  it('returns ranked results', async () => {
    await seedMultipleEntries();
    
    const result = await searchTool.execute({ query: 'trading strategies', limit: 3 });
    
    expect(result.success).toBe(true);
    expect(result.data.results.length).toBeLessThanOrEqual(3);
    expect(result.data.results[0].relevance_score).toBeGreaterThan(0.7);
  });
});
```

### 9.2 Integration Tests

```typescript
// tests/integration.test.ts

describe('MCP Server Integration', () => {
  it('handles full request/response cycle', async () => {
    const request = new Request('https://test.com/mcp', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${process.env.MCP_AUTH_TOKEN}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        tool: 'get_index',
        arguments: {}
      })
    });
    
    const response = await handler(request);
    
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.success).toBe(true);
  });
  
  it('rejects unauthenticated requests', async () => {
    const request = new Request('https://test.com/mcp', {
      method: 'POST',
      body: JSON.stringify({ tool: 'get_index', arguments: {} })
    });
    
    const response = await handler(request);
    
    expect(response.status).toBe(401);
  });
});
```

### 9.3 End-to-End Testing

Manual testing checklist:

- [ ] Add MCP connector in Claude.ai Settings
- [ ] Verify connector shows as connected
- [ ] Upload Skill package
- [ ] Test: "What are my active projects?" → Should trigger get_index
- [ ] Test: "What's my view on [topic]?" → Should trigger get_context
- [ ] Test: "Show me everything on [project]" → Should trigger get_deep
- [ ] Test: "Have we discussed [vague term]?" → Should trigger search
- [ ] Verify responses include provenance when appropriate
- [ ] Test on mobile (iOS/Android) to confirm sync works

---

## 10. Deployment

### 10.1 Initial Setup

```bash
# 1. Create Vercel project
vercel init knowledge-mcp

# 2. Add environment variables
vercel env add UPSTASH_REDIS_REST_URL
vercel env add UPSTASH_REDIS_REST_TOKEN
vercel env add UPSTASH_VECTOR_REST_URL
vercel env add UPSTASH_VECTOR_REST_TOKEN
vercel env add MCP_AUTH_TOKEN

# 3. Deploy
vercel --prod

# 4. Note the deployment URL for Claude connector config
```

### 10.2 Custom Domain (Optional)

```bash
# Add custom domain
vercel domains add knowledge-mcp.yourdomain.com

# Update DNS with provided records
# Wait for SSL provisioning
```

### 10.3 Skill Deployment

1. Create ZIP of skill folder:
   ```bash
   cd personal-knowledge-skill
   zip -r ../personal-knowledge-skill.zip .
   ```

2. Upload to Claude.ai → Settings → Skills

3. Verify skill appears in capabilities list

### 10.4 Verification

```bash
# Test MCP endpoint directly
curl -X POST https://knowledge-mcp.yourdomain.com/mcp \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool": "get_index", "arguments": {}}'
```

---

## 11. Maintenance

### 11.1 Monitoring Checklist

Weekly:
- [ ] Check Vercel function invocation counts and errors
- [ ] Review latency percentiles
- [ ] Verify Upstash usage is within limits

Monthly:
- [ ] Review access patterns (which entries are retrieved most)
- [ ] Check for stale entries that should be deprecated
- [ ] Update Skill if routing patterns need adjustment

### 11.2 Updating the Skill

When updating SKILL.md:
1. Modify files locally
2. Create new ZIP
3. Upload to Claude Settings (replaces existing)
4. Test key scenarios

### 11.3 Updating MCP Server

```bash
# Make changes
git commit -am "Update MCP server"

# Deploy
vercel --prod

# Verify
curl -X POST https://knowledge-mcp.yourdomain.com/mcp \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -d '{"tool": "get_index"}'
```

---

## 12. Future Enhancements

### 12.1 Planned for v1.1

- **Retrieval feedback loop**: Track when retrieved content is useful vs ignored
- **Proactive suggestions**: Skill notices relevant topics and offers to retrieve
- **Cross-reference tool**: Explicit tool for "how does X relate to Y"

### 12.2 Considered for v2.0

- **Write-back capability**: Let Claude propose new entries from conversation
- **Conflict resolution UI**: Web interface for resolving contested entries
- **Multi-user support**: Shared knowledge bases for teams
- **Voice integration**: Retrieve knowledge during voice conversations

---

## 13. Appendix

### 13.1 MCP Protocol Reference

Claude's MCP (Model Context Protocol) enables external tool integration:

- **Transport**: HTTPS
- **Authentication**: Bearer token
- **Request format**: JSON with tool name and arguments
- **Response format**: JSON with success status and data

Full spec: https://docs.anthropic.com/en/docs/build-with-claude/mcp

### 13.2 Upstash Client Setup

```typescript
// src/storage/redis.ts
import { Redis } from '@upstash/redis';

export const redis = new Redis({
  url: process.env.UPSTASH_REDIS_REST_URL!,
  token: process.env.UPSTASH_REDIS_REST_TOKEN!
});

// src/storage/vector.ts
import { Index } from '@upstash/vector';

export const vector = new Index({
  url: process.env.UPSTASH_VECTOR_REST_URL!,
  token: process.env.UPSTASH_VECTOR_REST_TOKEN!
});
```

### 13.3 Embedding Generation

```typescript
// src/utils/embedding.ts
import OpenAI from 'openai';

const openai = new OpenAI({ apiKey: process.env.OPENAI_API_KEY });

export async function embed(text: string): Promise<number[]> {
  const response = await openai.embeddings.create({
    model: 'text-embedding-3-small',
    input: text
  });
  return response.data[0].embedding;
}
```

---

*Document Version: 1.0*  
*Last Updated: December 2024*
