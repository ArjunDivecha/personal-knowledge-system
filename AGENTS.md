# AGENTS.md

## Purpose

This repository implements a personal knowledge system that ingests multiple sources of personal and project context, distills them into structured entries, stores them in Upstash Redis + Upstash Vector, and exposes retrieval tools over MCP.

The repo is partly historical and partly active. There are design docs describing earlier planned architectures, plus two MCP server implementations. Read the sections below before changing anything.

## What Is In This Repo

### Active high-level flow

1. Raw sources are ingested locally:
   - Claude / ChatGPT export files via `distillation/`
   - Claude Code and Codex session logs via `ingestion/agent_sessions/`
   - GitHub repos via `ingestion/github/`
   - Gmail mbox exports via `ingestion/gmail/`
2. Python ingestion/distillation code writes entries into:
   - Upstash Redis for canonical entry storage and indexes
   - Upstash Vector for embeddings and semantic search
3. MCP servers read from Upstash and expose:
   - `get_index`
   - `get_context`
   - `get_deep`
   - `search`
   - `github`

### Key directories

- `README.md`: best starting point for the current shipped system.
- `distillation/`: original Python pipeline for Claude/GPT export distillation.
- `ingestion/`: newer Python ingestion pipelines for GitHub, Gmail, and agent session logs.
- `mcp-server/`: older Vercel-style TypeScript MCP implementation.
- `cloudflare-mcp/mcp-server/`: Cloudflare Workers MCP implementation and current production path.
- `docs/`: copies of product/design docs.
- `skill/`: MCP/skill packaging artifacts.
- `archive/`: archived compressed entries; large and not a good place to start reading.

## Source Of Truth And Important Caveats

### 1. There are two MCP server implementations

Be careful here.

- `mcp-server/` is a Vercel-style implementation using `mcp-handler`.
- `cloudflare-mcp/mcp-server/` is a Cloudflare Workers implementation using `agents/mcp`.

The test scripts at repo root target the deployed Worker URL:
- `test_mcp.py`
- `test_mcp_simple.py`
- `test_mcp_tools.py`
- `test_sse_connection.py`

If the task is about the live remote MCP service, inspect `cloudflare-mcp/mcp-server/` first.
Per the updated README, `cloudflare-mcp/mcp-server/` is production and `mcp-server/` is legacy and not used.

### 2. The docs include older planned architecture

Files like:
- `knowledge-system-implementation-readme.md`
- `knowledge-distillation-prd-v1.1.md`
- `knowledge-retrieval-prd-v1.0_1.md`

describe earlier or aspirational designs. They are useful context, but they do not fully match the code currently in this repo. Prefer actual runtime code over the PRDs when they conflict.

### 3. The repo depends heavily on external credentials and personal local paths

Both Python pipelines and MCP servers require env vars and external services. Many defaults point at this machine's Dropbox exports and local home directories. Do not "clean up" these paths unless explicitly asked.

## How The Code Is Organized

### `distillation/`

This is the original export-processing pipeline.

Important files:
- `distillation/main.py`: richer CLI entrypoint with `--run`, `--dry-run`, and `--status`.
- `distillation/run.py`: simpler checkpointed runner for the full pipeline.
- `distillation/config.py`: env loading, Dropbox export paths, model names, thresholds.
- `distillation/pipeline/`: stages such as parse, filter, extract, merge, compress, index.
- `distillation/storage/`: Redis and Vector clients.
- `distillation/models/`: Pydantic/data models for entries and thin index.
- `distillation/prompts/`: prompt templates used by extraction/compression.
- `distillation/runs/`: persisted run reports.

Operational notes:
- Uses Anthropic for extraction and OpenAI for embeddings.
- Uses checkpointing heavily.
- `run.py` currently clears existing entries before re-storing them, so treat it as a destructive full refresh path.

### `ingestion/`

This is the newer source-specific ingestion layer.

Shared code:
- `ingestion/core/config.py`: env loading and shared settings.
- `ingestion/core/storage.py`: unified Upstash Redis/Vector/OpenAI storage client.
- `ingestion/core/extractor.py`: extraction logic used by ingestion jobs.

Source-specific runners:
- `ingestion/github/run.py`: ingests README, commits, and code comments from GitHub repos.
- `ingestion/gmail/run.py`: ingests substantive Gmail sent messages from an mbox export.
- `ingestion/agent_sessions/run.py`: ingests Claude Code and Codex session logs incrementally.

Agent session ingestion details:
- Reads `~/.claude/projects/**/*.jsonl`
- Reads `~/.codex/sessions/**/*.jsonl`
- Tracks byte offsets in `ingestion/checkpoints/agent_sessions_state.json`
- Optionally links cwd to GitHub repo context via `agent_sessions/github_linker.py`
- Distills sessions with Anthropic and stores durable knowledge only
- Intended to run automatically every 6 hours via launchd

### `mcp-server/`

Older TypeScript MCP implementation.

Relevant files:
- `mcp-server/api/mcp/[transport]/route.ts`: transport handler and tool wiring.
- `mcp-server/src/tools/`: per-tool implementations.
- `mcp-server/src/storage/`: Redis/Vector wrappers.

Important mismatch:
- This server uses `text-embedding-3-small` at 1536 dimensions in `api/mcp/[transport]/route.ts`.
- The Python pipelines are configured for `text-embedding-3-large` at 3072 dimensions.

Treat that as a potential bug/risk area if work touches retrieval quality or index compatibility.
Unless a task explicitly targets the legacy server, do not make it your default edit target.

### `cloudflare-mcp/mcp-server/`

Cloudflare Worker MCP implementation and current production server.

Relevant files:
- `cloudflare-mcp/mcp-server/src/index.ts`: all MCP tool definitions and retrieval logic.
- `cloudflare-mcp/mcp-server/wrangler.jsonc`: Worker configuration.
- `cloudflare-mcp/mcp-server/package.json`: deploy/dev/type-check commands.

Notable behavior:
- Adds recency scoring and source weighting on top of vector similarity.
- Includes live GitHub helper logic for querying Arjun's GitHub accounts.
- Uses `text-embedding-3-large` at 3072 dimensions, matching the Python pipelines.
- Exposes five tools: `get_index`, `get_context`, `get_deep`, `search`, and `github`.

## Environment And Dependencies

### Python

Install from:
- `distillation/requirements.txt`

Core dependencies:
- `anthropic`
- `openai`
- `upstash-redis`
- `upstash-vector`
- `python-dotenv`
- `pydantic`
- `rich`

The README also documents a minimal direct install path:
- `pip install anthropic openai upstash-redis upstash-vector python-dotenv requests`

### Node / TypeScript

There are separate Node environments:
- `mcp-server/package.json`
- `cloudflare-mcp/mcp-server/package.json`

Do not assume one lockfile or one package manager covers the whole repo.

### Required environment variables

Across the repo, common required vars include:
- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`
- `UPSTASH_VECTOR_REST_URL`
- `UPSTASH_VECTOR_REST_TOKEN`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`

Source-specific additions:
- `GITHUB_API_KEY`
- `GITHUB_USERNAME`
- `GMAIL_MBOX_PATH`
- `CLAUDE_EXPORT_PATH`
- `GPT_EXPORT_PATH`
- `ARCHIVE_PATH`

Env loading is flexible and a little inconsistent by subsystem. Check the relevant `config.py` before assuming where `.env` should live.

## Recommended Workflow For Agents

### When asked to work on ingestion

Start with:
- `ingestion/core/config.py`
- the relevant source runner in `ingestion/`
- `ingestion/core/storage.py`

Then verify whether the change also affects retrieval metadata in the Worker.

### When asked to work on retrieval or MCP

Start with:
- `cloudflare-mcp/mcp-server/src/index.ts`
- repo-root test scripts

Only touch `mcp-server/` if the user explicitly wants the Vercel implementation or you confirm that path is still in use.

### When asked about architecture

Use:
- `README.md` for current implementation
- PRDs for intent and upcoming direction
- live code to resolve conflicts

Explicitly call out when the docs and code disagree.

### When asked about automation or background ingestion

Start with:
- `ingestion/agent_sessions/run.py`
- `ingestion/agent_sessions/com.arjun.knowledge-agent-sessions.plist`

The intended setup is a macOS `launchd` job that runs agent-session ingestion every 6 hours.

## Commands That Are Usually Relevant

### Python distillation

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/distillation
pip install -r requirements.txt
python main.py --status
python main.py --dry-run
python main.py --run
```

### Agent session ingestion

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/ingestion
python agent_sessions/run.py --dry-run
python agent_sessions/run.py --backfill
python agent_sessions/run.py --source claude_code
python agent_sessions/run.py --source codex_cli
```

### GitHub ingestion

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/ingestion/github
python run.py --dry-run
python run.py --repos "repo1,repo2"
```

### Gmail ingestion

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/ingestion/gmail
python run.py --dry-run
python run.py --since 2022
```

### launchd agent-session daemon

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system
cp ingestion/agent_sessions/com.arjun.knowledge-agent-sessions.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.arjun.knowledge-agent-sessions.plist
launchctl start com.arjun.knowledge-agent-sessions
launchctl list | grep knowledge-agent
```

### Cloudflare MCP server

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/cloudflare-mcp/mcp-server
npm install
npm run type-check
npm run dev
```

### Legacy Vercel MCP server

```bash
cd /Users/arjundivecha/Dropbox/AAA\ Backup/A\ Working/Memory/knowledge-system/mcp-server
npm install
npm run type-check
```

## Testing Guidance

### Existing tests are mostly integration scripts, not a formal test suite

The root Python test files are manual/integration checks against the deployed Worker URL, not isolated unit tests.

Use them carefully:
- they hit live infrastructure
- they depend on valid deployed endpoints
- they may fail because of credentials, network state, or deployed data rather than local code defects

### Safest validation order

1. Run static/type checks in the subsystem you changed.
2. Run the most local dry-run path available.
3. Use live MCP tests only if the change actually affects the deployed retrieval service.

## Practical Warnings

- `distillation/run.py` performs a clear-and-rewrite flow for stored entries.
- The repo contains personal data paths and archived outputs; avoid broad refactors or bulk formatting.
- `archive/` is large and mostly output data; avoid scanning it unless the task is specifically about archived entries.
- The nested git repo is `knowledge-system/`, not the parent workspace folder.
- The repo root may include operational reports such as `INGESTION_REPORT_2026-03-15.md`; treat them as user files, not generated scratch output.

## If You Need A Fast Mental Model

Think of this repo as:
- Python pipelines for ingestion/distillation
- Upstash as the storage backbone
- Cloudflare Worker as the likely live MCP surface
- PRDs as strategy documents, not guaranteed implementation truth
