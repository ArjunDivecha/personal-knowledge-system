---
schema_version: 1
spec_id: PKS-USAGE-SIGNAL-001
status: draft
target_agent: either
scope:
  in:
  - cloudflare-mcp/mcp-server/src/**
  - DISCOVER_TARGETS
  out:
  - tests/probes/**
  - shared/memory_policy.json
  forbid:
  - mcp-server/**
  - distillation/run.py
  - '**/.env*'
  - archive/**
bet:
  if: the MCP search or get_context tool returns an entry as a hit
  then: that entry's metadata.last_accessed and metadata.access_count are persisted
    asynchronously, so the salience retrievalBoost term (salience.ts:147) becomes
    live instead of dead code
  observable: after a search returns entry E, re-reading E shows an updated last_accessed
    and incremented access_count, and computeSalience(E) yields a nonzero retrievalBoost
    within the 60-day access window
invariants:
- id: INV1
  holds: "access-write failures never fail, slow, or alter the read path \u2014 the\
    \ search/get_context response is identical whether the write-back succeeds or\
    \ fails"
  check_intent: unit test with a storage stub whose write throws; assert the tool
    response is unchanged and no error propagates to the caller
- id: INV2
  holds: 'write amplification is bounded: only entries actually returned to the caller
    (at most the requested limit) receive access writes, never the wider candidate
    pool'
  check_intent: unit test with fetchLimit-many candidates but limit-many results;
    assert exactly limit write-backs are issued
- id: INV3
  holds: access writes never modify any field other than last_accessed and access_count,
    and never touch archived entries or change state, tier, salience, or content
  check_intent: unit test asserting the persisted diff is exactly {last_accessed,
    access_count} and that an archived entry in results receives no write
- id: INV4
  holds: 'computeSalience consumes the persisted last_accessed: an entry accessed
    within 60 days scores strictly higher than an otherwise-identical entry never
    accessed'
  check_intent: unit test constructing two identical entries differing only in last_accessed;
    assert strict score ordering via the existing retrievalBoost term
- id: INV5
  holds: no file outside scope.in is modified and no scope.forbid path is touched
    in the final diff
  check_intent: git diff --name-only is a subset of scope.in and excludes every scope.forbid
    path
gates:
- id: G1
  intent: 'INV1 and INV2 hold: fail-open write-back with bounded amplification, proven
    by the Worker vitest suite'
  must_assert: INV1 (throwing write stub leaves response identical) and INV2 (exactly
    limit write-backs) pass as vitest cases; exit nonzero naming the failing case
    otherwise
  command: TODO
  requires_permission: false
- id: G2
  intent: 'INV3 and INV4 hold: writes are minimal and salience actually consumes them'
  must_assert: INV3 (diff is exactly last_accessed+access_count, archived untouched)
    and INV4 (strict score ordering from retrievalBoost) pass as vitest cases; exit
    nonzero otherwise
  command: TODO
  requires_permission: false
- id: G3
  intent: existing Worker suite and typecheck stay green; INV5 scope discipline holds
  must_assert: make worker-typecheck and make worker-test exit 0, and INV5 holds (git
    diff subset of scope.in, no forbid path); exit nonzero otherwise
  command: TODO
  requires_permission: false
- id: G4
  intent: 'end-to-end on staging: a real search updates access metadata without read-path
    errors (network, staging only)'
  must_assert: against the staging Worker, a search hit is followed by a read showing
    updated last_accessed/access_count; production is not targeted; exit nonzero on
    mismatch
  command: TODO
  requires_permission: true
review:
  mode: required
  command: TODO
  sees: &id001
  - diff
  - invariants
  - scope
budget:
  max_turns: 20
  max_consecutive_failures: 3
  preflight_estimate: required
kill:
  after_turns: 8
graduate: G1 through G3 exit 0, review verdict is pass, no scope.forbid path touched
scale: graduated AND G4 confirms staging behavior AND after 7 nights of production
  traffic the access-write rate is nonzero with zero read-path errors in Worker logs
ledger:
  turns: 0
  consecutive_failures: 0
  blockers: []
  lessons: []
legacy:
  goal_condition: all non-permissioned gates exit 0 AND git diff --name-only is a
    subset of scope.in AND no scope.forbid path is modified
  kill_scale_graduate:
    kill: "INV1 cannot be satisfied after 8 turns (write-back cannot be made fail-open\
      \ without touching the read path) \u2014 stop and report the structural blocker"
    graduate: G1 through G3 exit 0, review verdict is pass, no scope.forbid path touched
    scale: graduated AND G4 confirms staging behavior AND after 7 nights of production
      traffic the access-write rate is nonzero with zero read-path errors in Worker
      logs
  review:
    models:
    - council
    aggregation: worst_verdict_wins
    sees: *id001
---

## Context

Repo root: `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system`.
The production MCP Worker lives in `cloudflare-mcp/mcp-server/` (the root
`mcp-server/` is legacy and forbidden here). Entries live in Upstash Redis;
`computeSalience` (`cloudflare-mcp/mcp-server/src/salience.ts:93-173`) includes a
`retrievalBoost` term `0.15 * 0.5^(daysSinceLastAccessed/60)` read from
`metadata.last_accessed` (`salience.ts:147`).

The defect this contract fixes: nothing on the retrieval path ever writes
`last_accessed` or `access_count`. The `search` tool (`src/index.ts`, ~lines
2547-2760) and `get_context` (~lines 2460-2528) only read; the sole writers of
`last_accessed` are merge helpers (`index.ts:703`, `dream.ts:487`). Live entries
show `access_count: 0` corpus-wide, so the usage-reinforcement term is dead code
and salience is blind to whether a memory is ever used. This is the
highest-uplift-per-risk fix in the 2026-07-07 upgrade spec
(`docs/pks-foundational-upgrade-spec-2026-07-07.md` §2.2 Layer 1, §7.1).

The task: on each entry actually returned by `search`/`get_context`, persist
`last_accessed = now` and `access_count += 1` asynchronously (Cloudflare
`ctx.waitUntil` or equivalent), fail-open, bounded to returned hits only.
`get_context` already schedules a reconsolidation side-effect on its returned
entry — inspect that path first; the right implementation may extend it rather
than add a parallel mechanism. `shared/memory_policy.json` is scope.out: do not
retune weights here; this contract only makes the existing signal live.

## Build Loop vs Product Loop

The build loop is machine-checkable offline: vitest cases prove fail-open
behavior, bounded write amplification, minimal-field writes, and that
`computeSalience` ordering responds to `last_accessed`, plus the existing
`make worker-typecheck` / `make worker-test` staying green. Passing these gates
proves the implementation contract, not the product bet.

The product bet is that usage-driven reinforcement restores real discrimination
to salience over weeks of live traffic — that frequently-used entries separate
from never-used ones and ranking quality improves. That is only observable after
production exposure and the salience-v2 work (PKS-INJECTION-RANKING-002)
consumes the signal. The coding model may not claim the product bet is satisfied
merely because gates pass.

## Verification Narrative

Offline: run `make worker-typecheck` and `make worker-test` from the repo root
and confirm the new cases pass — a throwing-write stub leaves the search
response byte-identical; a query with limit=5 over a 40-candidate pool issues
exactly 5 write-backs; an archived entry in results receives no write; two
otherwise-identical entries order strictly by `last_accessed` under
`computeSalience`. Permissioned: `npm run dev` (or the staging deploy) in
`cloudflare-mcp/mcp-server`, issue one `search` via the MCP endpoint, then fetch
the top hit's entry JSON and confirm `last_accessed` is fresh and `access_count`
incremented, while the search response itself is well-formed. Finally confirm
`git diff --name-only` lists only paths under scope.in and nothing under
scope.forbid.
