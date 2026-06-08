# PKS Memory Upgrade Checklist

Last updated: 2026-06-05
Branch: `Dream`
Source PRD: [PKS-Upgrade-PRD.md](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/PKS-Upgrade-PRD.md)

## Status

- [x] Phase 0 started
- [ ] Phase 0 complete
- [x] Phase 1 complete
- [x] Phase 2 complete
- [x] Phase 3 complete
- [x] Phase 4 complete
- [ ] Phase 5 complete
- [ ] Phase 6 complete
- [x] Phase 7 offline memory architecture complete
- [x] Phase 8 offline retrieval contract complete
- [x] Phase 8 live Worker retrieval wiring complete

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
- [x] Verify current OAuth scope model for future write-capable MCP tools

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

- [x] Implement atomic access counting with `INCR entry_access:{id}`
- [x] Limit `search`-triggered reconsolidation to the top 5 returned results
- [x] Add `reconsolidation:errors:{date}` logging
- [x] Define fold-back semantics for access counters during Dream
- [x] Add acceptance tests for repeated retrieval promotion behavior

Phase 4 notes:
- `entry_access:{id}` and `entry_last_accessed:{id}` are now the authoritative live counters.
- Retrieval paths overlay those side keys on read, so stale entry blobs cannot regress visible access counts.
- Background reconsolidation folds the current side-key values back into the canonical Redis entry on each retrieval.
- Dream must treat the side keys as source of truth before any archive/prune decision, then persist the folded values into the run audit before any reset/rotation.
- Manual live acceptance check on `2026-03-27`: repeated `get_deep` calls promoted `ke_0c2508065679` from `task_query` to `recurring_pattern` and moved it to injection tier `2`.

## Phase 5: Dream Job

- [x] Add migration guard so Dream no-ops until backfill is complete
- [x] Add scheduled Worker handler
- [x] Add cron config
- [x] Keep initial Dream deterministic and non-LLM
- [ ] Add external-runner fallback path for replay-heavy work
- [x] Confirm execution path for Dream on the current Workers plan
- [x] Add `index:rebuild:lock` plus staging-key swap for index rebuilds
- [x] Add timestamped archive keys plus `:latest` pointers
- [x] Define `dream:run:{iso}` schema
- [x] Define `consolidation_notes` schema/format
- [ ] Add Dream audit retention policy
- [ ] Add Dream alert thresholds

Phase 5 notes:
- The nightly scheduled Dream path is now proposal-first. It generates bounded governance proposals with 10 archive / 10 promotion caps and does not directly mutate live entries.
- Dream replay now includes deterministic duplicate merge and contradiction handling before promotion/archive decisions.
- The latest public proposal is exposed through `dream:proposal:last` and `/health` fields such as `last_dream_proposal_run`.
- Live cron registration is active for `10 7 * * *` UTC, which is `00:10 PDT`.
- Reversible archival writes and restore semantics are implemented behind the Dream engine and were verified on `2026-03-27` with a controlled single-entry archive/restore test.
- Staging end-to-end validation now targets the PRD R5 lifecycle: proposal, grade, bounded apply, post-apply verify, rollback, post-rollback verify, `/openai/mcp` read-only compatibility, and final strict consistency verification.

## Phase 6: Ingestion Hardening And Operator Tools

- [ ] Add cross-source fusion helper
- [ ] Add source-aware mention counting
- [ ] Add project staleness rule to classification
- [x] Add `get_dream_summary`
- [x] Add `restore_archived`
- [x] Add `set_context_type`
- [x] Add OAuth scope checks and rate limits for write-capable tools
- [ ] Update `skill/SKILL.md` for tier-aware usage

## Phase 6.5: Salience Signal Enrichment

- [x] Add explicit-save signal detection in chat-export distillation
- [x] Add `signal_flags` metadata and shared salience multipliers
- [x] Add LLM correction-event extraction and contestation proposals
- [x] Add read-only outcome-quality baseline for recall, temporal freshness, and project lifecycle staleness
- [x] Research current memory-system designs before Phase 7 schema work
- [x] Vet the revised Phase 7 direction with Opus

## Phase 6.75: Memory Research Refresh

- [x] Write `docs/pks-memory-research-refresh-design-memo-2026-06-05.md`
- [x] Save Opus review in `docs/pks-memory-research-refresh-opus-review-2026-06-05.md`
- [x] Narrow Phase 7A to observations plus compiled claims only
- [x] Move explicit-vs-inferred distinction to `source_authority`
- [x] Keep procedural memory out of the Phase 7A compiler
- [x] Defer memory blocks until compiled claims exist

## Phase 7A: Offline Observation And Claim Schema

- [x] Write contradiction/supersession taxonomy spec
- [x] Write compile-latency/provisional-claim policy spec
- [x] Write builder-ready Phase 7A implementation plan
- [x] Vet Phase 7A builder packet with Opus
- [x] Add offline Phase 7A dataclasses for observations, compiled claims, and supersession edges
- [x] Add offline migration preview from legacy knowledge/project entries
- [x] Add synthetic Phase 7A fixtures
- [x] Add unit tests proving no live storage mutation
- [x] Export Phase 7A helpers from `distillation/models/__init__.py`

## Phase 7B: Temporal Normalization And Entity Linking

- [x] Fold temporal-language normalization into Phase 7 compile
- [x] Add entity mention extraction and stable entity IDs
- [x] Add source-aware entity index fixture
- [x] Add outcome probes for current vs stale temporal facts

## Phase 7C: Compiled Current View

- [x] Add offline compiled-view generator
- [x] Add current projection fixtures
- [x] Add Dream proposal operations for compile/supersede/mark-current
- [x] Add deterministic grade checks for compile operations

## Phase 7D: Memory Blocks

- [x] Add memory block schema
- [x] Add read-only operator profile block
- [x] Add current project status block
- [x] Add procedural/policy pointer block
- [x] Add tests for size limits and source traceability

## Phase 7 Acceptance Gate

- [x] Add end-to-end offline acceptance harness for current recall, stale exclusion, provisional TTL, compile-grade, memory-block, policy-pointer, and procedural-isolation probes

## Phase 8: Retrieval Upgrade

- [x] Write builder-ready Phase 8 implementation plan
- [x] Add offline hybrid retrieval candidates over compiled current claims, unexpired provisional claims, memory blocks, and observations for history/evidence queries
- [x] Add deterministic query classification for current-answer, evidence/history, point-in-time, and procedural/policy intents
- [x] Add lane-aware and source-priority-aware scoring with lexical, entity, optional vector, temporal, and provenance components
- [x] Add recall, evidence/history, point-in-time, stale-current, provisional, and policy-pointer fixture probes
- [x] Export Phase 8 helpers from `distillation/models/__init__.py`
- [x] Wire the live Cloudflare MCP search path to the Phase 8 retrieval contract after offline evals stay green

## Phase 9: Quality-Gated Dream

- [x] Write builder-ready Phase 9 implementation plan
- [x] Add offline pre/post outcome gate over Phase 8 eval reports
- [x] Detect post-apply recall regressions, missing post checks, and new post failures when the pre baseline is clean
- [x] Add rollback recommendation payload for the existing Dream rollback contract
- [x] Add validation-ledger detail payload for `dream_outcome_quality` without writing live ledger entries in tests
- [x] Add fixture and focused unit tests proving the gate catches a real Phase 8 retrieval regression
- [x] Wire production Dream apply runner to execute Phase 9 probes and invoke rollback automatically when explicitly enabled
- [x] Add Worker replay tests for pre-baseline block, green apply plus ledger write, and post-regression auto-rollback

Phase 6.5 audit notes:
- Concurrent-run safety: the full Dream cycle path has a Redis single-flight guard (`dream:lock`) with a 30-minute TTL and stale-lock reclaim before live mutations. The scheduled governance proposal path and the operator `run_dream_proposal` path currently call `runDreamProposal` directly, which snapshots all entries without taking that lock; `apply_dream_proposal` is protected by proposal grading, candidate snapshots, and expected revisions, but proposal generation itself can still overlap. Follow-up issue: add the same proposal-level single-flight guard, or an explicit idempotency key, around `runDreamProposal` before correction-derived contest proposals increase replay volume.
- Replay narrowness: candidate discovery is not delta-scoped today. Both scheduled and operator proposal generation load the full `knowledge:*` and `project:*` active sets unless `candidate_ids` are supplied, then run deterministic duplicate/contradiction replay over labels, current views, and position snippets. The scans are bounded to structured entry fields rather than full content embeddings, but Phase 6.5/7 should add a last-successful-run delta path before replay-heavy correction handling ships.
- Phase 7 framing: evidence-log plus compiled-view separation is still motivated by contradiction repair at the source, but Opus review narrowed the first implementation slice. Phase 7A is offline-only observations plus compiled claims; contradiction/supersession taxonomy and compile-latency policy are required before schema code. The compiled view can be rewritten when a belief changes, while the superseded claim remains preserved as evidence for auditability and future Dream review.
- Correction-event path: distillation now classifies user correction turns, creates a `correction_derived` knowledge entry for the new belief, searches active Tier 1/2 memories for the corrected belief, LLM-judges contradictions, and writes pending `dream:contest_hint:*` records. Dream proposal generation consumes those hints as governed `mark_contested` operations with `proposal_kind: "contest"`; apply marks the hint `applied` after the proposal passes grading and is applied.

## Acceptance Gates

- [ ] One-off PRD example entries land in Tier 3 after backfill
- [x] `index:current` matches active Redis state within expected bounds
- [x] Redis and vector metadata match on sampled entries
- [ ] `search("investing")` ranks Tier 1 and Tier 2 above Tier 3
- [x] Repeated retrieval increments access counters without races
- [x] Dream proposal generation produces reversible archive candidates only
- [x] Controlled proposal apply/rollback preserves apply artifacts and returns the entry to active state
- [x] New write-capable MCP tools reject unauthorized calls

## Cloudflare Plan Notes

- Workers Paid is now active on the deployment account.
- The chosen Dream execution path is a bounded nightly Worker cron with explicit archive and promotion caps.
- If replay-heavy Dream logic outgrows the current Worker budget, the next fallback is still an external runner.
