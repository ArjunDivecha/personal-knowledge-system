<!--
=============================================================================
FILE: docs/harness-checklist.md
INPUT FILES: none. OUTPUT FILES: none. This is a review checklist.
=============================================================================
-->

# Harness checklist — two gates for maintenance loops and "done" claims

A half-page working distillation of two ideas from Ryan Lopopolo's
**harness-engineering** anthology (https://github.com/lopopolo/harness-engineering,
CC BY 4.0), adapted to PKS with our own examples. Read it before writing a
maintenance-loop contract or claiming a change is done. The full anthology is
worth reading once; this is the part we reuse.

Both checklists earned their place the hard way: the 4-day nightly-Dream outage
(`IBROKEIT.md`) and the stall that followed it (`NIGHTLY-STALL.md`) each failed a
line below.

---

## Checklist A — is this a viable maintenance loop?

Any recurring, autonomous job (nightly semantic maintenance, ingestion,
retier/forgetting, a Dream governance pass) must be able to answer all five.
A missing answer means the work is investigation or a proposal, **not** a loop.
These are the same five facts a divecha contract encodes (invariants / gates /
`requires_permission` / ledger) — if you can't answer them, the contract isn't
build-ready.

1. **Invariant** — what condition must stay true? (e.g. "duplicate share < 0.2";
   "every active entry has a vector"; "verify-memory-full = 0 issues".)
2. **Drift signal** — what observable shows the system has departed from it, and
   is it actually wired to fire? (`audit_memory_quality` M4 share; the retrieval
   regression gate; a `verify` count.)
3. **Restoration proof** — what evidence proves a proposed change restored the
   invariant, gathered where it's experienced? (See Checklist B.)
4. **Authority boundary** — which operations may proceed autonomously and which
   need a human? Be explicit about the *safe* outcomes too. (Merges auto-apply
   under conservation gates; protected-type losers are **held**; a raise past a
   cap needs sign-off.) ← *`NIGHTLY-STALL.md` BUG 2 lives here: an all-held night
   is a **safe** autonomous outcome, but the loop treated it as a hard failure.
   If Q4 had named "all-held" as a legitimate result, that bug wouldn't ship.*
5. **Durable state + retirement** — what records the result for the next
   iteration, and what condition ends the loop? (The contract ledger; the
   semantic cursor; the checkpoint.) A loop with no retirement condition is a
   standing liability.

> Corollary (the outage lesson): a maintenance loop whose **cost scales with the
> corpus** is a time bomb, not a feature. Add a sensor that asserts the cost
> (subrequests, keys read, bytes loaded), not just the result — see
> `test/dreamProposalScoping.test.ts`. Every unit test here runs on a handful of
> fixture entries and cannot catch a size-driven failure.

---

## Checklist B — does the evidence match the claim?

"Done" means evidence of the promised result **in the environment where it will
be relied on**, and evidence that distinguishes success from a plausible
imitation (this is FAIL-IS-FAIL, restated). Map each green check to the claim it
actually makes; gather proof at that boundary. This is the bar for a divecha
`must_assert` and for any "verified" statement in a ledger.

| Claim | Evidence at the claim boundary |
|---|---|
| Worker deploy | the validated version running, plus a live post-deploy health/search check — not "wrangler uploaded it" |
| consequential production mutation (sweep, cap raise, live-flag flip) | staged on the distinct staging instance → canary → cutover → post-cutover verify → and it's reversible |
| data change (merge, archive, transition) | source-to-result reconciliation + `verify-memory-full` = 0, **and** a named rollback path |
| retrieval/ranking change | a real probe run against production compared to a committed baseline, not a passing unit test |
| a bounded/optimized job | an asserted **cost** measurement with headroom, not just a green result on fixture data |

Traps this table exists to stop:
- Unit tests / typecheck prove *internal* properties only. 327/327 green did not
  stop the outage — the bug was purely a function of corpus size.
- "Shipped but disabled" is not done. Name it as such in the ledger
  (`SEMANTIC_SLICE_SIZE=0` did this honestly).
- A claim inferred from a timeout is not evidence. Read the actual error (the
  outage root cause — "Too many subrequests" — only surfaced once the real 500
  body was read, not the client timeout).

---

*Attribution: theses "Run known work as a continuous loop" and "Prove the
outcome in the real environment" from lopopolo/harness-engineering (CC BY 4.0).
Examples and the divecha mapping are ours.*
