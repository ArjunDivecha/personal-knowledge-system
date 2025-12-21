# Personal Knowledge System

A system that distills AI chat histories (Claude, ChatGPT) into structured, searchable knowledge entries. Access your accumulated insights, decisions, and learnings during future Claude conversations via MCP (Model Context Protocol).

## What It Does

1. **Distills** years of AI conversations into ~1000 structured knowledge entries
2. **Stores** entries in Upstash Redis with semantic search via Upstash Vector
3. **Exposes** knowledge through an MCP server (Cloudflare Workers)
4. **Integrates** with Claude Desktop, iOS, and Web via MCP connector

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      LOCAL (Your Machine)                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Dropbox Exports              Python Distillation Pipeline     │
│   ├── Claude (conversations.json)    ├── Parse exports          │
│   └── ChatGPT (conversations.json)   ├── Filter (score ≥ 3)     │
│                                      ├── Extract (Claude API)   │
│                                      ├── Store + Embed          │
│                                      └── Generate thin index    │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      UPSTASH (Cloud Storage)                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Redis                           Vector                         │
│   ├── knowledge:{id} entries      └── 3072-dim embeddings       │
│   ├── project:{id} entries            (text-embedding-3-large)  │
│   └── index:current (thin index)                                │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                   CLOUDFLARE WORKERS (MCP Server)                │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   /sse endpoint (SSE transport for Claude MCP)                  │
│   ├── get_index    → Returns overview of all topics/projects    │
│   ├── get_context  → Returns summary for a specific topic       │
│   ├── get_deep     → Returns full entry with provenance         │
│   └── search       → Semantic search across all entries         │
│                                                                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      CLAUDE (All Platforms)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Desktop App    ←──┐                                           │
│   iOS App        ←──┼── MCP Connector ── your-worker.workers.dev│
│   claude.ai      ←──┘                                           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
knowledge-system/
├── distillation/              # Python pipeline (runs locally)
│   ├── run.py                 # Main entry point
│   ├── config.py              # Configuration + env loading
│   ├── pipeline/              # Processing stages
│   │   ├── parse.py           # Parse Claude/GPT JSON exports
│   │   ├── filter.py          # Score and filter conversations
│   │   ├── extract.py         # LLM extraction with provenance
│   │   ├── merge.py           # Merge logic (for incremental runs)
│   │   ├── compress.py        # Archive old entries
│   │   └── index.py           # Generate thin index
│   ├── models/                # Data models (dataclasses)
│   ├── prompts/               # LLM prompts for extraction
│   ├── storage/               # Upstash Redis/Vector clients
│   └── utils/                 # LLM + embedding wrappers
│
├── cloudflare-mcp/            # MCP server (deployed)
│   └── mcp-server/
│       ├── src/index.ts       # Cloudflare Worker with MCP tools
│       └── wrangler.jsonc     # Cloudflare config
│
├── mcp-server/                # (Legacy) Vercel attempt - not used
│
├── skill/                     # Claude Skill definition
│   ├── SKILL.md               # Routing instructions for Claude
│   └── examples/              # Example usage sessions
│
└── docs/                      # Design documents (PRDs)
```

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 20+
- Accounts: [Upstash](https://upstash.com), [Cloudflare](https://cloudflare.com), [Anthropic](https://anthropic.com), [OpenAI](https://openai.com)

### 1. Setup Upstash

1. Create a **Redis** database at [console.upstash.com](https://console.upstash.com)
2. Create a **Vector** index:
   - Dimensions: **3072**
   - Similarity: **Cosine**
3. Copy the REST URLs and tokens

### 2. Configure Local Environment

```bash
cd distillation
cp env.example .env
```

Edit `.env` with your credentials:

```env
# Upstash Redis
UPSTASH_REDIS_REST_URL=https://your-redis.upstash.io
UPSTASH_REDIS_REST_TOKEN=your_redis_token

# Upstash Vector
UPSTASH_VECTOR_REST_URL=https://your-vector.upstash.io
UPSTASH_VECTOR_REST_TOKEN=your_vector_token

# API Keys
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-proj-...
```

### 3. Configure Export Paths

Edit `distillation/config.py` to point to your Claude/GPT export folders:

```python
CLAUDE_EXPORT_PATH = Path("/path/to/your/Claude/exports")
GPT_EXPORT_PATH = Path("/path/to/your/ChatGPT/exports")
```

### 4. Install Dependencies & Run Pipeline

```bash
cd distillation
pip install -r requirements.txt
python run.py
```

The pipeline will:
1. Parse all conversations from both exports
2. Filter to valuable conversations (score ≥ 3)
3. Extract knowledge entries using Claude Sonnet 4.5
4. Store entries in Redis + embeddings in Vector
5. Generate a thin index for fast context loading

**Checkpointing**: Progress is saved after each stage. If interrupted, re-run to resume.

### 5. Deploy MCP Server to Cloudflare

```bash
cd cloudflare-mcp/mcp-server
npm install

# Set secrets
wrangler secret put UPSTASH_REDIS_REST_URL
wrangler secret put UPSTASH_REDIS_REST_TOKEN
wrangler secret put UPSTASH_VECTOR_REST_URL
wrangler secret put UPSTASH_VECTOR_REST_TOKEN
wrangler secret put OPENAI_API_KEY

# Deploy
wrangler deploy
```

Your MCP server will be at: `https://personal-knowledge-mcp.YOUR-SUBDOMAIN.workers.dev`

### 6. Connect Claude

1. Go to Claude Settings → MCP Integrations
2. Click "Add Integration"
3. Enter your Cloudflare Worker URL: `https://personal-knowledge-mcp.YOUR-SUBDOMAIN.workers.dev/sse`
4. No authentication required (authless)

### 7. Test It

In Claude, try:
- "What do I know about machine learning?"
- "Show me my active projects"
- "Search my knowledge for trading strategies"

## MCP Tools

| Tool | Description | Example Trigger |
|------|-------------|-----------------|
| `get_index` | Returns overview of all topics + projects | "What topics do I have stored?" |
| `get_context(topic)` | Returns current view + insights for a topic | "What's my view on MLX?" |
| `get_deep(id)` | Returns full entry with evidence + evolution | "How did my view on X evolve?" |
| `search(query)` | Semantic search across all entries | "Have we discussed volatility?" |

## Updating with New Data

When you have new Claude/GPT exports:

```bash
cd distillation

# Clear old checkpoints
rm checkpoints/*.pkl

# Run full pipeline
python run.py
```

## Key Concepts

- **Knowledge Entry**: A structured insight on a topic with current view, key insights, know-how, and evidence
- **Project Entry**: An ongoing project with goal, status, phase, decisions made, and blockers
- **Provenance**: Every insight links back to source conversation + message IDs
- **Thin Index**: A compressed (~10K token) overview of all topics/projects for fast context injection
- **Semantic Search**: Uses OpenAI `text-embedding-3-large` (3072 dimensions) for relevance matching

## Current Stats

After initial distillation:
- **1007** knowledge entries
- **325** project entries
- **1332** total vectors
- **85** topics in thin index
- **42** projects in thin index

## Troubleshooting

### Pipeline hangs at extraction
- Check `ANTHROPIC_API_KEY` is valid
- Monitor with: `tail -f distillation/runs/*.json`

### MCP tools fail with "error code: 1016"
- This is a DNS/network error from Cloudflare
- Check Cloudflare Worker logs: `wrangler tail`
- Verify all secrets are set: `wrangler secret list`

### Search returns no results
- Ensure Vector index has correct dimensions (3072)
- Check `OPENAI_API_KEY` is valid for embeddings

### Claude doesn't see the MCP tools
- Verify the URL ends with `/sse`
- Check MCP integration is enabled in Claude settings

## Tech Stack

- **Distillation**: Python 3.10+, Anthropic SDK, OpenAI SDK
- **Storage**: Upstash Redis + Upstash Vector
- **MCP Server**: Cloudflare Workers, TypeScript, @modelcontextprotocol/sdk
- **Embeddings**: OpenAI text-embedding-3-large (3072 dims)
- **Extraction**: Claude Sonnet 4.5 (claude-sonnet-4-5-20250929)

## License

Private repository. Not for redistribution.

## Version History

- **1.0.0** (December 2024): Initial implementation with full pipeline, Cloudflare MCP server, and Claude integration
