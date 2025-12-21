# Personal Knowledge System - Implementation Guide

## Overview

Build a personal knowledge system that distills AI chat histories (Claude, GPT) into searchable, structured knowledge entries, then makes them accessible during future Claude conversations via MCP.

**Goal:** Make Claude "remember" your past conversations, understand your current projects, and provide contextually-aware responses without re-explaining background every time.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA FLOW                                    │
└─────────────────────────────────────────────────────────────────────┘

Raw Exports (Dropbox)          Distillation Pipeline (Vercel Cron)
├── Claude conversations.json   ├── Parse → Filter → Extract
└── ChatGPT conversations.json  ├── Link → Merge → Compress
                                └── Index → Write to storage
                                           │
                                           ▼
                        ┌─────────────────────────────────┐
                        │         Storage Layer           │
                        │  ┌──────────┐  ┌─────────────┐  │
                        │  │ Upstash  │  │  Upstash    │  │
                        │  │ Redis    │  │  Vector     │  │
                        │  │ Entries  │  │  Embeddings │  │
                        │  └──────────┘  └─────────────┘  │
                        └─────────────────────────────────┘
                                           │
                                           ▼
                        ┌─────────────────────────────────┐
                        │    MCP Server (Vercel Edge)     │
                        │  get_index | get_context        │
                        │  get_deep  | search             │
                        └─────────────────────────────────┘
                                           │
                                           ▼
                        ┌─────────────────────────────────┐
                        │      Claude Conversation        │
                        │  Profile Preferences + Skill    │
                        │  → Triggers MCP calls           │
                        └─────────────────────────────────┘
```

## Source Data Locations

**Claude exports:**
```
/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/Anthropic
```

**ChatGPT exports:**
```
/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/ChatGPT
```

These are manual exports from each platform. The pipeline should read from these directories.

## Project Structure

Create a monorepo with the following structure:

```
knowledge-system/
├── README.md
├── package.json                    # Workspace root
├── turbo.json                      # Turborepo config (optional)
│
├── apps/
│   ├── distillation/               # Batch processing pipeline
│   │   ├── package.json
│   │   ├── vercel.json
│   │   ├── tsconfig.json
│   │   └── src/
│   │       ├── api/
│   │       │   ├── distill/
│   │       │   │   ├── start.ts    # POST /api/distill/start
│   │       │   │   ├── process.ts  # POST /api/distill/process
│   │       │   │   └── finalize.ts # POST /api/distill/finalize
│   │       │   └── admin/
│   │       │       ├── runs.ts     # GET /api/admin/runs
│   │       │       └── entries.ts  # GET /api/admin/entries
│   │       ├── pipeline/
│   │       │   ├── parse.ts        # Stage 1: Parse exports
│   │       │   ├── filter.ts       # Stage 2: Filter by value
│   │       │   ├── extract.ts      # Stage 3: LLM extraction
│   │       │   ├── link.ts         # Stage 4: Repo linking
│   │       │   ├── merge.ts        # Stage 5: Merge with existing
│   │       │   ├── compress.ts     # Stage 6: Compress old entries
│   │       │   └── index.ts        # Stage 7: Write to storage
│   │       ├── prompts/
│   │       │   ├── extraction.ts   # Extraction prompt template
│   │       │   ├── compression.ts  # Compression prompt
│   │       │   └── verification.ts # Link verification prompt
│   │       ├── storage/
│   │       │   ├── redis.ts
│   │       │   ├── vector.ts
│   │       │   └── dropbox.ts
│   │       ├── types/
│   │       │   ├── entries.ts      # Knowledge/Project entry types
│   │       │   ├── pipeline.ts     # Pipeline types
│   │       │   └── exports.ts      # Claude/GPT export types
│   │       └── utils/
│   │           ├── embedding.ts
│   │           ├── llm.ts
│   │           └── logging.ts
│   │
│   └── mcp-server/                 # MCP retrieval server
│       ├── package.json
│       ├── vercel.json
│       ├── tsconfig.json
│       └── src/
│           ├── index.ts            # MCP server entry point
│           ├── tools/
│           │   ├── get-index.ts
│           │   ├── get-context.ts
│           │   ├── get-deep.ts
│           │   └── search.ts
│           ├── storage/
│           │   ├── redis.ts
│           │   └── vector.ts
│           ├── types/
│           │   └── mcp.ts
│           └── utils/
│               ├── auth.ts
│               └── formatting.ts
│
├── packages/
│   └── shared/                     # Shared types and utilities
│       ├── package.json
│       ├── tsconfig.json
│       └── src/
│           ├── types/
│           │   ├── knowledge-entry.ts
│           │   ├── project-entry.ts
│           │   └── thin-index.ts
│           └── utils/
│               ├── tokenizer.ts
│               └── truncate.ts
│
├── skill/                          # Claude Skill package
│   ├── SKILL.md
│   ├── examples/
│   │   └── retrieval-examples.md
│   └── schemas/
│       └── entry-schemas.md
│
├── docs/
│   ├── prd-distillation-v1.1.md   # Distillation PRD
│   └── prd-retrieval-v1.0.md      # Retrieval PRD
│
└── scripts/
    ├── setup.sh                    # Initial setup script
    ├── deploy.sh                   # Deploy both apps
    └── test-mcp.sh                 # Test MCP endpoints
```

## Implementation Order

### Phase 1: Infrastructure Setup

1. **Create Upstash accounts and resources**
   - Create Upstash Redis database (free tier)
   - Create Upstash Vector index (free tier, 1536 dimensions)
   - Save credentials

2. **Create Vercel project**
   - Initialize monorepo
   - Configure environment variables
   - Set up deployment

3. **Set up Dropbox API access** (for archiving compressed entries)
   - Create Dropbox app
   - Get access token
   - Configure archive folder path

### Phase 2: Shared Package

Build the shared types first since both apps depend on them.

**packages/shared/src/types/knowledge-entry.ts:**
```typescript
export interface Evidence {
  conversation_id: string;
  message_ids: string[];
  snippet: string;  // Max 200 chars
}

export interface Insight {
  insight: string;
  evidence: Evidence;
}

export interface Capability {
  capability: string;
  evidence: Evidence;
}

export interface OpenQuestion {
  question: string;
  context?: string;
  evidence: Evidence;
}

export interface RepoLink {
  repo: string;           // owner/repo format
  path?: string;
  link_type: 'explicit' | 'semantic';
  confidence: number;     // 0.0-1.0
  evidence?: string;
}

export interface Position {
  view: string;
  confidence: 'high' | 'medium' | 'low';
  as_of: string;          // ISO8601
  evidence: Evidence;
}

export interface Evolution {
  delta: string;
  trigger: string;
  from_view: string;
  to_view: string;
  date: string;           // ISO8601
  evidence: Evidence;
}

export interface KnowledgeEntry {
  id: string;             // ke_uuid
  type: 'knowledge';
  
  // Classification
  domain: string;
  subdomain?: string;
  
  // State
  state: 'active' | 'contested' | 'stale' | 'deprecated';
  detail_level: 'full' | 'compressed';
  
  // Current position
  current_view: string;
  confidence: 'high' | 'medium' | 'low';
  
  // All positions (for contested states)
  positions: Position[];
  
  // Structured knowledge
  key_insights: Insight[];
  knows_how_to: Capability[];
  open_questions: OpenQuestion[];
  
  // Linkages
  related_repos: RepoLink[];
  related_knowledge: {
    knowledge_id: string;
    relationship: 'related' | 'depends_on' | 'contradicts' | 'supersedes';
  }[];
  
  // Evolution
  evolution: Evolution[];
  
  // Metadata
  metadata: {
    created_at: string;
    updated_at: string;
    source_conversations: string[];
    source_messages: string[];
    access_count: number;
    last_accessed?: string;
  };
  
  // Archive reference
  full_content_ref?: string;
}
```

**packages/shared/src/types/project-entry.ts:**
```typescript
import { Evidence, RepoLink } from './knowledge-entry';

export interface Decision {
  decision: string;
  rationale?: string;
  date: string;
  evidence: Evidence;
}

export interface ProjectEntry {
  id: string;             // pe_uuid
  type: 'project';
  name: string;
  
  // State
  status: 'active' | 'paused' | 'completed' | 'abandoned';
  detail_level: 'full' | 'compressed';
  
  // Current state
  goal: string;
  current_phase: string;
  blocked_on?: string;
  
  // Decisions
  decisions_made: Decision[];
  
  // Technical
  tech_stack: string[];
  
  // Linkages
  related_repos: (RepoLink & { is_primary?: boolean })[];
  related_knowledge: {
    knowledge_id: string;
    relationship: 'depends_on' | 'informed_by' | 'produced';
  }[];
  
  // History
  phase_history: {
    phase: string;
    entered_at: string;
    evidence: { conversation_id: string };
  }[];
  
  // Metadata
  metadata: {
    created_at: string;
    updated_at: string;
    source_conversations: string[];
    source_messages: string[];
    last_touched: string;
  };
  
  full_content_ref?: string;
}
```

**packages/shared/src/types/thin-index.ts:**
```typescript
export interface ThinIndexTopic {
  id: string;
  domain: string;
  current_view_summary: string;  // Max 80 chars
  state: 'active' | 'contested' | 'stale';
  confidence: 'high' | 'medium' | 'low';
  last_updated: string;
  top_repo?: string;
}

export interface ThinIndexProject {
  id: string;
  name: string;
  status: 'active' | 'paused' | 'completed' | 'abandoned';
  goal_summary: string;          // Max 80 chars
  current_phase: string;
  blocked_on?: string;
  last_touched: string;
  primary_repo?: string;
}

export interface ThinIndexEvolution {
  entry_id: string;
  entry_type: 'knowledge' | 'project';
  domain_or_name: string;
  delta_summary: string;         // Max 60 chars
  date: string;
}

export interface ThinIndex {
  generated_at: string;
  token_count: number;
  topics: ThinIndexTopic[];
  projects: ThinIndexProject[];
  recent_evolutions: ThinIndexEvolution[];
  contested_count: number;
}
```

### Phase 3: Distillation Pipeline

Implement in this order:

1. **Export parsing** (parse.ts)
   - Claude JSON parser with branch resolution
   - GPT JSON parser with DAG traversal
   - Output: normalized conversations with message IDs preserved

2. **Filtering** (filter.ts)
   - Value scoring system (see PRD)
   - Skip logging for tuning

3. **Extraction** (extract.ts)
   - LLM prompt with evidence requirements
   - Chunking for long conversations
   - Validation (all insights have evidence)

4. **Linking** (link.ts)
   - Explicit regex matching
   - Semantic embedding + LLM verification
   - Confidence scoring

5. **Merging** (merge.ts)
   - Multi-signal matching (embedding + repo + keyword)
   - Contested state handling
   - Evolution tracking

6. **Compression** (compress.ts)
   - Archive to Dropbox before compressing
   - Keep evolution summaries
   - Non-destructive (creates view)

7. **Indexing** (index.ts)
   - Write to Redis
   - Update Vector embeddings
   - Generate thin index with token budget

8. **API routes**
   - /api/distill/start → queue conversations
   - /api/distill/process → batch process
   - /api/distill/finalize → merge and index

### Phase 4: MCP Server

1. **Tool implementations**
   - get_index: Read thin index from Redis
   - get_context: Find + return summary
   - get_deep: Return full entry
   - search: Vector similarity search

2. **MCP protocol handler**
   - Request parsing
   - Tool routing
   - Response formatting

3. **Authentication**
   - Bearer token validation

### Phase 5: Claude Skill

1. Create SKILL.md with routing logic
2. Add examples and schemas
3. Package as ZIP for upload

### Phase 6: Integration

1. Deploy both Vercel apps
2. Configure MCP connector in claude.ai
3. Upload Skill
4. Configure Profile Preferences
5. Test end-to-end

## Environment Variables

**apps/distillation/.env:**
```bash
# Upstash Redis
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxx

# Upstash Vector
UPSTASH_VECTOR_REST_URL=https://xxx.upstash.io
UPSTASH_VECTOR_REST_TOKEN=xxx

# Anthropic (for extraction)
ANTHROPIC_API_KEY=sk-ant-xxx

# OpenAI (for embeddings)
OPENAI_API_KEY=sk-xxx

# Dropbox (for archiving)
DROPBOX_ACCESS_TOKEN=xxx
DROPBOX_ARCHIVE_PATH=/knowledge-system/archive

# Source paths (for local development/testing)
CLAUDE_EXPORT_PATH=/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/Anthropic
GPT_EXPORT_PATH=/Users/macbook2024/Library/CloudStorage/Dropbox/Identity and Important Papers/Arjun Digital Identity/ChatGPT

# Admin
ADMIN_API_KEY=xxx
```

**apps/mcp-server/.env:**
```bash
# Upstash Redis
UPSTASH_REDIS_REST_URL=https://xxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxx

# Upstash Vector  
UPSTASH_VECTOR_REST_URL=https://xxx.upstash.io
UPSTASH_VECTOR_REST_TOKEN=xxx

# OpenAI (for query embeddings)
OPENAI_API_KEY=sk-xxx

# MCP Auth
MCP_AUTH_TOKEN=xxx  # Generate secure token
```

## Key Implementation Details

### 1. Claude Export Parsing

Claude exports have a tree structure with `parent_message_uuid`. Handle branch resolution:

```typescript
// apps/distillation/src/pipeline/parse.ts

interface ClaudeMessage {
  uuid: string;
  sender: 'human' | 'assistant';
  text: string;
  created_at: string;
  parent_message_uuid: string | null;
}

interface ClaudeConversation {
  uuid: string;
  name: string;
  created_at: string;
  updated_at: string;
  chat_messages: ClaudeMessage[];
}

function parseClaudeExport(data: { conversations: ClaudeConversation[] }): NormalizedConversation[] {
  return data.conversations.map(conv => {
    // Build message tree
    const messageMap = new Map<string, ClaudeMessage>();
    const children = new Map<string, string[]>();
    let roots: string[] = [];
    
    for (const msg of conv.chat_messages) {
      messageMap.set(msg.uuid, msg);
      if (msg.parent_message_uuid) {
        const existing = children.get(msg.parent_message_uuid) || [];
        existing.push(msg.uuid);
        children.set(msg.parent_message_uuid, existing);
      } else {
        roots.push(msg.uuid);
      }
    }
    
    // Traverse to build linear thread, selecting primary branch
    const selectedPath = selectPrimaryPath(roots[0], messageMap, children);
    
    return {
      id: conv.uuid,
      source: 'claude',
      title: conv.name,
      created_at: conv.created_at,
      updated_at: conv.updated_at,
      messages: selectedPath.map(id => {
        const msg = messageMap.get(id)!;
        return {
          message_id: msg.uuid,
          role: msg.sender === 'human' ? 'user' : 'assistant',
          created_at: msg.created_at,
          content: msg.text,
          content_type: detectContentType(msg.text),
          code_blocks: extractCodeBlocks(msg.text)
        };
      }),
      parse_metadata: {
        total_nodes: conv.chat_messages.length,
        branches_found: countBranches(children),
        selected_path: selectedPath,
        alternate_branches_kept: 0,
        parser_version: '1.0.0'
      }
    };
  });
}

function selectPrimaryPath(
  nodeId: string,
  messages: Map<string, ClaudeMessage>,
  children: Map<string, string[]>
): string[] {
  const path: string[] = [nodeId];
  let current = nodeId;
  
  while (children.has(current)) {
    const childIds = children.get(current)!;
    if (childIds.length === 0) break;
    
    // Select child with latest timestamp (or longest subtree)
    const bestChild = childIds.reduce((best, childId) => {
      const bestMsg = messages.get(best)!;
      const childMsg = messages.get(childId)!;
      return new Date(childMsg.created_at) > new Date(bestMsg.created_at) ? childId : best;
    });
    
    path.push(bestChild);
    current = bestChild;
  }
  
  return path;
}
```

### 2. GPT Export Parsing

GPT uses a `mapping` object with parent/children references:

```typescript
// apps/distillation/src/pipeline/parse.ts

interface GPTMessage {
  id: string;
  message?: {
    author: { role: 'user' | 'assistant' | 'system' };
    content: { parts: string[] };
    create_time: number;
  };
  parent: string | null;
  children: string[];
}

interface GPTConversation {
  title: string;
  create_time: number;
  update_time: number;
  mapping: Record<string, GPTMessage>;
}

function parseGPTExport(conversations: GPTConversation[]): NormalizedConversation[] {
  return conversations.map(conv => {
    // Find root node (no parent)
    const rootId = Object.keys(conv.mapping).find(
      id => conv.mapping[id].parent === null
    );
    
    // Traverse from root, selecting primary path
    const selectedPath = traverseGPTTree(rootId!, conv.mapping);
    
    // Filter to actual messages (skip system nodes)
    const messages = selectedPath
      .map(id => conv.mapping[id])
      .filter(node => node.message && node.message.author.role !== 'system')
      .map(node => ({
        message_id: node.id,
        role: node.message!.author.role === 'user' ? 'user' : 'assistant',
        created_at: new Date(node.message!.create_time * 1000).toISOString(),
        content: node.message!.content.parts.join('\n'),
        content_type: detectContentType(node.message!.content.parts.join('\n')),
        code_blocks: extractCodeBlocks(node.message!.content.parts.join('\n'))
      }));
    
    return {
      id: `gpt_${conv.create_time}`,
      source: 'gpt',
      title: conv.title,
      created_at: new Date(conv.create_time * 1000).toISOString(),
      updated_at: new Date(conv.update_time * 1000).toISOString(),
      messages,
      parse_metadata: {
        total_nodes: Object.keys(conv.mapping).length,
        branches_found: countGPTBranches(conv.mapping),
        selected_path: selectedPath,
        alternate_branches_kept: 0,
        parser_version: '1.0.0'
      }
    };
  });
}
```

### 3. Extraction Prompt

```typescript
// apps/distillation/src/prompts/extraction.ts

export const EXTRACTION_PROMPT = `You are extracting knowledge entries from a conversation. Your extractions must include evidence linking back to specific messages.

## User Context
Investment analyst working on trading systems, MLX fine-tuning, and agentic coding projects.

## Task
Analyze this conversation and extract structured knowledge. For EVERY insight, decision, or finding, you MUST provide evidence pointing to the specific message(s) that support it.

## Output Format
Return valid JSON matching this schema:

{
  "knowledge_entries": [
    {
      "domain": "specific topic (e.g., 'MLX layer selection' not 'machine learning')",
      "current_view": "1-3 sentences: what the user now thinks/knows",
      "confidence": "high|medium|low",
      "key_insights": [
        {
          "insight": "specific learning or conclusion",
          "evidence": {
            "message_ids": ["msg_id1", "msg_id2"],
            "snippet": "key quote, max 200 chars"
          }
        }
      ],
      "knows_how_to": [
        {
          "capability": "practical skill demonstrated",
          "evidence": {
            "message_ids": ["msg_id"],
            "snippet": "optional supporting quote"
          }
        }
      ],
      "open_questions": [
        {
          "question": "unresolved question",
          "evidence": {
            "message_ids": ["msg_id"]
          }
        }
      ],
      "repo_mentions": ["any GitHub repos mentioned"]
    }
  ],
  "project_entries": [
    {
      "name": "project name",
      "goal": "what the user is trying to achieve",
      "current_phase": "where they are in the work",
      "decisions_made": [
        {
          "decision": "specific choice",
          "rationale": "why, if stated",
          "evidence": {
            "message_ids": ["msg_id"],
            "snippet": "key quote"
          }
        }
      ],
      "blocked_on": "what's stopping progress, or null",
      "tech_stack": ["technologies involved"],
      "repo_mentions": ["GitHub repos mentioned"]
    }
  ]
}

## Rules
1. EVERY insight/decision MUST have evidence with message_ids
2. Snippets should be direct quotes, max 200 characters
3. If you cannot find evidence for a claim, do not include that claim
4. Be specific in domain naming
5. Return empty arrays if no extractable knowledge

## Conversation
Messages are formatted as [message_id] role: content

{conversation}`;
```

### 4. Merge Logic with Contested States

```typescript
// apps/distillation/src/pipeline/merge.ts

import { KnowledgeEntry, Position } from '@knowledge-system/shared';

interface MergeAction {
  action: 'create' | 'update' | 'evolve' | 'contest';
  reason: string;
  operations: string[];
  evolution_record?: any;
  new_position?: Position;
}

async function determineMergeAction(
  candidate: Partial<KnowledgeEntry>,
  existing: KnowledgeEntry
): Promise<MergeAction> {
  // Compare views semantically
  const viewSimilarity = await computeSimilarity(
    candidate.current_view!,
    existing.current_view
  );
  
  if (viewSimilarity > 0.85) {
    // Views align - merge insights
    return {
      action: 'update',
      reason: 'Views aligned',
      operations: ['merge_insights', 'merge_capabilities', 'update_timestamps']
    };
  }
  
  if (viewSimilarity > 0.50) {
    // Views evolved - track evolution
    return {
      action: 'evolve',
      reason: 'View has evolved',
      operations: ['append_evolution', 'update_current_view', 'merge_insights'],
      evolution_record: {
        delta: `View shifted`,
        trigger: candidate.metadata?.source_conversations?.[0],
        from_view: existing.current_view,
        to_view: candidate.current_view!,
        date: new Date().toISOString(),
        evidence: candidate.positions?.[0]?.evidence
      }
    };
  }
  
  // Views contradict - DO NOT overwrite
  return {
    action: 'contest',
    reason: 'Views contradict',
    operations: ['set_state_contested', 'add_position', 'keep_both_views'],
    new_position: {
      view: candidate.current_view!,
      confidence: candidate.confidence!,
      as_of: new Date().toISOString(),
      evidence: candidate.positions?.[0]?.evidence!
    }
  };
}

function applyMergeAction(
  existing: KnowledgeEntry,
  candidate: Partial<KnowledgeEntry>,
  action: MergeAction
): KnowledgeEntry {
  const updated = { ...existing };
  
  switch (action.action) {
    case 'update':
      // Merge insights (union, dedupe by evidence)
      updated.key_insights = mergeInsights(existing.key_insights, candidate.key_insights || []);
      updated.knows_how_to = mergeCapabilities(existing.knows_how_to, candidate.knows_how_to || []);
      updated.metadata.updated_at = new Date().toISOString();
      updated.metadata.source_conversations = [
        ...new Set([...existing.metadata.source_conversations, ...(candidate.metadata?.source_conversations || [])])
      ];
      break;
      
    case 'evolve':
      updated.evolution.push(action.evolution_record);
      updated.current_view = candidate.current_view!;
      updated.confidence = candidate.confidence!;
      updated.key_insights = mergeInsights(existing.key_insights, candidate.key_insights || []);
      updated.metadata.updated_at = new Date().toISOString();
      break;
      
    case 'contest':
      updated.state = 'contested';
      updated.positions.push(action.new_position!);
      updated.metadata.updated_at = new Date().toISOString();
      // Keep current_view as the newer one for retrieval, but positions array has both
      updated.current_view = candidate.current_view!;
      break;
  }
  
  return updated;
}
```

### 5. MCP Tool Implementation

```typescript
// apps/mcp-server/src/tools/get-context.ts

import { redis } from '../storage/redis';
import { vector } from '../storage/vector';
import { embed } from '../utils/embedding';

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
  
  async execute({ topic }: { topic: string }) {
    const startTime = Date.now();
    
    // Try exact match first
    let entry = await findExactMatch(topic);
    
    // Fall back to semantic search
    if (!entry) {
      const queryEmbedding = await embed(topic);
      const results = await vector.query({
        vector: queryEmbedding,
        topK: 1,
        includeMetadata: true
      });
      
      if (results.length > 0 && results[0].score > 0.75) {
        const key = results[0].metadata.type === 'knowledge'
          ? `knowledge:${results[0].id}`
          : `project:${results[0].id}`;
        const data = await redis.get(key);
        entry = data ? JSON.parse(data) : null;
      }
    }
    
    if (!entry) {
      return {
        success: true,
        data: {
          found: false,
          message: `No knowledge entry found for "${topic}".`,
          suggestion: 'Try get_index() to see available topics.'
        },
        latency_ms: Date.now() - startTime
      };
    }
    
    // Increment access count (fire and forget)
    redis.hincrby(`${entry.type}:${entry.id}`, 'access_count', 1);
    
    // Format response
    return {
      success: true,
      data: {
        found: true,
        type: entry.type,
        id: entry.id,
        domain: entry.domain || entry.name,
        state: entry.state || entry.status,
        current_view: entry.current_view || entry.goal,
        confidence: entry.confidence,
        key_insights: entry.key_insights?.slice(0, 3).map(i => ({
          insight: i.insight,
          evidence_summary: `From ${i.evidence.conversation_id}`
        })),
        related_repos: entry.related_repos?.slice(0, 2),
        open_questions: entry.open_questions?.slice(0, 2).map(q => q.question),
        last_updated: entry.metadata?.updated_at
      },
      latency_ms: Date.now() - startTime
    };
  }
};

async function findExactMatch(topic: string) {
  const normalized = topic.toLowerCase().trim();
  
  // Try knowledge entries
  const knowledgeKeys = await redis.keys(`knowledge:*`);
  for (const key of knowledgeKeys) {
    const data = await redis.get(key);
    if (data) {
      const entry = JSON.parse(data);
      if (entry.domain?.toLowerCase().includes(normalized)) {
        return entry;
      }
    }
  }
  
  // Try project entries
  const projectKeys = await redis.keys(`project:*`);
  for (const key of projectKeys) {
    const data = await redis.get(key);
    if (data) {
      const entry = JSON.parse(data);
      if (entry.name?.toLowerCase().includes(normalized)) {
        return entry;
      }
    }
  }
  
  return null;
}
```

### 6. Thin Index Generation

```typescript
// apps/distillation/src/pipeline/index.ts

import { ThinIndex, KnowledgeEntry, ProjectEntry } from '@knowledge-system/shared';
import { countTokens } from '../utils/tokenizer';

const MAX_TOKENS = 3000;

export function generateThinIndex(
  knowledgeEntries: KnowledgeEntry[],
  projectEntries: ProjectEntry[]
): ThinIndex {
  const index: ThinIndex = {
    generated_at: new Date().toISOString(),
    token_count: 0,
    topics: [],
    projects: [],
    recent_evolutions: [],
    contested_count: 0
  };
  
  // Sort knowledge by relevance
  const sortedKnowledge = [...knowledgeEntries]
    .filter(e => e.state !== 'deprecated')
    .sort((a, b) => {
      // Active first, then by access count, then by recency
      if (a.state === 'active' && b.state !== 'active') return -1;
      if (b.state === 'active' && a.state !== 'active') return 1;
      if (a.metadata.access_count !== b.metadata.access_count) {
        return b.metadata.access_count - a.metadata.access_count;
      }
      return new Date(b.metadata.updated_at).getTime() - new Date(a.metadata.updated_at).getTime();
    });
  
  // Add topics
  for (const entry of sortedKnowledge) {
    index.topics.push({
      id: entry.id,
      domain: entry.domain,
      current_view_summary: truncate(entry.current_view, 80),
      state: entry.state,
      confidence: entry.confidence,
      last_updated: entry.metadata.updated_at,
      top_repo: entry.related_repos?.[0]?.repo
    });
    if (entry.state === 'contested') {
      index.contested_count++;
    }
  }
  
  // Add projects (active first)
  const sortedProjects = [...projectEntries].sort((a, b) => {
    if (a.status === 'active' && b.status !== 'active') return -1;
    if (b.status === 'active' && a.status !== 'active') return 1;
    return new Date(b.metadata.last_touched).getTime() - new Date(a.metadata.last_touched).getTime();
  });
  
  for (const entry of sortedProjects) {
    index.projects.push({
      id: entry.id,
      name: entry.name,
      status: entry.status,
      goal_summary: truncate(entry.goal, 80),
      current_phase: entry.current_phase,
      blocked_on: entry.blocked_on,
      last_touched: entry.metadata.last_touched,
      primary_repo: entry.related_repos?.find(r => r.is_primary)?.repo
    });
  }
  
  // Add recent evolutions (last 30 days)
  const thirtyDaysAgo = new Date();
  thirtyDaysAgo.setDate(thirtyDaysAgo.getDate() - 30);
  
  const allEvolutions: ThinIndex['recent_evolutions'] = [];
  for (const entry of knowledgeEntries) {
    for (const evo of entry.evolution || []) {
      if (new Date(evo.date) > thirtyDaysAgo) {
        allEvolutions.push({
          entry_id: entry.id,
          entry_type: 'knowledge',
          domain_or_name: entry.domain,
          delta_summary: truncate(evo.delta, 60),
          date: evo.date
        });
      }
    }
  }
  
  index.recent_evolutions = allEvolutions
    .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime())
    .slice(0, 10);
  
  // Enforce token budget
  enforceTokenBudget(index);
  index.token_count = countTokens(JSON.stringify(index));
  
  return index;
}

function enforceTokenBudget(index: ThinIndex): void {
  while (countTokens(JSON.stringify(index)) > MAX_TOKENS) {
    if (index.topics.length > 10) {
      index.topics = index.topics.slice(0, 10);
    } else if (index.projects.length > 5) {
      index.projects = index.projects.slice(0, 5);
    } else if (index.recent_evolutions.length > 5) {
      index.recent_evolutions = index.recent_evolutions.slice(0, 5);
    } else {
      // Truncate summaries further
      for (const topic of index.topics) {
        topic.current_view_summary = truncate(topic.current_view_summary, 50);
      }
      break;
    }
  }
}

function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength - 3) + '...';
}
```

## Vercel Configuration

**apps/distillation/vercel.json:**
```json
{
  "version": 2,
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "crons": [
    {
      "path": "/api/distill/start",
      "schedule": "0 10 * * 0"
    }
  ],
  "functions": {
    "src/api/**/*.ts": {
      "maxDuration": 300
    }
  }
}
```

**apps/mcp-server/vercel.json:**
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
  ]
}
```

## Profile Preferences (for claude.ai)

Copy this to Claude Settings → Profile:

```
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

## Testing Checklist

### Unit Tests
- [ ] Claude export parsing with branches
- [ ] GPT export parsing with DAG
- [ ] Value scoring for filtering
- [ ] Extraction validation (evidence required)
- [ ] Merge action determination
- [ ] Contested state handling
- [ ] Thin index token budget enforcement

### Integration Tests
- [ ] Full pipeline: parse → filter → extract → link → merge → index
- [ ] Redis read/write operations
- [ ] Vector search accuracy
- [ ] MCP tool responses

### End-to-End Tests
- [ ] Trigger distillation via API
- [ ] Verify entries in Redis after run
- [ ] MCP connector in Claude.ai
- [ ] Skill triggers on relevant phrases
- [ ] Context retrieval in conversation

## Deployment Steps

1. **Set up Upstash**
   ```bash
   # Create Redis database at upstash.com
   # Create Vector index (1536 dimensions)
   # Save credentials
   ```

2. **Deploy distillation app**
   ```bash
   cd apps/distillation
   vercel env add UPSTASH_REDIS_REST_URL
   vercel env add UPSTASH_REDIS_REST_TOKEN
   # ... add all env vars
   vercel --prod
   ```

3. **Deploy MCP server**
   ```bash
   cd apps/mcp-server
   vercel env add UPSTASH_REDIS_REST_URL
   # ... add all env vars
   vercel --prod
   # Note the URL: https://your-mcp-server.vercel.app
   ```

4. **Run initial distillation**
   ```bash
   curl -X POST https://your-distillation.vercel.app/api/distill/start \
     -H "Authorization: Bearer $ADMIN_API_KEY"
   ```

5. **Configure Claude**
   - Go to claude.ai → Settings → Connectors
   - Add MCP Server:
     - Name: Personal Knowledge
     - URL: https://your-mcp-server.vercel.app/mcp
     - Auth: Bearer token (MCP_AUTH_TOKEN)
   - Upload Skill ZIP
   - Update Profile Preferences

6. **Test**
   ```
   In Claude: "What are my active projects?"
   Expected: Claude calls get_index() and lists your projects
   ```

## Reference Documents

The full PRDs with complete schemas and logic are in:
- `docs/prd-distillation-v1.1.md` - Distillation pipeline
- `docs/prd-retrieval-v1.0.md` - MCP server and Skill

## Cost Estimate

| Component | Monthly Cost |
|-----------|--------------|
| Upstash Redis | $0 (free tier) |
| Upstash Vector | $0-5 (free tier or low usage) |
| Vercel | $0 (within Pro plan) |
| Claude API (extraction) | ~$7/run × 4 runs = ~$28 |
| OpenAI Embeddings | ~$1 |
| **Total** | **~$30/month** |

## Common Issues

**"No conversations found"**
- Check export file paths are correct
- Verify JSON structure matches expected format

**"Extraction returning empty"**
- Check Anthropic API key is valid
- Review extraction prompt for issues
- Check conversation has substantive content

**"MCP tool not responding"**
- Verify Vercel deployment is live
- Check MCP_AUTH_TOKEN matches
- Test endpoint directly with curl

**"Skill not triggering"**
- Ensure Skill is uploaded and enabled
- Check Profile Preferences includes knowledge system mention
- Try explicit trigger: "Check my knowledge system for..."

---

*This README provides everything needed to implement the complete system. Start with Phase 1 (infrastructure) and work through sequentially. The PRD documents contain the authoritative specifications for edge cases and detailed logic.*
