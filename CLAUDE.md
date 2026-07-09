# CLAUDE.md — knowledge-system (PKS)

Operator's manual for coding agents. Global rules (light mode, doc headers,
result links, FAIL-IS-FAIL) live in `~/CLAUDE.md` and `../CLAUDE.md` — not repeated here.
For deep architecture read `AGENTS.md` and `openwiki/quickstart.md`; this file is the fast path.

## Purpose
This is the **Personal Knowledge System (PKS)** — the `personal-knowledge` MCP server
Arjun uses across sessions. It ingests conversations, GitHub repos, Gmail, and X;
distills durable memories; stores them in Upstash Redis (canonical) + Upstash Vector
(embeddings); and serves retrieval + "Dream" maintenance through a Cloudflare Worker MCP.
**State: active** (commits through 2026-07). This is live infrastructure — changes here
affect every other session's recall. Move carefully.

## Architecture map (load-bearing files, absolute paths)
Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`
- `cloudflare-mcp/mcp-server/src/index.ts` — **production** MCP: all tool defs + retrieval logic
- `cloudflare-mcp/mcp-server/src/salience.ts` / `retrievalPolicy.ts` — query-time ranking + policy shaping
- `cloudflare-mcp/mcp-server/src/dream.ts` / `judgeQueue.ts` / `phase9OutcomeGate.ts` — Dream consolidation/forgetting + safety gates
- `cloudflare-mcp/mcp-server/wrangler.json` — Worker config (note: `.json`, not `.jsonc`)
- `ingestion/core/config.py` / `storage.py` / `extractor.py` — shared ingestion config, Upstash+OpenAI client, extraction
- `ingestion/{github,gmail,twitter,agent_sessions}/run.py` — per-source ingestion runners
- `distillation/main.py` (CLI) / `run.py` (checkpointed) — legacy Claude/GPT export pipeline
- `orchestrator/engine.py` / `stages.py` / `dream.py` — nightly state machine (run identity, locking, ledger, resume)
- `scripts/nightly_orchestrator.py` — thin CLI entry for the orchestrator
- `scripts/run_eval.py` — retrieval-quality eval runner (probes → metrics report)
- `mcp-server/` — **legacy** Vercel-style MCP. NOT production. Do not default edits here.

## Commands that work (verified: files/targets exist in-repo)
From repo root (`make` targets read the Makefile):
- `make worker-typecheck` — `tsc --noEmit` in the Worker (verified target)
- `make worker-test` — vitest worker suite (verified target)
- `make test-python-checker` — `unittest discover -s tests/python` (verified target)
- `make verify-memory-full` — memory consistency check (uses `distillation/venv/bin/python`, which exists)
- `make audit-memory-quality` / `make verify-memory-quality` — quality audit / write-gate
Worker dev (`cd cloudflare-mcp/mcp-server`): `npm install`, `npm run type-check`, `npm run dev`, `npm run test:worker`, `npm run deploy` (verified in package.json).
Retrieval eval (read-only, hits live MCP): `python3 scripts/run_eval.py` — see the script's own header (verified).
Ingestion dry-runs, e.g. `cd ingestion && python github/run.py --dry-run` (verified entry point).
Anything below not run by you this session: treat as **(unverified)** and confirm before claiming it works.

## Data locations (full paths)
- Canonical store: **Upstash Redis** (remote); embeddings: **Upstash Vector** (remote) — credentials in `.env` (gitignored)
- `archive/` — 550 archived entry JSONs, ~2.6M, **gitignored**; do not scan unless the task is about archived entries
- Eval reports: `scripts/reports/eval_baseline_<UTC>.json` — **gitignored** (not durable)
- Probe suite: `tests/probes/*.json` (one file per retrieval axis)
- Fixtures: `tests/fixtures/*.json`; agent-session checkpoints: `ingestion/checkpoints/agent_sessions_state.json`
- Env: `.env`, `.env.staging.local`, `.env.bak_2026_07_05` in repo root — all gitignored, all contain **real secrets**

## Conventions & gotchas (repo-specific, from code + git history)
- **Two MCP servers.** `cloudflare-mcp/mcp-server/` is production; root `mcp-server/` is legacy. They use *different embedding models* (legacy: `text-embedding-3-small`@1536; prod + Python: `text-embedding-3-large`@3072, see `AGENTS.md:135`). Never deploy legacy against the live Vector index.
- **`distillation/run.py` is destructive** — it clears entries before re-storing (full refresh). Do not run casually.
- **Dream is safety-gated.** History is dominated by data-loss prevention (`Phase 0+1: stop data loss`, outcome gates, judge queue). Read the relevant tests + recent commits before changing Dream/forgetting behavior; explicit-save and active-project entries are protected from automatic weakening.
- **No local `launchd` for ingestion.** Scheduling is remote (GitHub Actions for ingestion/Twitter, Cloudflare cron for Dream). If source files must be read from this Mac, use the self-hosted runner (`knowledge-agent-sessions`), keep the scheduler remote.
- **Env loading is fragile & per-subsystem.** Check the relevant `config.py` before assuming where `.env` lives; a repo-level provider key can shadow the ingestion OpenAI key (the in-flight `override=True` fix in `ingestion/core/config.py`).
- **No-fake-zero rule.** Unmeasured metrics render as `UNMEASURED`/null, never a fabricated 0 (see `scripts/run_eval.py` header).
- **PRDs in `docs/` are aspirational** — prefer runtime code + recent git history when they conflict.
- Write tools (`create_entry`, `archive_entry`, Dream apply, …) require `mcp:write` scope; reads are always available.

## Current state
- **Active.** Recent work: nightly orchestrator hardening, SDK auth/billing routing, Dream insight synthesis, and an **eval-baseline** effort (8-axis probe suite + `run_eval.py`).
- **In flight (uncommitted):** `ingestion/core/config.py` env-override fix; untracked `scripts/benchmark_openrouter_extraction_models.py` (extraction-model benchmarking).
- **Known gap:** the eval runner measures retrieval quality but nothing *enforces* a no-regression baseline (see `FABLE.md` P0).
