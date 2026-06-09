# Nightly Ingestion Health Report

Generated: 2026-06-09T17:08:09Z

## Overall: ⚠️ WARN

## Storage deltas (after − before)

| Metric | Before | After | Δ |
| --- | ---: | ---: | ---: |
| Knowledge entries | 10801 | 10968 | 167 |
| Project entries | 54 | 54 | 0 |
| Vectors | 4705 | 4878 | 173 |
| Twitter sources | 2661 | 2661 | 0 |
| GitHub sources | 56 | 56 | 0 |

## Pipelines

| Pipeline | Status | Notes |
| --- | --- | --- |
| twitter | ✅ PASS | ran clean, data changed |
| github | ⚠️ WARN | ran cleanly but no new github sources this run (OK if nothing pushed) |
| agent_sessions | ✅ PASS | last_run total_saved=41 redis_write_failed=False |
| dream_judge | ✅ PASS | no before/after count available |

## Run log

- Started: True  ·  Completed: True
- Error lines: 0
- Browser/OAuth storm hits in log: 0 (must be 0)

## Success marker

```json
{
  "completed_at": "2026-06-09T17:07:35Z",
  "log": "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/nightly/2026-06-09.log",
  "sdk_model": "sonnet",
  "api_fallback": "0",
  "dream_api_fallback": "0",
  "agent_session_status_file": "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/checkpoints/agent_sessions_last_run.json",
  "agent_session_redis_write_failed": false,
  "ok": true,
  "stages": {
    "Twitter ingestion": 0,
    "GitHub ingestion": 0,
    "Agent sessions ingestion": 0,
    "Dream judge": 0
  },
  "failed_stages": []
}
```
