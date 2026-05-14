# llmchat.md — Project Context Log

This file is the shared memory between Claude.ai and Claude Code.
It is append-only. Do not edit existing entries.
Each session appends a timestamped block at the bottom.

---

---
SESSION START: 2026-05-11 22:30 PST | Agent: Claude Code
---

### Session Summary
Diagnosed and fixed three nightly ingestion pipeline failures: ARCHIVE_PATH crash on CI runners, silently dropped nightly cron for agent session ingestion, and Mac sleep causing mid-job network drops on the self-hosted runner. All three pipelines were verified end-to-end after fixes.

### Decisions Made
- **Remove module-level `ARCHIVE_PATH.mkdir()`** from `distillation/config.py`. It was executing at import time, crashing any CI job that imports distillation code. `compress.py` already does the mkdir lazily at the point of use — the module-level call was pure duplication.
- **Add `ARCHIVE_PATH=/tmp/knowledge-system/archive`** to both `github-ingestion.yml` and `twitter-ingestion.yml` as an env var so the path is always writable on Linux runners.
- **Restore nightly schedule** to `agent-session-ingestion.yml`. The `cron: "10 6 * * *"` trigger was silently dropped in commit `3bce3a2` when `repository_dispatch` was added and the workflow was renamed to "Agent Session Ingestion Backfill". Renamed back to "Agent Session Ingestion".
- **Add `caffeinate -i`** wrapping the Python runner in `agent-session-ingestion.yml`. The self-hosted Mac runner was losing network mid-job because `pmset sleep 1` fired during idle gaps between Claude API calls. Runner diagnostic log confirmed `Socket Error: HostNotFound` at the cancellation point.

### Architecture / Design

#### Nightly Pipeline Schedule (UTC)
| Time  | Workflow                 | Runner                              |
|-------|--------------------------|-------------------------------------|
| 05:40 | Twitter Ingestion        | ubuntu-latest                       |
| 06:10 | GitHub Ingestion         | ubuntu-latest                       |
| 06:10 | Agent Session Ingestion  | self-hosted macOS (knowledge-agent-sessions-mac) |

#### Ingestion Sources
- **GitHub** (`ingestion/github/run.py`): Scans 56 repos for README changes, commits, code comments, agent-context artifacts. Repo baselines + mtime prevent re-processing unchanged repos.
- **Twitter** (`ingestion/twitter/run.py`): Fetches @arjundivecha tweets since `last_seen_id`. Three paths: originals (batched 25), replies (bundled with parent), quote-tweets (bundled with quoted). Claude extracts knowledge entries.
- **Agent Sessions** (`ingestion/agent_sessions/run.py`): Reads `~/.claude/projects/**/*.jsonl` (Claude Code, 3,307 files) and `~/.codex/sessions/**/rollout-*.jsonl` (Codex CLI, 206 files). Byte-offset + mtime checkpointing ensures only new content is processed. Distills with `claude-sonnet-4-6`. Links sessions to GitHub repos via cwd detection.

#### Storage
- Upstash Redis: knowledge entries + thin index (`index:current`)
- Upstash Vector: 12,983 vectors after today's runs
- Agent session state mirrored to Redis (`--require-redis-state` enforces this on remote runs)

#### Distillation Models
- Agent sessions: `claude-sonnet-4-6` (~$0.01/session, ~$3–8/month total)
- Twitter + GitHub: `claude-opus-4-6` via `EXTRACTION_MODEL` config

#### Key Paths
- `distillation/config.py` — central config; ARCHIVE_PATH default is Dropbox path (wrong on CI)
- `ingestion/agent_sessions/run.py` — agent session runner
- `ingestion/core/storage.py` — StorageClient (Redis + Vector)
- `~/actions-runner-personal-knowledge/_diag/` — runner diagnostic logs
- `~/actions-runner-personal-knowledge/_work/` — runner work directory

### What to Build Next
1. Monitor tonight's scheduled runs (05:40 and 06:10 UTC) to confirm all three pass
2. Investigate `rollout-test.jsonl` — bad JSON, retried every run, checkpoint never advances
3. Wire up the personal-knowledge MCP server in `~/.claude/settings.json` so Claude Code can query the knowledge base directly (the skill is installed but the MCP server is not registered)
4. Clean up worktree at `.claude/worktrees/fix-agent-session-schedule`

### Constraints & Gotchas
- **`pmset sleep 1`** — Mac sleeps after 1 minute idle. `caffeinate -i` in the workflow fixes scheduled runs; manual runs via `run_scheduled.sh` have no such wrapper.
- **`--require-redis-state`** — agent session ingestion aborts if checkpoint isn't from Redis, preventing remote runners from reprocessing all history.
- **`ARCHIVE_PATH` fallback** — hardcoded to Dropbox path in `distillation/config.py`; always set the env var in any workflow that imports distillation code.
- **`worker-runtime-tests.yml`** is CI-only (push/PR to cloudflare-mcp paths) — do not add a schedule.

### Open Questions
- Why is the personal-knowledge MCP server not in `~/.claude/settings.json`? Intentional or never set up for Claude Code?
- Where is `rollout-test.jsonl` and should it be deleted or excluded?
- Did the re-triggered agent session run (post caffeinate fix, ID 25715571004) complete cleanly?

### Context for Claude Code
- Three commits landed on main today: `d100b6d` (ARCHIVE_PATH), `b165cf9` (schedule), `db71864` (caffeinate).
- Storage after today: 7,021 knowledge entries, 12,983 vectors.
- The PKS MCP skill is installed at `~/.claude/skills/personal-knowledge-system/` but MCP server not registered — it silently does nothing in Claude Code sessions until wired up.
- `caffeinate -d` PID 21408 running manually to keep Mac awake during re-triggered run.

---
SESSION END: 2026-05-12 00:15 PST | Agent: Claude Code
---
