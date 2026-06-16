"""
Opt-in LIVE test of the production atomic path (Upstash Redis Lua EVAL).

Skipped unless UPSTASH_REDIS_REST_URL/TOKEN are set. This proves the real Lua
acquire+fence / heartbeat / check_fence / cas_set behave identically to the
in-memory backend the other tests use. It writes only to throwaway
`pks:orchestrator:*:__livetest__*` keys and deletes them.
"""
import os

import pytest

LIVE = bool(os.environ.get("UPSTASH_REDIS_REST_URL") and os.environ.get("UPSTASH_REDIS_REST_TOKEN"))
pytestmark = pytest.mark.skipif(not LIVE, reason="Upstash creds not in env")

RUN_A = "pksn_20260616_230000_aaaaaaaa"
RUN_B = "pksn_20260616_233000_bbbbbbbb"


@pytest.fixture
def live_backend():
    from orchestrator.backends import redis_lua_backend_from_env
    b = redis_lua_backend_from_env()
    yield b
    for k in ("pks:orchestrator:lock:__livetest__",
              "pks:orchestrator:fence:__livetest__",
              "pks:orchestrator:run:__livetest__"):
        try:
            b.delete(k)
        except Exception:
            pass


def test_live_lua_lock_and_fence(live_backend):
    b = live_backend
    lock_key = "pks:orchestrator:lock:__livetest__"
    fence_key = "pks:orchestrator:fence:__livetest__"
    target = "pks:orchestrator:run:__livetest__"
    b.delete(lock_key); b.delete(fence_key)

    a = b.acquire(lock_key, fence_key, run_id=RUN_A, owner_host="m4max-base", pid=1,
                  now_iso="t0", now_epoch=1000, ttl=7200, stale=5400)
    assert a.acquired and a.outcome == "acquired"
    fence_a = a.fence

    # Within stale window, a different run is refused.
    held = b.acquire(lock_key, fence_key, run_id=RUN_B, owner_host="m4max-base", pid=2,
                     now_iso="t1", now_epoch=1000 + 60, ttl=7200, stale=5400)
    assert not held.acquired and held.outcome == "held"

    # A's heartbeat + fence still valid; CAS write succeeds.
    assert b.heartbeat(lock_key, run_id=RUN_A, fence=fence_a, now_iso="t2",
                       now_epoch=1000 + 120, ttl=7200) is True
    assert b.check_fence(lock_key, run_id=RUN_A, fence=fence_a) == "valid"
    assert b.cas_set(lock_key, target, '{"ok":true}', run_id=RUN_A, fence=fence_a) == "valid"

    # After the stale threshold, B takes over and the fence increments.
    took = b.acquire(lock_key, fence_key, run_id=RUN_B, owner_host="m4max-base", pid=2,
                     now_iso="t3", now_epoch=1000 + 6000, ttl=7200, stale=5400)
    assert took.acquired and took.outcome == "stale_takeover" and took.fence == fence_a + 1

    # A's stale fence is now rejected on check and CAS.
    assert b.check_fence(lock_key, run_id=RUN_A, fence=fence_a) == "superseded"
    assert b.cas_set(lock_key, target, '{"stale":true}', run_id=RUN_A, fence=fence_a) == "superseded"

    # A's stale release cannot delete B's live lock; B can release atomically.
    assert b.release_lock(lock_key, run_id=RUN_A, fence=fence_a) == "superseded"
    assert b.read_lock(lock_key)["orchestrator_run_id"] == RUN_B
    assert b.release_lock(lock_key, run_id=RUN_B, fence=took.fence) == "valid"
    assert b.read_lock(lock_key) is None
