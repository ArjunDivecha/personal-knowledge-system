# IBROKEIT — source-first rebuild has been failing promotion since 2026-08-21 04:54Z

**Written:** 2026-08-21, by Claude (Opus 5), the session that caused it.
**Protocol:** per `post-incident-handoff-protocol` memory — the session that caused a break is
the wrong context to trust with the repair. **No fix attempted. Nothing deleted or reverted.**
**File:** `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/IBROKEIT-20260821-source-first-rebuild.md`

---

## Bottom line

I created `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/beam-eval/README.md`,
a long document about **eval discipline for changing PKS retrieval**. That is almost verbatim
the query of the `esave_eval_discipline` probe. Two chunks of it now occupy top-5 slots for
that probe and pushed the expected strings out. The probe fails, the promotion gate refuses,
and **PKS has not taken a new generation in ~15.5 hours.**

**Serving is NOT corrupted.** The gate worked exactly as designed: build → stage → verify →
probe → refuse to promote. The live generation `sf_20260821T031653Z` is the last good one,
health is green, and search is correct. This is a *freshness* outage, not a correctness one.

---

## What is broken

| | |
|:--|:--|
| Workflow | `Source-First Memory Rebuild` (`.github/workflows/source-first-rebuild.yml`) |
| Failing step | `Run exact production retrieval policy against candidate` |
| Failing probe | `esave_eval_discipline` — **1 of 52**, the other 51 pass |
| Consecutive failures | **7** — runs `32448607712`, `32457306098`, `32465690652`, `32474257745`, `32485528504`, `32494616852`, `32505104300` |
| First failure | 2026-08-21 **04:54:39Z** |
| Last success | 2026-08-21 **03:16:34Z** → generation `sf_20260821T031653Z` (still serving) |
| Live generation age | ~15.5h against a 2-hourly SLA |

### How urgent

Not an emergency, but on a clock. `verify_current` runs with `--max-age-hours 36`, and the
serving generation published at `2026-08-21T03:23:04Z`. **Health flips to stale/red at about
2026-08-22 15:23Z.** Until then the only cost is that memory is frozen — ~15h of recent Claude
Code / Codex sessions and any source edits since 03:16Z are not retrievable.

---

## Root cause — mine, unambiguously

The probe:

```json
{
  "id": "esave_eval_discipline",
  "query": "what is my eval discipline for changing PKS retrieval",
  "expect_any_of": ["run_eval.py", "before/after"],
  "min_rank": 5
}
```

**The probe's own notes already warned this would happen:**

> FRAGILITY WARNING: measured against sf_20260813T094909Z, 'run_eval.py' hits just 1/5 top
> chunks and 'before/after' hits 0/5 (though it is live in 28 corpus chunks), so this probe is
> **one ranking shift from failing and blocking promotion**.

I supplied the ranking shift. Top-5 for that probe in the **first** failing candidate
(`sf_20260821T045500Z`, 04:55Z):

| Rank | Source |
|---:|:--|
| 1 | **`A Working/Memory/beam-eval/README.md`** ← mine |
| 2 | `A Working/Memory/PRD-eval-baseline-v1.md` |
| 3 | `A Working/Memory/knowledge-system/ARJUN.md` |
| 4 | `A Working/Memory/knowledge-system/runs/20260814T2325Z_pks_deep_dive/PKS_DEEP_DIVE_REPORT.md` |
| 5 | **`A Working/Memory/beam-eval/README.md`** ← mine (second chunk) |

And in the **latest** failing candidate (`sf_20260821T165153Z`, 16:51Z), rank 1 is still
`beam-eval/README.md` at `final_score` 0.7318. **None of the five contains `run_eval.py` or
`before/after`.**

My README is a genuinely strong semantic match for that query — it is a document about
running an eval baseline before changing PKS retrieval, containing "eval", "baseline",
"retrieval", "PKS", "ranking", "before"/"after" as table headers. It is not spam; it is
exactly what the query asks for. It simply does not contain the two literal strings the probe
greps for.

### What did NOT cause it

- **Not my `sourceFirst.ts` ranking fixes.** Those are committed **locally only** — `git status`
  shows `main...origin/main [ahead 3]`, and CI checks out `origin/main` at `d97c42e`
  (2026-08-13). The failing runs execute the OLD ranking code. Verified independently: I ran
  the full 52-probe suite with the NEW code against the live generation and got **52/52 PASS**.
- **Not the Worker deploy** (07:27Z) — failures began 04:54Z, over 2.5 hours earlier.
- **Not infrastructure.** Build, staging, and storage verification all pass every run; only the
  retrieval probe step fails.

---

## Why I am not fixing it

Every available fix is a judgment call about production ranking policy, and the session that
caused the break is the wrong one to make it. Options, with the trade-off I see:

1. **Exclude `beam-eval/` from the source-first scan** (`shared/source_first_config.json`).
   Fastest, and arguably correct since it is a harness, not knowledge. But it treats the
   symptom — *any* future eval-related document breaks this probe the same way, and the
   corpus is supposed to contain Arjun's real work.
2. **Re-anchor the probe** to strings that live in a durable, authoritative document rather
   than ones that happen to be scattered across 28 chunks. Addresses the documented fragility.
   Risk: re-anchoring a canary is one step from weakening it, and this probe exists
   specifically to watch whether the highest-authority context class gets weakened (June
   review C4).
3. **Make `run_eval.py` / `before/after` appear in a top-ranking authoritative doc** — e.g. add
   an explicit eval-discipline section to `ARJUN.md` (already rank 3). Fixes the probe honestly
   without weakening it, but is a content change to a load-bearing file.
4. **Disable the probe.** Worst option. It is the only enabled `explicit_save` probe, and
   `esave_class_empty_note` records that the class was empty before this canary existed.

My read, offered as input and not as a decision: this is **primarily a probe-design problem**
that my files triggered rather than caused. A probe that greps for two literal strings, whose
own notes say one string hits 0/5, is not a stable promotion gate — it will keep firing every
time Arjun writes something new about evals. Option 3 or 2, or both, look better than 1.
But that is Arjun's call, or a fresh agent's after review.

---

## Reproduce / verify

```bash
cd "/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system"

# Failure history
gh run list --workflow=source-first-rebuild.yml --limit 10

# The failing probe's actual top-5, from CI's own artifact
gh run download 32505104300 -R ArjunDivecha/personal-knowledge-system
#   -> source_first/candidate-eval-sf_20260821T165153Z.json

# Re-run the gate by hand against any staged candidate
cd cloudflare-mcp/mcp-server
npm run test:source-first-candidate -- sf_20260821T165153Z /tmp/probes.json

# Confirm live serving is healthy and correct
curl -s https://mcp.dancing-ganesh.com/health | python3 -m json.tool
```

Staged-but-unpromoted candidates from every failed run are still in production Upstash (the
pruner only runs inside `promote_generation`, which never fired). They are safe to inspect and
should be cleaned up once the gate is green again.

---

## Also in this session, for context

Unrelated to the break, and all verified before it mattered:

- **PKS commits `2e2a25d` and `31c535f`** fix defect #1 (`exact_identifier_count` misclassifying
  common words, then outranking `final_score`). **Local only — not pushed.** Worker version
  `ae073acc-dc50-4691-932f-c4bd3199f646` deployed 07:27Z and verified live.
- **Important interaction:** because the fixes are deployed to the Worker but NOT pushed to
  GitHub, CI is gating candidates with the *old* ranking while the *new* ranking serves. That
  divergence should be closed on its own merits.
  **Tested, so nobody wastes time on it:** running the NEW ranking against the exact candidate
  CI rejected (`sf_20260821T165153Z`) still fails the same probe — **51/52, `esave_eval_discipline`
  still red.** Pushing the commits does NOT fix this break. The two issues are independent.
- Measured effect of the fixes over 400 BEAM questions: floor-bypass 0.090 → 0.000,
  relevance-outranking 0.058 → 0.003. Harness and reports:
  `/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/beam-eval/`
- 24 `beam_*` generations remain staged in the **staging** Upstash (never production).
