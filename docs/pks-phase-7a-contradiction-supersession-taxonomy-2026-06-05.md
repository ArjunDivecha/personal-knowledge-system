# PKS Phase 7A Contradiction And Supersession Taxonomy

- Status: Builder-ready policy spec
- Date: 2026-06-05
- Phase: 7A prerequisite
- Related docs:
  - `docs/pks-memory-research-refresh-design-memo-2026-06-05.md`
  - `docs/pks-memory-research-refresh-opus-review-2026-06-05.md`
- Scope: Define how Dream should classify relationships between new observations and existing compiled claims before any Phase 7 schema code changes.

## 1. Objective

Give Dream a small, explicit taxonomy for deciding whether a new observation:

- replaces an old claim
- enriches an old claim
- coexists with an old claim under a narrower scope
- conflicts with an old claim
- expires an old temporal claim
- duplicates an existing claim

This spec prevents Phase 7 from turning every belief change into either silent overwrite or unbounded `contested` state.

## 2. Core Objects

Phase 7A introduces two main concepts:

- Observation: append-only source-backed evidence.
- Compiled claim: current or historical projection used by retrieval.

This taxonomy applies when Dream compares an observation or candidate compiled claim with an existing compiled claim for the same or related subject.

## 3. Decision Labels

### 3.1 Duplicate

Use when the new observation and existing claim express the same belief at the same scope.

Required conditions:

- Same subject or resolved entity.
- Same factual predicate.
- No materially different time, scope, or condition.
- New observation only adds redundant support.

Dream action:

- Keep one compiled claim.
- Add the new observation ID to `support_observation_ids`.
- Update evidence strength, mention/source counts, and salience inputs.
- Do not create a supersession edge.

Example:

- Existing: "PKS Dream uses proposal, grade, bounded apply, verify."
- New: "Dream applies governed changes through proposal, deterministic grading, bounded apply, and verification."
- Classification: duplicate.

### 3.2 Refinement

Use when the new observation preserves the old claim but makes it more precise.

Required conditions:

- The old claim remains true.
- The new observation adds details, constraints, implementation path, dates, thresholds, or exceptions.
- The new detail should become part of the current compiled view.

Dream action:

- Create or update a compiled claim with the refined text.
- Mark old claim as `historical` or keep it current only if still useful as a broader summary.
- Add a `refines` relationship or supersession edge with reason `refinement`.
- Preserve both support sets.

Example:

- Existing: "GitHub ingestion stores repository entries."
- New: "GitHub ingestion stamps README, commit, and code entries with `github_repo` metadata."
- Classification: refinement.

### 3.3 Supersession

Use when the new observation says the old claim is no longer current.

Required conditions:

- Same subject or resolved entity.
- Same predicate or operational slot.
- New claim is more recent or has higher source authority.
- Old claim cannot remain true in the same scope.

Dream action:

- Mark old compiled claim `superseded`.
- Set old claim `valid_to` or `invalidated_at` when known.
- Create a new current compiled claim.
- Add a supersession edge with source observation ID and reason.

Example:

- Existing: "Codex should use proposal-only Dream mode."
- New: "Dream live mode is `DREAM_AUTO_APPLY_MODE=full` with tripwire and kill-flag protection."
- Classification: supersession.

### 3.4 Scoped Exception

Use when old and new claims appear inconsistent but are both true under different scopes.

Required conditions:

- The subject or predicate differs by repo, environment, tool, account, project, date range, user mode, or other meaningful condition.
- The new observation can be expressed as a scoped claim rather than overwriting the old claim.
- No evidence says the old scope is invalid.

Dream action:

- Preserve both claims as current within their scopes.
- Add or update explicit `scope` fields.
- Avoid marking either claim superseded.
- If scope is unknown, classify as `contest` rather than guessing.

Example:

- Existing: "Speed mode can use broad web search."
- New: "Accuracy/client legal workflows must not external-search raw client facts."
- Classification: scoped exception.

### 3.5 Contestation

Use when the new observation conflicts with the old claim and Dream cannot safely decide supersession, refinement, or scope.

Required conditions:

- Same or overlapping subject.
- Claims cannot both be true in the same scope.
- Source authority, recency, or scope is insufficient to choose a current claim safely.

Dream action:

- Keep old claim current only if needed for continuity, but mark subject or claim `contested`.
- Add new claim as `contested`.
- Create contest edge and include reasons.
- Queue or retain for later Dream review/evidence.
- Do not archive either claim solely because it is contested.

Example:

- Existing: "A repo's active runtime path is root `mcp-server/`."
- New: "The live production MCP path is `cloudflare-mcp/mcp-server/`."
- If no repo-local evidence confirms which is live, classify as contestation.
- If repo docs and tests confirm the latter, classify as supersession.

### 3.6 Temporal Expiry

Use when a claim was time-bound and is no longer current because the relevant date or validity window has passed.

Required conditions:

- Claim contains or has derived temporal validity.
- `valid_to`, due date, planned date, event date, or temporal phrase resolves before `now_utc`.
- The claim describes a future/pending/action state rather than timeless history.

Dream action:

- Mark old claim `expired` or `historical`.
- Remove it from default current-answer projection.
- Preserve it for point-in-time and audit queries.
- Optionally create a new open-question claim if completion status is unknown.

Example:

- Existing: "The user is going to Singapore in July."
- Now: after July, with no source confirming the trip is still future.
- Classification: temporal expiry.

### 3.7 Deprecation

Use when a claim is no longer useful or should not appear in active retrieval, but it is not factually contradicted.

Required conditions:

- Claim is obsolete, low-value, or attached to an abandoned/completed context.
- No direct replacement claim is needed.
- Evidence should remain auditable.

Dream action:

- Mark compiled claim `deprecated` or archive the parent entry through existing governed archive paths.
- Do not infer a new current claim.

Example:

- A one-off PRD example entry remains source-backed but no longer belongs in Tier 1 or current project context.

## 4. Source Authority Rules

`source_authority` is a field on observations, not a separate lane.

Recommended values:

- `explicit`: operator directly asked to remember, correct, forget, or update a belief.
- `manual`: operator or maintainer edited a memory/document/tool config directly.
- `system`: validated tool output, test result, Git commit, GitHub run, ledger, or deploy status.
- `inferred`: LLM extraction from conversation, email, repo text, or summarization.

Default precedence for same-scope claims:

1. explicit
2. manual
3. system
4. inferred

Precedence is not absolute. A stale explicit claim can expire temporally, and a later system-validated result can refine an older explicit instruction if the old instruction was about desired execution state rather than stable preference.

## 5. Recency Rules

Recency matters only after scope and authority are understood.

Use recency to choose supersession when:

- Same subject.
- Same operational slot.
- Same scope.
- Newer source has equal or higher authority.

Do not use recency to overwrite:

- stable identity facts
- long-lived preferences
- procedural rules
- scoped exceptions
- contested claims with unresolved evidence conflict

## 6. Scope Dimensions

Dream should look for these scope fields before deciding contradiction:

- repo or workspace
- project
- environment, deployment, or runtime path
- model/provider/tool
- account or tenant
- user mode or task mode
- date range or temporal validity
- source type
- agent/client
- legal/compliance boundary

If a conflict disappears after adding scope, classify as scoped exception or refinement rather than supersession.

## 7. Deterministic Grade Requirements

A future Dream compile proposal that changes claim status must include:

- old claim ID
- new claim ID or observation ID
- decision label from this taxonomy
- source authority comparison
- scope comparison
- temporal comparison when relevant
- expected revisions for all mutated claims
- rollback metadata
- operator review flag when classification is `contest`

Hard-gate failures:

- supersession without same subject or resolved entity
- supersession without source observation evidence
- refinement that drops old support evidence
- scoped exception without explicit scope fields
- temporal expiry without resolved date or validity window
- procedural memory mutation through the semantic compiler
- contested claim archived solely because it is contested

## 8. Minimal Test Fixtures For Phase 7A

Add synthetic fixtures, not live user facts, covering these taxonomy states.
Phase 7A fixture coverage is representational: tests must prove the schema can
encode and round-trip the outcome, and pure helpers must reject procedural
semantic compilation. Automatic classification of these cases is Phase 7C.

1. Duplicate support merge.
2. Refinement preserving old evidence.
3. Supersession with `valid_to`.
4. Scoped exception keeping both current.
5. Contestation requiring review.
6. Temporal expiry.
7. Deprecation without replacement.
8. Procedural-memory mutation rejected.

## 9. Real-PKS Examples To Mine Before Phase 7C

Before implementing Dream compile operations, sample real cases from existing entries or docs:

- root `mcp-server/` versus `cloudflare-mcp/mcp-server/`
- proposal-only Dream versus governed full apply
- local scheduling versus remote-first scheduled ingestion
- stale active project statuses
- client-legal search mode boundaries
- repo metadata backfill rules

The Phase 7A schema does not need to solve every real case, but it must be able to represent them without losing evidence.

## 10. Builder Acceptance Criteria

Phase 7A code is not ready until:

- this taxonomy is referenced by the Phase 7A builder plan
- fixture cases cover every decision label representationally
- no compiler path can mutate procedural memory
- every status-changing operation can cite observation evidence
- every supersession/deprecation is reversible through rollback metadata
