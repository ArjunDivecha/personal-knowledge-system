"""NOTIFY is skip-if-completed; shadow_validation never alerts the user (Phase 1)."""
from datetime import date, datetime

from orchestrator import dream as D
from orchestrator import states
from orchestrator.ids import make_identity
from orchestrator.ledger import RunLedger
from orchestrator.lock import FencingLock
from orchestrator.stages import StageContext, stage_notify


def _ctx(backend, clock, *, force_notify, prior_notify):
    idn = make_identity(date(2026, 6, 16), datetime(2026, 6, 16, 23, 0, 0), suffix="ab12cd34")
    lock = FencingLock(backend, "2026-06-16", idn.orchestrator_run_id, clock=clock,
                       owner_host="m4max-base")
    lock.acquire()
    led = RunLedger.new("2026-06-16", idn, mode="shadow", fence=lock.fence,
                        owner_host="m4max-base", pid=lock.pid).attach(backend, lock)
    if prior_notify:
        led.doc["stages"]["NOTIFY"] = states.make_stage_record("NOTIFY", status="completed")
    return StageContext(
        run_date="2026-06-16", requested_mode="shadow", effective_mode="shadow",
        identity=idn, ledger=led, lock=lock, backend=backend,
        dream_client=D.Phase1ShadowDreamClient(), force_notify=force_notify)


def test_notify_skips_if_completed(backend, clock):
    rec = stage_notify(_ctx(backend, clock, force_notify=False, prior_notify=True))
    assert rec["status"] == "completed"
    assert rec["counts"]["sent"] is False
    assert rec["counts"]["reason"] == "skip-if-completed"


def test_notify_force_does_not_skip(backend, clock):
    rec = stage_notify(_ctx(backend, clock, force_notify=True, prior_notify=True))
    assert rec["counts"]["reason"] != "skip-if-completed"
    assert rec["counts"]["sent"] is False  # shadow_validation: still no user alert


def test_notify_first_time_is_shadow_validation(backend, clock):
    rec = stage_notify(_ctx(backend, clock, force_notify=False, prior_notify=False))
    assert rec["counts"]["sent"] is False
    assert "shadow_validation" in rec["counts"]["reason"]
