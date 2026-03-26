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
├── ingestion/                     # Data ingestion pipelines
│   ├── .env                       # Credentials (Upstash, Anthropic, OpenAI, GitHub)
│   ├── core/                      # Shared utilities
│   │   ├── config.py              # Configuration + env loading
│   │   ├── storage.py             # StorageClient (Redis + Vector)
│   │   └── extractor.py           # LLM-based knowledge extraction
│   ├── agent_sessions/            # Claude Code + Codex CLI ingestion (NEW)
│   │   ├── run.py                 # Entry point (daily scan or backfill)
│   │   ├── parsers.py             # JSONL parsers for both session formats
│   │   └── github_linker.py       # Resolves cwd → GitHub repo + README
│   ├── github/                    # GitHub repo ingestion
│   │   ├── client.py              # GitHub API client
│   │   └── run.py                 # GitHub ingestion pipeline
│   ├── gmail/                     # Gmail ingestion
│   │   ├── parser.py              # Mbox file parser
│   │   └── run.py                 # Gmail ingestion pipeline
│   └── checkpoints/               # Resumable state files
│
├── distillation/                  # Original pipeline (Claude/GPT exports)
│   ├── run.py                     # Main entry point
│   ├── pipeline/                  # Processing stages
│   └── ...
│
├── cloudflare-mcp/                # MCP server (deployed)
│   └── mcp-server/
│       ├── src/index.ts           # Cloudflare Worker with MCP tools
│       └── wrangler.jsonc         # Cloudflare config
│
├── skill/                         # Claude Skill definition
│
└── docs/                          # Design documents
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

## Agent Session Ingestion (Claude Code + Codex CLI)

Automatically pulls knowledge from your Claude Code and Codex CLI sessions every 6 hours via launchd. Detects which GitHub repo each session was working in and links the repo URL + README to knowledge entries.

### How It Works

```
~/.claude/projects/**/*.jsonl    ┐
~/.codex/sessions/**/*.jsonl     ┤→ parsers.py (byte-offset tracking)
                                 │       ↓
                                 │  Detect GitHub repo from cwd (github_linker.py)
                                 │       ↓
                                 │  Claude API distillation → structured entries
                                 │       ↓
                                 └→ StorageClient → Upstash Redis + Vector
```

### Manual Run

```bash
cd ingestion

# Process new sessions since last run
python agent_sessions/run.py

# Full backfill of all history
python agent_sessions/run.py --backfill

# Dry run (parse + distill, don't save)
python agent_sessions/run.py --dry-run --limit 5

# Process only one source
python agent_sessions/run.py --source claude_code
python agent_sessions/run.py --source codex_cli
```

### launchd Daemon

Installed at `~/Library/LaunchAgents/com.arjun.knowledge-agent-sessions.plist`. Runs every 6 hours + at login.

```bash
# Check status
launchctl list | grep knowledge-agent

# Restart
launchctl stop com.arjun.knowledge-agent-sessions
launchctl start com.arjun.knowledge-agent-sessions

# View logs
tail -f ~/.knowledge_agent_sessions_stdout.log
```

### GitHub Linking

When a session's working directory is inside a git repo with a GitHub remote, entries are enriched with:
- `metadata.github_repo`: e.g. `ArjunDivecha/loop-pilot`
- `metadata.github_url`: e.g. `https://github.com/ArjunDivecha/loop-pilot`
- `metadata.readme_summary`: First 500 chars of the repo README

## Updating with New Data

### Agent Sessions (automatic)
The launchd daemon runs every 6 hours. No manual action needed.

### Claude/GPT Exports (manual)

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

- **1.1.0** (March 2026): Agent session ingestion — auto-pulls from Claude Code + Codex CLI, GitHub repo linking, launchd daemon
- **1.0.0** (December 2024): Initial implementation with full pipeline, Cloudflare MCP server, and Claude integration
