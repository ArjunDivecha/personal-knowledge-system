# PKS Memory Upgrade Checklist

Last updated: 2026-03-27
Branch: `Dream`
Source PRD: [PKS-Upgrade-PRD.md](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/PKS-Upgrade-PRD.md)

## Status

- [x] Phase 0 started
- [ ] Phase 0 complete
- [x] Phase 1 complete
- [x] Phase 2 complete
- [x] Phase 3 complete
- [ ] Phase 4 complete
- [ ] Phase 5 complete
- [ ] Phase 6 complete

## Phase 0: Baseline Audit

- [x] Confirm production implementation target is `knowledge-system/cloudflare-mcp/mcp-server`
- [x] Confirm legacy `knowledge-system/mcp-server` should not be the default target
- [x] Confirm `.gitignore` covers `.env`, `.dev.vars`, checkpoints, and archives
- [x] Confirm `cloudflare-mcp/mcp-server/.dev.vars` is not tracked by git
- [x] Confirm local `.env` files exist for `distillation/` and `ingestion/`
- [x] Measure live Redis key counts
- [x] Measure current Upstash Vector index stats
- [x] Inspect live `index:current` footprint and freshness
- [x] Sample live Redis entry shapes for knowledge entries
- [x] Sample live Redis entry shapes for project entries
- [x] Sample live vector metadata shape
- [x] Identify current schema coverage gaps versus PRD
- [x] Identify current index consistency issues
- [x] Identify current vector metadata normalization gaps
- [x] Identify config-level blockers that still require manual confirmation
- [x] Verify Cloudflare plan tier supports the intended scheduled CPU budget
- [ ] Verify deployed Worker rejects external scheduled invocations
- [ ] Verify current OAuth scope model for future write-capable MCP tools

## Phase 1: Schema And Migration Hooks

- [x] Add `schema_version` to knowledge and project metadata
- [x] Add migration-safe defaults:
  - `classification_status`
  - `context_type`
  - `mention_count`
  - `first_seen`
  - `last_seen`
  - `auto_inferred`
  - `source_weights`
  - `injection_tier`
  - `salience_score`
  - `last_consolidated`
  - `consolidation_notes`
  - `archived`
- [x] Add project-side `access_count` and `last_accessed`
- [x] Add Worker read-time shim for missing `schema_version`
- [x] Append new writes to `classification:pending` during the migration window
- [ ] Add deprecation note for legacy `knowledge-system/mcp-server`

## Phase 2: Backfill And Storage Normalization

- [x] Run `distillation/backup_upstash.py` before any live mutation
- [x] Create resumable backfill scripts under `knowledge-system/scripts/`
- [x] Add backfill budget cap and rate-limit abort conditions
- [x] Backfill knowledge entry metadata
- [x] Backfill project entry metadata
- [x] Normalize vector metadata for all active entries
- [x] Backfill missing vector `source` metadata for ingestion-created entries
- [x] Rebuild `index:current`
- [x] Run Redis <-> Vector consistency verification
- [x] Mark backfill complete with a dedicated migration flag
- [x] Stop appending to `classification:pending`
- [x] Clean up `classification:pending`

## Phase 3: Tier-Aware Retrieval

- [x] Pin the salience formula in one shared config/fixture contract
- [x] Add shared salience fixtures for Python and TypeScript
- [x] Implement tier precedence rules
- [x] Add `tier_filter` to production `search`
- [x] Exclude archived entries from retrieval by default
- [x] Return `context_type`, `injection_tier`, and `salience_score` from retrieval tools
- [x] Update `get_index` to return tier counts and Dream status
- [x] Add health/status endpoint for rollout confidence

## Phase 4: Reconsolidation

- [ ] Implement atomic access counting with `INCR entry_access:{id}`
- [ ] Limit `search`-triggered reconsolidation to the top 5 returned results
- [ ] Add `reconsolidation:errors:{date}` logging
- [ ] Define fold-back semantics for access counters during Dream
- [ ] Add acceptance tests for repeated retrieval promotion behavior

## Phase 5: Dream Job

- [ ] Add migration guard so Dream no-ops until backfill is complete
- [ ] Add scheduled Worker handler
- [ ] Add cron config
- [ ] Keep initial Dream deterministic and non-LLM
- [ ] Add external-runner fallback path for replay-heavy work
- [ ] Decide free-plan-compatible execution path for Dream:
  - external runner / GitHub Actions
  - or Cron trigger that only wakes a Durable Object / Queue consumer
- [ ] Add `index:rebuild:lock` plus staging-key swap for index rebuilds
- [ ] Add timestamped archive keys plus `:latest` pointers
- [ ] Define `dream:run:{iso}` schema
- [ ] Define `consolidation_notes` schema/format
- [ ] Add Dream audit retention policy
- [ ] Add Dream alert thresholds

## Phase 6: Ingestion Hardening And Operator Tools

- [ ] Add cross-source fusion helper
- [ ] Add source-aware mention counting
- [ ] Add project staleness rule to classification
- [ ] Add `get_dream_summary`
- [ ] Add `restore_archived`
- [ ] Add `set_context_type`
- [ ] Add OAuth scope checks and rate limits for write-capable tools
- [ ] Update `skill/SKILL.md` for tier-aware usage

## Acceptance Gates

- [ ] One-off PRD example entries land in Tier 3 after backfill
- [x] `index:current` matches active Redis state within expected bounds
- [x] Redis and vector metadata match on sampled entries
- [ ] `search("investing")` ranks Tier 1 and Tier 2 above Tier 3
- [ ] Repeated retrieval increments access counters without races
- [ ] Dream dry run produces reversible archive candidates only
- [ ] New write-capable MCP tools reject unauthorized calls

## Cloudflare Free Plan Notes

- Current free-plan limits are sufficient for the existing personal MCP service pattern:
  - Workers requests: `100,000/day`
  - HTTP Worker CPU: `10 ms`
  - Subrequests: `50/request`
  - Cron Triggers: `5/account`
  - Durable Object requests: `100,000/day`
- The PRD as originally written is **not** free-plan-safe if Dream runs directly inside a Worker Cron Trigger, because free-plan Cron CPU is only `10 ms`.
- Free-plan-compatible path:
  - keep the interactive MCP service on Workers + Durable Objects
  - move Dream to an external runner, or use the Cron Trigger only as a lightweight wake-up mechanism for a more capable downstream path
