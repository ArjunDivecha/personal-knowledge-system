"""
=============================================================================
SCRIPT NAME: run_eval.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/tests/probes/*.json
  : probe suite, one file per axis (see tests/probes/README.md for schema)
- (network) the production or staging PKS MCP server (default
  https://mcp.dancing-ganesh.com) — read-only `search` tool calls only

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/eval_baseline_<UTCSTAMP>.json
  : full report — per-probe results (retrieved ids/ranks/scores, pass/fail,
    leak details) + aggregate metrics per axis + token-cost estimates
- stdout: human-readable summary table

VERSION: 1.0   LAST UPDATED: 2026-07-06   AUTHOR: Claude (Fable 5) for Arjun Divecha

DESCRIPTION:
Retrieval-mode eval runner for the PKS memory system (Phase A of
PRD-eval-baseline-v1.md). Loads the probe suite, issues each enabled probe's
query against the MCP `search` tool (read-only), and scores:

  recall_at_k            recall / project / explicit_save / exact_lexical axes:
                         pass if any expected entry id OR expected string
                         appears in the top-k results
  stale_leak_rate        stale_fact axis: share of probes where a forbidden
                         (obsolete) string surfaced in top-k
  supersession_accuracy  supersession axis: new value present AND old absent
  negative_precision     negative axis: share of irrelevant queries where the
                         serving layer explicitly abstains and returns no evidence
  paraphrase_consistency paraphrase axis: share of paraphrase groups whose
                         variants share at least one common top-k entry id
  tokens_per_query       ESTIMATE (chars/4 of returned label+summary text) —
                         the MCP search response carries no token usage field

Axes with zero enabled probes are reported as UNMEASURED (metric null), never
silently omitted (no-fake-zero rule). Any network/tool error on an enabled
probe is recorded and forces a non-zero exit (FAIL IS FAIL) — partial results
are still written.

--compare OLD.json NEW.json diffs two prior reports (no network) — the shadow
A/B safety rail: per-axis metric deltas plus per-probe pass/fail flips.

--fail-on-regression (used with --compare) turns that diff into a deterministic
gate: exit 1 if any enabled probe flips pass->fail, or any measured axis metric
degrades beyond its tolerance (recall-family/supersession/negative/paraphrase:
new < old - 0.02; stale_leak_rate, where lower is better: new > old + 0.02).
An axis UNMEASURED (null) in either report is skipped, never scored as a
regression (no-fake-zero rule). Without the flag, --compare is unchanged
(descriptive only, always exits 0). See tests/python/test_run_eval_compare.py
and contracts/retrieval-regression-gate.spec.md for the gate contract.

The LLM-judge escalation path (DeepSeek V4 Flash direct API, validated 26/26 on
2026-07-06, see judge-shootout results) applies to ANSWER-mode evaluation
(Phase B/C: judging which injected memories an assistant's answer used); this
retrieval-mode runner needs no LLM judge — scoring is deterministic string/id
matching.

DEPENDENCIES: requests (already required by sibling scripts). Reuses the OAuth
client-registration flow from check_overnight_dream_run.py (mcp:read scope).

USAGE:
  python3 scripts/run_eval.py                                # baseline vs prod
  python3 scripts/run_eval.py --base-url https://arjun-knowledge-mcp-staging.arjun-divecha.workers.dev --config-tag shadow-X
  python3 scripts/run_eval.py --only-axis exact_lexical
  python3 scripts/run_eval.py --compare reports/eval_baseline_A.json reports/eval_baseline_B.json
  python3 scripts/run_eval.py --compare tests/baselines/retrieval_baseline.json reports/fresh.json --fail-on-regression
      # exit 0 = no regression, exit 1 = a probe flipped pass->fail or an axis degraded beyond tolerance

NOTES:
- Read-only: only the `search` tool is called. Every probe passes
  suppress_access_signals=true (PKS-USAGE-SIGNAL-001) so benchmark queries do
  NOT increment server-side access counts once a Worker honoring the flag is
  deployed; on older Workers the unknown argument is stripped by zod and the
  legacy rank-1 reconsolidation side effect applies (flagged in the report
  header either way so baseline runs aren't mistaken for organic use).
- Runtime: ~1 rps sequential; ~40 enabled probes ≈ under 2 minutes.
=============================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
PROBES_DIR = REPO_ROOT / "tests" / "probes"
REPORTS_DIR = SCRIPT_DIR / "reports"
DEFAULT_BASE_URL = "https://mcp.dancing-ganesh.com"

sys.path.insert(0, str(SCRIPT_DIR))
from check_overnight_dream_run import call_mcp_tool, fetch_dream_session  # noqa: E402

AXIS_METRIC = {
    "carry_forward_recall": "recall_at_k",
    "project_recall": "recall_at_k",
    "explicit_save_recall": "recall_at_k",
    "exact_lexical": "recall_at_k",
    "stale_fact": "stale_leak_rate",
    "supersession": "supersession_accuracy",
    "negative": "negative_precision",
    "paraphrase": "paraphrase_consistency",
}

# stale_leak_rate is the one metric where LOWER is better (fewer stale leaks);
# every other metric here is HIGHER-is-better. Used by --fail-on-regression to
# pick the correct "worse" direction per axis (PKS-RETRIEVAL-REGRESSION-GATE-001).
LOWER_IS_BETTER_METRICS = {"stale_leak_rate"}
REGRESSION_TOLERANCE = 0.02  # absolute, applied in the "worse" direction per axis


def build_search_arguments(query: str) -> dict:
    """Arguments for every MCP `search` call this runner makes.

    suppress_access_signals=True (PKS-USAGE-SIGNAL-001) marks the query as
    synthetic benchmark traffic: a Worker honoring the flag skips the
    access_count/last_accessed reinforcement write, so nightly eval runs don't
    fabricate usage signal that salience then treats as organic. Removing the
    flag would silently re-poison the usage loop — the unit test in
    tests/python/test_run_eval_search_args.py pins it.
    """
    return {"query": query, "suppress_access_signals": True}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PKS retrieval-mode eval runner")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--k", type=int, default=5, help="default top-k cutoff")
    p.add_argument("--negative-threshold", type=float, default=0.65,
                   help="deprecated compatibility field; source-first negatives must return no results")
    p.add_argument("--config-tag", default="baseline",
                   help="label stored in the report (e.g. shadow-hybrid)")
    p.add_argument("--only-axis", default=None)
    p.add_argument("--include-disabled", action="store_true",
                   help="also run enabled:false draft probes (reported separately, never scored)")
    p.add_argument("--compare", nargs=2, metavar=("OLD", "NEW"), default=None)
    p.add_argument("--fail-on-regression", action="store_true",
                   help="with --compare: exit 1 if any enabled probe flips pass->fail "
                        "or any measured axis metric degrades beyond tolerance "
                        f"({REGRESSION_TOLERANCE} absolute); UNMEASURED axes are skipped")
    return p.parse_args()


def load_probes() -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for path in sorted(PROBES_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise ValueError(f"{path} is not a probe list")
        for probe in data:
            probe["_file"] = path.name
            probes.append(probe)
    seen: set[str] = set()
    for probe in probes:
        if probe["id"] in seen:
            raise ValueError(f"duplicate probe id: {probe['id']}")
        seen.add(probe["id"])
    return probes


def result_text(r: dict[str, Any]) -> str:
    # Legacy entries expose label/summary/domain; source-first evidence exposes
    # title/text/project/source_path. Normalize both shapes so the same probe
    # suite measures retrieval quality across the cutover instead of reporting
    # false zeroes merely because the response schema changed.
    return " ".join(str(r.get(k) or "") for k in (
        "label", "summary", "domain", "title", "text", "project", "source_path", "source_kind",
    )).lower()


def score_probe(probe: dict[str, Any], results: list[dict[str, Any]],
                k: int, negative_threshold: float) -> dict[str, Any]:
    axis = probe["axis"]
    top = results[: probe.get("min_rank", k)]
    top_ids = [r.get("id") for r in top]
    texts = [result_text(r) for r in top]

    expect_ids = probe.get("expect_entry_ids") or []
    expect_txt = [s.lower() for s in (probe.get("expect_any_of") or [])]
    forbid_txt = [s.lower() for s in (probe.get("forbid_any_of") or [])]

    id_hit = any(i in top_ids for i in expect_ids)
    txt_hit = any(any(s in t for t in texts) for s in expect_txt)
    expected_found = id_hit or txt_hit if (expect_ids or expect_txt) else True
    leaks = [s for s in forbid_txt if any(s in t for t in texts)]

    detail: dict[str, Any] = {
        "top_ids": top_ids,
        "top_final_scores": [r.get("final_score") for r in top],
        "expected_found": expected_found,
        "id_hit": id_hit,
        "text_hit": txt_hit,
        "leaks": leaks,
    }

    if axis == "negative":
        top_score = results[0].get("final_score") if results else 0.0
        detail["passed"] = len(results) == 0
        detail["top_score"] = top_score
    elif axis == "stale_fact":
        detail["passed"] = not leaks
    elif axis == "supersession":
        detail["passed"] = expected_found and not leaks
    else:  # recall-family and paraphrase (individual leg)
        detail["passed"] = expected_found and not leaks
    return detail


def run_eval(args: argparse.Namespace) -> int:
    probes = load_probes()
    if args.only_axis:
        probes = [p for p in probes if p["axis"] == args.only_axis]
    enabled = [p for p in probes if p.get("enabled")]
    drafts = [p for p in probes if not p.get("enabled")]
    to_run = enabled + (drafts if args.include_disabled else [])
    print(f"{len(enabled)} enabled probes ({len(drafts)} drafts) against {args.base_url}")

    session, token, session_id = fetch_dream_session(args.base_url)
    rows: list[dict[str, Any]] = []
    errors = 0
    rpc_id = 10
    for probe in to_run:
        rpc_id += 1
        row: dict[str, Any] = {"id": probe["id"], "axis": probe["axis"],
                               "enabled": bool(probe.get("enabled")),
                               "query": probe["query"],
                               "paraphrase_group": probe.get("paraphrase_group")}
        try:
            t0 = time.time()
            payload = call_mcp_tool(session, args.base_url, token, session_id,
                                    rpc_id=rpc_id, name="search",
                                    arguments=build_search_arguments(probe["query"]))
            results = payload.get("results", [])
            row["latency_s"] = round(time.time() - t0, 2)
            row["token_estimate"] = sum(
                len(result_text(r)) // 4
                for r in results[: probe.get("min_rank", args.k)])
            row.update(score_probe(probe, results, args.k, args.negative_threshold))
        except Exception as exc:  # noqa: BLE001 — recorded, run continues, exit non-zero
            errors += 1
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["passed"] = False
        rows.append(row)
        time.sleep(0.3)

    scored = [r for r in rows if r["enabled"]]

    # paraphrase groups: variants must share >=1 common top-k entry id
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in scored:
        if r["axis"] == "paraphrase" and r.get("paraphrase_group"):
            groups.setdefault(r["paraphrase_group"], []).append(r)
    group_results = {}
    for gname, legs in groups.items():
        id_sets = [set(leg.get("top_ids") or []) for leg in legs]
        common = set.intersection(*id_sets) if id_sets else set()
        group_results[gname] = {"consistent": bool(common),
                                "common_ids": sorted(common),
                                "legs": [leg["id"] for leg in legs]}

    axes: dict[str, Any] = {}
    for axis, metric in AXIS_METRIC.items():
        ax_rows = [r for r in scored if r["axis"] == axis]
        if not ax_rows:
            axes[axis] = {"metric": metric, "value": None, "n": 0, "status": "UNMEASURED"}
            continue
        if axis == "paraphrase":
            n = len(group_results)
            value = round(sum(g["consistent"] for g in group_results.values()) / n, 3) if n else None
        elif axis == "stale_fact":
            value = round(sum(bool(r.get("leaks")) for r in ax_rows) / len(ax_rows), 3)
        else:
            value = round(sum(bool(r.get("passed")) for r in ax_rows) / len(ax_rows), 3)
        axes[axis] = {"metric": metric, "value": value,
                      "n": len(group_results) if axis == "paraphrase" else len(ax_rows),
                      "status": "measured"}

    tok = [r["token_estimate"] for r in scored if r.get("token_estimate") is not None]
    tok.sort()
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "config_tag": args.config_tag,
        "base_url": args.base_url,
        "k": args.k,
        "negative_threshold": args.negative_threshold,
        "note_access_side_effect": "all probes passed suppress_access_signals=true; Workers "
                                   "honoring the flag write no access signals for this run — "
                                   "older Workers strip the arg and apply legacy rank-1 "
                                   "reconsolidation; baseline runs are not organic usage",
        "note_tokens": "token_estimate = chars/4 of normalized legacy or source-first result text; search response has no usage field",
        "n_enabled": len(scored), "n_drafts": len(drafts), "errors": errors,
        "axes": axes,
        "tokens_per_query": {"median": tok[len(tok) // 2] if tok else None,
                              "p95": tok[max(0, int(len(tok) * 0.95) - 1)] if len(tok) >= 2 else None},
        "paraphrase_groups": group_results,
        "probes": rows,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S+0000")
    out = REPORTS_DIR / f"eval_baseline_{stamp}.json"
    out.write_text(json.dumps(report, indent=1))

    print(f"\n{'axis':26} {'metric':24} {'value':>8} {'n':>4}")
    for axis, a in axes.items():
        val = "UNMEAS" if a["value"] is None else f"{a['value']:.3f}"
        print(f"{axis:26} {a['metric']:24} {val:>8} {a['n']:>4}")
    print(f"\ntokens/query median={report['tokens_per_query']['median']} "
          f"p95={report['tokens_per_query']['p95']}  errors={errors}")
    print(f"wrote {out}")
    return 1 if errors else 0


def axis_regressed(axis: str, old_value: float, new_value: float) -> bool:
    """True iff new_value is worse than old_value by more than REGRESSION_TOLERANCE,
    direction-aware per LOWER_IS_BETTER_METRICS (PKS-RETRIEVAL-REGRESSION-GATE-001 INV2)."""
    metric = AXIS_METRIC.get(axis)
    if metric in LOWER_IS_BETTER_METRICS:
        return new_value > old_value + REGRESSION_TOLERANCE
    return new_value < old_value - REGRESSION_TOLERANCE


def compare(old_path: str, new_path: str, fail_on_regression: bool = False) -> int:
    old = json.loads(Path(old_path).read_text())
    new = json.loads(Path(new_path).read_text())
    print(f"OLD {old['config_tag']} ({old['generated_at']})  vs  "
          f"NEW {new['config_tag']} ({new['generated_at']})\n")
    print(f"{'axis':26} {'old':>8} {'new':>8} {'delta':>8}")
    regressed_axes: list[str] = []
    for axis in AXIS_METRIC:
        ov = old["axes"].get(axis, {}).get("value")
        nv = new["axes"].get(axis, {}).get("value")
        both_measured = isinstance(ov, (int, float)) and isinstance(nv, (int, float))
        delta = f"{nv - ov:+.3f}" if both_measured else "—"
        print(f"{axis:26} {ov if ov is not None else 'UNMEAS':>8} "
              f"{nv if nv is not None else 'UNMEAS':>8} {delta:>8}")
        # UNMEASURED (either side null) is skipped, never scored as a regression (INV4).
        if both_measured and axis_regressed(axis, ov, nv):
            regressed_axes.append(axis)
    old_p = {r["id"]: r.get("passed") for r in old["probes"] if r.get("enabled")}
    new_p = {r["id"]: r.get("passed") for r in new["probes"] if r.get("enabled")}
    flips = [(pid, old_p.get(pid), new_p.get(pid))
             for pid in sorted(set(old_p) | set(new_p)) if old_p.get(pid) != new_p.get(pid)]
    print("\npass/fail flips:" if flips else "\nno per-probe flips")
    for pid, o, n in flips:
        print(f"  {pid}: {o} -> {n}")
    om, nm = old["tokens_per_query"], new["tokens_per_query"]
    print(f"tokens/query median {om['median']} -> {nm['median']}")

    if not fail_on_regression:
        return 0

    # A probe that passed in OLD and is not True in NEW is a regression — this
    # includes an explicit pass->fail flip AND a probe silently missing from
    # the NEW report (e.g. a --only-axis run, a dropped/renamed probe id):
    # both mean "a previously-passing check is no longer proven to pass".
    regressed_probes = [pid for pid, o, n in flips if o is True and n is not True]
    if regressed_axes or regressed_probes:
        print("\nREGRESSION GATE: FAIL")
        if regressed_axes:
            print(f"  degraded axes (> {REGRESSION_TOLERANCE} beyond baseline): {regressed_axes}")
        if regressed_probes:
            print(f"  probes no longer proven passing (flipped or missing): {regressed_probes}")
        return 1
    print("\nREGRESSION GATE: PASS")
    return 0


def main() -> int:
    args = parse_args()
    if args.compare:
        return compare(*args.compare, fail_on_regression=args.fail_on_regression)
    return run_eval(args)


if __name__ == "__main__":
    raise SystemExit(main())
