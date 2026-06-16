"""Stage terminal write rejects a stale fence (Atomicity And Race Contract)."""
from datetime import date, datetime

from orchestrator import states
from orchestrator.ids import make_identity
from orchestrator.ledger import RunLedger
from orchestrator.lock import FencingLock

RUN_B = "pksn_20260616_233000_bbbbbbbb"


def test_terminal_write_rejects_stale_fence(backend, clock):
    idn = make_identity(date(2026, 6, 16), datetime(2026, 6, 16, 23, 0, 0), suffix="ab12cd34")
    lock_a = FencingLock(backend, "2026-06-16", idn.orchestrator_run_id, clock=clock,
                         owner_host="m4max-base", stale=5400)
    lock_a.acquire()
    led = RunLedger.new("2026-06-16", idn, mode="shadow", fence=lock_a.fence,
                        owner_host="m4max-base", pid=lock_a.pid).attach(backend, lock_a)

    # A newer run supersedes A via a stale takeover (fence 1 -> 2).
    clock.t += 5401
    lock_b = FencingLock(backend, "2026-06-16", RUN_B, clock=clock,
                         owner_host="m4max-base", stale=5400)
    res_b = lock_b.acquire()
    assert res_b.acquired and res_b.fence == 2

    # A's terminal stage commit must be rejected and self-record abandoned.
    led.begin_stage("PREFLIGHT")  # local-only effects fine; CAS will be superseded
    verdict = led.commit_stage(
        "PREFLIGHT", states.make_stage_record("PREFLIGHT", status="completed"))
    assert verdict == "superseded"
    assert led.doc["stages"]["PREFLIGHT"]["status"] == "abandoned_by_newer_run"
    assert led.doc["status"] == "abandoned_by_newer_run"
