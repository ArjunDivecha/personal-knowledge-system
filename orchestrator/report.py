"""
=============================================================================
MODULE: orchestrator/report.py
=============================================================================

DESCRIPTION:
Renders the nightly report from the durable ledger document. The renderer is
PARTIAL-SAFE: it works on an incomplete/interrupted ledger, marking stages that
were never reached as `pending` and reflecting a non-terminal verdict. Per the
Cutover Gates, the report surfaces Dream status, held ops, tripwires, the final
verdict, and ingestion count deltas.

INPUT FILES:
- the in-memory ledger doc (from orchestrator/ledger.py)

OUTPUT FILES:
- scripts/reports/pks-nightly-{run_date}.json
- scripts/reports/pks-nightly-{run_date}.md
=============================================================================
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config, states

# Material ingestion-delta threshold (Cutover Gates): "above 10 entries or 5
# percent, whichever is larger, must be explained".
DELTA_ABS = 10
DELTA_PCT = 0.05


def _stage_view(doc: dict) -> list[dict]:
    out = []
    for stg in states.STAGES:
        rec = doc.get("stages", {}).get(stg)
        if rec is None:
            out.append({"stage": stg, "status": "pending", "attempt": 0,
                        "counts": {}, "warnings": [], "errors": []})
        else:
            out.append({k: rec.get(k) for k in
                        ("stage", "status", "attempt", "counts", "warnings", "errors")})
    return out


def _dream_section(doc: dict) -> dict:
    """Pull executed_mode / applied_count from the DREAM stage counts."""
    counts = {}
    for stg in ("DREAM_VERIFY", "DREAM_WAIT", "DREAM_START"):
        c = doc.get("stages", {}).get(stg, {}).get("counts") or {}
        counts.update({k: v for k, v in c.items() if k not in counts})
    return {
        "dream_run_id": doc.get("dream_run_id"),
        "requested_mode": counts.get("requested_mode"),
        "executed_mode": counts.get("executed_mode"),
        "applied_count": counts.get("applied_count"),
        "dream_status": counts.get("dream_status"),
    }


def _ingestion_deltas(doc: dict) -> list[dict]:
    deltas = []
    for stg in ("INGEST_TWITTER", "INGEST_GITHUB", "INGEST_AGENT_SESSIONS"):
        c = doc.get("stages", {}).get(stg, {}).get("counts") or {}
        saved = c.get("saved")
        before = c.get("before")
        if saved is None:
            continue
        material = False
        if isinstance(saved, (int, float)):
            base = before if isinstance(before, (int, float)) and before else 0
            pct = (saved / base) if base else (1.0 if saved else 0.0)
            material = abs(saved) > DELTA_ABS or abs(pct) > DELTA_PCT
        deltas.append({"stage": stg, "saved": saved, "before": before,
                       "material": material,
                       "explained": bool(c.get("delta_explanation")) if material else True,
                       "explanation": c.get("delta_explanation")})
    return deltas


def render_json(doc: dict) -> dict:
    stages = _stage_view(doc)
    reached_done = doc.get("stages", {}).get("DONE", {}).get("status") in states.STAGE_OK_STATUSES
    verdict = doc.get("status") or states.derive_run_status(doc.get("stages", {}), reached_done)
    tripwires = [s["stage"] for s in stages if s["status"] in states.STAGE_FAIL_STATUSES]
    held = [s["stage"] for s in stages if s["status"] == "completed_with_holds"]
    pending = [s["stage"] for s in stages if s["status"] == "pending"]
    return {
        "orchestrator_run_id": doc.get("orchestrator_run_id"),
        "run_date": doc.get("run_date"),
        "mode": doc.get("mode"),
        "verdict": verdict,
        "complete": not pending and reached_done,
        "started_at": doc.get("started_at"),
        "updated_at": doc.get("updated_at"),
        "completed_at": doc.get("completed_at"),
        "fencing_token": doc.get("fencing_token"),
        "dream": _dream_section(doc),
        "ingestion_deltas": _ingestion_deltas(doc),
        "held_ops": held,
        "tripwires": tripwires,
        "pending_stages": pending,
        "stages": stages,
    }


def render_markdown(doc: dict) -> str:
    j = render_json(doc)
    lines = [
        f"# PKS Nightly — {j['run_date']}  ({j['mode']} mode)",
        "",
        f"- Verdict: **{j['verdict']}**" + ("" if j["complete"] else "  _(incomplete)_"),
        f"- Orchestrator run: `{j['orchestrator_run_id']}`  (fence {j['fencing_token']})",
        f"- Started: {j['started_at']}  | Updated: {j['updated_at']}  | Completed: {j['completed_at']}",
        "",
        "## Dream",
        f"- run id: `{j['dream']['dream_run_id']}`",
        f"- requested_mode: {j['dream']['requested_mode']}  | executed_mode: "
        f"{j['dream']['executed_mode']}  | applied_count: {j['dream']['applied_count']}",
        "",
    ]
    if j["tripwires"]:
        lines += [f"## Tripwires: {', '.join(j['tripwires'])}", ""]
    if j["held_ops"]:
        lines += [f"## Held ops: {', '.join(j['held_ops'])}", ""]
    if j["pending_stages"]:
        lines += [f"## Pending (not reached): {', '.join(j['pending_stages'])}", ""]
    if j["ingestion_deltas"]:
        lines += ["## Ingestion deltas", "",
                  "| stage | saved | material | explained |",
                  "|---|---|---|---|"]
        for d in j["ingestion_deltas"]:
            lines.append(f"| {d['stage']} | {d['saved']} | {d['material']} | {d['explained']} |")
        lines.append("")
    lines += ["## Stages", "", "| stage | status | attempt |", "|---|---|---|"]
    for s in j["stages"]:
        lines.append(f"| {s['stage']} | {s['status']} | {s['attempt']} |")
    lines.append("")
    return "\n".join(lines)


def write_reports(doc: dict) -> tuple[str, str]:
    """Atomically write JSON + MD reports; return their absolute paths."""
    run_date = doc["run_date"]
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    jpath = config.REPORTS_DIR / config.REPORT_JSON.format(run_date=run_date)
    mpath = config.REPORTS_DIR / config.REPORT_MD.format(run_date=run_date)
    _atomic_write(jpath, json.dumps(render_json(doc), indent=2, sort_keys=True))
    _atomic_write(mpath, render_markdown(doc))
    return str(jpath), str(mpath)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
