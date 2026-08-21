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

---

# RESOLUTION (appended 2026-08-21, after Arjun said "fix it")

The handoff above was overridden by explicit instruction. History left intact; this section
records what was actually done.

**Two independent causes, not one.** Fixing the first exposed a second.

## 1. `esave_eval_discipline` — mine

The remedy was **none of the four options listed above** — a fifth, better one. Diagnosis
first: `run_eval.py` *does* live in `ARJUN.md` (3 mentions) and both it and
`PRD-eval-baseline-v1.md` still ranked (#3 and #2). But the **chunks** that ranked were
chunk 1 of ARJUN.md and chunk 1 of the PRD — and every `run_eval.py` mention is in chunk 0.
The probe greps chunk text, so the right documents ranked with the wrong halves. It had been
relying on incidental scattered mentions surviving chunk boundaries.

**Fix:** `shared/source_first_curated_memory.json` was empty, but the mechanism exists and is
the post-cutover equivalent of the `explicit_save` class this probe was built to watch —
`authority 1.0`, `pinned`, and **unchunked**, so anchors cannot be split. Wrote the eval
discipline down as a first-class curated entry. It is real, previously undocumented content,
not test-gaming. Now ranks **#1 at 0.8565**, and the probe passes under **both** the old and
new ranking.

## 2. `neg_quicksort` — not mine, pre-existing

With the first fix in, a *different* probe failed: an unrelated **Prediction Markets** Claude
Code session from 18:55Z (created after CI's last attempt, so it could not have shown up
earlier) was returned for *"explain how quicksort works"*.

```
similarity 0.5846   lexical 0.6667   authority 0.7   recency 1.0
base_score  0.6292  <- BELOW the 0.65 floor
wc_bonus   +0.0467
FINAL       0.6759  <- admitted
```

`docs/source-first-memory.md` claimed the lift *"cannot rescue unrelated session text because
it is multiplied by semantic relevance."* Multiplying bounds the lift's **size**; it does not
stop the lift being what carries a below-floor record over the line. The code contradicted its
own documented contract.

**Fix:** apply the relevance floor to `base_score`, not `final_score`. Attention reorders what
already qualifies; it never admits. A literal no-op for authoritative evidence
(`working_context_bonus` is 0, so `base == final`) — it bites only on session text, **6.8% of
the corpus**. Doc corrected to match the code.

## Measured cost of fix 2

BEAM, 400 questions, like-for-like (`runs/20260821T2000Z_fix3/COMPARISON.md`):

| Metric | before | after | McNemar p |
|:--|--:|--:|--:|
| Correct behavior | 0.880 | 0.860 | 0.0078 |
| Evidence support (proxy) | 0.366 | 0.361 | 0.73 |

8 questions newly abstain, 0 improve. All 8 were marginal — top-1 between 0.656 and 0.701,
admitted purely by the lift. **This is the worst case by construction:** BEAM's rebase arm is
100% `working_context`, so the lift uniformly inflated everything and the effective floor was
~0.57. Production is 6.8% `working_context`. BEAM measures only the downside here — the upside
(unrelated session text no longer leaking into negative queries) is what the PKS negative
probes measure, and `neg_quicksort` now passes.

## Verification

- Staged candidate `sf_20260821T193745Z`: **52/52 probes** (was 51/52 under both rankings)
- Worker suite **366/366**; three regression tests added, each verified failing first
- Worker `1943b265-5f33-4184-b505-66b2a0e2fe6f` deployed
- Pushed `d97c42e..3d24054` to `origin/main` (with Arjun's explicit approval) so CI gates with
  the same code that serves — that divergence was itself a hazard
- CI `workflow_dispatch` run triggered to prove stage → gate → promote end to end

## Left alone, deliberately

- The staged-but-unpromoted candidates from the 7 failed runs (`sf_20260821T045500Z` and
  siblings) are still in production Upstash. Safe to clean up; **not deleted** — they are the
  evidence behind this document, and deletion is Arjun's call.
- **Unshipped follow-up:** the lexical score of 0.6667 in the `neg_quicksort` case came from
  the generic tokens `explain` and `works`, neither of which is in `TOKEN_STOP_WORDS` even
  though `about`/`how`/`what`/`tell`/`please` are. Treating generic instruction verbs as
  content inflates lexical overlap corpus-wide. Fixing that might recover part of the 8-question
  cost above. Not done here: it is a second ranking change, and rule 2 of the eval discipline
  now in curated memory is *change one thing at a time*.
