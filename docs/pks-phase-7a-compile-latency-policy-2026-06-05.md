# PKS Phase 7A Compile-Latency And Provisional-Claim Policy

- Status: Builder-ready policy spec
- Date: 2026-06-05
- Phase: 7A prerequisite
- Related docs:
  - `docs/pks-memory-research-refresh-design-memo-2026-06-05.md`
  - `docs/pks-memory-research-refresh-opus-review-2026-06-05.md`
  - `docs/pks-phase-7a-contradiction-supersession-taxonomy-2026-06-05.md`
- Scope: Define what retrieval may do after a new observation exists but before Dream compiles it into a durable current claim.

## 1. Objective

Phase 7 adopts an ADD-only hot path:

- ingestion appends observations
- Dream compiles current claims later
- retrieval prefers compiled current claims

This creates compile latency. A new high-authority observation may be true and useful before the next Dream run. This policy defines when such observations can be visible as provisional context without bypassing Dream governance.

## 2. Definitions

### Observation

Append-only source-backed event.

### Compiled Claim

Dream-approved current or historical projection used by retrieval.

### Provisional Claim

Temporary retrieval projection created from a high-authority observation before Dream compilation.
In the Phase 7A schema, this is represented as a compiled claim with
`status: pending_compile`, `compiled_by: provisional_projection`, and
`ttl_expires_at` set. Do not use `valid_to` for provisional TTL; `valid_to`
means fact validity.

### Pending Compile

State indicating that Dream must either promote, refine, supersede, contest, or expire the provisional claim.

## 3. Policy Summary

1. Compiled current claims remain the default retrieval surface.
2. Explicit or manual observations may create provisional claims.
3. System observations may create provisional claims only for validation/status facts with clear scope.
4. Inferred observations do not create provisional claims by default.
5. Provisional claims have short TTLs and must be reconciled by Dream.
6. Provisional claims cannot mutate, supersede, archive, or delete existing claims.
7. If a provisional claim conflicts with a compiled current claim, retrieval must show the conflict rather than silently replacing the current claim.

## 4. Source Authority Matrix

| Source authority | Provisional claim allowed? | Default TTL | Notes |
|---|---:|---:|---|
| explicit | yes | 7 days | Operator directly said remember/correct/update. |
| manual | yes | 7 days | Maintainer changed a memory doc, config, or policy file. |
| system | limited | 48 hours | Tool/test/deploy/ledger result with exact scope. |
| inferred | no | n/a | Wait for Dream compile unless a future policy grants exception. |

## 5. Allowed Provisional Claim Cases

### 5.1 Explicit Save

Use when the operator says:

- remember this
- save this
- update my memory
- that is wrong; use this instead
- forget or stop using a prior memory

Behavior:

- Append observation.
- Create provisional claim with `status: pending_compile`.
- Mark `source_authority: explicit`.
- Set TTL to 7 days.
- Add to retrieval only as provisional.
- Queue for Dream compile.

### 5.2 Manual Maintainer Update

Use when a version-controlled doc, AGENTS.md, policy file, or explicit memory artifact changes.

Behavior:

- Append observation or file-change pointer.
- Create provisional claim only if the changed artifact is already trusted as a memory authority.
- Mark `source_authority: manual`.
- Set TTL to 7 days.
- Queue for Dream compile.

### 5.3 System Validation Fact

Use when a tool result states a scoped current status.

Examples:

- "make test-python-checker ran 77 tests OK"
- "origin/main points at commit X"
- "Opus latest model resolved to claude-opus-4-8"

Behavior:

- Append observation.
- Create provisional claim only with precise scope and timestamp.
- Mark `source_authority: system`.
- Set TTL to 48 hours.
- Do not turn transient results into durable identity or project claims without Dream.

In Phase 7A pure helpers, the precise-scope requirement is represented by a
non-empty observation `scope` map. Unscoped system observations remain
evidence-only until a future Dream compile step.

## 6. Disallowed Provisional Claim Cases

Do not create provisional claims for:

- ordinary LLM-inferred semantic extraction
- ambiguous corrections
- procedural rule changes not backed by explicit file/manual edit
- unscoped project status changes
- stale temporal facts without resolved date
- claims requiring entity resolution that is not yet available
- observations that would archive, delete, or supersede another claim

## 7. Retrieval Behavior

### 7.1 Normal Query

Default order:

1. Compiled current claims.
2. Provisional claims with matching query and unexpired TTL.
3. Observations only when no current/provisional claim exists and the query is explicitly evidence-seeking.

### 7.2 Conflict Query

If an uncompiled provisional claim conflicts with a compiled current claim:

- return the compiled claim as current
- include the provisional claim as pending update
- surface a conflict marker in future retrieval metadata
- do not silently replace current answer

User-facing wording can be decided later, but the data model must preserve both.
For Phase 7A, "preserve both" means the fixture can contain coexisting claims
with distinct statuses (`current` and `pending_compile`). Do not add a dedicated
conflict field in the Phase 7A schema.

### 7.3 Evidence Query

For queries like "why do you think that?", "where did this come from?", or "what changed?", retrieval may return observations and supersession edges directly.

### 7.4 Point-In-Time Query

For queries like "what was true as of March 2026?", retrieval should ignore provisional claims unless their observation timestamp and validity window match the requested time.

## 8. Dream Reconciliation

Dream must process provisional claims before ordinary low-priority consolidation when they are:

- expired
- conflicting with current claims
- explicit authority
- linked to an active project
- referenced by a quality/outcome probe

Allowed Dream outcomes:

- promote to compiled current claim
- refine existing compiled claim
- supersede existing compiled claim
- mark scoped exception
- mark contested
- expire provisional claim
- discard provisional projection while preserving observation

Dream must not delete the underlying observation unless a hard-delete policy is explicitly invoked.

## 9. TTL And Expiry

Defaults:

- explicit/manual: 7 days
- system: 48 hours
- inferred: no provisional claim

On expiry:

- provisional claim is removed from default retrieval
- observation remains in evidence log
- Dream ledger notes expiry
- if the provisional claim was explicit and unprocessed, the quality gate should flag backlog

## 10. Forget/Delete Interaction

Forget commands can create high-authority observations, but they are not ordinary provisional claims.

A forget/delete observation should:

- suppress matching compiled/provisional claims from default retrieval immediately when safe
- queue a Dream proposal for durable deletion, suppression, or redaction
- preserve or remove audit evidence according to the future deletion policy

This spec does not resolve hard-delete versus audit-retention tradeoffs. It requires that Phase 7A not make that conflict worse.

## 11. Deterministic Grade Requirements

Any future proposal that promotes a provisional claim must include:

- source observation ID
- source authority
- TTL status
- target compiled claim ID
- conflict check result
- taxonomy decision label
- expected revisions
- rollback metadata

Hard-gate failures:

- inferred observation promoted provisionally
- provisional claim supersedes current claim outside Dream
- provisional claim lacks TTL
- system provisional claim lacks exact scope
- expired provisional claim remains in default retrieval
- procedural memory enters provisional semantic retrieval

## 12. Minimal Test Fixtures For Phase 7A

Add synthetic fixtures for schema representation and pure helper behavior. Phase
7A does not implement live retrieval changes or automatic Dream reconciliation;
it proves the model can represent these states without losing source evidence.

Add fixtures for:

1. Explicit observation creates provisional claim.
2. Inferred observation remains evidence-only.
3. System validation creates short-lived scoped provisional claim.
4. Unscoped system validation remains evidence-only.
5. Expired provisional claim drops from retrieval projection.
6. Conflicting provisional claim is shown as pending, not current, through coexisting claims with distinct statuses.
7. Dream reconciliation promotes provisional claim.
8. Dream reconciliation expires provisional claim but preserves observation.
9. Procedural observation is rejected from provisional semantic retrieval.

## 13. Builder Acceptance Criteria

Phase 7A is builder-ready only if:

- provisional behavior is implemented as pure offline projection first
- no live retrieval route changes in 7A
- no Redis or Vector write path changes in 7A
- TTL behavior has deterministic tests
- explicit/inferred/system/manual authority cases are covered
- every provisional claim can be traced to an observation
- no provisional claim can mutate another claim
