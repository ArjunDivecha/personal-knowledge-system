# PKS Dream Governance Rewrite PRD (v3)

Status: In implementation on branch `V2`
Date: 2026-05-07
Owner: Arjun / PKS Core
Scope: PKS-native governance rewrite of Dream maintenance, not a wholesale platform migration.

---

## 1. Decision

Keep the current PKS architecture:

- Cloudflare Worker remains the canonical API and write gateway.
- Upstash Redis remains the source of truth for entries and run ledgers.
- Upstash Vector remains a derived semantic search index.
- The production MCP split remains unchanged:
  - `/openai/mcp` is read-only.
  - `/mcp` is read/write.
- Existing read tools remain stable:
  - `get_index`
  - `search`
  - `get_context`
  - `get_deep`
  - `get_dream_summary`

Do not migrate PKS wholesale into Anthropic Managed Agents. Borrow the strongest architectural ideas from Managed Agents Dreams and Outcomes, but keep PKS as the durable system of record.

The rewrite target is narrower and safer than the previous "complete rewrite" framing:

> Rewrite Dream into a governed, auditable maintenance pipeline that proposes, grades, applies, verifies, and can roll back bounded memory mutations through the existing PKS write gateway.

---

## 2. Why This Rewrite Exists

The current system is live and useful, but operational assurance is weaker than runtime availability.

Known baseline facts at the start of the rewrite:

- `/health` can be green while correctness gates fail.
- `check_overnight_dream_run.py` failed because `UTC` was initialized after `main()` executed.
- `verify_memory_consistency.py --full --strict` failed on legacy evolution records missing `delta`.
- Scheduled Dream is live but bounded in code:
  - `archiveLimit = 10`
  - `promotionLimit = 10`
  - cron: `10 7 * * *` UTC
- Several older docs and probe scripts still describe or target stale workers.dev behavior.

Current implementation notes:

- The validation hotfixes are implemented and the local gates pass.
- Scheduled Dream now generates proposal-first governance artifacts instead of direct live mutation.
- Deterministic grading, bounded apply, and conflict-aware rollback are implemented.
- The staging harness now targets the full R5 lifecycle and writes a `staging_e2e` validation gate.

The product gap is not "no memory service." The gap is that PKS needs a trustworthy memory-governance layer:

- A green health endpoint must not imply memory correctness.
- Dream should not directly mutate live state without a reviewable proposal.
- Rollback must be possible and conflict-aware.
- Operators need run records that explain what happened, why, and with what blast radius.

---

## 3. Product Goals

### Primary Goals

- Keep memory high-signal over long horizons without deleting recoverable history.
- Preserve the current Cloudflare + Upstash + MCP foundation.
- Make Dream deterministic, auditable, reversible, and operator-reviewable.
- Separate runtime health from memory correctness with explicit validation gates.
- Route all Dream applies and rollbacks through the same safe mutation semantics used by MCP write tools.
- Preserve compatibility for Claude, Codex, ChatGPT, iOS, and future MCP clients.

### Non-Goals

- No migration of canonical PKS memory into Anthropic Managed Agents memory stores.
- No migration away from Cloudflare Worker, Upstash Redis, or Upstash Vector in this phase.
- No breaking change to current read tools.
- No uncapped scheduled auto-apply in the v1 cutover.
- No local scheduled jobs; scheduled maintenance remains remote.
- No semantic LLM merge engine as a required launch dependency.
- No new human dashboard in this PRD; APIs and ledgers come first.

---

## 4. Anthropic Managed Agents Fit

PKS is a strong candidate for the Managed Agents architecture pattern, especially Dreams and Outcomes, but not for full runtime replacement.

Patterns to borrow:

- Dream as an asynchronous job with explicit lifecycle.
- Immutable input snapshot during evaluation.
- Proposed output or mutation plan before apply.
- Optional operator review before mutation.
- Independent grader with a rubric and its own context.
- Event stream and retained run transcript for forensics.
- Parallel workers later for heavy replay or contradiction analysis.

Boundary to preserve:

- Anthropic or Anthropic-style workers may help generate or grade proposals.
- They must not become the canonical memory database.
- They must not directly mutate Redis or Vector.
- They must submit bounded proposals to PKS, and PKS applies them through its own write gateway.

Reference sources:

- https://claude.com/blog/new-in-claude-managed-agents
- https://platform.claude.com/docs/en/managed-agents/dreams
- https://platform.claude.com/cookbook/managed-agents-cma-verify-with-outcome-grader

---

## 5. Target Architecture

## 5.1 Planes

1. Ingestion Plane
   - Existing Python ingestion/distillation continues to write normalized entries and embeddings.
   - No schema-breaking ingestion migration is required for this PRD.

2. Retrieval Plane
   - MCP read surface remains stable.
   - Side-key access counters remain.
   - Retrieval-triggered reconsolidation remains but must not hide validation failures.

3. Mutation Plane
   - Cloudflare Worker remains the only write gateway.
   - All writes use actor attribution, `mutation_id`, and expected revision checks.
   - Dream applies and rollbacks use the same mutation engine as MCP write tools wherever possible.

4. Dream Governance Plane
   - Dream becomes `snapshot -> propose -> grade -> apply -> verify -> publish`.
   - Scheduled runs remain bounded by default.
   - Manual operator runs require explicit bounded candidate scope.

5. Observability Plane
   - Runtime health and memory correctness are separate.
   - Run artifacts, validation results, gate status, and rollback drills are queryable.

---

## 6. Dream Lifecycle Contract

## 6.1 Snapshot

Capture an immutable input view for a bounded candidate set.

Snapshot includes:

- run id
- candidate entry ids
- candidate entry types
- candidate revisions
- current entry state
- metadata needed for policy checks
- side-key access signals used by Dream
- thin-index summary version or generated timestamp
- vector metadata sample/check references

Snapshot does not mutate live entries.

## 6.2 Propose

Generate a mutation proposal against the snapshot.

Allowed v1 operation types:

- `archive_entry`
- `promote_context_type`
- `mark_contested`
- `merge_duplicates`
- `add_consolidation_note`
- `rebuild_thin_index`

Deferred operation types:

- semantic auto-resolution of contradictions
- broad uncapped duplicate merging
- free-form LLM rewriting of canonical views without operator approval

## 6.3 Grade

Grade the proposal before any live mutation.

The launch grader is deterministic. An LLM grader may be added behind a feature flag later, but cannot replace deterministic hard gates.

Hard-fail conditions:

- proposal has no snapshot id
- proposal references entries outside the snapshot
- proposal is missing expected revisions for any touched entry
- proposal would exceed configured apply caps
- proposal includes unsupported operation types
- proposal lacks rollback metadata for any mutating operation
- proposal attempts to write through `/openai/mcp`
- proposal would archive an entry already updated since the snapshot
- proposal would make archived entries visible in default retrieval
- proposal cannot explain the evidence or policy threshold for each mutation

Rubric fields:

- evidence sufficiency
- revision safety
- idempotency safety
- reversibility
- expected blast radius
- retrieval/index impact
- policy-threshold compliance
- operator-review requirement

## 6.4 Apply

Apply is a bounded mutation batch.

Apply requirements:

- `mcp:write` scope or privileged operator auth
- run-level actor attribution
- idempotency key
- expected revisions for all touched entries
- per-operation mutation ids or a deterministic derived id
- before/after metadata sufficient for rollback
- no direct Redis mutation path that bypasses the Worker mutation semantics

Scheduled apply launch policy:

- keep current scheduled caps: 10 archive / 10 promotion
- do not enable uncapped scheduled applies
- if proposal volume exceeds caps, split into continuation runs

Manual apply policy:

- manual live applies require explicit candidate ids
- live manual applies remain capped
- broad manual analysis can run as dry-run/proposal only

## 6.5 Verify

Post-apply verification must run before a run can be marked fully successful.

Required checks:

- touched entries have expected final revisions
- archived entries are excluded from default `search`
- `get_index` excludes archived entries
- thin index totals match active Redis counts
- vector metadata parity passes for touched entries
- side-key access fold-back remains monotonic
- validation ledger records pass/fail independently of `/health`

## 6.6 Publish

Publish updates the operator-facing run state.

Rules:

- `dream:last_run` becomes a pointer plus compact summary.
- Full artifacts are stored separately.
- Failed or partial runs remain inspectable.
- Runs are never silently discarded.
- A failed verify marks the run as `failed_verify`, not `completed`.

---

## 7. Data Model

## 7.1 Entry Metadata Alignment

The TypeScript Worker and Python models must agree on core mutable metadata.

Required metadata fields:

- `revision` (int, monotonic; default legacy value is 0)
- `updated_at` (ISO timestamp)
- `updated_by` (actor metadata when mutation-originated)
- `last_mutation_id`
- `last_consolidated`
- `consolidation_notes[]`
- `archived`

Governance fields:

- `state_confidence` (optional float)
- `contested_reason_codes[]`
- `last_dream_run_id`
- `last_validation_status`

Compatibility requirement:

- Legacy records must deserialize without crashing.
- Missing `evolution[].delta` must be tolerated in validation paths.
- Unknown metadata fields must be preserved where possible.

## 7.2 Proposal Schema

`dream:run:{run_id}:proposal` stores or points to:

```json
{
  "schema_version": 1,
  "run_id": "dr_...",
  "snapshot_id": "snap_...",
  "created_at": "2026-05-07T00:00:00Z",
  "trigger": "scheduled",
  "candidate_ids": ["ke_..."],
  "candidate_revisions": {
    "ke_...": 3
  },
  "operation_count": 1,
  "operations": [
    {
      "operation_id": "op_...",
      "type": "archive_entry",
      "entry_id": "ke_...",
      "entry_type": "knowledge",
      "expected_revision": 3,
      "reason": "salience below archive threshold with no retrieval reinforcement",
      "evidence": {
        "policy": "dream_thresholds.archive_candidate_salience",
        "metrics": {
          "salience_score": 0.12,
          "mention_count": 1,
          "access_count": 0
        }
      },
      "rollback": {
        "type": "restore_entry",
        "requires_revision": 4
      }
    }
  ],
  "requires_operator_review": false,
  "risk_score": 0.2
}
```

## 7.3 Run Artifacts

Canonical keys:

- `dream:run:{run_id}:summary`
- `dream:run:{run_id}:snapshot`
- `dream:run:{run_id}:proposal`
- `dream:run:{run_id}:grade`
- `dream:run:{run_id}:apply`
- `dream:run:{run_id}:verify`
- `dream:run:{run_id}:events`

Pointer keys:

- `dream:last_run`
- `dream:last_attempt`
- `dream:runs:index`

Storage rule:

- Redis may store compact summaries directly.
- Large artifacts must be chunked or moved to object storage/R2 with Redis pointers.
- The implementation must respect Upstash request-size limits.
- Do not store full unbounded snapshots as one Redis value.

## 7.4 Validation Ledger

Add:

- `validation:last`
- `validation:history:{date}`
- `validation:gate_status`

Gate status includes:

- Worker typecheck status
- Worker test status
- Python checker status
- overnight Dream check status
- full strict memory consistency status
- latest staging E2E status
- latest rollback drill status

---

## 8. API / Tool Surface

## 8.1 Keep Stable

- `get_index`
- `search`
- `get_context`
- `get_deep`
- `get_dream_summary`

## 8.2 Add Read Tools

Available on read-capable surfaces, including `/openai/mcp` if the payloads are safe and read-only:

- `get_validation_status`
- `get_dream_run(run_id)`
- `list_dream_runs(limit, status_filter)`
- `get_dream_events(run_id)`

Compatibility test:

- `/openai/mcp` must expose read tools only.
- `/openai/mcp` must not expose mutation, proposal-apply, rollback, or rebuild tools.

## 8.3 Add Write Tools

Available only on `/mcp` with `mcp:write`:

- `run_dream_proposal(dry_run=true, candidate_ids?, max_candidates?)`
- `grade_dream_proposal(run_id, rubric_version?)`
- `apply_dream_proposal(run_id, require_grade_pass=true)`
- `rollback_dream_run(run_id)`
- `rebuild_thin_index(force_consistency_check=true)`

Write-tool requirements:

- all mutating calls require actor attribution
- all mutating calls require idempotency
- all entry mutations require expected revisions
- rollback is conflict-aware and may refuse to mutate

---

## 9. Rollback Semantics

Rollback is not a blind restore.

Rollback creates and applies an inverse mutation plan only if current live state is compatible with the original apply record.

Rollback requirements:

- original run has an apply artifact
- original apply artifact includes before/after metadata
- each touched entry still has the expected post-apply revision
- no later mutation has changed the touched entry
- inverse operations pass the same deterministic grade checks

If any entry has changed since the original apply:

- rollback must not mutate that entry
- rollback produces a conflict report
- conflict report identifies current revision, expected revision, and last mutation id
- operator can decide whether to create a new repair proposal

Rollback drill success means:

- apply a bounded proposal in staging
- verify post-apply state
- run rollback
- verify restored state
- verify search/index/vector parity
- record the drill in `validation:gate_status`

---

## 10. Scheduling and Capacity

Current production schedule remains:

- `10 7 * * *` UTC
- 00:10 PDT during daylight time

Launch capacity policy:

- scheduled applies keep current 10 archive / 10 promotion caps
- scheduled proposal generation may analyze more than it applies, but apply is capped
- high-volume proposals are split into continuation runs
- uncapped scheduled apply is explicitly out of scope for v1

Manual capacity policy:

- dry-run/manual proposal can inspect a larger bounded set
- live manual apply requires explicit candidate ids
- live manual apply has caps and rate limits

Open capacity decision after v1:

- whether to raise scheduled caps after seven consecutive green overnight checks and one validated rollback drill

---

## 11. Security and Access

Must preserve:

- `/openai/mcp`: read-only
- `/mcp`: read/write
- OAuth resource metadata per path
- `mcp:write` required for mutation, Dream apply, rollback, and rebuild operations

Additional requirements:

- run-level actor attribution on every apply and rollback
- operator-auth path remains separate from ordinary read clients
- no write-capable Dream operation is exposed through the OpenAI-compatible surface
- generated proposals are read-only artifacts until applied through the write gateway

---

## 12. Testing Strategy

## 12.1 Required Green Gates

Before governance expansion:

1. Worker typecheck passes.
2. Worker runtime tests pass.
3. Python checker tests pass.
4. `check_overnight_dream_run.py` passes.
5. `verify_memory_consistency.py --full --strict` passes.

Before production cutover:

1. Proposal generation passes in staging.
2. Deterministic grading passes and fails known fixtures correctly.
3. Apply is idempotent.
4. Rollback drill passes end-to-end.
5. Post-apply verification detects intentional corruption in fixtures.
6. `/openai/mcp` remains read-only.
7. `/mcp` write tools remain write-scope protected.

## 12.2 New Test Suites

- `test_dream_proposal_integrity`
- `test_dream_grade_policy`
- `test_dream_apply_idempotency`
- `test_dream_rollback_reversibility`
- `test_validation_gate_truthfulness`
- `test_openai_mcp_read_only_compatibility`
- `test_dream_artifact_storage_budget`

## 12.3 Invariant Tests

- thin index totals match active Redis counts
- vector metadata parity for touched entries
- sampled full-mode vector parity remains available
- no archived entry returned in default `search`
- no archived entry returned in `get_index`
- retrieval updates side keys
- fold-back from side keys into metadata remains monotonic
- `dream:last_run` never claims success when verify failed

---

## 13. Migration Plan

## Phase R0: Reliability Hotfixes

Goal: make current assurance gates trustworthy before adding governance features.

Tasks:

- Fix `check_overnight_dream_run.py` UTC initialization.
- Make legacy evolution deserialization tolerant when `delta` is missing.
- Add regression tests for both fixes.
- Move or clearly deprecate root-level workers.dev probe scripts.
- Align docs that currently contradict scheduled Dream caps.

Exit criteria:

- Worker typecheck passes.
- Worker tests pass.
- Python checker tests pass.
- `make check-overnight-dream` passes.
- `make verify-memory-full` passes or fails only on real consistency issues, not parser crashes.

## Phase R1: Validation and Run Ledger

Goal: separate runtime health from memory correctness.

Tasks:

- Add validation ledger schema.
- Add compact run summary schema.
- Add `get_validation_status`.
- Add `get_dream_run`.
- Add `list_dream_runs`.
- Keep existing `get_dream_summary` backward compatible.

Exit criteria:

- `/health` still works.
- validation status is independently queryable.
- failed validation gates are visible and do not look like healthy success.

## Phase R2: Proposal Engine Extraction

Goal: make Dream capable of producing no-write proposals.

Tasks:

- Extract current Dream selection logic into a pure proposal generator.
- Include snapshot ids and expected revisions.
- Include policy evidence for every operation.
- Store proposal artifacts within storage-budget rules.
- Run proposal mode in shadow beside current scheduled Dream.

Exit criteria:

- proposal generation can run without live mutation.
- proposal output is deterministic enough for fixture testing.
- scheduled apply behavior remains unchanged.

## Phase R3: Deterministic Grader

Goal: block unsafe proposals before mutation.

Tasks:

- Implement deterministic grade rubric.
- Add hard-fail checks.
- Store grade artifact.
- Add fixtures for pass/fail policy cases.
- Add optional LLM-grader feature flag, disabled by default.

Exit criteria:

- unsafe proposals fail closed.
- safe bounded proposals pass.
- grade result explains each failure.

## Phase R4: Apply and Conflict-Aware Rollback

Goal: apply only graded bounded proposals and prove reversibility.

Tasks:

- Implement `apply_dream_proposal`.
- Reuse existing mutation semantics where possible.
- Add rollback artifact generation.
- Implement `rollback_dream_run`.
- Refuse rollback on revision conflicts.

Exit criteria:

- apply is idempotent.
- rollback drill passes in staging.
- conflicting rollback produces a clear conflict report without mutation.

## Phase R5: Staging E2E

Goal: test the complete lifecycle outside production.

Required scenario:

1. Seed staging fixture.
2. Generate proposal.
3. Grade proposal.
4. Apply proposal.
5. Verify post-apply state.
6. Roll back.
7. Re-verify restored state.
8. Confirm `/openai/mcp` remains read-only.

Exit criteria:

- one full staging lifecycle passes.
- validation ledger records the run.
- artifacts are inspectable by tool.

## Phase R6: Production Shadow and Cutover

Goal: switch scheduled Dream only after parity and safety are proven.

Tasks:

- Run new proposal/grade path in shadow mode for N days.
- Compare old scheduled Dream actions with new proposals.
- Keep current scheduled apply path until shadow results are acceptable.
- Cut over scheduled apply to proposal/grade/apply only after approval.

Cutover criteria:

- seven consecutive green overnight checks
- full strict consistency verification passes on schedule
- one successful production-safe rollback drill or staging drill accepted as equivalent
- no contradictory cap/scope docs remain
- `/openai/mcp` and `/mcp` compatibility tests pass

---

## 14. Success Criteria

The rewrite is complete only when all are true:

1. Production `/health` is green.
2. `get_validation_status` reports green correctness gates.
3. Overnight Dream checks pass automatically for seven consecutive days.
4. Full strict consistency verification passes on schedule.
5. Dream runs through proposal, grade, apply, verify, and publish.
6. At least one rollback drill is executed and validated end-to-end.
7. `dream:last_run` is a pointer/summary, not the only source of truth.
8. Large artifacts respect storage-budget rules.
9. `/openai/mcp` is still read-only.
10. Documentation matches runtime behavior with no contradictory cap/scope statements.

---

## 15. Risks and Mitigations

Risk: the word "rewrite" encourages rebuilding working systems.

Mitigation: scope this as Dream governance only. Ingestion, retrieval, and canonical storage stay PKS-native.

Risk: Dream mutation blast radius.

Mitigation: proposal, deterministic grade, bounded apply, expected revisions, rollback artifacts.

Risk: false confidence from health-only checks.

Mitigation: validation ledger and `get_validation_status` are separate from `/health`.

Risk: rollback overwrites later user changes.

Mitigation: rollback is revision-gated and conflict-aware.

Risk: run artifacts exceed Upstash request-size limits.

Mitigation: compact summaries in Redis; chunk or object-store large artifacts; test artifact storage budget.

Risk: connector/client drift.

Mitigation: compatibility tests for `/openai/mcp` and `/mcp` are required before cutover.

Risk: LLM grader makes nondeterministic safety decisions.

Mitigation: deterministic grader is required at launch; LLM grader is optional and advisory until proven.

---

## 16. Open Decisions

1. Artifact storage backend for large snapshots: Redis chunking, R2, or both?
2. Retention duration by artifact type:
   - summary
   - snapshot
   - proposal
   - grade
   - apply
   - verify
   - events
3. Whether staging rollback drill is sufficient for first production cutover, or whether production needs a tiny live rollback drill.
4. Whether LLM grading should be added in R3 as advisory-only or deferred completely.
5. Whether scheduled caps should remain 10/10 after launch or increase only after a green-run threshold.

---

## 17. Immediate Next Execution Step

Execute Phase R0 first.

Do not begin governance-layer expansion until the two broken validation gates are fixed and passing locally:

- `make check-overnight-dream`
- `make verify-memory-full`

After R0, implement R1 read-only observability before any new apply/rollback mutation behavior.
