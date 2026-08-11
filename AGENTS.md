# AGENTS.md

## Production truth

PKS production is the source-first Cloudflare Worker under
`cloudflare-mcp/mcp-server/`, with `SOURCE_FIRST_MODE=on`. Read `README.md`,
`docs/source-first-memory.md`, and `openwiki/quickstart.md` before changing the
runtime.

The production corpus is one immutable generation built by
`scripts/source_first_rebuild.py` from:

- authoritative files discovered by `ingestion/source_first/scanner.py`;
- curated/pinned sources in `shared/source_first_config.json`;
- recent Claude Code and Codex conversations parsed and redacted directly by
  `ingestion/source_first/session_scanner.py`.

The old `ke_*` store, thin index, tiers, salience, reconsolidation, Dream,
distillation, and source-specific legacy ingesters do not maintain production.
Do not use their validation ledgers to report source-first health.

## Invariants

- One corpus, evidence contract, generation, and search calculation.
- Sessions are `working_context`, not canonical truth or a second result lane.
- Redaction occurs before text normalization, checksums, embeddings, reports,
  Redis, or Vector metadata.
- Developer/system/tool content and retrieval-validation meta transcripts are
  excluded from session evidence.
- The public Worker resolves only `sf:current_generation`; arbitrary generation
  search is confined to the authenticated CI harness.
- A candidate is staged and retrieval-tested before the heartbeat or live
  pointer moves.
- General retrieval abstains below the relevance floor.
- Byte-identical chunks collapse by checksum; source-family maps power
  `get_deep`.
- No production ranking uses access signals, salience, tiers, classification,
  or Dream state.
- Project status derives only from authoritative files, never session activity.
- GitHub Actions is the scheduler. Do not add a local PKS LaunchAgent or cron.

## Active source locations

- `shared/source_first_config.json` — roots, authority, required projects,
  recent-session bounds and scoring policy.
- `ingestion/source_first/models.py` — evidence/project/manifest schema.
- `ingestion/source_first/scanner.py` — authoritative source scanner.
- `ingestion/source_first/session_scanner.py` — recent-session integration.
- `ingestion/source_first/publisher.py` — staged publish and promotion.
- `scripts/source_first_rebuild.py` — builder/operator CLI.
- `.github/workflows/source-first-rebuild.yml` — two-hour remote schedule and
  candidate gate.
- `cloudflare-mcp/mcp-server/src/sourceFirst.ts` — live retrieval.
- `cloudflare-mcp/mcp-server/src/index.ts` — MCP tool wiring.
- `cloudflare-mcp/mcp-server/wrangler.json` — production/staging runtime config.
- `tests/python/test_source_first.py` and
  `cloudflare-mcp/mcp-server/test/sourceFirst.test.ts` — focused invariants.
- `tests/probes/` — candidate and live retrieval contract.

`mcp-server/` is the older Vercel implementation. Do not edit it unless the
task explicitly targets that legacy surface.

## Verification order

```bash
ingestion/.venv/bin/python -m unittest discover -s tests/python -p 'test_source_first.py'
ingestion/.venv/bin/python scripts/source_first_rebuild.py
cd cloudflare-mcp/mcp-server
npm run type-check
npm run test:worker -- test/sourceFirst.test.ts
```

For a deployment, also stage and evaluate a real generation, deploy with
`bash scripts/deploy_cloudflare_worker.sh`, run the GitHub source-first workflow,
and exercise the live MCP tools. Static tests alone are not an end-to-end claim.

## Credentials and safety

Prefer the global `oprun` helper for commands that need API credentials. Never
print raw session content or matched secret values. Build artifacts uploaded by
CI may include manifests, counts, checksums, project metadata, suppressions, and
candidate-evaluation rows, but never `evidence.jsonl`.

This repo contains personal paths and old data. Avoid broad cleanup of legacy
stores or generated history unless explicitly requested. `distillation/run.py`
contains a destructive clear-and-rewrite path and is not part of source-first.

## OpenWiki

Start at [OpenWiki quickstart](openwiki/quickstart.md), then use the source-first
domain and workflow pages. Pages about Dream, memory tiers, nightly
orchestration, and old ingesters describe legacy code unless they explicitly
say otherwise.
