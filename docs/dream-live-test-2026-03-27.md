# Dream Live Test: March 27, 2026

This document records the first controlled live test of the Phase 5 Dream archive layer.

## Goal

Validate that Dream can:

- identify real archive candidates in the live memory store
- archive a bounded batch without disturbing the canonical nightly dry-run state
- create reversible archive snapshots plus `:latest` pointers
- restore the archived entries back to active memory
- leave the system consistent afterward

## Setup

- Branch: `Dream`
- Worker deploy used for the live test: `e367552e-189c-4d09-bd12-8e62d6fa00fe`
- Backup taken before mutation:
  - [distillation/backups/20260327_130558](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/distillation/backups/20260327_130558)
- Test harness:
  - [test-dream-live.ts](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/cloudflare-mcp/mcp-server/scripts/test-dream-live.ts)
- Operator endpoints used for the test:
  - `POST /ops/dream/run`
  - `POST /ops/dream/restore`
- Operator auth:
  - bearer token in Worker secret `DREAM_OPERATOR_TOKEN`
- Full JSON report:
  - [dream_live_test_2026-03-27T20-12-12-686Z.json](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/reports/dream_live_test_2026-03-27T20-12-12-686Z.json)

## Why The Test Runs Through The Worker

The first local harness attempt exposed an environment mismatch: local Node on this machine could reach Redis but could not reliably resolve the Upstash Vector hostname. Dream writes both Redis state and vector metadata, so a workstation-local mutation path was not a trustworthy production-shaped test.

The fix was to run the archive and restore calls through the deployed Worker, where Vector connectivity is already proven, and use local Redis reads only for inspection and documentation.

## Test Procedure

1. Read `/health` from both live endpoints and record baseline state.
2. Run a preflight Dream dry run through `POST /ops/dream/run` with `set_as_latest=false`.
3. Select three clean archive candidates:
   - no existing archive pointer
   - active entry
   - currently surfaced by Dream as archive candidates
4. Capture each selected entry's Redis state before mutation.
5. Run a bounded non-dry-run Dream archive pass against only those three ids, again with `set_as_latest=false`.
6. Capture each entry after archive.
7. Restore each entry with `POST /ops/dream/restore`.
8. Capture each entry after restore.
9. Run a post-restore dry run against the same ids and confirm they are no longer archive candidates.
10. Run a full post-test consistency verifier.

## Selected Entries

The controlled batch was:

- `ke_07f75621ebc0` — `AI-driven drug discovery pipelines`
- `ke_08ac41c8ac91` — `Network device discovery and troubleshooting`
- `ke_12c0a4f9b649` — `Python virtual environment recovery`

## Before / After

### Before archive

| Entry | Context type | Tier | Archived |
| --- | --- | --- | --- |
| `ke_07f75621ebc0` | `passing_reference` | `3` | `false` |
| `ke_08ac41c8ac91` | `task_query` | `3` | `false` |
| `ke_12c0a4f9b649` | `task_query` | `3` | `false` |

All three were clean candidates: active, unarchived, and without pre-existing `archived:*:latest` pointers.

### After archive

The live archive run id was `dr_2026-03-27T20-11-48-170Z`.

| Entry | Context type | Tier | Archived | Archived at |
| --- | --- | --- | --- | --- |
| `ke_07f75621ebc0` | `passing_reference` | `3` | `true` | `2026-03-27T20:11:48.170Z` |
| `ke_08ac41c8ac91` | `task_query` | `3` | `true` | `2026-03-27T20:11:48.170Z` |
| `ke_12c0a4f9b649` | `task_query` | `3` | `true` | `2026-03-27T20:11:48.170Z` |

For each entry, the test verified:

- the active Redis record was marked `archived=true`
- a timestamped snapshot key was created
- an `archived:{type}:{id}:latest` pointer was created

### After restore

| Entry | Context type | Tier | Archived | Restored at |
| --- | --- | --- | --- | --- |
| `ke_07f75621ebc0` | `explicit_save` | `1` | `false` | `2026-03-27T20:11:53.764Z` |
| `ke_08ac41c8ac91` | `explicit_save` | `1` | `false` | `2026-03-27T20:11:54.072Z` |
| `ke_12c0a4f9b649` | `explicit_save` | `1` | `false` | `2026-03-27T20:11:54.341Z` |

Restore preserved the `:latest` archive pointer while returning each entry to active memory.

## What Worked

The harness reported `status: passed` with all assertions green:

- selected candidate count
- clean baseline state
- live archive count matches selection
- after-archive state is reversible
- restore completed for all entries
- restored entries return to active Tier 1 state
- post-restore candidates are no longer immediately prunable
- `dream:last_run` remained unchanged

The test also confirmed that the public nightly dry-run state was not disturbed:

- baseline `dream:last_run`: `dr_2026-03-27T08-06-49-267Z`
- final `dream:last_run`: `dr_2026-03-27T08-06-49-267Z`

Both `/health` endpoints remained stable before and after:

- schema version `2`
- migration complete flag present
- pending classifications `0`
- archived count in thin index `0`
- latest nightly Dream dry run still reporting `83` archive candidates

## Intermediate Cleanup

The first full post-test verifier did **not** fail on the three test entries. It surfaced four unrelated Redis/vector metadata mismatches, including one older single-entry restore sample and one Phase 4 promotion case:

- `ke_061246a8a733`
- `ke_0c2508065679`
- `ke_58a8ec30e134`
- `ke_9fdbd7d0bd21`

Those were resynced with:

- [backfill_counts_2026-03-27T201426+0000.json](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/reports/backfill_counts_2026-03-27T201426+0000.json)

That cleanup updated four vector metadata rows and did not change the Dream test results.

## Final Verification

The final full consistency check passed cleanly:

- [verify_memory_consistency_2026-03-27T201453+0000.json](/Users/arjundivecha/Dropbox/AAA%20Backup/A%20Working/Memory/knowledge-system/scripts/reports/verify_memory_consistency_2026-03-27T201453+0000.json)

Result:

- checked entries: `609`
- issues: `0`

## Conclusion

The Dream archive layer is now tested in a production-shaped way for a bounded batch.

What is proven:

- live Dream can archive selected low-salience entries
- archive writes are reversible
- restore returns entries to active memory with explicit-save semantics
- nightly dry-run state can be preserved while operator-triggered test runs execute
- the store can be brought back to a clean verified state afterward

What is not yet proven:

- a full live non-dry-run Dream pass across the whole archive candidate set
- replay-heavy Dream logic such as dedupe, contradiction handling, or broader consolidation
- public operator tooling through MCP with scope enforcement
