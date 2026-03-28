# Prompt for Nano Banana Pro

**Instructions:** Copy and paste the text below into Nano Banana Pro, Whimsical AI, Excalidraw AI, or another diagram-generation tool to create a polished architectural flowchart for the system as it exists now.

The key requirement is accuracy. This diagram should reflect the **actual current implementation**, not an older planned-only version. Where a capability is still partial, mark it visually as **"bounded live"** or **"planned next"** rather than showing it as fully autonomous.

---

**Prompt:**

Create a highly detailed, professional architecture diagram titled:

**"Personal Knowledge System: Selective AI Memory Architecture"**

Use a modern technical style. Prefer a clean systems-diagram look over marketing graphics. Good options are:
- dark background with restrained neon accents
- or light enterprise architecture styling with clear swimlanes and subtle color coding

The visual should explain how the system turns raw personal data into a selective autobiographical memory layer for AI assistants.

The diagram must show **six major areas**:

1. **Raw Sources**
2. **Local Ingestion and Distillation**
3. **Memory Storage**
4. **Cloudflare Retrieval Layer**
5. **AI Client Interaction**
6. **Maintenance, Controls, and Validation**

Also include a small legend for:
- **Live in production**
- **Bounded live / controlled**
- **Planned next**

## 1. Raw Sources Layer

Show multiple source streams entering from the left or top:

- **Claude exports**
- **ChatGPT exports**
- **Claude Code sessions**
- **Codex CLI sessions**
- **GitHub repositories**
  - commits
  - READMEs
  - comments
- **Gmail exports**
  - substantive sent mail

Label this layer:

**"Raw autobiographical and work data"**

## 2. Local Ingestion and Distillation Layer

This should be shown as a **local Python processing layer**, not as a cloud-hosted service.

Include these components:

- **Python ingestion pipelines**
  - source-specific extractors
  - parser/processor scripts
  - filtering and deduplication
- **Distillation layer**
  - writes structured entries with provenance
  - outputs two schemas:
    - `KnowledgeEntry`
    - `ProjectEntry`
- **Anthropic classification / backfill step**
  - used for `context_type` classification during migration and upgrade workflows
- **OpenAI embeddings**
  - `text-embedding-3-large`
  - `3072 dimensions`
- **Thin index generation**
  - compressed topology of memory
  - tier and salience aware
- **Backfill and normalization scripts**
  - schema migration
  - vector metadata normalization
  - consistency verification

Add a note that this layer is:

**"Python pipelines running locally, with scheduled ingestion and one-off migration tooling"**

## 3. Memory Storage Layer

Show two primary storage systems side by side:

### A. Upstash Redis

Label it:

**"Canonical memory store"**

Show that it stores:
- `knowledge:{id}`
- `project:{id}`
- `index:current`
- `dream:last_run`
- `dream:run:{id}`
- `entry_access:{id}`
- `entry_last_accessed:{id}`
- `archived:{type}:{id}:{run_id}`
- `archived:{type}:{id}:latest`
- migration flags
- rate-limit buckets
- reconsolidation error logs

### B. Upstash Vector

Label it:

**"Semantic retrieval index"**

Show that it stores:
- one embedding per active entry
- metadata fields used during retrieval:
  - `type`
  - `context_type`
  - `injection_tier`
  - `salience_score`
  - `mention_count`
  - `archived`
  - `last_consolidated`

Add a small annotation between Redis and Vector:

**"Redis is canonical. Vector metadata is normalized to stay in sync."**

## 4. Retrieval Layer: Cloudflare MCP Server

Show a prominent **Cloudflare Worker** box as the live serving layer.

Label it:

**"Cloudflare Workers MCP Server"**

Inside or attached to this box, include:

- **OAuth-enabled MCP transport**
- **Custom domain**
  - `mcp.dancing-ganesh.com`
- **Bounded scheduled Dream trigger**
  - cron: `0 3 * * *`
- **Tier-aware retrieval logic**
  - semantic similarity
  - recency
  - salience
  - source weight
  - tier precedence
- **Archive-aware filtering**
  - archived entries excluded from normal retrieval
- **Reconsolidation on retrieval**
  - background writes after read
- **Write-capable MCP controls**
  - scope-gated
  - rate-limited

Also show the **MCP tool surface** branching from the Worker:

- `get_index`
- `get_dream_summary`
- `get_context`
- `get_deep`
- `search`
- `github`
- `restore_archived` *(requires `mcp:write`)*
- `set_context_type` *(requires `mcp:write`)*

Show that the Worker reads from:
- Upstash Redis
- Upstash Vector

And also calls:
- OpenAI embeddings for live semantic search queries

## 5. AI Client Interaction Layer

Show the user-facing AI clients on the right:

- **Claude app / Claude Desktop / Claude iOS**
- **Other MCP-capable AI clients**

Include:
- the client calls MCP tools
- the Worker returns:
  - thin index summaries
  - ranked memory results
  - full entries
  - Dream summaries

Label this layer:

**"AI assistant uses memory as context, not as a blind archive"**

Also include a small callout showing the memory behavior:

- **Tier 1:** durable identity and active projects
- **Tier 2:** recurring patterns and topic-adjacent context
- **Tier 3:** direct-query-only, low-salience memories

## 6. Maintenance, Controls, and Validation

This should be a separate side loop or bottom lane.

### A. Reconsolidation Loop

Show a loop from retrieval back into storage.

Label it:

**"Reconsolidation on retrieval"**

Include these steps:
- retrieval increments `entry_access:{id}`
- updates `entry_last_accessed:{id}`
- folds side-key values back into canonical Redis entry
- may promote weak entries based on repeated use
- patches vector metadata to avoid Redis/Vector drift

### B. Dream Loop

Show a scheduled maintenance cycle touching storage.

Label it:

**"Dream: bounded live nightly maintenance"**

Break it into four phases:

1. **Survey**
   - load active entries
   - compute salience
   - bucket into stable / active / weak / decay candidates
2. **Replay**
   - current live behavior is limited
   - promotion candidates identified
   - duplicate merge / contradiction handling still planned
3. **Consolidate**
   - promote context when warranted
   - append structured consolidation notes
   - rebuild thin index using lock + staging swap
4. **Prune**
   - archive weak low-evidence entries
   - write reversible snapshot + `:latest` pointer

Add these explicit notes:

- **Current mode:** bounded live
- **Nightly caps:** `archiveLimit=5`, `promotionLimit=10`
- **Replay-heavy logic:** planned next
- **Strategic forgetting:** optimal memory is not maximal memory

### C. Control Plane

Show a control box connected to the Worker and Dream loop:

- OAuth scopes
- `mcp:read`
- `mcp:write`
- write rate limits
- operator token for `/ops/dream/*`
- restore and context override controls

### D. Validation and Staging

Show a smaller parallel testing track:

- **Staging Worker**
- **Staging Redis**
- **Staging Vector**
- fixture seeding
- staging smoke flow
- bounded live archive test
- MCP restore test
- MCP context override test
- strict Redis / Vector / thin-index consistency verification
- local Worker runtime tests in `workerd`

Label this area:

**"Production is not the default test bed"**

## Required Visual Relationships

Make sure these flows are visually obvious:

1. Raw sources -> Python ingestion/distillation -> Redis + Vector
2. Redis + Vector -> Cloudflare Worker MCP server -> AI client
3. Retrieval -> reconsolidation -> Redis + Vector
4. Scheduled Dream -> archive / promote / rebuild index -> Redis + Vector
5. Staging and test infrastructure as a parallel safety lane, not part of the main production data path

## Important Accuracy Constraints

Do **not** depict:
- a monolithic database
- a generic RAG system with no tiering
- Dream as fully autonomous replay intelligence
- write tools as unauthenticated
- nightly Dream as unbounded
- local Python ingestion as if it were running inside Cloudflare

Do depict:
- Redis as canonical
- Vector as retrieval index
- tier-aware memory retrieval
- reconsolidation as a live write-after-read mechanism
- Dream as a bounded live maintenance loop with reversible archive semantics
- write-capable MCP tools as authenticated and rate-limited
- staging validation as part of the engineering system

## Optional Finishing Touches

If space allows, add a small callout panel titled:

**"Why this is not vanilla RAG"**

With these bullets:
- memories have `context_type`
- memories have `mention_count`
- memories have `injection_tier`
- memories have `salience_score`
- retrieval changes memory
- weak memories can be archived instead of injected forever

The overall impression should be:

**"A selective autobiographical memory system with explicit forgetting, bounded maintenance, and production-grade retrieval controls."**
