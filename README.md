# Personal Knowledge System

PKS is Arjun's source-backed memory service for AI assistants. Production is a
single read-only, source-first system: it builds immutable evidence generations
from authoritative project files, explicit operating-memory files, curated
records, and a bounded window of recent Claude Code and Codex sessions.

The older entry store, tiers, salience, reconsolidation, and Dream machinery
remain in the repository as legacy code and audit history. They do not maintain
or validate the production corpus.

## Production architecture

One complete generation contains:

- authoritative README, PRD, spec, report, handoff, FABLE, and ARJUN files;
- explicitly configured operating-policy and curated-memory files;
- recent redacted Claude Code and Codex conversational context;
- a project catalog derived from real source folders and timestamps;
- vectors, exact-identifier maps, project maps, source-family maps, checksums,
  suppressions, and a manifest under the same generation ID.

Recent sessions are not a second memory lane. They use the same
`EvidenceRecord`, publication transaction, vector namespace, Redis namespace,
and search calculation as durable files. They are marked `working_context`,
retain lower authority than durable sources, and receive only a small
semantic-relevance-gated attention lift with a three-day half-life.

## Retrieval contract

The live Cloudflare Worker is under `cloudflare-mcp/mcp-server/` and production
sets `SOURCE_FIRST_MODE=on`.

Search uses one transparent score:

```text
base_score =
    0.70 * semantic_similarity
  + 0.15 * lexical_overlap
  + 0.10 * source_authority
  + 0.05 * source_recency

working_context_bonus =
    0.08 * semantic_similarity * exp(-ln(2) * age_days / 3)

final_score = min(1, base_score + working_context_bonus)
```

Exact identifiers and explicitly named projects retain ordering priority.
Byte-identical evidence collapses on `content_checksum`, with alternate source
paths retained. General results must clear a `0.65` final-score floor; otherwise
the service explicitly abstains and returns no evidence.

The main read tools are:

- `get_index` — current generation and project catalog;
- `search` / `get_context` — source-backed excerpts and score components;
- `get_deep` — every sibling chunk from one `ev_*` evidence source;
- `get_validation_status` — current source-first generation and freshness;
- `get_dream_summary` — the same source-first status plus explicit notice that
  Dream is retired from production maintenance;
- `health` — Worker build identity, generation, and source/session freshness.

Legacy write and Dream tools may remain in the MCP schema for compatibility.
They are disabled or non-authoritative in production source-first mode.

## Atomic rebuild and scheduling

`.github/workflows/source-first-rebuild.yml` is the only scheduled production
maintenance path. GitHub schedules it every two hours on the self-hosted macOS
runner that can read the local Dropbox and agent-session sources. There is no
PKS serving LaunchAgent or local cron.

Each run:

1. scans, redacts, chunks, and checksums all configured evidence;
2. stages a complete candidate without moving `sf:current_generation`;
3. verifies Redis records, vectors, project maps, and source maps;
4. runs the exact production Worker retrieval implementation against the staged
   generation, including negative-control abstention;
5. promotes by writing the heartbeat and live pointer only after every gate
   passes;
6. verifies the now-serving generation and uploads text-free build diagnostics.

A failed scan, publish, verification, or retrieval evaluation leaves the last
good generation live.

## Commands

Build local artifacts without remote writes:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py
```

Stage a remote candidate without moving the serving pointer:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --stage
```

Verify or promote a staged generation:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --verify-generation sf_YYYYMMDDTHHMMSSZ
ingestion/.venv/bin/python scripts/source_first_rebuild.py --promote-generation sf_YYYYMMDDTHHMMSSZ
```

Verify production freshness and storage completeness:

```bash
ingestion/.venv/bin/python scripts/source_first_rebuild.py --verify-current --max-age-hours 36
```

Run focused tests:

```bash
ingestion/.venv/bin/python -m unittest discover -s tests/python -p 'test_source_first.py'
cd cloudflare-mcp/mcp-server
npm run type-check
npm run test:worker -- test/sourceFirst.test.ts
```

Deploy the production Worker:

```bash
bash scripts/deploy_cloudflare_worker.sh
```

## Source map

- `shared/source_first_config.json` — source roots, bounds, authority, and
  recent-session policy;
- `ingestion/source_first/scanner.py` — authoritative file discovery;
- `ingestion/source_first/session_scanner.py` — direct session parsing,
  mapping, bounds, and pre-persistence redaction;
- `ingestion/source_first/models.py` — unified immutable evidence contract;
- `ingestion/source_first/publisher.py` — staged storage, verification, and
  promotion;
- `scripts/source_first_rebuild.py` — build/operator CLI;
- `cloudflare-mcp/mcp-server/src/sourceFirst.ts` — production retrieval and
  operational health;
- `tests/probes/` — deterministic retrieval probes;
- `docs/source-first-memory.md` — detailed product and operator contract;
- `openwiki/quickstart.md` — code-grounded navigation.

## Legacy boundary

The following are not production-serving sources of truth:

- `ke_*` entries and the old thin index;
- tier counts, salience, access-count reinforcement, and reconsolidation;
- Dream runs, validation ledgers over the old entry store, and local nightly
  ingestion success markers;
- `mcp-server/`, the older Vercel MCP implementation.

Use them only for explicit legacy investigation. A green legacy gate does not
prove the production source-first corpus is healthy, and a red legacy Dream
ledger does not make the production corpus unhealthy.
