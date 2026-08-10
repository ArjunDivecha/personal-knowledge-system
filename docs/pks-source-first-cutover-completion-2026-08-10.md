# PKS Source-First Cutover Completion Report

**Completed:** 2026-08-10

## Outcome

PKS production is now one source-first system. Authoritative files and bounded,
redacted recent Claude Code/Codex working context are built into one immutable
generation, evaluated through one production search implementation, and
promoted through one remote workflow.

The legacy `ke_*` store, validation ledger, tiers, salience, access
reinforcement, reconsolidation, and Dream machinery no longer describe or
maintain production health. They remain only as legacy code and audit history.

## Shipped state

- Repository commit: `345e5eabf289c76a1ccbbffbd347a5a8ff19196f`
- Cloudflare Worker version: `7c7f097e-f63d-4c29-9625-f8d9b442ec11`
- Verified scheduler run:
  <https://github.com/ArjunDivecha/personal-knowledge-system/actions/runs/31419693410>
- Generation promoted by that run: `sf_20260810T183414Z`
- Live generation contents at verification: 3,435 evidence chunks, 631 source
  files, 114 projects, 34 Claude Code sessions, 7 Codex sessions, and 250 total
  working-context chunks.

## Repairs completed

- Excluded linked Git worktrees from file discovery.
- Collapsed byte-identical query results by `content_checksum` while preserving
  alternate provenance.
- Added a `0.65` relevance floor and explicit empty-result abstention.
- Tightened opaque-identifier recognition so ordinary capitalized words and
  plain numbers cannot bypass the relevance floor.
- Made completed and working projects use equal file authority (`0.9`).
- Added stable `source_id` families and made `get_deep` return every sibling
  chunk from the source.
- Corrected `get_context`, `get_deep`, Dream, validation, search, and health
  tool descriptions/behavior for the live source-first mode.
- Replaced stale legacy validation output with active generation, heartbeat,
  source/session freshness, and retired-Dream status.
- Added Worker version metadata so health no longer reports build `unknown`.
- Split remote publication into stage, verify, candidate retrieval test, and
  atomic promote operations.
- Retained the live generation plus two rollback generations and added bounded
  cleanup for older tracked generations.
- Integrated recent sessions directly into `EvidenceRecord` with logical
  `session://` provenance, lower authority, and a transparent relevance-gated
  three-day attention decay.
- Added fail-closed session-root checks, deterministic bounds, role filtering,
  malformed-rate gates, pre-persistence secret redaction, and text-free CI
  artifacts.
- Changed the only production maintenance schedule to every two hours on the
  self-hosted GitHub runner. No local PKS scheduler was added.
- Rewrote the shared `personal-knowledge-system` skill at
  `/Users/arjundivecha/.claude/skills/personal-knowledge-system/SKILL.md` so
  Claude and Codex use the current system rather than the retired model.

## Verification evidence

- Full Worker suite: 360/360 tests passed.
- Focused Python and evaluation-contract suite: 28/28 tests passed.
- Real staged-generation retrieval gate: 50/50 probes passed.
- Live retrieval evaluation: every measured recall, project, explicit-save,
  exact-lexical, supersession, negative, and paraphrase metric was `1.000`;
  stale-leak rate was `0.000`; transport errors were zero.
- Session security scan: 250 session records, zero raw-path violations, zero
  detected bearer/API-key/private-key/credential-URL patterns, and 18 redaction
  markers.
- Live health: `ok`, source-first `green`, matching generation/heartbeat,
  recent-session freshness `fresh`, and a concrete Worker version ID.
- Live negative control: sourdough query returned `abstained: true`, reason
  `no_relevant_evidence_above_threshold`, and an empty result list.
- Live diversity control: the deflated-Sharpe query returned five unique
  checksums, with T2 first and no worktree-clone duplication.
- Live recent-context control: relevant Codex evidence returned as
  `working_context` with `base_score`, `attention_score`,
  `working_context_bonus`, and logical session provenance.
- Live deep-retrieval control: `get_deep` returned all 9/9 sibling chunks and
  `complete_source: true`.
- Live operational controls: `get_validation_status` was green and
  source-first; `get_dream_summary` identified Dream as retired and the active
  maintenance path as `source_first_rebuild`.

The GitHub run emitted one non-blocking platform warning: `upload-artifact@v4`
still declares Node.js 20 while GitHub forced it onto Node.js 24. The upload and
the full workflow completed successfully.
