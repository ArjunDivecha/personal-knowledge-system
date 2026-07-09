# Commission: A Foundational Upgrade to the PKS / Dream Engine

## Why this, why you, why tonight

You are the most capable model I will have access to for a while, and I'm spending you deliberately. The target is the system that feeds every *other* session I run: my personal knowledge system (PKS) and its "Dream" reconsolidation engine. Every improvement here compounds — a sharper injection policy or a safer consolidation gate makes all my future work with cheaper models better, quietly, forever.

So don't spend yourself on things a cheaper model can do. Parameter tuning, decay-curve fiddling, and boilerplate are not why you're here. You're here for the **judgment-heavy, safety-gating, design-once problems**: the ranking function that decides what I see, the grading rubric that decides what's safe to merge, the contradiction-resolution logic, and — above all — the evaluation methodology that lets everything keep improving without you. Design those well tonight and Haiku/Sonnet can execute and iterate for months.

Think as hard as you can. The failure modes matter more than the features.

## The system, and its current state (verify, don't trust)

The PKS runs on Cloudflare Workers + Upstash (Redis for structured entries, vector for semantic search). It auto-ingests every few hours from Claude Code/Codex JSONL sessions, Gmail, GitHub, and Twitter, distilling them into knowledge entries and project entries. Entries carry a stable id, a canonical view, evidence, evolution history, a context type, a salience score, an injection tier, and a state (active / contested / archived). Retrieval is tier-aware and routed by query intent. "Dream" is a governed reconsolidation lifecycle — Snapshot → Propose → Grade → Apply → Verify → Publish — with a validation ledger and rollback.

I pulled the live index before writing this. Treat the following as **hypotheses to confirm or refute in Phase 0**, not settled facts — but they're where I'd start:

- **Scale:** ~10,500 topics, 26 projects, ~7,600 archived, with **~2,465 entries at injection tier 1**. That is far more than any context budget, so the *ranking within* tier 1 is doing all the real work.
- **Salience discrimination has collapsed.** In the tier-1 sample, a few entries sit at 1.0 and the rest bunch into a narrow band around 0.48–0.49 with many exact ties. The scoring function appears to have gone nearly flat across the majority of the corpus — it can no longer separate signal from noise.
- **Consolidation is falling behind ingestion.** Despite a Dream run within the last day, obvious duplicate clusters survive — e.g. ~4 entries about one file (`categoryMapping.js`), and 5–6 overlapping entries each for "PKS-about-itself architecture" and "ASADO architecture."
- **Contested knowledge is being injected at tier 1.** Multiple entries in `contested` state sit at the top injection tier, unresolved.
- **The project layer is polluted.** One-shot Claude.ai sessions from 2024 are promoted to "active projects" alongside genuinely live work.
- **Observability is minimal** — effectively a weekly digest. There is no measurement of whether a Dream run improved or degraded the store.

## Phase 0 — Ground yourself, then diagnose

Before proposing anything, establish ground truth using the PM read tools and the `ArjunDivecha/personal-knowledge-system` repo:

- Pull the index, the recent Dream run history, and the validation ledger. Read enough real entries — especially several `contested` ones and a couple of the duplicate clusters — to see the actual data, not the schema.
- **Localize the consolidation bottleneck.** This is the key diagnostic: is Dream *not proposing* the obvious merges, *proposing but failing the grade gate*, or *proposing and passing but not applying*? The fix is completely different in each case. Determine which it is from the run history before you design anything.
- Characterize the salience-flatness quantitatively: what is the score actually a function of, and why has it saturated?

Produce a one-page diagnosis. Where I'm wrong above, say so plainly.

## The mission, in priority order

For each, tell me what "good" looks like, the mechanism, and the cheapest way to validate it.

**1. Restore ranking discrimination in the injection layer.** Highest frequency — it runs every session, so it dominates the system's felt quality. Redesign whatever decides *what gets surfaced* so it produces real spread across the corpus and is *query-aware* (what this task needs), *diversity-aware* (no near-duplicate pile-ups), and *budget-aware* (respects a finite context window without diluting attention — context rot is a real cost). Move beyond a single saturated salience scalar and beyond pure vector similarity.

**2. Make Dream's consolidation keep up — safely.** Two halves, and the second is the hard one. (a) Better *proposal generation*: reliable duplicate-cluster detection and merge candidacy, staleness archiving, and the highest-value output of all — *insight extraction*, turning scattered episodic observations into durable higher-order knowledge (this is the gist-formation that a reconsolidation engine exists to do). (b) A **grading rubric that makes a merge provably lossless.** An automated system rewriting a 10k-entry knowledge base is dangerous: reconsolidation is reconstructive, so the failure mode is silent information loss and confabulated "insights." Design the hard-gates that catch a lossy merge, a hallucinated generalization, or a provenance break *before* it's applied, and specify when a change must be routed to me instead of auto-approved.

**3. A first-class contradiction-resolution path.** Contested-at-tier-1 is the worst epistemic state — knowingly injecting inconsistent knowledge. Design how contradictions are detected, adjudicated, and resolved or explicitly flagged. Resolution must not be naive recency: encode a precedence model where *what I stated or decided* outranks *what an assistant suggested*, and a durable decision outranks a passing hypothesis.

**4. The evaluation harness — the crown jewel.** Design a rigorous, mostly-automatic way to measure three things: retrieval/injection quality (is the right context surfaced for a given query?), consolidation safety (did a Dream run preserve information and improve structure, or degrade it?), and end-to-end session uplift (did injected context actually make the session better?). This is the deliverable that matters most, because it is what lets *cheaper models keep improving this system without you* — a proposed change either measurably helps against the harness or it doesn't ship. Make it concrete enough to build: what's the gold set, what are the metrics, what's the pass/fail gate, how does it run in CI or on a schedule.

## The backbone under all of it

Provenance and epistemic hygiene are load-bearing for a system that will feed real decisions. Every claim should carry its source, timestamp, confidence, and — critically — the *Arjun-said vs assistant-suggested vs hypothesis* distinction. The contradiction logic, the merge gates, and the injection ranking should all read from this backbone. If it isn't first-class today, designing it in is part of the job.

Optional but encouraged: mine memory science for principles. Biological systems consolidation extracts schemas from episodic traces during sleep, treats interference and *forgetting as adaptive features* (relevant to your saturation problem), and is lossy and reconstructive (relevant to your safety gates). It's a good design lens for a system literally named "Dream."

## Scope boundary

I've centered this on reconsolidation, salience/injection, contradiction handling, and evaluation — the core you feed every session from. Ingestion-quality and extraction are adjacent; touch them only where they directly bear on the above.

## Safety constraints (non-negotiable)

- **Do not mutate the live store.** You have write tools (`apply_dream_proposal`, `consolidate_entries`, `update_entry`, `archive_entry`); do not call them. The deliverable is a design, not an applied change.
- At most, generate *dry-run, no-write* Dream proposals for my review to illustrate a point.
- Every design you propose must be reversible, must preserve provenance, and must never silently drop information.

## Definition of done

A single written upgrade spec — save it as markdown I can drop into the repo — containing: (1) the Phase 0 diagnosis, including where the consolidation bottleneck actually is; (2) the redesigned injection/ranking policy; (3) the consolidation redesign with its lossless-merge grading rubric; (4) the contradiction-resolution model; (5) the evaluation-harness specification, concrete enough to hand to a cheaper model to build; (6) a safe migration/rollout plan for the existing ~10k-entry live corpus, staged and reversible.

End with the **three changes with the highest expected uplift-per-unit-risk**, and exactly why each earns its place.
