# PKS Memory Research Refresh Design Memo

- Status: Revised after Opus review
- Date: 2026-06-05
- Phase: 6.75, before Phase 7 implementation
- Repo: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`
- Purpose: Decide which current memory-system ideas should change the Phase 7+ plan before schema work starts.
- Opus review: `docs/pks-memory-research-refresh-opus-review-2026-06-05.md`

## 1. Executive Recommendation

Phase 7 still makes sense, but Opus correctly flagged that the first implementation slice was over-scoped. The revised plan is:

1. Phase 7A narrows to the minimum viable separation: append-only observations plus Dream-compiled claims.
2. Explicit vs inferred memory becomes `source_authority`, not a separate storage lane.
3. Project lifecycle becomes semantic claims about project entities, not a separate lane.
4. Procedural memory stays outside the compiler for now, in version-controlled files or explicit policy blocks.
5. Memory blocks, hybrid retrieval, entity traversal, and heat-based paging are later phases that depend on compiled claims existing first.

The repo should not copy OpenAI Memory, Mem0, Zep, Letta, or LangMem wholesale. The strongest design is a hybrid that keeps PKS's existing advantage: governed mutation through Dream proposal, deterministic grading, bounded apply, rollback, and validation.

The main change from the old plan is this:

- Hot path should append observations and explicit-save facts.
- Dream should compile, supersede, demote, archive, and reconcile.
- Retrieval should query a current compiled projection while preserving old evidence for audit and point-in-time reasoning.

That is closer to Zep/Graphiti's temporal invalidation model and Mem0's ADD-only extraction discipline, but with stronger governance than either.

Before Phase 7A code, two short specs are now required:

- contradiction/supersession taxonomy with real PKS examples
- compile-latency/provisional-claim policy for observations not yet compiled by Dream

## 2. Current PKS Position

PKS already has unusually strong operational machinery:

- Structured knowledge and project entries with provenance.
- Salience and tiering.
- Thin index retrieval.
- Dream proposal, deterministic grade, bounded apply, rollback, and validation.
- Phase 6.5 quality audit with carry-forward recall, temporal freshness fixture schema, and stale active-project detection.

The known gap is that PKS still stores too much semantic state directly in current entries. When a belief changes, the old and new facts compete inside entry-level state instead of becoming first-class historical observations with temporal validity and a separately compiled current view.

Phase 7 should fix that, but the web research suggests the fix should be broader than simply adding one evidence-log table.

## 3. Source-Informed Findings

### 3.1 OpenAI Memory

OpenAI's user-facing memory distinguishes two mechanisms:

- Saved memories: durable facts, preferences, or goals that are remembered until deleted.
- Reference chat history: mutable context inferred from prior chats and updated over time.

Useful PKS implications:

- Add a first-class distinction between explicit operator memories and inferred memories.
- Add user controls: "what do you remember?", forget/delete, and a no-ingest or temporary mode.
- Treat explicit "remember this" as a stronger write signal than ordinary inference.

Limits:

- OpenAI Memory is consumer-assistant personalization, not a complete technical-knowledge system.
- Its public docs describe controls and behavior, not a full architecture for evidence, contradiction repair, or temporal compilation.

### 3.2 OpenAI API and Agents Sessions

OpenAI API conversation state and Agents SDK sessions are mostly session or conversation-history persistence. They preserve message items across turns or runs; they are not a replacement for semantic long-term memory.

Useful PKS implications:

- Keep raw agent/session histories as episodic evidence.
- Do not confuse session continuation with distilled durable memory.
- Preserve retention and deletion semantics explicitly, because API conversation objects and ordinary response logs have different persistence rules.

Limits:

- Session memory is working context, not the answer to PKS's current-view, salience, and historical-validity problem.

### 3.3 LangGraph and LangMem

LangGraph/LangMem separate memory into semantic, episodic, and procedural categories. LangMem also makes the key point that memory systems are application-specific, and that collection-style semantic memory has to reconcile new information with previous beliefs or it loses precision.

Useful PKS implications:

- Keep semantic facts/claims distinct from episodic sessions/source events.
- Treat project lifecycle as semantic claims about project entities, not a separate compiler lane.
- Add outcome evals per lane, not one generic recall metric.
- Represent procedural memory separately from ordinary knowledge entries and keep it out of the Phase 7A compiler.

Limits:

- LangMem's generic manager pattern is not enough for PKS because PKS needs operator-grade rollback, audit, and temporal source preservation.

### 3.4 Letta Memory Blocks

Letta's memory blocks are always-visible, structured sections of context with labels, descriptions, values, and limits. They can be writable or read-only and shared across agents.

Useful PKS implications:

- Add "operator memory blocks" for the very small set of context that should be always visible:
  - stable identity
  - current flagship projects
  - hard preferences and rules
  - tool usage guardrails
- Give blocks labels, descriptions, limits, read-only flags, and source links.
- Generate blocks from compiled views, not directly from raw retrieval.

Limits:

- Always-visible memory can easily become a context tax. Blocks should be compact and strongly gated.

### 3.5 Zep / Graphiti

Zep/Graphiti's strongest idea is temporal graph memory: facts have validity intervals, old facts are invalidated rather than deleted, and current retrieval combines vector, full-text, and graph traversal.

Useful PKS implications:

- Add temporal fields to observations and compiled claims:
  - observed_at
  - learned_at
  - valid_from
  - valid_to
  - invalidated_at
  - temporal_status
- Preserve old facts as history.
- Serve current state from compiled projections.
- Support point-in-time questions later.
- Add entity and relationship extraction as a retrieval boost.

Limits:

- A full graph rewrite is too much for Phase 7.
- PKS can start with entity links and temporal claim records before adopting a graph backend.

### 3.6 Mem0

Mem0's newer open-source direction is notable: ADD-only extraction, hybrid retrieval, and entity linking. It moves update/delete decisions away from the initial add call.

Useful PKS implications:

- Make the ingestion path append observations rather than deciding final current truth.
- Use entity linking and BM25/full-text as ranking boosts alongside vectors.
- Keep exact dedup cheap with hashes, then let Dream handle semantic consolidation.

Limits:

- Retrieval ranking alone should not decide truth when old and new facts conflict.
- PKS should not remove explicit update/delete/reconcile operations; it should move them to governed Dream, not eliminate them.

### 3.7 A-MEM

A-MEM adds Zettelkasten-like linked notes: each new memory has contextual descriptions, keywords, tags, links to related memories, and memory evolution of older notes.

Useful PKS implications:

- Add bounded link suggestions when new observations arrive.
- Use links for multi-hop recall and Dream candidate discovery.
- Keep link generation explainable and capped.

Limits:

- Open-ended LLM note evolution could reintroduce uncontrolled memory drift. PKS should make link/evolution proposals reviewable and reversible.

### 3.8 MemoryOS / Hierarchical Memory

MemoryOS-style systems treat memory as hot, warm, and cold storage with promotion, eviction, and heat signals.

Useful PKS implications:

- PKS already has injection tiers and salience; map them more explicitly to hot/warm/cold memory.
- Add heat inputs beyond mention count:
  - access recency
  - source diversity
  - explicit-save flag
  - current project link
  - correction event
  - outcome-probe importance

Limits:

- Do not add another independent lifecycle engine. Dream should remain the lifecycle authority.

## 4. Proposed Memory Lanes

Phase 7A should keep the lane model small. The old five-lane proposal over-modeled the first implementation. The revised model is:

### 4.1 Semantic Claims

Facts, preferences, project state, technical claims, and entity relationships that can be compiled into current or historical beliefs.

Examples:

- durable operator facts
- explicit preferences
- inferred technical knowledge
- active, paused, completed, abandoned, or stale project state
- claims about repos, tools, people, products, and domains

Properties:

- source-backed
- salience-driven
- subject to Dream consolidation
- current view compiled from observations
- temporal status and validity fields
- `source_authority` distinguishes explicit operator memory from inferred memory

Explicit operator memory is not a separate lane. It is semantic memory with stronger authority:

```json
{
  "source_authority": "explicit"
}
```

Inferred semantic memory uses:

```json
{
  "source_authority": "inferred"
}
```

Project lifecycle is also not a separate lane. It is a semantic claim about a project entity with status and temporal fields.

### 4.2 Episodic Observations

Raw or lightly structured events:

- conversation turns
- coding-agent sessions
- tool calls and outputs
- Dream proposals and apply records
- correction events

Properties:

- append-only
- retrieval mostly on demand
- used as evidence for semantic compilation
- not normally injected as current context
- strong provenance

### 4.3 Procedural Memory

Rules about how agents should behave.

Examples:

- AGENTS.md rules
- skill instructions
- repo-specific practices
- "when I ask for review, do not edit" preferences

Properties:

- stored separately from ordinary semantic claims
- preferably as version-controlled files or explicit policy blocks
- tested through behavior-following probes
- high risk if mutated automatically
- not routed through the Phase 7A observation-to-claim compiler

Procedural memory should be acknowledged in the architecture, but Phase 7A should not auto-compile it. A wrongly compiled procedural rule can cause active agent harm, not just stale recall.

### 4.4 Presentation Blocks

Always-visible memory blocks remain useful, but they are a presentation layer that depends on compiled claims.

Examples:

- operator profile block
- current flagship project block
- read-only procedural pointer block
- repo-specific policy block

Properties:

- compact
- size-limited
- strongly gated
- source-linked
- deferred until after compiled claims exist

For the first operator-profile block, a hand-authored or manually reviewed version is safer than an automatic compiler.

### 4.5 Pre-Code Specs Required By Opus Review

Before schema code, write two focused specs.

### Contradiction/Supersession Taxonomy

Dream must distinguish:

- supersession: new claim replaces old claim
- refinement: new claim narrows or enriches old claim
- scoped exception: both claims remain true in different contexts
- contestation: claims conflict and need review or future evidence
- stale temporal claim: old claim expired by date/time validity
- duplicate: two claims describe the same belief

### Compile-Latency / Provisional-Claim Policy

The design must answer what retrieval does after an observation is written but before Dream compiles it. Candidate policy:

- explicit-save observations can create a provisional current claim with a short TTL or "pending_compile" status
- inferred observations remain evidence-only until Dream compiles them
- retrieval may fall back to recent high-authority observations when no compiled claim exists
- Dream must either promote, supersede, or expire provisional claims

## 5. Proposed Phase 7 Data Model

Phase 7 should add an offline schema contract first. Live writes should come later.

### 5.1 Evidence Observation

An observation is an append-only source-backed event.

Candidate fields:

```json
{
  "observation_id": "obs_...",
  "subject_id": "entity_or_entry_id",
  "memory_lane": "semantic|episodic|procedural",
  "source_authority": "explicit|inferred|system|manual",
  "claim_text": "text as extracted",
  "source_type": "claude_export|chatgpt_export|codex_session|github|gmail|dream|manual",
  "source_id": "conversation_or_repo_or_run_id",
  "message_ids": [],
  "source_path": null,
  "snippet": "verbatim or near-verbatim evidence excerpt",
  "observed_at": "source event timestamp",
  "learned_at": "ingestion timestamp",
  "valid_from": null,
  "valid_to": null,
  "invalidated_at": null,
  "confidence": "high|medium|low",
  "entity_mentions": [],
  "relationship_edges": [],
  "signal_flags": [],
  "extraction_method": "explicit_save|llm_extract|repo_parse|dream_compile"
}
```

### 5.2 Compiled Claim

A compiled claim is the current or historical projection the retrieval layer can use.

Candidate fields:

```json
{
  "claim_id": "claim_...",
  "subject_id": "entity_or_entry_id",
  "memory_lane": "semantic",
  "compiled_text": "current normalized claim",
  "status": "current|historical|superseded|contested|stale|deprecated",
  "temporal_status": "current|future|expired|historical|timeless|unknown",
  "support_observation_ids": [],
  "supersedes_claim_ids": [],
  "superseded_by_claim_id": null,
  "compiled_at": "timestamp",
  "compiled_by": "dream|migration_preview|manual",
  "confidence": "high|medium|low",
  "expected_source_revisions": {}
}
```

In Phase 7A, compiled claims should be limited to semantic claims. Episodic observations can support those claims, but do not need compiled-claim projections unless a later phase introduces episodic retrieval summaries. Procedural observations should be out-of-band pointers only.

### 5.3 Memory Block

A memory block is compact context intentionally visible without retrieval.

Candidate fields:

```json
{
  "block_id": "block_...",
  "label": "operator_profile",
  "description": "What this block is for and when agents should use it.",
  "value": "compact current context",
  "scope": "operator|repo|agent|project",
  "read_only": true,
  "chars_limit": 2000,
  "compiled_from_claim_ids": [],
  "updated_at": "timestamp"
}
```

## 6. Proposed Retrieval Changes

Phase 7 should not immediately rewrite retrieval, but the schema should support:

1. Vector search for semantic similarity.
2. BM25 or lightweight full-text search for exact technical terms.
3. Entity matching for people, projects, repos, tickers, products, places, and tool names.
4. Temporal scoring for "current", "as of", "old", and "what changed" queries.
5. Salience and tier boosts.
6. Memory-lane filtering.

The key design rule:

Current-answer queries should prefer compiled current claims. Audit, history, and "why do you think that?" queries should retrieve observations and supersession history.

## 7. Proposed Write Path

### Hot Path

The hot path should do the minimum reversible write:

- append observations
- tag explicit-save facts
- extract entities
- hash exact duplicates
- record source metadata

The hot path should not decide that an older belief is deleted or globally replaced.

### Dream Path

Dream should remain the authority for:

- merging duplicate subjects
- compiling current views
- marking facts superseded
- invalidating stale temporal claims
- demoting, promoting, archiving, and restoring
- updating memory blocks

Dream already has the right skeleton: proposal, grade, bounded apply, verify, rollback.

### Review and Rollback

Every compiled-view update should carry:

- source observation IDs
- expected revisions
- rollback metadata
- outcome probes where applicable
- a reason field for supersession or invalidation

## 8. Proposed Outcome Evals

Phase 6.5 added the first outcome-quality baseline. Phase 7+ should extend it:

1. Carry-forward recall:
   - Does a current query still retrieve the needed fact after merges and archives?
2. Staying current:
   - Does a stale plan get demoted or reframed after the relevant date passes?
3. Preference/procedural following:
   - Does the system apply operator rules and preferences in behavior, not merely store them?
4. Evidence traceability:
   - Can the system explain which observations support a current claim?
5. Point-in-time correctness:
   - Can the system answer "what was true as of date X?" separately from "what is true now?"
6. Forget/delete behavior:
   - Does user-commanded forgetting remove or suppress the correct memory lane without corrupting source audit records?

## 9. Revised Phase Plan

### Phase 6.75 - Research Refresh And Review

Status: this memo.

Deliverables:

- design memo
- Opus critique
- revised design memo or appendix

### Phase 7A - Offline Schema Contract

Build:

- dataclasses/types for observations, compiled claims, and supersession edges
- JSON fixtures
- migration preview from existing entries
- tests proving no live storage mutation
- contradiction/supersession taxonomy spec
- compile-latency/provisional-claim policy spec

Do not change:

- Redis key format
- Vector metadata
- Dream apply behavior
- MCP retrieval output
- procedural memory files or AGENTS.md behavior

Phase 7A scope correction from Opus review:

- observations plus compiled claims only
- semantic and episodic data modeled directly
- procedural memory acknowledged but not auto-compiled
- memory blocks deferred to Phase 7D

### Phase 7B - Temporal Normalization And Entity Linking

Build:

- temporal phrase normalization for observations and compiled claims
- entity mention extraction and stable entity IDs
- source-aware entity index fixture
- outcome probes for current vs stale temporal facts

### Phase 7C - Compiled Current View

Build:

- offline compiled-view generator
- current projection fixtures
- Dream proposal operations for compile/supersede/mark-current
- deterministic grade checks for compile operations

### Phase 7D - Memory Blocks

Build:

- block schema
- read-only operator profile block
- project status block
- policy/procedural block pointer
- tests for size limits and source traceability

### Phase 8 - Retrieval Upgrade

Build:

- hybrid retrieval experiment using vector plus full-text plus entities
- temporal query classification
- lane-aware retrieval
- recall and stale-current evals

### Phase 9 - Quality-Gated Dream

Build:

- pre/post outcome probes around Dream apply
- rollback on probe regression
- quality ledger integration

## 10. Recommended Adoption Decisions

### Adopt Now

- OpenAI-style split between explicit saved memory and inferred history-derived memory.
- LangMem-style semantic, episodic, procedural distinction.
- Zep-style temporal validity and current/historical separation.
- Mem0-style ADD-only observation ingestion.
- Letta-style compact always-visible memory blocks, but only for the highest-value context.

### Adopt Later

- Full graph backend.
- Multi-hop graph traversal.
- A-MEM-style LLM-generated memory links.
- MemoryOS-style heat-based paging beyond the existing salience model.

### Avoid

- Vendor lock-in as the architecture.
- Letting retrieval ranking alone resolve contradictions.
- Allowing the hot path to delete or rewrite old claims.
- Making every memory block writable by agents.
- Combining procedural rules, user facts, project status, and technical claims into one undifferentiated entry type.
- Implementing Phase 7 schema changes directly in the Worker before offline migration fixtures exist.

## 11. Opus Review Outcome

Opus reviewed the draft memo and returned `RECOMMENDATION: REVISE`. The full review is saved in `docs/pks-memory-research-refresh-opus-review-2026-06-05.md`.

Accepted review changes:

1. Narrow Phase 7A to observations plus compiled claims.
2. Use `source_authority` instead of a separate explicit-operator lane.
3. Treat project lifecycle as semantic claims about project entities.
4. Keep procedural memory out of the compiler for now.
5. Defer memory blocks until compiled claims exist.
6. Add contradiction/supersession and compile-latency specs before schema code.

## 12. Source List

- OpenAI Help: What is Memory? https://help.openai.com/en/articles/8983136-what-is-memory
- OpenAI Help: Reference saved memories and reference chat history. https://help.openai.com/en/articles/11146739-how-does-reference-saved-memories-work
- OpenAI API: Conversation state. https://developers.openai.com/api/docs/guides/conversation-state
- OpenAI Agents SDK: Sessions. https://openai.github.io/openai-agents-python/sessions/
- LangMem conceptual guide. https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- Letta memory blocks. https://docs.letta.com/guides/core-concepts/memory/memory-blocks
- Zep / Graphiti temporal context graphs. https://www.getzep.com/platform/graphiti/
- Mem0 open-source v3 migration and memory algorithm. https://docs.mem0.ai/migration/oss-v2-to-v3
- A-MEM: Agentic Memory for LLM Agents. https://arxiv.org/abs/2502.12110
- Memory OS of AI Agent. https://arxiv.org/abs/2506.06326
- Episodic-Semantic Memory Architecture for Long-Horizon Scientific Agents. https://arxiv.org/abs/2605.17625

## 13. Bottom Line

The right Phase 7 is not just a schema cleanup. It is the point where PKS separates:

- what happened
- what is currently believed
- when it was true
- why the system believes it
- where it should appear in context
- who or what is allowed to change it

That separation is the common thread across the best current memory systems, and PKS is well positioned to implement it safely because Dream already provides governed mutation.
