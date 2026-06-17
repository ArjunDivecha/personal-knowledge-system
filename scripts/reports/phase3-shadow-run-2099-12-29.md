# Phase 3 — Manual Shadow Run Evidence (2099-12-29)

Generated: 2026-06-17T04:55:13.388Z

## Verdict: PASS — all acceptance criteria met; zero active-memory mutation.

## Check A — Synthetic Shadow Proof

- Orchestrator status: **completed_with_holds** (resume exit 0)
- First attempt failed (exit 1) on Cloudflare 1010; fixed UA, then `resume` → exit 0.
- dream_run_id: `dga_20991229_3caaf64c` (reattached on resume — no second run)
- Dream terminal: **completed_shadow**, executed_mode **shadow**, applied_count **0**, held 12
- Report: `scripts/reports/pks-nightly-2099-12-29.json` (complete=True, verdict=completed_with_holds)
- Redis mirror: pks:orchestrator:run:2099-12-29 (present)
- active-memory mutation detected: **False**

| stage | status |
|---|---|
| CONSISTENCY_VERIFY | completed |
| DONE | completed |
| DREAM_START | completed |
| DREAM_VERIFY | completed_with_holds |
| DREAM_WAIT | completed |
| INDEX_VERIFY | completed |
| INGEST_AGENT_SESSIONS | completed |
| INGEST_GITHUB | completed |
| INGEST_TWITTER | completed |
| INIT | completed |
| LOCKED | completed |
| NOTIFY | completed |
| PREFLIGHT | completed |
| REPORT_WRITE | completed |
| SNAPSHOT_BEFORE | completed |
| VALIDATE_AGENT_SESSIONS | completed |
| VALIDATE_GITHUB | completed |
| VALIDATE_TWITTER | completed |

### thin_index before -> after (unchanged = no mutation)

| key | before | after |
|---|---|---|
| stored_topic_count | 100 | 100 |
| stored_project_count | 26 | 26 |
| total_topic_count | 6061 | 6061 |
| total_project_count | 26 | 26 |
| archived_count | 6602 | 6602 |
| tier_1_count | 921 | 921 |
| tier_2_count | 4177 | 4177 |
| tier_3_count | 989 | 989 |

## Check B — Comparison vs latest old nightly

- Old nightly Dream (`dga_2026-06-16T16-25-06-235Z`): status completed_with_holds, **applied_count 60** (governed live).
- Phase 3 Dream (`dga_20991229_3caaf64c`): status completed_with_holds, **applied_count 0** (shadow).

### Expected differences
- Phase 3 ingestion saved counts are 0 (shadow no-ops); old nightly ingests for real.
- Phase 3 Dream is async Worker-backed and SHADOW: applied_count=0, executed_mode=shadow; old nightly is governed live apply (applied_count=60).
- Phase 3 created a fresh Worker proposal/grade/status artifact, but applied_count stayed 0.
- Dream id format differs: new caller-supplied dga_YYYYMMDD_<8hex> vs old synchronous dga_<ISO timestamp>.
- Held counts differ (12 vs 22): different proposal computed at a different time; the PRD does not require count parity.

### Unexpected differences
- NONE

## Bugs surfaced and fixed by this phase
- engine.py: dream/orchestrator ids were built from today's date, not run_date; a far-future date would fail the Worker run_date-vs-id validation. Fixed to date.fromisoformat(run_date). Regression test added.
- dream.py HttpDreamClient: urllib default User-Agent is banned by Cloudflare bot-management (edge error 1010, HTTP 403) before the Worker runs. Added a non-default User-Agent header. Regression test added.

## Lingering synthetic-date keys (harmless, TTL-bounded)
- pks:orchestrator:lock/fence/run:2099-12-29 (orchestrator ledger; harmless, far-future)
- dream:scheduled-governed:date-lock:2099-12-29 (36h TTL)
- dream:scheduled-governed:status:dga_20991229_3caaf64c (14d TTL)
- Worker proposal/grade artifacts for the shadow run (no mutation applied).

No launchd/GitHub/Cloudflare scheduler state modified. Old nightly remains production source of truth.
