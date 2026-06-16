"""Dream wait timeout + reattach; start-once-per-dream_run_id (Phase 1)."""
from orchestrator import dream as D

DID = "dga_20260616_ab12cd34"


class ControllableDream:
    """A Dream client whose terminal state and start count are observable."""
    def __init__(self):
        self.statuses = {}
        self.terminal = False
        self.start_calls = 0

    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode, fencing_token):
        self.start_calls += 1
        self.statuses[dream_run_id] = {
            "state": "running", "requested_mode": mode, "executed_mode": "shadow",
            "dream_run_id": dream_run_id, "orchestrator_run_id": orchestrator_run_id,
            "run_date": run_date, "applied_count": 0, "held_count": 0,
        }
        return {"accepted": True, "executed_mode": "shadow"}

    def status(self, dream_run_id):
        s = self.statuses.get(dream_run_id)
        if s and self.terminal:
            s["state"] = "terminal"
            s.setdefault("status", "completed_shadow")
        return s


def _fake_time():
    t = {"v": 0.0}
    def mono(): return t["v"]
    def sleep(dt): t["v"] += dt
    return t, mono, sleep


def test_wait_times_out_without_starting():
    c = ControllableDream()
    c.start(dream_run_id=DID, orchestrator_run_id="x", run_date="2026-06-16",
            mode="shadow", fencing_token=1)
    c.start_calls = 0  # reset; we only count starts during wait below
    _, mono, sleep = _fake_time()
    res = D.wait_for_dream(c, DID, poll_interval=30, timeout=120, sleep=sleep, monotonic=mono)
    assert res["outcome"] == "timeout"
    assert c.start_calls == 0  # wait never starts a run


def test_start_is_idempotent_by_dream_run_id():
    c = ControllableDream()
    r1 = D.ensure_dream_started(c, dream_run_id=DID, orchestrator_run_id="x",
                                run_date="2026-06-16", mode="shadow", fencing_token=1)
    r2 = D.ensure_dream_started(c, dream_run_id=DID, orchestrator_run_id="x",
                                run_date="2026-06-16", mode="shadow", fencing_token=1)
    assert r1["started"] is True
    assert r2["started"] is False and r2["reason"] == "status_exists"
    assert c.start_calls == 1


def test_reattach_after_timeout_then_terminal():
    c = ControllableDream()
    D.ensure_dream_started(c, dream_run_id=DID, orchestrator_run_id="x",
                           run_date="2026-06-16", mode="shadow", fencing_token=1)
    _, mono, sleep = _fake_time()
    first = D.wait_for_dream(c, DID, poll_interval=30, timeout=120, sleep=sleep, monotonic=mono)
    assert first["outcome"] == "timeout"
    # Worker finishes; a resumed wait reattaches to the SAME id and sees terminal.
    c.terminal = True
    _, mono2, sleep2 = _fake_time()
    second = D.wait_for_dream(c, DID, poll_interval=30, timeout=120, sleep=sleep2, monotonic=mono2)
    assert second["outcome"] == "terminal"
    assert c.start_calls == 1  # never restarted across both waits


def test_verify_contract():
    ok = {"executed_mode": "shadow", "applied_count": 0}
    assert D.verify_dream_status(ok, "shadow")["ok"] is True
    assert D.verify_dream_status({"applied_count": 0}, "shadow")["problems"] == ["missing executed_mode"]
    assert "missing applied_count" in D.verify_dream_status({"executed_mode": "shadow"}, "shadow")["problems"]
    bad = {"executed_mode": "shadow", "applied_count": 3}
    assert D.verify_dream_status(bad, "shadow")["ok"] is False
    rej = {"executed_mode": "live", "applied_count": 0, "status": "rejected_superseded"}
    assert any("rejected" in p for p in D.verify_dream_status(rej, "live")["problems"])
