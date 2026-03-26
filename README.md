# Personal Knowledge System

A system that distills AI conversations, coding agent sessions, GitHub repos, and email into structured, searchable knowledge entries. Access your accumulated insights, decisions, and learnings during future Claude conversations via MCP (Model Context Protocol).

## What It Does

1. **Ingests** from 5 sources: Claude AI exports, ChatGPT exports, Claude Code sessions, Codex CLI sessions, GitHub repos, and Gmail
2. **Distills** raw content into structured knowledge entries using Claude Sonnet 4.6
3. **Stores** entries in Upstash Redis with semantic search via Upstash Vector (3072-dim embeddings)
4. **Links** coding sessions to their GitHub repos (URL, README summary)
5. **Exposes** knowledge through an MCP server (Cloudflare Workers)
6. **Integrates** with Claude Desktop, iOS, and Web via MCP connector
7. **Runs automatically** — agent session ingestion every 6 hours via launchd

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LOCAL (Your Machine)                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Data Sources                      Ingestion Pipelines              │
│   ├── Claude AI exports (.json)     ├── distillation/run.py          │
│   ├── ChatGPT exports (.json)       ├── ingestion/github/run.py      │
│   ├── Claude Code sessions (.jsonl) ├── ingestion/gmail/run.py       │
│   ├── Codex CLI sessions (.jsonl)   └── ingestion/agent_sessions/    │
│   ├── GitHub repos (API)                run.py (every 6h via launchd)│
│   └── Gmail (mbox)                                                   │
│                                                                      │
│   All pipelines use:                                                 │
│   ├── Claude Sonnet 4.6 (distillation/extraction)                    │
│   ├── OpenAI text-embedding-3-large (embeddings, 3072 dims)          │
│   └── StorageClient (unified Redis + Vector writer)                  │
│                                                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        UPSTASH (Cloud Storage)                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Redis                             Vector                           │
│   ├── knowledge:{id} entries        └── 3072-dim embeddings          │
│   ├── project:{id} entries              (text-embedding-3-large)     │
│   ├── by_domain:{domain} indexes                                     │
│   ├── by_state:{state} indexes                                       │
│   ├── ingested:{source}:{id} dedup                                   │
│   └── index:current (thin index)                                     │
│                                                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   CLOUDFLARE WORKERS (MCP Server)                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   /sse endpoint (SSE transport for Claude MCP)                       │
│   ├── get_index    → Overview of all topics/projects                 │
│   ├── get_context  → Summary for a specific topic                    │
│   ├── get_deep     → Full entry with provenance                      │
│   ├── search       → Semantic search (70% relevance + 30% recency)  │
│   └── github       → GitHub-linked entry lookup                      │
│                                                                      │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        CLAUDE (All Platforms)                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Desktop App    ←──┐                                                │
│   iOS App        ←──┼── MCP Connector                                │
│   claude.ai      ←──┘                                                │
│   Claude Code    ←──── (also generates sessions that feed back in)   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Project Structure

```
knowledge-system/
├── ingestion/                         # Data ingestion pipelines
│   ├── .env                           # Credentials (Upstash, Anthropic, OpenAI, GitHub)
│   ├── core/                          # Shared utilities
│   │   ├── config.py                  # Configuration + env loading
│   │   ├── storage.py                 # StorageClient (Redis + Vector)
│   │   └── extractor.py              # LLM-based knowledge extraction
│   ├── agent_sessions/                # Claude Code + Codex CLI ingestion
│   │   ├── run.py                     # Entry point (daily scan or backfill)
│   │   ├── parsers.py                 # JSONL parsers for both session formats
│   │   ├── github_linker.py           # Resolves cwd → GitHub repo + README
│   │   └── com.arjun.knowledge-agent-sessions.plist  # launchd config
│   ├── github/                        # GitHub repo ingestion
│   │   ├── client.py                  # GitHub API client (repos, READMEs, commits)
│   │   └── run.py                     # GitHub ingestion pipeline
│   ├── gmail/                         # Gmail ingestion
│   │   ├── parser.py                  # Mbox file parser
│   │   └── run.py                     # Gmail ingestion pipeline
│   ├── checkpoints/                   # Resumable state files
│   └── logs/                          # Processing logs
│
├── distillation/                      # Original pipeline (Claude/GPT JSON exports)
│   ├── run.py                         # Main entry point
│   ├── requirements.txt               # Python dependencies
│   ├── config.py                      # Export paths + settings
│   ├── pipeline/                      # Processing stages
│   │   ├── parse.py                   # Parse Claude/GPT JSON exports
│   │   ├── filter.py                  # Score and filter conversations
│   │   ├── extract.py                 # LLM extraction with provenance
│   │   ├── merge.py                   # Merge logic (incremental runs)
│   │   ├── compress.py                # Archive old entries
│   │   └── index.py                   # Generate thin index
│   ├── models/                        # Data models (dataclasses)
│   ├── prompts/                       # LLM prompts for extraction
│   ├── storage/                       # Upstash Redis/Vector clients
│   └── utils/                         # LLM + embedding wrappers
│
├── cloudflare-mcp/                    # MCP server (deployed, production)
│   └── mcp-server/
│       ├── src/index.ts               # Cloudflare Worker with MCP tools
│       └── wrangler.jsonc             # Cloudflare config
│
├── mcp-server/                        # (Legacy) Vercel attempt — not used
│
├── skill/                             # Claude Skill definition
│   ├── SKILL.md                       # Routing instructions for Claude
│   └── examples/example-session.md    # Example usage sessions
│
├── docs/                              # Design documents
│   ├── knowledge-distillation-prd-v1.1.md
│   └── knowledge-retrieval-prd-v1.0_1.md
│
└── INGESTION_REPORT_2026-03-15.md     # Report from initial ingestion run
```

## Data Sources

| Source | Method | Frequency | Files |
|--------|--------|-----------|-------|
| **Claude Code sessions** | Parse `~/.claude/projects/**/*.jsonl` | Every 6h (launchd) | `ingestion/agent_sessions/` |
| **Codex CLI sessions** | Parse `~/.codex/sessions/**/*.jsonl` | Every 6h (launchd) | `ingestion/agent_sessions/` |
| **GitHub repos** | GitHub API (READMEs, commits, code comments) | Manual | `ingestion/github/` |
| **Gmail** | Parse mbox export | Manual | `ingestion/gmail/` |
| **Claude AI exports** | Parse `conversations.json` from claude.ai export | Manual | `distillation/` |
| **ChatGPT exports** | Parse `conversations.json` from ChatGPT export | Manual | `distillation/` |

## Models Used

| Model | ID | Purpose |
|-------|-----|---------|
| **Claude Sonnet 4.6** | `claude-sonnet-4-6` | All knowledge extraction and distillation across all pipelines |
| **OpenAI text-embedding-3-large** | `text-embedding-3-large` (3072 dims) | Vector embeddings for semantic search |

## Current Stats

As of March 2026:
- **398** knowledge entries
- **36** project entries
- **6,354** total vectors
- **299** agent session files processed
- **5** ingestion sources active

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

### 2. Configure Environment

```bash
cd ingestion
cp .env.example .env
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

# GitHub (optional, for repo ingestion + agent session linking)
GITHUB_API_KEY=ghp_...
GITHUB_USERNAME=YourUsername
```

### 3. Install Dependencies

```bash
pip install anthropic openai upstash-redis upstash-vector python-dotenv requests
```

### 4. Run Ingestion Pipelines

```bash
cd ingestion

# Agent sessions (Claude Code + Codex CLI) — backfill all history
python agent_sessions/run.py --backfill

# GitHub repos
python github/run.py

# Gmail (requires mbox export)
python gmail/run.py

# Claude/GPT exports (uses separate pipeline)
cd ../distillation
python run.py
```

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

### 7. Install Agent Session Daemon (macOS)

```bash
# Copy plist to LaunchAgents
cp ingestion/agent_sessions/com.arjun.knowledge-agent-sessions.plist ~/Library/LaunchAgents/

# Load and start
launchctl load ~/Library/LaunchAgents/com.arjun.knowledge-agent-sessions.plist
launchctl start com.arjun.knowledge-agent-sessions

# Verify
launchctl list | grep knowledge-agent
```

### 8. Test It

In Claude (any platform), try:
- "What do I know about machine learning?"
- "Search my knowledge for LoopPilot"
- "What are my active projects?"

## MCP Tools

| Tool | Description | Example Trigger |
|------|-------------|-----------------|
| `get_index` | Returns overview of all topics + projects | "What topics do I have stored?" |
| `get_context(topic)` | Returns current view + insights for a topic | "What's my view on MLX?" |
| `get_deep(id)` | Returns full entry with evidence + evolution | "How did my view on X evolve?" |
| `search(query)` | Semantic search (70% relevance + 30% recency) | "Have we discussed volatility?" |
| `github(query)` | GitHub-linked entry lookup | "What repos have I worked on?" |

## Agent Session Ingestion (Claude Code + Codex CLI)

Automatically pulls knowledge from your Claude Code and Codex CLI sessions every 6 hours via launchd. Detects which GitHub repo each session was working in and links the repo URL + README to knowledge entries.

### How It Works

```
~/.claude/projects/**/*.jsonl    ┐
~/.codex/sessions/**/*.jsonl     ┤→ parsers.py (byte-offset tracking)
                                 │       ↓
                                 │  Detect GitHub repo from cwd (github_linker.py)
                                 │       ↓
                                 │  Claude Sonnet 4.6 distillation → structured entries
                                 │       ↓
                                 └→ StorageClient → Upstash Redis + Vector
                                          ↓
                                 Immediately available via MCP (no redeploy)
```

### Session File Formats

**Claude Code** (`~/.claude/projects/<encoded-path>/<session-uuid>.jsonl`):
- Event types: `user`, `assistant`, `queue-operation`, `progress`
- Content can be string or list of text blocks
- `cwd` field on events identifies the working directory

**Codex CLI** (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`):
- Wrapper format: `{timestamp, type, payload}`
- Event types: `session_meta`, `response_item`, `turn_context`, `compacted`
- Uses `developer` role for user messages

### Filtering

Sessions are skipped if they have:
- Fewer than 4 turns (trivial sessions like `cd`/`ls`)
- Less than 300 chars of user content

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

Installed at `~/Library/LaunchAgents/com.arjun.knowledge-agent-sessions.plist`. Runs every 6 hours (21600 seconds) + at login.

```bash
# Check status
launchctl list | grep knowledge-agent

# Restart
launchctl stop com.arjun.knowledge-agent-sessions
launchctl start com.arjun.knowledge-agent-sessions

# View logs
tail -f ~/.knowledge_agent_sessions_stdout.log
tail -f ingestion/logs/agent_sessions.log
```

### GitHub Linking

When a session's working directory is inside a git repo with a GitHub remote, entries are enriched with:
- `metadata.github_repo`: e.g. `ArjunDivecha/loop-pilot`
- `metadata.github_url`: e.g. `https://github.com/ArjunDivecha/loop-pilot`
- `metadata.readme_summary`: First 500 chars of the repo README

The linker resolves repos by:
1. Walking up from `cwd` to find `.git` directory
2. Parsing `git remote get-url origin`
3. Matching SSH (`git@github.com:`) or HTTPS (`https://github.com/`) formats
4. Fetching README via GitHub API (cached per repo)

### Deduplication

Entry IDs are deterministic: `ke_` + MD5 of `source:session_id:domain`. Re-running backfill skips already-saved entries.

## Updating with New Data

### Agent Sessions (automatic)
The launchd daemon runs every 6 hours. No manual action needed. State is tracked in `ingestion/checkpoints/agent_sessions_state.json` (byte offsets per file).

### GitHub Repos (manual)
```bash
cd ingestion
python github/run.py                    # All repos
python github/run.py --repos "A,B,C"   # Specific repos
python github/run.py --dry-run          # Preview without saving
```

### Gmail (manual)
```bash
cd ingestion
python gmail/run.py                     # Since 2020
python gmail/run.py --since 2024        # Custom start year
```

### Claude/GPT Exports (manual)
```bash
cd distillation
rm checkpoints/*.pkl                    # Clear old checkpoints
python run.py
```

## Key Concepts

- **Knowledge Entry** (`ke_*`): A structured insight on a topic with current view, key insights, know-how, and evidence
- **Project Entry** (`pe_*`): An ongoing project with goal, status, phase, decisions made, and blockers
- **Provenance**: Every insight links back to source conversation, session, or repo
- **Thin Index**: A compressed (~10K token) overview of all topics/projects for fast context injection
- **Semantic Search**: 70% semantic similarity + 30% recency weighting, with source-based scoring (GitHub 1.1x, email 0.6x)
- **GitHub Linking**: Agent session entries include repo URL and README summary when the session was in a git repo

## Knowledge Entry Schema

```json
{
  "id": "ke_a1b2c3d4e5f6",
  "domain": "MLX LoRA fine-tuning",
  "current_view": "For LoRA on MLX use layers 8-16 for domain adaptation...",
  "state": "active",
  "confidence": "high",
  "detail_level": "full",
  "metadata": {
    "updated_at": "2026-03-26T07:06:38+00:00",
    "sources": ["codex_cli:session_abc123"],
    "project": "loop-pilot",
    "source_type": "codex_cli",
    "github_repo": "ArjunDivecha/loop-pilot",
    "github_url": "https://github.com/ArjunDivecha/loop-pilot",
    "readme_summary": "# Loop Pilot\nAutomated research loop..."
  }
}
```

## Troubleshooting

### Agent session daemon not running
```bash
launchctl list | grep knowledge-agent
# If not listed:
launchctl load ~/Library/LaunchAgents/com.arjun.knowledge-agent-sessions.plist
# Check logs:
cat ~/.knowledge_agent_sessions_stderr.log
```

### Pipeline hangs at extraction
- Check `ANTHROPIC_API_KEY` is valid
- Monitor with: `tail -f ingestion/logs/agent_sessions.log`

### MCP tools fail with "error code: 1016"
- DNS/network error from Cloudflare
- Check Worker logs: `wrangler tail`
- Verify all secrets are set: `wrangler secret list`

### Search returns no results
- Ensure Vector index has correct dimensions (3072)
- Check `OPENAI_API_KEY` is valid for embeddings

### Claude doesn't see the MCP tools
- Verify the URL ends with `/sse`
- Check MCP integration is enabled in Claude settings

### Prevent Claude Code session cleanup
Sessions older than 30 days are deleted by default. To keep all history:
```bash
# In ~/.claude/settings.json, set:
"cleanupPeriodDays": 99999
```

## Tech Stack

- **Extraction**: Claude Sonnet 4.6 (`claude-sonnet-4-6`) via Anthropic SDK
- **Embeddings**: OpenAI `text-embedding-3-large` (3072 dimensions)
- **Storage**: Upstash Redis (key-value + indexes) + Upstash Vector (semantic search)
- **MCP Server**: Cloudflare Workers, TypeScript, `@modelcontextprotocol/sdk`
- **Session Parsing**: Python, byte-offset JSONL parsing, `watchdog` (optional)
- **Scheduling**: macOS launchd (6-hour interval)
- **GitHub Integration**: GitHub REST API via custom client

## License

Private repository. Not for redistribution.

## Version History

- **1.1.0** (March 2026): Agent session ingestion — auto-pulls from Claude Code + Codex CLI, GitHub repo linking, launchd daemon, model upgrade to Claude Sonnet 4.6
- **1.0.1** (March 2026): GitHub + Gmail ingestion pipelines, recency weighting, source-based scoring, thin index compaction
- **1.0.0** (December 2024): Initial implementation with distillation pipeline, Cloudflare MCP server, and Claude integration
