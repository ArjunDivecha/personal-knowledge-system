# Product Requirements Document: Knowledge Distillation Pipeline

**Version:** 1.1  
**Author:** Claude + Arjun  
**Date:** December 2024  
**Status:** Draft

---

## 1. Overview

### 1.1 Purpose

The Knowledge Distillation Pipeline converts raw conversation exports from Claude and GPT into structured, compressed knowledge entries suitable for storage and retrieval. It is the core intelligence layer that transforms ephemeral chat logs into a persistent personal knowledge system.

This system solves the "digital amnesia" problem: valuable insights generated in AI conversations are currently buried and effectively lost despite being digitally preserved.

### 1.2 Scope

This PRD covers only the distillation component—from raw export ingestion through structured output. It does not cover:

- The MCP server (retrieval layer)
- The Skill definition (routing layer)  
- The storage schema details (Upstash)
- The UI/access layer

### 1.3 Design Principles

1. **Provenance everywhere**: Every insight, decision, and conclusion must trace back to source messages
2. **Conservative merging**: Never silently overwrite; preserve contested positions
3. **Compression as view**: Compression generates a retrievable summary; original content is archived, not deleted
4. **Debuggability**: Every pipeline decision is logged and auditable

### 1.4 Success Criteria

- Processes 100 conversations in under 10 minutes
- Produces entries that are >80% useful when retrieved (measured by user feedback)
- Correctly links discussions to repositories >90% of the time when explicit mentions exist
- Compresses content by 10-20x vs. raw conversation while preserving actionable signal
- 100% of extracted insights have valid provenance (traceable to source)

---

## 2. Input Specification

### 2.1 Claude Exports

**Source:** Manual export from claude.ai Settings → Export Data, or periodic bulk download

**Format:** JSON archive containing `conversations.json`

**Structure:**
```json
{
  "conversations": [
    {
      "uuid": "string",
      "name": "string (auto-generated title)",
      "created_at": "ISO8601",
      "updated_at": "ISO8601",
      "chat_messages": [
        {
          "uuid": "string",
          "sender": "human" | "assistant",
          "text": "string",
          "created_at": "ISO8601",
          "attachments": [...],
          "parent_message_uuid": "string | null"
        }
      ]
    }
  ]
}
```

**Considerations:**

- Messages form a tree structure (regenerations create branches)
- Must resolve `parent_message_uuid` to flatten into linear thread
- Preserve message UUIDs for provenance tracking
- Attachments may reference files but content is not always included

### 2.2 GPT Exports

**Source:** ChatGPT Data Export (Settings → Data Controls → Export)

**Format:** ZIP containing `conversations.json`

**Structure:**
```json
[
  {
    "title": "string",
    "create_time": "unix_timestamp",
    "update_time": "unix_timestamp",
    "mapping": {
      "message_id": {
        "id": "string",
        "message": {
          "author": { "role": "user" | "assistant" | "system" },
          "content": { "parts": ["string"] },
          "create_time": "unix_timestamp"
        },
        "parent": "message_id | null",
        "children": ["message_id"]
      }
    }
  }
]
```

**Considerations:**

- Uses `mapping` object with parent/children references (DAG structure)
- Must traverse from root to construct linear thread
- Preserve message IDs for provenance tracking
- `parts` array may contain multiple content blocks

### 2.3 GitHub Repositories

**Source:** GitHub API (authenticated)

**Data Extracted:**
```yaml
per_repository:
  name: string
  description: string
  readme_content: string (truncated to 2000 chars)
  primary_language: string
  topics: [string]
  last_commit_date: ISO8601
  last_commit_message: string
  directory_structure: [string] (top 2 levels, key folders only)
  dependencies: object (from package.json, requirements.txt, etc.)
```

**Considerations:**

- Rate limits: 5000 requests/hour authenticated
- Secondary limit: cap concurrent requests at 10 (GitHub guideline)
- Skip archived repos and forks (unless explicitly included)
- README is primary signal for semantic linking

---

## 3. Output Specification

### 3.1 Normalized Conversation (Intermediate)

Output of Stage 1, input to subsequent stages:

```yaml
normalized_conversation:
  id: "string"  # Original conversation UUID
  source: "claude" | "gpt"
  title: "string"
  created_at: "ISO8601"
  updated_at: "ISO8601"
  
  messages:
    - message_id: "string"  # Preserved for provenance
      role: "user" | "assistant"
      created_at: "ISO8601"
      content: "string"
      content_type: "text" | "code" | "mixed"
      code_blocks:  # Extracted separately for linking
        - language: "string"
          content: "string"
  
  parse_metadata:
    total_nodes: int
    branches_found: int
    selected_path: [message_id]  # The linearized thread
    alternate_branches_kept: int
    parser_version: "string"
```

### 3.2 Knowledge Entry

```yaml
knowledge_entry:
  # Identity
  id: "ke_uuid"  # Stable, never changes
  type: "knowledge"
  
  # Classification
  domain: "string"  # Display label, e.g., "MLX fine-tuning"
  subdomain: "string | null"  # Optional specificity
  
  # State
  state: "active" | "contested" | "stale" | "deprecated"
  detail_level: "full" | "compressed"
  
  # Current position (for fast retrieval)
  current_view: "string"  # 1-3 sentences: what you currently think/know
  confidence: "high" | "medium" | "low"
  
  # Positions (for contested states or history)
  positions:
    - view: "string"
      confidence: "high" | "medium" | "low"
      as_of: "ISO8601"
      evidence:
        conversation_id: "string"
        message_ids: ["string"]
        snippet: "string"  # Max 200 chars, the key quote
  
  # Structured knowledge with provenance
  key_insights:
    - insight: "string"
      evidence:
        conversation_id: "string"
        message_ids: ["string"]
        snippet: "string"
  
  knows_how_to:
    - capability: "string"
      evidence:
        conversation_id: "string"
        message_ids: ["string"]
        snippet: "string | null"  # Optional for capabilities
  
  open_questions:
    - question: "string"
      context: "string | null"
      evidence:
        conversation_id: "string"
        message_ids: ["string"]
  
  # Linkages
  related_repos:
    - repo: "string"  # owner/repo format
      path: "string | null"  # Specific folder if known
      link_type: "explicit" | "semantic"
      confidence: 0.0-1.0  # Post-verification confidence
      evidence: "string | null"  # The mention or match reason
  
  related_knowledge:
    - knowledge_id: "ke_uuid"
      relationship: "related" | "depends_on" | "contradicts" | "supersedes"
  
  # Evolution tracking
  evolution:
    - delta: "string"  # What changed
      trigger: "string"  # Why it changed (conversation topic)
      from_view: "string"  # Previous position
      to_view: "string"  # New position
      date: "ISO8601"
      evidence:
        conversation_id: "string"
        message_ids: ["string"]
  
  # Metadata
  metadata:
    created_at: "ISO8601"
    updated_at: "ISO8601"
    source_conversations: ["conversation_id"]
    source_messages: ["message_id"]  # Union of all evidence message_ids
    access_count: 0
    last_accessed: "ISO8601 | null"
    
  # Archive reference (when compressed)
  full_content_ref: "string | null"  # e.g., "archive/ke_uuid.json"
```

### 3.3 Project Entry

```yaml
project_entry:
  # Identity
  id: "pe_uuid"
  type: "project"
  name: "string"  # e.g., "Opus Ensemble"
  
  # State
  status: "active" | "paused" | "completed" | "abandoned"
  detail_level: "full" | "compressed"
  
  # Current state
  goal: "string"  # 1-2 sentences
  current_phase: "string"  # e.g., "architecture", "implementation"
  blocked_on: "string | null"
  
  # Decisions with provenance
  decisions_made:
    - decision: "string"
      rationale: "string | null"
      date: "ISO8601"
      evidence:
        conversation_id: "string"
        message_ids: ["string"]
        snippet: "string | null"
  
  # Technical context
  tech_stack:
    - "string"
  
  # Linkages
  related_repos:
    - repo: "string"
      path: "string | null"
      is_primary: boolean
      confidence: 0.0-1.0
  
  related_knowledge:
    - knowledge_id: "ke_uuid"
      relationship: "depends_on" | "informed_by" | "produced"
  
  # Evolution
  phase_history:
    - phase: "string"
      entered_at: "ISO8601"
      evidence:
        conversation_id: "string"
  
  # Metadata
  metadata:
    created_at: "ISO8601"
    updated_at: "ISO8601"
    source_conversations: ["conversation_id"]
    source_messages: ["message_id"]
    last_touched: "ISO8601"  # Most recent conversation or commit
    
  # Archive reference
  full_content_ref: "string | null"
```

### 3.4 Thin Index

```yaml
thin_index:
  generated_at: "ISO8601"
  token_count: int  # Actual count using target tokenizer
  
  topics:
    - id: "ke_uuid"  # For retrieval resolution
      domain: "string"
      current_view_summary: "string"  # Max 80 chars
      state: "active" | "contested" | "stale"
      confidence: "high" | "medium" | "low"
      last_updated: "ISO8601"
      top_repo: "string | null"  # Most relevant repo
  
  projects:
    - id: "pe_uuid"  # For retrieval resolution
      name: "string"
      status: "active" | "paused" | "completed" | "abandoned"
      goal_summary: "string"  # Max 80 chars
      current_phase: "string"
      blocked_on: "string | null"
      last_touched: "ISO8601"
      primary_repo: "string | null"
  
  recent_evolutions:
    - entry_id: "ke_uuid | pe_uuid"
      entry_type: "knowledge" | "project"
      domain_or_name: "string"
      delta_summary: "string"  # Max 60 chars
      date: "ISO8601"
  
  contested_count: int  # Number of entries in contested state
```

**Constraint:** Thin index must serialize to <3000 tokens. Token count is measured using cl100k_base tokenizer (OpenAI) as reference standard.

---

## 4. Processing Pipeline

### 4.1 Stage 1: Parse

**Input:** Raw export files (JSON)

**Output:** Normalized conversation objects

**Logic:**

```
For each conversation in export:
  1. Extract metadata (id, title, timestamps, source platform)
  
  2. Build message graph:
     - Create node for each message with: id, role, content, timestamp
     - Establish parent-child relationships
     - Identify root node(s)
  
  3. Linearize thread:
     - Traverse from root following primary path
     - Primary path selection: prefer longest path to latest timestamp
     - If multiple branches exist at a node:
       a. Compare content similarity of branches
       b. If >90% similar: keep only primary (latest)
       c. If substantively different: flag for alternate_branches
  
  4. Extract code blocks:
     - Parse content for ``` delimited blocks
     - Extract language hint if present
     - Store separately for linking stage
  
  5. Output normalized conversation with:
     - Preserved message_ids (critical for provenance)
     - Parse metadata documenting decisions made
```

**Branch Handling Detail:**

```python
def select_primary_branch(branches):
    """
    Given multiple child branches at a node, select primary path.
    Returns: (primary_branch, alternate_branches_to_keep)
    """
    # Sort by: latest leaf timestamp descending
    sorted_branches = sort_by_latest_leaf_timestamp(branches)
    primary = sorted_branches[0]
    
    alternates_to_keep = []
    for branch in sorted_branches[1:]:
        similarity = compute_content_similarity(primary, branch)
        if similarity < 0.90:
            # Substantively different, worth keeping
            alternates_to_keep.append(branch)
    
    return primary, alternates_to_keep
```

### 4.2 Stage 2: Filter

**Input:** Normalized conversations

**Output:** Filtered conversations with filter metadata

**Logic:**

Rather than hard exclusions, compute a value score and keep conversations above threshold:

```yaml
value_signals:
  has_code_blocks: +3
  has_explicit_decision: +3  # "I decided", "let's go with", "the answer is"
  has_user_learning: +2  # "I see", "that makes sense", "I didn't know"
  has_project_reference: +2  # Mentions known project names
  has_repo_reference: +2  # Mentions GitHub repos
  conversation_length: +1 per 5 exchanges (max +3)
  has_conclusion: +2  # Final message contains summary or decision
  
negative_signals:
  pure_troubleshooting: -2  # "why isn't this working", "error message"
  meta_about_ai: -3  # "can you do X?", "what are your capabilities"
  abandoned_thread: -2  # No user message in last 3 exchanges
  
threshold: 3  # Keep if score >= threshold
```

**Output enrichment:**

```yaml
filter_metadata:
  value_score: int
  signals_present: [string]
  signals_absent: [string]
  decision: "keep" | "skip"
  skip_reason: "string | null"  # For tuning later
```

**Important:** Even skipped conversations are logged with their scores and reasons. This enables future tuning of the filter.

### 4.3 Stage 3: Extract

**Input:** Filtered conversations

**Output:** Candidate entries (knowledge and project) with full provenance

**Method:** LLM pass with structured extraction prompt

**Extraction Prompt:**

```markdown
You are extracting knowledge entries from a conversation between a user and an AI assistant. Your extractions must include evidence linking back to specific messages.

## User Context
{brief_identity_context}

## Task
Analyze this conversation and extract structured knowledge. For EVERY insight, decision, or finding, you MUST provide evidence pointing to the specific message(s) that support it.

## Output Schema

### Knowledge Entries
For each distinct topic where the user learned something, made a decision, or demonstrated expertise:

```json
{
  "domain": "specific topic area (e.g., 'MLX layer selection' not 'machine learning')",
  "current_view": "1-3 sentences describing what the user now thinks/knows",
  "confidence": "high|medium|low",
  "key_insights": [
    {
      "insight": "specific learning or conclusion",
      "evidence": {
        "message_ids": ["msg_id1", "msg_id2"],
        "snippet": "key quote, max 200 chars"
      }
    }
  ],
  "knows_how_to": [
    {
      "capability": "practical skill demonstrated",
      "evidence": {
        "message_ids": ["msg_id"],
        "snippet": "optional supporting quote"
      }
    }
  ],
  "open_questions": [
    {
      "question": "unresolved question",
      "evidence": {
        "message_ids": ["msg_id"]
      }
    }
  ],
  "repo_mentions": ["any GitHub repos or code paths mentioned"]
}
```

### Project Entries
For distinct projects the user is working on:

```json
{
  "name": "project name (use explicit name if mentioned)",
  "goal": "what the user is trying to achieve",
  "current_phase": "where they are in the work",
  "decisions_made": [
    {
      "decision": "specific choice",
      "rationale": "why, if stated",
      "evidence": {
        "message_ids": ["msg_id"],
        "snippet": "key quote"
      }
    }
  ],
  "blocked_on": "what's stopping progress, or null",
  "tech_stack": ["technologies involved"],
  "repo_mentions": ["any GitHub repos mentioned"]
}
```

## Rules
1. EVERY insight/decision MUST have evidence with message_ids
2. Snippets should be direct quotes, max 200 characters
3. If you cannot find evidence for a claim, do not include that claim
4. Be specific in domain naming - prefer "React state management" over "React"
5. Skip the conversation entirely if it contains no extractable knowledge

## Conversation
Messages are formatted as [message_id] role: content

{formatted_conversation}
```

**Chunking Strategy:**

If conversation exceeds 8000 tokens:

1. Split into overlapping segments (500 token overlap)
2. Run extraction on each segment
3. Consolidation pass:
   - Group candidate entries by domain/project name
   - Merge entries with same domain: union insights, reconcile views
   - Deduplicate evidence (same message_id)
   - If views conflict within same conversation: flag for review

**Validation:**

Before accepting extracted entries:

```python
def validate_extraction(entry):
    errors = []
    
    # Every insight must have evidence
    for insight in entry.get('key_insights', []):
        if not insight.get('evidence', {}).get('message_ids'):
            errors.append(f"Insight missing evidence: {insight['insight'][:50]}")
    
    # Message IDs must exist in source conversation
    all_message_ids = extract_all_message_ids(entry)
    for msg_id in all_message_ids:
        if msg_id not in conversation_message_ids:
            errors.append(f"Invalid message_id: {msg_id}")
    
    # Domain must be specific enough
    if len(entry.get('domain', '').split()) < 2:
        errors.append(f"Domain too generic: {entry.get('domain')}")
    
    return errors
```

### 4.4 Stage 4: Link

**Input:** Candidate entries + GitHub repository data

**Output:** Entries with `related_repos` populated and verified

**Method:** Two-phase linking (candidate generation + verification)

**Phase 1: Candidate Generation**

```python
def generate_repo_candidates(entry, repos):
    candidates = []
    
    # Explicit linking (high confidence)
    for mention in entry.get('repo_mentions', []):
        for repo in repos:
            if matches_repo(mention, repo):
                candidates.append({
                    'repo': repo.full_name,
                    'link_type': 'explicit',
                    'match_reason': f"Explicit mention: {mention}",
                    'initial_confidence': 1.0
                })
    
    # Semantic linking (needs verification)
    entry_embedding = embed(entry.domain + ' ' + entry.current_view)
    for repo in repos:
        readme_embedding = get_cached_embedding(repo.readme)
        similarity = cosine_similarity(entry_embedding, readme_embedding)
        if similarity > 0.70:  # Lower threshold, will verify
            candidates.append({
                'repo': repo.full_name,
                'link_type': 'semantic',
                'match_reason': f"README similarity: {similarity:.2f}",
                'initial_confidence': similarity
            })
    
    # Return top 5 semantic candidates for verification
    explicit = [c for c in candidates if c['link_type'] == 'explicit']
    semantic = sorted(
        [c for c in candidates if c['link_type'] == 'semantic'],
        key=lambda x: x['initial_confidence'],
        reverse=True
    )[:5]
    
    return explicit + semantic
```

**Phase 2: Verification (for semantic links)**

```python
def verify_semantic_link(entry, repo, match_reason):
    """
    Use LLM to verify if semantic match is actually relevant.
    """
    prompt = f"""
    Does this knowledge entry genuinely relate to this repository?
    
    Knowledge Entry:
    - Domain: {entry.domain}
    - Current View: {entry.current_view}
    - Key Insights: {entry.key_insights[:2]}
    
    Repository:
    - Name: {repo.full_name}
    - Description: {repo.description}
    - README excerpt: {repo.readme[:500]}
    - Primary language: {repo.language}
    
    Match reason: {match_reason}
    
    Respond with JSON:
    {{
      "is_relevant": true/false,
      "confidence": 0.0-1.0,
      "explanation": "brief reason"
    }}
    """
    
    result = llm_call(prompt)
    return result
```

**Output:**

```yaml
related_repos:
  - repo: "owner/repo-name"
    path: "/specific/folder"  # If mentioned
    link_type: "explicit"
    confidence: 1.0
    evidence: "Mentioned 'trading-systems repo' in message msg_123"
    
  - repo: "owner/other-repo"
    path: null
    link_type: "semantic"
    confidence: 0.85  # Post-verification
    evidence: "README discusses similar MLX fine-tuning approaches"
```

### 4.5 Stage 5: Merge

**Input:** New candidate entries + existing index from Redis

**Output:** Merge operations (create, update, contest, archive)

**Matching Logic:**

```python
def find_existing_match(candidate, existing_entries):
    """
    Find existing entry that candidate should merge with.
    Require multiple signals to avoid false merges.
    """
    matches = []
    
    for existing in existing_entries:
        signals = 0
        
        # Signal 1: High embedding similarity
        similarity = embedding_similarity(candidate.domain, existing.domain)
        if similarity > 0.85:
            signals += 1
        
        # Signal 2: Shared repository
        candidate_repos = set(r['repo'] for r in candidate.related_repos)
        existing_repos = set(r['repo'] for r in existing.related_repos)
        if candidate_repos & existing_repos:
            signals += 1
        
        # Signal 3: Keyword overlap in domain
        candidate_keywords = set(candidate.domain.lower().split())
        existing_keywords = set(existing.domain.lower().split())
        if len(candidate_keywords & existing_keywords) >= 2:
            signals += 1
        
        # Signal 4: Same project context
        if candidate.get('project_ref') == existing.get('project_ref'):
            signals += 1
        
        # Require at least 2 signals to match
        if signals >= 2:
            matches.append((existing, signals, similarity))
    
    if matches:
        # Return best match
        return max(matches, key=lambda x: (x[1], x[2]))[0]
    return None
```

**Merge Decision Logic:**

```python
def determine_merge_action(candidate, existing):
    """
    Determine how to merge candidate with existing entry.
    Returns: MergeAction with full decision record.
    """
    # Compare current views
    view_similarity = semantic_similarity(
        candidate.current_view, 
        existing.current_view
    )
    
    if view_similarity > 0.85:
        # Views are substantively the same
        return MergeAction(
            action="update",
            reason="Views aligned",
            operations=[
                "merge_insights",  # Union, dedupe by evidence
                "merge_capabilities",
                "update_timestamps",
                "add_source_conversations"
            ]
        )
    
    elif view_similarity > 0.50:
        # Views are related but different - evolution
        return MergeAction(
            action="evolve",
            reason="View has evolved",
            operations=[
                "append_evolution_record",
                "update_current_view",
                "merge_insights",
                "update_timestamps"
            ],
            evolution_record={
                "delta": f"View shifted from '{existing.current_view[:100]}' to '{candidate.current_view[:100]}'",
                "trigger": candidate.source_conversations[0],
                "from_view": existing.current_view,
                "to_view": candidate.current_view,
                "evidence": candidate.positions[0].evidence if candidate.positions else None
            }
        )
    
    else:
        # Views contradict - do not overwrite
        return MergeAction(
            action="contest",
            reason="Views contradict",
            operations=[
                "set_state_contested",
                "add_position",  # Add new view to positions array
                "keep_both_views",
                "update_timestamps"
            ],
            new_position={
                "view": candidate.current_view,
                "confidence": candidate.confidence,
                "as_of": now(),
                "evidence": candidate.positions[0].evidence if candidate.positions else None
            }
        )
```

**Merge Decision Record:**

Every merge operation produces a logged record:

```yaml
merge_record:
  timestamp: "ISO8601"
  candidate_id: "temp_id"
  existing_id: "ke_uuid | null"
  action: "create" | "update" | "evolve" | "contest"
  reason: "string"
  
  # For debugging/audit
  match_signals: ["embedding_similarity", "shared_repo"]
  view_similarity: 0.45
  
  # What changed
  before:
    current_view: "string | null"
    state: "string | null"
    insight_count: int
  after:
    current_view: "string"
    state: "string"
    insight_count: int
    
  # If contested
  positions_count: int
```

### 4.6 Stage 6: Compress

**Input:** All entries

**Output:** Entries with compressed views (originals archived)

**Trigger Criteria:**

```python
def should_compress(entry):
    return (
        entry.detail_level == "full" and
        entry.state not in ["contested", "active_project"] and
        days_since(entry.metadata.updated_at) > 90 and
        entry.metadata.access_count < 3 and
        not is_linked_to_active_project(entry) and
        days_since_last_evolution(entry) > 60
    )
```

**Compression is Non-Destructive:**

```python
def compress_entry(entry):
    """
    Compression creates a view, does not delete original.
    """
    # 1. Archive full entry to cold storage
    archive_path = f"archive/{entry.id}.json"
    write_to_dropbox(archive_path, entry.to_json())
    
    # 2. Generate compressed view
    compressed = llm_compress(entry)
    
    # 3. Update entry in Redis
    entry.detail_level = "compressed"
    entry.full_content_ref = archive_path
    entry.current_view = compressed.current_view
    entry.key_insights = compressed.key_insights[:3]  # Keep top 3
    entry.knows_how_to = compressed.knows_how_to[:2]  # Keep top 2
    entry.open_questions = []  # Drop, can retrieve from archive
    
    # 4. KEEP evolution summary (critical)
    entry.evolution = summarize_evolution(entry.evolution)
    
    return entry
```

**Compression Prompt:**

```markdown
Compress this knowledge entry while preserving what matters most.

KEEP (in order of priority):
1. Core insight or conclusion - the "so what"
2. Non-obvious learnings that would be hard to re-derive
3. Pointers to code/repos that still exist
4. Summary of how thinking evolved (if applicable)

DROP:
1. Reasoning steps that led to obvious conclusions
2. Failed approaches (unless the failure mode itself is instructive)
3. Context that can be recovered from linked repos
4. Verbose explanations of well-known concepts

Original entry:
{entry_json}

Return compressed entry matching schema, with:
- current_view: Max 2 sentences
- key_insights: Max 3, each max 1 sentence
- knows_how_to: Max 2
- evolution: Summarize to 1 sentence if multiple evolutions exist

Preserve ALL evidence fields - just make the content shorter.
```

**Evolution Preservation:**

```python
def summarize_evolution(evolution_list):
    """
    Keep evolution even when compressing, just summarize it.
    """
    if not evolution_list:
        return []
    
    if len(evolution_list) == 1:
        return evolution_list  # Keep as-is
    
    # Multiple evolutions: summarize trajectory
    first = evolution_list[0]
    last = evolution_list[-1]
    
    return [{
        "delta": f"Evolved through {len(evolution_list)} stages: {first['from_view'][:50]}... → {last['to_view'][:50]}...",
        "trigger": "Multiple conversations",
        "date": last['date'],
        "evidence": last.get('evidence'),
        "full_history_ref": "See archived entry"
    }]
```

### 4.7 Stage 7: Index

**Input:** All entries (after merge and compress)

**Output:** 
- Entries written to Upstash Redis
- Embeddings written to Upstash Vector
- Thin index regenerated

**Redis Operations:**

```python
def write_to_redis(entries, redis_client):
    pipe = redis_client.pipeline()
    
    for entry in entries:
        key = f"{entry.type}:{entry.id}"
        pipe.set(key, entry.to_json())
        
        # Secondary indexes
        pipe.sadd(f"by_domain:{normalize(entry.domain)}", entry.id)
        pipe.sadd(f"by_state:{entry.state}", entry.id)
        if entry.related_repos:
            for repo in entry.related_repos:
                pipe.sadd(f"by_repo:{repo['repo']}", entry.id)
    
    pipe.execute()
```

**Vector Operations:**

```python
def write_to_vector(entries, vector_client):
    vectors = []
    
    for entry in entries:
        # Generate embedding from key fields
        text = f"{entry.domain} {entry.current_view} {' '.join(i['insight'] for i in entry.key_insights[:3])}"
        embedding = embed(text)  # 1536 dimensions for compatibility
        
        vectors.append({
            "id": entry.id,
            "vector": embedding,
            "metadata": {
                "type": entry.type,
                "domain": entry.domain,
                "state": entry.state,
                "updated_at": entry.metadata.updated_at,
                "access_count": entry.metadata.access_count
            }
        })
    
    # Batch upsert (Upstash counts each vector in batch)
    vector_client.upsert(vectors)
```

**Thin Index Generation:**

```python
def generate_thin_index(entries):
    index = {
        "generated_at": now_iso(),
        "topics": [],
        "projects": [],
        "recent_evolutions": [],
        "contested_count": 0
    }
    
    # Select entries for index
    knowledge_entries = [e for e in entries if e.type == "knowledge" and e.state != "deprecated"]
    project_entries = [e for e in entries if e.type == "project"]
    
    # Sort by relevance: active first, then by recency
    knowledge_entries.sort(key=lambda e: (
        e.state == "active",
        e.metadata.access_count,
        e.metadata.updated_at
    ), reverse=True)
    
    # Build topics list
    for entry in knowledge_entries:
        index["topics"].append({
            "id": entry.id,
            "domain": entry.domain,
            "current_view_summary": truncate(entry.current_view, 80),
            "state": entry.state,
            "confidence": entry.confidence,
            "last_updated": entry.metadata.updated_at,
            "top_repo": entry.related_repos[0]["repo"] if entry.related_repos else None
        })
        if entry.state == "contested":
            index["contested_count"] += 1
    
    # Build projects list
    for entry in sorted(project_entries, key=lambda e: e.status == "active", reverse=True):
        index["projects"].append({
            "id": entry.id,
            "name": entry.name,
            "status": entry.status,
            "goal_summary": truncate(entry.goal, 80),
            "current_phase": entry.current_phase,
            "blocked_on": entry.blocked_on,
            "last_touched": entry.metadata.last_touched,
            "primary_repo": next((r["repo"] for r in entry.related_repos if r.get("is_primary")), None)
        })
    
    # Recent evolutions (last 30 days)
    all_evolutions = []
    for entry in entries:
        for evo in entry.evolution:
            if days_since(evo["date"]) <= 30:
                all_evolutions.append({
                    "entry_id": entry.id,
                    "entry_type": entry.type,
                    "domain_or_name": entry.domain if entry.type == "knowledge" else entry.name,
                    "delta_summary": truncate(evo["delta"], 60),
                    "date": evo["date"]
                })
    index["recent_evolutions"] = sorted(all_evolutions, key=lambda x: x["date"], reverse=True)[:10]
    
    # Enforce token budget
    index = enforce_token_budget(index, max_tokens=3000)
    index["token_count"] = count_tokens(index)
    
    return index

def enforce_token_budget(index, max_tokens):
    """
    Trim index to fit token budget, prioritizing active/recent items.
    """
    while count_tokens(index) > max_tokens:
        # Remove oldest/lowest priority items
        if len(index["topics"]) > 10:
            index["topics"] = index["topics"][:10]
        elif len(index["projects"]) > 5:
            index["projects"] = index["projects"][:5]
        elif len(index["recent_evolutions"]) > 5:
            index["recent_evolutions"] = index["recent_evolutions"][:5]
        else:
            # Truncate summaries further
            for topic in index["topics"]:
                topic["current_view_summary"] = truncate(topic["current_view_summary"], 50)
            break
    
    return index
```

---

## 5. Scheduling & Execution

### 5.1 Trigger

**Primary:** Vercel Cron, weekly (Sunday 2:00 AM PT)

**Manual:** API endpoint for on-demand run: `POST /api/distill/start`

### 5.2 Execution Environment

**Platform:** Vercel Serverless Functions (Pro plan)

**Constraints:**
- Function timeout: Configure to 300s (Vercel Pro allows up to 900s with Fluid Compute)
- Memory: 1024MB default, increase if needed
- Must handle partial failures gracefully

**Execution Flow:**

```
1. Cron triggers: POST /api/distill/start
   ├── Authenticate
   ├── Check for existing run in progress (Redis lock)
   ├── Create run record with status: "started"
   ├── Read new exports from Dropbox
   ├── Write conversation IDs to Redis queue: distill:queue
   ├── Trigger: POST /api/distill/process
   └── Return: { run_id, conversations_queued }

2. /api/distill/process (may run multiple times)
   ├── Pull batch from queue (N=10 conversations)
   ├── For each conversation:
   │   ├── Stage 1: Parse
   │   ├── Stage 2: Filter
   │   ├── Stage 3: Extract
   │   └── Stage 4: Link
   ├── Write candidate entries to Redis: distill:staging:{run_id}
   ├── If queue not empty AND time_remaining > 30s:
   │   └── Continue processing
   ├── If queue not empty AND time_remaining < 30s:
   │   └── Trigger new /api/distill/process invocation
   └── If queue empty:
       └── Trigger: POST /api/distill/finalize

3. /api/distill/finalize
   ├── Read all candidates from distill:staging:{run_id}
   ├── Read existing entries from Redis
   ├── Stage 5: Merge
   ├── Stage 6: Compress (entries meeting criteria)
   ├── Stage 7: Index
   │   ├── Write entries to Redis
   │   ├── Write embeddings to Upstash Vector
   │   └── Write thin index to Redis: index:current
   ├── Archive processed exports
   ├── Clear staging data
   ├── Update run record with status: "completed"
   └── Return: run_report
```

**Failure Handling:**

```python
# Run lock to prevent concurrent runs
def acquire_run_lock(redis_client, run_id, ttl=3600):
    return redis_client.set("distill:lock", run_id, nx=True, ex=ttl)

def release_run_lock(redis_client, run_id):
    # Only release if we own the lock
    if redis_client.get("distill:lock") == run_id:
        redis_client.delete("distill:lock")

# Checkpoint progress for resumability
def checkpoint_progress(redis_client, run_id, stage, data):
    redis_client.hset(f"distill:checkpoint:{run_id}", stage, json.dumps(data))

def resume_from_checkpoint(redis_client, run_id):
    checkpoint = redis_client.hgetall(f"distill:checkpoint:{run_id}")
    if checkpoint:
        return {k: json.loads(v) for k, v in checkpoint.items()}
    return None
```

### 5.3 Cost Estimation

**Cost Model:**

```
Cost per run =
  Extraction LLM calls:
    input_tokens / 1M × $3.00 (Claude 3.5 Sonnet input)
    + output_tokens / 1M × $15.00 (Claude 3.5 Sonnet output)
  
  + Verification LLM calls (linking):
    input_tokens / 1M × $3.00
    + output_tokens / 1M × $15.00
  
  + Compression LLM calls:
    input_tokens / 1M × $3.00
    + output_tokens / 1M × $15.00
  
  + Embedding calls:
    tokens / 1M × $0.02 (text-embedding-3-small)
  
  + Infrastructure:
    Vercel: $0 (within Pro plan)
    Upstash Redis: $0 (free tier, <256MB, <500K commands/month)
    Upstash Vector: $0-5 (free tier 10K vectors, 10K daily operations)
```

**Example Run (100 conversations):**

| Component | Calculation | Cost |
|-----------|-------------|------|
| Extraction | 100 × 8K input + 100 × 2K output = 800K + 200K tokens | $2.40 + $3.00 = $5.40 |
| Verification | 50 semantic links × 1K tokens each = 50K tokens | $0.15 + $0.75 = $0.90 |
| Compression | 20 entries × 3K input + 1K output = 60K + 20K tokens | $0.18 + $0.30 = $0.48 |
| Embeddings | 100 entries × 500 tokens = 50K tokens | $0.001 |
| Infrastructure | Within free tiers | $0 |
| **Total per run** | | **~$7** |
| **Monthly (4 runs)** | | **~$28** |

---

## 6. Error Handling

### 6.1 Parse Failures

```yaml
on_parse_error:
  action: log_and_skip
  log_fields:
    - conversation_id
    - error_type
    - error_message
    - raw_content_sample (first 500 chars)
  continue: true
  surface_in_report: true
```

### 6.2 Extraction Failures

```yaml
on_extraction_error:
  retry:
    max_attempts: 3
    backoff: exponential (1s, 2s, 4s)
  on_persistent_failure:
    action: log_for_review
    log_fields:
      - conversation_id
      - error_type
      - last_error_message
      - conversation_content_hash
    continue: true
    add_to_review_queue: true
```

### 6.3 Validation Failures

```yaml
on_validation_error:
  # Missing evidence, invalid message IDs, etc.
  action: reject_entry
  log_fields:
    - entry_type
    - domain_or_name
    - validation_errors
    - conversation_id
  continue: true
  surface_in_report: true
```

### 6.4 Merge Conflicts

```yaml
on_merge_conflict:
  # Handled by design - creates contested state
  action: create_contested_entry
  log_fields:
    - existing_entry_id
    - candidate_summary
    - conflict_type
    - both_views
```

### 6.5 Rate Limits

```yaml
github_rate_limit:
  check_header: X-RateLimit-Remaining
  pause_threshold: 100
  pause_duration: 60s
  max_concurrent_requests: 10

llm_rate_limit:
  implement: request_queue
  max_requests_per_minute: 50  # Adjust based on tier
  retry_on_429: true
  backoff: exponential

upstash_rate_limit:
  free_tier_daily_ops: 10000
  track_ops_count: true
  alert_threshold: 8000
```

### 6.6 Partial Run Recovery

```yaml
on_run_interruption:
  # Function timeout, crash, etc.
  checkpoint_stages: [parse, filter, extract, link]
  recovery:
    - check for checkpoint on run start
    - resume from last completed stage
    - reprocess only unprocessed conversations
  stale_checkpoint_ttl: 24h
```

---

## 7. Observability

### 7.1 Run Report Schema

```yaml
run_report:
  # Identity
  run_id: "string"
  triggered_by: "cron" | "manual"
  
  # Timing
  started_at: "ISO8601"
  completed_at: "ISO8601"
  duration_seconds: int
  
  # Status
  status: "completed" | "completed_with_errors" | "failed"
  
  # Input metrics
  input:
    exports_found:
      claude: int
      gpt: int
    conversations_total: int
    conversations_new: int  # Not previously processed
    repos_scanned: int
    
  # Processing metrics
  processing:
    conversations_parsed: int
    parse_errors: int
    conversations_filtered_in: int
    conversations_filtered_out: int
    filter_score_distribution:
      - score: int
        count: int
    
  # Extraction metrics
  extraction:
    knowledge_entries_extracted: int
    project_entries_extracted: int
    extraction_errors: int
    validation_failures: int
    insights_with_evidence: int
    insights_without_evidence: int  # Should be 0
    
  # Linking metrics
  linking:
    explicit_links_created: int
    semantic_candidates_generated: int
    semantic_links_verified: int
    semantic_links_rejected: int
    
  # Merge metrics
  merge:
    entries_created: int
    entries_updated: int
    entries_evolved: int
    entries_contested: int
    merge_conflicts_logged: int
    
  # Compression metrics
  compression:
    entries_eligible: int
    entries_compressed: int
    entries_archived: int
    
  # Output metrics
  output:
    total_knowledge_entries: int
    total_project_entries: int
    active_entries: int
    contested_entries: int
    compressed_entries: int
    thin_index_token_count: int
    
  # Cost tracking
  cost:
    llm_input_tokens: int
    llm_output_tokens: int
    embedding_tokens: int
    estimated_cost_usd: float
    
  # Errors (detailed)
  errors:
    - timestamp: "ISO8601"
      stage: "string"
      conversation_id: "string | null"
      error_type: "string"
      error_message: "string"
      recoverable: boolean
      
  # Review queue
  review_queue:
    - conversation_id: "string"
      reason: "string"
```

### 7.2 Storage & Retention

```yaml
run_reports:
  storage: redis
  key_pattern: "runs:{run_id}"
  retention: 20 runs
  
  # Also write to Dropbox for long-term
  archive_to_dropbox: true
  archive_path: "knowledge-system/run-reports/{run_id}.json"

merge_records:
  storage: redis
  key_pattern: "merge_log:{date}"
  retention: 90 days
  
checkpoint_data:
  storage: redis
  key_pattern: "distill:checkpoint:{run_id}"
  retention: 24 hours (auto-expire)
```

### 7.3 Admin Endpoints

```yaml
endpoints:
  GET /api/admin/runs:
    description: List recent runs
    response: [run_report_summary]
    
  GET /api/admin/runs/{run_id}:
    description: Get detailed run report
    response: run_report
    
  GET /api/admin/entries:
    description: List all entries with filters
    params:
      type: knowledge | project
      state: active | contested | stale | deprecated
      domain: string (partial match)
    response: [entry_summary]
    
  GET /api/admin/entries/{entry_id}:
    description: Get full entry detail
    response: entry
    
  POST /api/admin/entries/{entry_id}/resolve:
    description: Resolve contested entry (pick winning view)
    body: { winning_position_index: int }
    
  GET /api/admin/review-queue:
    description: Get conversations flagged for review
    response: [{ conversation_id, reason, timestamp }]
```

---

## 8. Constraints & Dependencies

### 8.1 Upstash Redis

```yaml
upstash_redis:
  tier: free
  limits:
    storage: 256MB
    commands_per_month: 500K
  
  estimated_usage:
    storage: ~50MB (500 entries × 100KB avg)
    commands_per_run: ~5000 (reads + writes + index ops)
    commands_per_month: ~25000 (4 runs + retrieval)
  
  status: within_limits
```

### 8.2 Upstash Vector

```yaml
upstash_vector:
  tier: free
  limits:
    vectors: 10K
    dimensions: 1536 max
    daily_operations: 10K
  
  considerations:
    - Use 1536-dim embeddings (text-embedding-3-small compatible)
    - Hybrid search NOT supported on free tier
    - Batch upserts count per-vector toward daily limit
  
  estimated_usage:
    vectors: ~500 (one per entry)
    daily_ops: ~1000 on run day, ~100 on retrieval days
  
  status: within_limits
```

### 8.3 Vercel

```yaml
vercel:
  tier: pro
  limits:
    function_duration: 300s default, 900s max with Fluid Compute
    concurrent_executions: 1000
    bandwidth: 1TB/month
  
  configuration:
    function_max_duration: 300  # In vercel.json
    memory: 1024  # MB
  
  estimated_usage:
    function_time_per_run: ~10 minutes total across invocations
    monthly_function_time: ~40 minutes
  
  status: within_limits
```

### 8.4 LLM API

```yaml
anthropic_api:
  model: claude-3-5-sonnet-20241022
  pricing:
    input: $3.00 / 1M tokens
    output: $15.00 / 1M tokens
  rate_limits:
    requests_per_minute: varies by tier
  
  fallback:
    on_rate_limit: exponential backoff
    on_error: retry 3x, then skip with log
```

### 8.5 Embedding API

```yaml
openai_embeddings:
  model: text-embedding-3-small
  dimensions: 1536
  pricing: $0.02 / 1M tokens
  
  alternative:
    model: text-embedding-3-large
    dimensions: 3072 (requires paid Upstash Vector tier)
```

---

## 9. Security Considerations

### 9.1 Data Sensitivity

```yaml
data_classification:
  conversation_content: sensitive
    - May contain personal information
    - May contain business/work details
    - May contain code/credentials (should be avoided but possible)
  
  extracted_entries: sensitive
    - Derived from sensitive source
    - Contains personal knowledge/decisions
```

### 9.2 Storage Security

```yaml
upstash:
  encryption_at_rest: yes (default)
  encryption_in_transit: yes (TLS)
  soc2_compliance: paid tier only
  
  recommendation:
    - Acceptable for personal use on free tier
    - For sensitive work data, consider paid tier or self-hosted alternative

dropbox:
  encryption_at_rest: yes
  encryption_in_transit: yes
  
  recommendation:
    - Use existing Dropbox with 2FA enabled
    - Archive folder should not be shared
```

### 9.3 API Security

```yaml
vercel_endpoints:
  authentication: required
  method: API key in header (X-API-Key)
  key_storage: Vercel environment variables
  
  rate_limiting:
    admin_endpoints: 10 req/min
    distill_endpoints: 1 req/min (prevent duplicate runs)
```

---

## 10. Future Considerations (Out of Scope for v1.1)

1. **Real-time ingestion**: Webhook from Claude/GPT when available
2. **User review UI**: Web interface for managing entries, resolving contested states
3. **Multi-user**: Currently single-user design
4. **Additional sources**: 
   - Obsidian/markdown notes
   - Email threads
   - Slack conversations
   - Voice transcripts
5. **Feedback loop**: Track retrieval quality, use to tune extraction prompt
6. **Hybrid search**: Combine semantic + keyword when Upstash supports it
7. **Automated conflict resolution**: LLM-based resolution of contested entries after time threshold

---

## 11. Glossary

| Term | Definition |
|------|------------|
| **Entry** | A knowledge or project record in the system |
| **Provenance** | Evidence linking an extracted insight to source messages |
| **Contested** | State where an entry has conflicting positions that haven't been resolved |
| **Thin Index** | Compressed summary of all entries for fast context injection |
| **Evolution** | Record of how an entry's view changed over time |
| **Compression** | Process of summarizing an entry while archiving the full version |
| **Distillation** | The overall process of converting raw chats to structured entries |

---

## 12. Changelog

### v1.1 (Current)
- Added evidence/provenance fields to all insights and decisions
- Added `positions` array for contested entries
- Changed merge logic: contradictions create `contested` state instead of overwriting
- Compression now non-destructive: creates view, archives full content
- Evolution summaries preserved even when entry is compressed
- Added entry IDs to thin index for retrieval resolution
- Added filter scoring model (replaces hard exclusions)
- Added semantic link verification step
- Updated Vercel timeout documentation
- Added detailed Upstash constraints
- Revised cost estimation with explicit formulas

### v1.0
- Initial PRD

---

*Document Version: 1.1*  
*Last Updated: December 2024*
