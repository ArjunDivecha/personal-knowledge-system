# Ingestion and distillation workflow

This repository ingests multiple source types and normalizes them into structured memory entries.

The ingestion side is split between a newer source-specific ingestion layer and an older export-processing pipeline. The newer layer is the better first stop for current work.

## Primary inputs

Current source families called out in the repo docs and code include:

- GitHub repositories, including README files, recent commits, code comments, and repo-attached agent context artifacts
- Gmail mbox exports
- Twitter/X timeline data
- legacy Claude Code / Codex CLI session logs
- older Claude and GPT export material handled by the distillation pipeline

`AGENTS.md` and `README.md` both stress that the repo depends on personal exports and local paths. Most pipelines are not generic SaaS-style ingestion jobs; they are tailored to one operator's environment.

## The newer ingestion layer

The newer ingestion code lives under `ingestion/`.

### Shared configuration

`ingestion/core/config.py` is the main configuration surface. It:

- loads `.env` from the ingestion repo, parent repo, or distillation folder
- defines shared Upstash and LLM credentials
- defines source-specific settings for GitHub, Gmail, and Twitter/X
- creates a checkpoint directory for resumable runs
- validates required credentials for each source

Important behavior from the code:

- the ingestion env load is intentionally opinionated and can override a repo-level `.env`
- GitHub ingestion is bounded by file/commit limits and a skip list for generated directories
- Gmail ingestion skips short or transactional messages
- Twitter/X ingestion uses bearer-token API access and paginates incrementally

### Source-specific runners

`AGENTS.md` identifies these active runners:

- `ingestion/github/run.py`
- `ingestion/gmail/run.py`
- `ingestion/agent_sessions/run.py`
- `ingestion/twitter/run.py`

The repository history also shows ongoing hardening around GitHub ingestion, agent-session ingestion, and billing/budget guards.

### Storage and extraction

`AGENTS.md` points to shared helper modules such as:

- `ingestion/core/storage.py`
- `ingestion/core/extractor.py`

Those modules are where source material is turned into stored entries with embeddings and provenance.

## The older distillation pipeline

`distillation/` contains the earlier export-processing pipeline. It still matters because:

- it owns the original parse/filter/extract/merge/compress/index flow
- it contains storage and model helpers that other code still uses
- several docs and tests reference it directly

`AGENTS.md` notes that some of the older distillation commands are destructive refresh paths, so future edits should treat them carefully.

## Repo-attached agent context

`docs/repo-agent-context.md` describes a workflow where pre-commit hooks export redacted repo-scoped agent context artifacts under `.pks/agent-context/`.

Those artifacts are then ingested by the GitHub pipeline so the repo itself can be treated as evidence. This is a distinct source path worth preserving because it changes how the system learns from coding sessions.

## Common change risks

When editing ingestion code, watch for:

- environment loading order differences between ingestion and distillation
- source-specific file limits and skip lists
- checkpoint/resume behavior
- whether a change affects embedding dimensionality or storage compatibility
- hidden dependency on local filesystem paths or credentials

## Good next stops

- `ingestion/core/config.py`
- `AGENTS.md` ingestion section
- `docs/repo-agent-context.md`
- `README.md`
- `distillation/` entrypoints and storage helpers if you are working on the older pipeline
