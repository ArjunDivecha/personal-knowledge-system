# ARJUN.md — knowledge-system (PKS)

*From Fable 5, 2026-07-06. Read time ~4 min. Ranked by value-to-you ÷ effort.*

## What this repo is worth

**Alive and load-bearing — do not touch its status.** This is the `personal-knowledge`
MCP server that every other session (including this repo-review run) reaches into. It is
the most infrastructurally central thing you own: it is not superseded by anything, and
nothing else in the ecosystem replaces it. The engineering is genuinely mature — the whole
first half of 2026 was reliability and data-loss work, and it shows. The one thing that
undercuts all of it: **the system decides what your agents remember, but nothing guarantees
that memory keeps getting *better* instead of quietly degrading.** You measure retrieval
quality now (`run_eval.py`, added today) but you don't *enforce* it. Fix that and PKS goes
from "probably fine" to "provably not regressing." Everything below assumes you keep this
as core infra and invest in *trust* and *ingestion breadth*, not features.

## Extensions ranked by value

1. **Turn the eval into a trust guarantee (the P0 in FABLE.md).** Commit a retrieval
   baseline and make `run_eval.py --compare` fail on regression, wired into `make` and the
   nightly orchestrator. *Why now:* you just built the measurement; without the gate it's a
   thermometer nobody reads. *First step:* hand the validated contract in `FABLE.md` §3 to
   Codex in Build Mode. *Reuse:* `scripts/run_eval.py`, `tests/probes/*`, the Makefile,
   `orchestrator/` for nightly wiring.

2. **A decision-rationale journal, auto-ingested from your quant repos.** You are a GMO
   veteran — the expensive thing to lose is *why* a trade or factor change was made, not the
   number. `ingestion/github/run.py` already pulls commit messages and code comments; point
   it at **T2 Factor Timing Fuzzy**, **QScreen**, **Fable Daily Trading**, **Market Top**,
   **200-week-quality**, and let PKS distill "why did I change this" into durable, searchable
   memory. *Why now:* it compounds — every month you don't capture rationale is gone. *First
   step:* add those repo names to the GitHub ingestion config and run a dry-run. *Reuse:*
   `ingestion/github/`, the existing extractor, `.pks/agent-context/` hook.

3. **Wire PKS into the book.** **book-ghostwriter** writes in your voice; PKS *holds* your
   voice and views. Let the ghostwriter call `search`/`get_context` so chapters draw on the
   real corpus of what you've actually said across years of conversations, not just the
   current voice memo. *Why now:* the book is only as good as the material it can reach.
   *First step:* add a retrieval step to the book-ghostwriter skill that pulls the top
   memories for the chapter topic. *Reuse:* PKS `search`/`get_context` tools, book-ghostwriter.

4. **Ingest the research you already consume.** **SemiAnalysis**, **News**, **NightWatch**,
   **GDELT** produce material you read and forget. A thin ingestion source that distills
   their outputs into PKS makes your reading durable and cross-queryable against your own
   views (e.g. "what did SemiAnalysis say that contradicts my thesis on X"). *First step:*
   clone `ingestion/twitter/run.py` as a template for one new source. *Reuse:* `ingestion/core/`.

5. **Proactive "your thinking changed" surfacing.** PKS already tracks supersession and runs
   Dream nightly (`generate_weekly_dream_digest.py` exists). Extend the digest to actively
   email you "you used to believe X, the evidence now says Y." *Why now:* the machinery is
   built; you're one report-formatter away from it being useful. *First step:* extend the
   weekly digest script to email via your existing Gmail path. *Reuse:* `scripts/generate_weekly_dream_digest.py`, Dream supersession data.

## Quick wins (< 1 hour, outsized payoff)

- **Commit a baseline eval report and add a `make eval-gate` target.** Half of P0, and it
  immediately gives you a "did retrieval get worse" one-liner.
- **Flip more probes to `enabled: true`.** `recall.json` has 20 probes but several axes run
  thin; `negative.json` and `paraphrase.json` are all enabled but `explicit_save`/`supersession`
  have only 1-2. Wider probes = a baseline that actually catches drift. Pure JSON edits.
- **Delete the stray `.env.bak_2026_07_05`** (gitignored, but it's clutter with real secrets
  sitting in the working tree). Your call — I did not delete it.

## What NOT to do

- **Don't resurrect the legacy `mcp-server/` (Vercel).** It embeds at 1536 dims vs
  production's 3072 — porting it to parity is effort spent on a path you don't use and that
  can corrupt your live index if deployed. Either quarantine it under `legacy/` or kill it;
  either way, stop maintaining two servers.
- **Don't build a dashboard/UI for PKS.** It's an MCP server consumed by agents — a front-end
  is a shiny distraction. Your marginal hour is worth far more spent on ingestion breadth
  (idea 2/4) and retrieval trust (idea 1) than on making memories pretty to look at.
