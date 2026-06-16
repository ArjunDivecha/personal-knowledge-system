"""Partial report renders from an incomplete ledger (Phase 1)."""
from orchestrator import report, states


def _doc(stage_statuses: dict, run_status="running"):
    stages = {}
    for stg, st in stage_statuses.items():
        stages[stg] = states.make_stage_record(stg, status=st)
    return {
        "orchestrator_run_id": "pksn_20260616_230000_ab12cd34",
        "dream_run_id": "dga_20260616_ab12cd34",
        "run_date": "2026-06-16", "mode": "shadow", "status": run_status,
        "fencing_token": 1, "started_at": "t0", "updated_at": "t1",
        "completed_at": None, "stages": stages,
    }


def test_partial_report_marks_unreached_pending():
    doc = _doc({"INIT": "completed", "LOCKED": "completed",
                "PREFLIGHT": "completed", "SNAPSHOT_BEFORE": "completed"})
    j = report.render_json(doc)
    assert j["complete"] is False
    assert j["verdict"] == "running"
    # later stages never reached -> pending
    assert "INGEST_GITHUB" in j["pending_stages"]
    assert "DONE" in j["pending_stages"]
    # markdown renders without error and lists pending
    md = report.render_markdown(doc)
    assert "Pending (not reached)" in md and "PKS Nightly" in md


def test_complete_report_when_all_done():
    full = {s: "completed" for s in states.STAGES}
    doc = _doc(full, run_status="completed")
    j = report.render_json(doc)
    assert j["complete"] is True and j["verdict"] == "completed"
    assert j["pending_stages"] == []


def test_failure_report_surfaces_tripwire():
    doc = _doc({"INIT": "completed", "LOCKED": "completed",
                "PREFLIGHT": "failed_recoverable"}, run_status="failed_recoverable")
    j = report.render_json(doc)
    assert "PREFLIGHT" in j["tripwires"]
    assert j["verdict"] == "failed_recoverable" and j["complete"] is False
