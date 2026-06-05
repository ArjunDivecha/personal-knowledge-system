# Opus Review - PKS Memory Research Refresh

- Date: 2026-06-05
- Reviewed memo: `docs/pks-memory-research-refresh-design-memo-2026-06-05.md`
- Recommendation: REVISE

## Summary

Opus agreed with the core architecture: append-only evidence observations plus Dream-compiled current claims with temporal validity. It identified the memo's main weakness as over-scoping Phase 7A and under-specifying contradiction/supersession logic.

Accepted review changes:

- Narrow Phase 7A to observations plus compiled claims only.
- Use `source_authority: explicit|inferred` instead of making explicit operator memory a separate lane.
- Treat project lifecycle as semantic claims about project entities, not a separate storage lane.
- Keep procedural memory in version-controlled files/policy blocks for now, outside the observation-to-claim compiler.
- Defer memory blocks until compiled claims exist.
- Add two pre-code specs before Phase 7A implementation:
  - contradiction/supersession taxonomy
  - compile-latency/provisional-claim policy
- Treat contradiction-resolution, not temporal fields themselves, as the biggest hidden risk.

## Full Review

### 1. Problem Framing

The core problem is well-articulated and correct: PKS currently conflates evidence with current belief inside entry-level state, so when a belief changes, old and new facts compete in the same mutable slot. The fix - separate the append-only evidence log from a compiled current-view projection, with temporal validity - is the right architectural instinct and matches where the strongest systems (Zep/Graphiti, Mem0 v3) have converged.

One reframe worth making explicit: this memo is doing two jobs at once. (a) A research synthesis deciding which external ideas to adopt, and (b) a Phase 7 implementation plan. The synthesis is strong. The implementation plan is over-scoped relative to the actual unblocking decision needed before "schema work starts." The only decision that truly gates Phase 7A is: what is the minimal evidence/claim schema, and does it preserve Dream's governance invariants? Everything else (lanes, blocks, hybrid retrieval, heat signals) can be sequenced behind that without re-litigating.

### 2. Assessment of Current/Proposed Solution

Strengths:

- The append-on-hot-path / compile-in-Dream split is the single most important decision and it is correct. It directly preserves PKS's differentiator (governed mutation) while fixing the belief-competition gap.
- "Retrieval ranking should not decide truth" is the right guardrail and is stated explicitly. This is the failure mode of naive RAG-memory systems.
- Offline-schema-first with no-live-mutation tests is disciplined and de-risks correctly.
- The six-way separation in Section 13 (what happened / what's believed / when true / why / where shown / who can change) is a clean conceptual spine.

Concerns:

1. Lane over-modeling. Five lanes is too many for a first implementation, and the boundaries are not cleanly disjoint. "Project lifecycle" is not parallel to the other four - it is a property of entities/claims, not a separate storage lane. A stale-project belief is a semantic claim about a project entity with temporal status. Explicit operator vs inferred semantic is better modeled as a `source_authority` field on observations than as separate lanes, because both produce semantic claims through the same compile path. Recommended schema-time lanes: episodic, semantic, and procedural.

2. Procedural memory is the under-analyzed danger. The memo correctly says procedural rules should be files/policy blocks and high risk if mutated automatically, but then lists them as a lane inside the same observation-to-claim machinery. These should not flow through ADD-only ingestion plus Dream compilation in Phase 7. A wrongly compiled procedural rule can cause active harm. Keep procedural memory as version-controlled files with explicit human edits for now.

3. The supersession/contradiction logic is under-specified. The schema has `supersedes_claim_ids`, `status: contested`, and `invalidated_at`, but the memo never specifies how Dream decides that observation B supersedes claim A versus merely contradicting it versus being a scoped exception. This is the hardest part of the system. The deterministic grade checks in Phase 7C need a concrete contradiction taxonomy.

4. Compile cost and staleness lag. If current view is only recompiled during Dream runs, a freshly explicit-saved fact may exist in the observation log but not yet in any compiled claim. The design needs a policy for queries that arrive between observation write and Dream compilation.

### 3. Recommended Solution

Proceed, but narrow Phase 7A to the minimum viable separation and defer the rest:

- Phase 7A: observations plus compiled claims only.
- Use three lanes: episodic, semantic, procedural.
- Use `source_authority: explicit|inferred` as a field, not a lane.
- Exclude memory blocks from 7A.
- Add a contradiction/supersession taxonomy as a written spec deliverable before code.
- Keep procedural memory as files; do not route it through compile in this phase.
- Add a compile-latency design note.
- Keep later sequencing for temporal/entity work, compiled current view, memory blocks, retrieval upgrade, and quality-gated Dream.

ADD-only hot-path ingestion and Dream governance are compatible and complementary. ADD-only is the minimum reversible write; Dream remains the only authority that resolves truth. The caveat is the compile-latency gap.

The biggest hidden risk is not temporal fields themselves. It is contradiction-resolution logic that decides `valid_to` and `superseded_by`.

### 4. Alternatives and Tradeoffs

- Bi-temporal modeling is correct and should stay: `observed_at` should remain separate from `learned_at`.
- Entity extraction can be deferred, but supersession often depends on entity resolution. Phase 7B may need to partially precede Phase 7C.
- For the first operator-profile memory block, hand-authoring is safer than building an automatic block compiler immediately.

### 5. Missing Information

- Current observation scale and projected ingest rate.
- Concrete contradiction taxonomy.
- Compile-latency policy.
- Migration-preview acceptance bar.
- Forget/delete versus append-only audit conflict.

### 6. Next Action

Before writing Phase 7A schema code, produce:

1. A contradiction/supersession taxonomy with worked examples from real PKS export data.
2. A compile-latency/provisional-claim policy.

Then narrow 7A to observations plus claims with three lanes and `source_authority` metadata. Defer blocks and procedural compile.

RECOMMENDATION: REVISE
