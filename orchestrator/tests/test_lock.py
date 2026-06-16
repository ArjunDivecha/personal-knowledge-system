"""Fencing lock: acquisition, stale refusal, heartbeat, fence increment (Phase 1)."""
from orchestrator.lock import FencingLock

RUN_A = "pksn_20260616_230000_aaaaaaaa"
RUN_B = "pksn_20260616_233000_bbbbbbbb"


def _lock(backend, clock, run_id, **kw):
    return FencingLock(backend, "2026-06-16", run_id, clock=clock,
                       owner_host="m4max-base", stale=5400, **kw)


def test_fresh_acquire(backend, clock):
    a = _lock(backend, clock, RUN_A)
    res = a.acquire()
    assert res.acquired and res.outcome == "acquired" and res.fence == 1
    assert a.fence == 1


def test_held_lock_refused_within_stale(backend, clock):
    a = _lock(backend, clock, RUN_A); a.acquire()
    b = _lock(backend, clock, RUN_B)
    res = b.acquire()
    assert not res.acquired and res.outcome == "held" and res.fence == 1
    assert b.fence is None


def test_same_run_reacquire_keeps_fence(backend, clock):
    a = _lock(backend, clock, RUN_A); a.acquire()
    res = a.acquire()
    assert res.acquired and res.outcome == "reacquired" and res.fence == 1


def test_stale_takeover_increments_fence(backend, clock):
    a = _lock(backend, clock, RUN_A); a.acquire()
    clock.t += 5401  # past the 90-minute stale threshold
    b = _lock(backend, clock, RUN_B)
    res = b.acquire()
    assert res.acquired and res.outcome == "stale_takeover" and res.fence == 2
    # the superseded holder now fails its fence check + heartbeat
    assert a.check() == "superseded"
    assert a.heartbeat() is False


def test_heartbeat_and_release(backend, clock):
    a = _lock(backend, clock, RUN_A); a.acquire()
    assert a.heartbeat() is True
    assert a.check() == "valid"
    assert a.release() is True
    # second release is a no-op (lock gone)
    assert a.check() == "lost"
