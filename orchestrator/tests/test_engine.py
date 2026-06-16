"""Engine integration: shadow run, live downgrade, partial report, resume (Phase 1)."""
import json

from orchestrator import config, preflight as PF


def _ledger_doc(rd="2026-06-16"):
    return json.loads((config.LEDGER_DIR / f"{rd}.json").read_text())


def _report_json(rd="2026-06-16"):
    return json.loads((config.REPORTS_DIR / f"pks-nightly-{rd}.json").read_text())


def test_full_shadow_run(make_orch, backend):
    orch = make_orch()
    assert orch.run(mode="shadow", run_date="2026-06-16") == 0
    doc = _ledger_doc()
    assert doc["status"] == "completed" and doc["stages"]["DONE"]["status"] == "completed"
    rep = _report_json()
    assert rep["complete"] is True and rep["verdict"] == "completed"
    assert rep["dream"]["executed_mode"] == "shadow" and rep["dream"]["applied_count"] == 0
    # Redis mirror written under the run key.
    assert backend.get(config.KEY_RUN.format(run_date="2026-06-16")) is not None
    assert backend.get(config.KEY_LAST_STATUS) == "completed"


def test_live_mode_downgraded_to_shadow(make_orch):
    orch = make_orch()
    assert orch.run(mode="live", run_date="2026-06-16") == 0
    doc = _ledger_doc()
    assert doc["mode"] == "shadow"  # downgraded; nothing mutated
    assert any("mutations are disabled" in w for w in doc.get("warnings", []))
    assert _report_json()["dream"]["applied_count"] == 0


def test_mid_run_failure_renders_partial_report(make_orch):
    red = PF.PreflightDeps(
        env_readable=lambda: (True, "ok"), redis_reachable=lambda: (True, "ok"),
        vector_reachable=lambda: (True, "ok"), cloudflare_health=lambda: (True, "ok"),
        dream_token_present=lambda: (True, "ok"), claude_cli_present=lambda: (True, "ok"),
        sdk_auth_live=lambda: (False, "no sdk"),
        api_fallback_available=lambda: (False, "no key"),
        no_browser_guards=lambda: (True, "ok"))
    orch = make_orch(preflight_deps=red)
    assert orch.run(mode="shadow", run_date="2026-06-16") == 1
    rep = _report_json()
    assert rep["verdict"] == "failed_recoverable" and rep["complete"] is False
    assert "PREFLIGHT" in rep["tripwires"]
    assert "INGEST_GITHUB" in rep["pending_stages"]  # never reached
    assert rep["dream"]["applied_count"] is None      # Dream never ran


class _ControllableDream:
    def __init__(self):
        self.statuses = {}; self.terminal = False; self.start_calls = 0

    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode, fencing_token):
        self.start_calls += 1
        self.statuses[dream_run_id] = {
            "state": "running", "requested_mode": mode, "executed_mode": "shadow",
            "dream_run_id": dream_run_id, "orchestrator_run_id": orchestrator_run_id,
            "run_date": run_date, "applied_count": 0, "held_count": 0}
        return {"accepted": True, "executed_mode": "shadow"}

    def status(self, dream_run_id):
        s = self.statuses.get(dream_run_id)
        if s and self.terminal:
            s["state"] = "terminal"; s.setdefault("status", "completed_shadow")
        return s


def _advancing_time():
    tk = {"v": 0.0}
    return (lambda: tk["v"]), (lambda dt: tk.__setitem__("v", tk["v"] + dt))


def test_same_host_resume_completes(make_orch):
    cd = _ControllableDream()
    mono, slp = _advancing_time()
    orch1 = make_orch(dream_client=cd, sleep=slp, monotonic=mono,
                      dream_timeout=120, dream_poll_interval=30)
    assert orch1.run(mode="shadow", run_date="2026-06-16") == 1  # DREAM_WAIT times out
    doc = _ledger_doc()
    assert doc["status"] == "failed_recoverable"
    assert doc["stages"]["DREAM_WAIT"]["status"] == "failed_recoverable"
    run_id = doc["orchestrator_run_id"]

    cd.terminal = True  # Worker finishes
    mono2, slp2 = _advancing_time()
    orch2 = make_orch(dream_client=cd, sleep=slp2, monotonic=mono2,
                      dream_timeout=120, dream_poll_interval=30)
    assert orch2.resume(run_date="2026-06-16") == 0
    doc2 = _ledger_doc()
    assert doc2["status"] == "completed"
    assert doc2["orchestrator_run_id"] == run_id     # same run, reattached
    assert cd.start_calls == 1                        # DREAM never restarted


def test_cross_host_resume_refused(make_orch):
    cd = _ControllableDream()
    mono, slp = _advancing_time()
    orch1 = make_orch(dream_client=cd, sleep=slp, monotonic=mono,
                      dream_timeout=120, dream_poll_interval=30)
    assert orch1.run(mode="shadow", run_date="2026-06-16") == 1  # incomplete
    cd.terminal = True
    other = make_orch(dream_client=cd, owner_host="some-other-host",
                      sleep=slp, monotonic=mono, dream_timeout=120, dream_poll_interval=30)
    assert other.resume(run_date="2026-06-16") == 1  # refused, not completed
    assert _ledger_doc()["status"] == "failed_recoverable"
