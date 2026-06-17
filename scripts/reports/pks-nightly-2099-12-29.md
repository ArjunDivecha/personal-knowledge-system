# PKS Nightly — 2099-12-29  (shadow mode)

- Verdict: **completed_with_holds**
- Orchestrator run: `pksn_20991229_215107_3caaf64c`  (fence 2)
- Started: 2026-06-16T21:51:08-07:00  | Updated: 2026-06-16T21:54:35-07:00  | Completed: 2026-06-16T21:54:35-07:00

## Dream
- run id: `dga_20991229_3caaf64c`
- requested_mode: shadow  | executed_mode: shadow  | applied_count: 0

## Held ops: DREAM_VERIFY

## Ingestion deltas

| stage | saved | material | explained |
|---|---|---|---|
| INGEST_TWITTER | 0 | False | True |
| INGEST_GITHUB | 0 | False | True |
| INGEST_AGENT_SESSIONS | 0 | False | True |

## Stages

| stage | status | attempt |
|---|---|---|
| INIT | completed | 1 |
| LOCKED | completed | 1 |
| PREFLIGHT | completed | 0 |
| SNAPSHOT_BEFORE | completed | 0 |
| INGEST_TWITTER | completed | 0 |
| VALIDATE_TWITTER | completed | 0 |
| INGEST_GITHUB | completed | 0 |
| VALIDATE_GITHUB | completed | 0 |
| INGEST_AGENT_SESSIONS | completed | 0 |
| VALIDATE_AGENT_SESSIONS | completed | 0 |
| DREAM_START | completed | 0 |
| DREAM_WAIT | completed | 0 |
| DREAM_VERIFY | completed_with_holds | 0 |
| INDEX_VERIFY | completed | 0 |
| CONSISTENCY_VERIFY | completed | 0 |
| REPORT_WRITE | completed | 0 |
| NOTIFY | completed | 0 |
| DONE | completed | 0 |
