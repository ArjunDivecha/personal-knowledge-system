"""Ledger + state-machine transition validation (Phase 1)."""
from orchestrator import states
from orchestrator.backends import InMemoryBackend
from orchestrator.ids import make_identity
from orchestrator.ledger import RunLedger
from orchestrator.lock import FencingLock
from datetime import date, datetime

import pytest


def test_transition_rules():
    assert states.is_valid_transition("PREFLIGHT", "PREFLIGHT")        # retry ok
    assert states.is_valid_transition("PREFLIGHT", "SNAPSHOT_BEFORE")  # advance ok
    assert not states.is_valid_transition("PREFLIGHT", "DREAM_START")  # skip
    assert not states.is_valid_transition("DREAM_WAIT", "PREFLIGHT")   # regress


def _ledger(backend, clock):
    idn = make_identity(date(2026, 6, 16), datetime(2026, 6, 16, 23, 0, 0), suffix="ab12cd34")
    lock = FencingLock(backend, "2026-06-16", idn.orchestrator_run_id, clock=clock,
                       owner_host="m4max-base")
    lock.acquire()
    led = RunLedger.new("2026-06-16", idn, mode="shadow", fence=lock.fence,
                        owner_host="m4max-base", pid=lock.pid).attach(backend, lock)
    return led


def test_begin_stage_rejects_invalid_transition(backend, clock):
    led = _ledger(backend, clock)
    # current_stage == LOCKED; jumping to DREAM_START skips stages -> invalid
    with pytest.raises(ValueError):
        led.begin_stage("DREAM_START")


def test_commit_requires_terminal_status(backend, clock):
    led = _ledger(backend, clock)
    led.begin_stage("PREFLIGHT")
    with pytest.raises(ValueError):
        led.commit_stage("PREFLIGHT", states.make_stage_record("PREFLIGHT", status="running"))


def test_derive_run_status_precedence():
    def recs(*sts):
        return {f"S{i}": {"status": s} for i, s in enumerate(sts)}
    assert states.derive_run_status(recs("completed", "completed"), True) == "completed"
    assert states.derive_run_status(recs("completed", "completed_with_warnings"), True) == "completed_with_warnings"
    assert states.derive_run_status(recs("completed_with_holds", "completed"), True) == "completed_with_holds"
    assert states.derive_run_status(recs("completed", "failed_recoverable"), True) == "failed_recoverable"
    assert states.derive_run_status(recs("failed_recoverable", "failed_terminal"), True) == "failed_terminal"
    assert states.derive_run_status(recs("abandoned_by_newer_run", "completed"), True) == "abandoned_by_newer_run"
    # not reaching DONE with only OK stages -> still running
    assert states.derive_run_status(recs("completed"), False) == "running"


def test_resume_stage_points_at_first_incomplete(backend, clock):
    led = _ledger(backend, clock)
    led.begin_stage("PREFLIGHT")
    led.commit_stage("PREFLIGHT", states.make_stage_record("PREFLIGHT", status="completed"))
    # INIT, LOCKED, PREFLIGHT done -> next incomplete is SNAPSHOT_BEFORE
    assert led.resume_stage() == "SNAPSHOT_BEFORE"
