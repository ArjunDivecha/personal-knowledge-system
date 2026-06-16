"""
=============================================================================
MODULE: orchestrator/backends.py
=============================================================================

DESCRIPTION:
The atomic state backend behind the orchestrator lock and ledger. Two
implementations with IDENTICAL semantics:

- RedisLuaBackend  (production): every compound operation (lock acquire + fence
  increment, heartbeat, fence check, fence-guarded CAS write, lock release) is ONE atomic
  Redis Lua `EVAL`, satisfying the Atomicity And Race Contract clause:
  "Lock acquisition and fence increment must be one atomic Redis operation. Use
  a Lua script ... do not use separate compare-then-write calls."
- InMemoryBackend  (tests): a process-local dict guarded by a re-entrant lock,
  emulating the same return contract so the lock/ledger logic can be unit-tested
  with no network. The Lua path is additionally exercised by an opt-in live
  test (orchestrator/tests/test_lock_live.py).

Fence semantics (contract): the fence token is EQUALITY-ONLY. A holder of fence
N may commit only if the live lock still has fence N and the same
orchestrator_run_id; a higher fence means superseded.

INPUT/OUTPUT FILES:
- Reads/writes Upstash Redis keys (RedisLuaBackend) named per orchestrator/config.py.
  No local files.
=============================================================================
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Optional, Protocol

# ── Return contract ──────────────────────────────────────────────────────────
VALID = "valid"            # fence still ours -> commit allowed
SUPERSEDED = "superseded"  # a newer run holds the lock (fence N+1)
LOST = "lost"             # lock gone or taken by an unrelated run


@dataclass
class AcquireResult:
    acquired: bool
    fence: int
    outcome: str  # acquired | reacquired | held | stale_takeover


# ── Lua scripts (production atomic path) ─────────────────────────────────────
# KEYS[1]=lock_key KEYS[2]=fence_counter_key
# ARGV: run_id, owner_host, pid, now_iso, now_epoch, ttl, stale_threshold
_ACQUIRE = """
local raw = redis.call('GET', KEYS[1])
if raw then
  local d = cjson.decode(raw)
  if d.orchestrator_run_id == ARGV[1] then
    d.heartbeat_at = ARGV[4]
    d.heartbeat_epoch = tonumber(ARGV[5])
    redis.call('SET', KEYS[1], cjson.encode(d), 'EX', tonumber(ARGV[6]))
    return {1, d.fencing_token, 'reacquired'}
  end
  local hb = tonumber(d.heartbeat_epoch) or 0
  if (tonumber(ARGV[5]) - hb) < tonumber(ARGV[7]) then
    return {0, d.fencing_token, 'held'}
  end
end
local fence = redis.call('INCR', KEYS[2])
local nd = {
  orchestrator_run_id = ARGV[1], owner_host = ARGV[2], pid = tonumber(ARGV[3]),
  acquired_at = ARGV[4], heartbeat_at = ARGV[4], heartbeat_epoch = tonumber(ARGV[5]),
  fencing_token = fence
}
redis.call('SET', KEYS[1], cjson.encode(nd), 'EX', tonumber(ARGV[6]))
if raw then return {1, fence, 'stale_takeover'} else return {1, fence, 'acquired'} end
"""

# KEYS[1]=lock_key  ARGV: run_id, fence, now_iso, now_epoch, ttl
_HEARTBEAT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local d = cjson.decode(raw)
if d.orchestrator_run_id ~= ARGV[1] or tonumber(d.fencing_token) ~= tonumber(ARGV[2]) then
  return 0
end
d.heartbeat_at = ARGV[3]
d.heartbeat_epoch = tonumber(ARGV[4])
redis.call('SET', KEYS[1], cjson.encode(d), 'EX', tonumber(ARGV[5]))
return 1
"""

# KEYS[1]=lock_key  ARGV: run_id, fence
_CHECK_FENCE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 'lost' end
local d = cjson.decode(raw)
if d.orchestrator_run_id == ARGV[1] and tonumber(d.fencing_token) == tonumber(ARGV[2]) then
  return 'valid'
end
if tonumber(d.fencing_token) > tonumber(ARGV[2]) then return 'superseded' end
return 'lost'
"""

# KEYS[1]=lock_key KEYS[2]=target_key  ARGV: run_id, fence, value, ttl ('' = no expiry)
_CAS_SET = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 'lost' end
local d = cjson.decode(raw)
if d.orchestrator_run_id == ARGV[1] and tonumber(d.fencing_token) == tonumber(ARGV[2]) then
  if ARGV[4] ~= '' then
    redis.call('SET', KEYS[2], ARGV[3], 'EX', tonumber(ARGV[4]))
  else
    redis.call('SET', KEYS[2], ARGV[3])
  end
  return 'valid'
end
if tonumber(d.fencing_token) > tonumber(ARGV[2]) then return 'superseded' end
return 'lost'
"""

# KEYS[1]=lock_key  ARGV: run_id, fence
_RELEASE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 'lost' end
local d = cjson.decode(raw)
if d.orchestrator_run_id == ARGV[1] and tonumber(d.fencing_token) == tonumber(ARGV[2]) then
  redis.call('DEL', KEYS[1])
  return 'valid'
end
if tonumber(d.fencing_token) > tonumber(ARGV[2]) then return 'superseded' end
return 'lost'
"""


class AtomicBackend(Protocol):
    def acquire(self, lock_key: str, fence_key: str, *, run_id: str, owner_host: str,
                pid: int, now_iso: str, now_epoch: float, ttl: int,
                stale: int) -> AcquireResult: ...
    def heartbeat(self, lock_key: str, *, run_id: str, fence: int, now_iso: str,
                  now_epoch: float, ttl: int) -> bool: ...
    def check_fence(self, lock_key: str, *, run_id: str, fence: int) -> str: ...
    def cas_set(self, lock_key: str, target_key: str, value: str, *, run_id: str,
                fence: int, ttl: Optional[int] = None) -> str: ...
    def release_lock(self, lock_key: str, *, run_id: str, fence: int) -> str: ...
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str, ex: Optional[int] = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def read_lock(self, lock_key: str) -> Optional[dict]: ...


class RedisLuaBackend:
    """Production backend: atomic compound ops via Upstash Redis Lua EVAL."""

    def __init__(self, redis):
        self._r = redis

    @staticmethod
    def _as_list(result) -> list:
        return list(result) if isinstance(result, (list, tuple)) else [result]

    def acquire(self, lock_key, fence_key, *, run_id, owner_host, pid, now_iso,
                now_epoch, ttl, stale) -> AcquireResult:
        out = self._as_list(self._r.eval(
            _ACQUIRE, keys=[lock_key, fence_key],
            args=[run_id, owner_host, str(pid), now_iso, str(int(now_epoch)),
                  str(ttl), str(stale)],
        ))
        return AcquireResult(acquired=bool(int(out[0])), fence=int(out[1]),
                             outcome=str(out[2]))

    def heartbeat(self, lock_key, *, run_id, fence, now_iso, now_epoch, ttl) -> bool:
        out = self._r.eval(_HEARTBEAT, keys=[lock_key],
                           args=[run_id, str(fence), now_iso,
                                 str(int(now_epoch)), str(ttl)])
        return bool(int(out))

    def check_fence(self, lock_key, *, run_id, fence) -> str:
        return str(self._r.eval(_CHECK_FENCE, keys=[lock_key],
                               args=[run_id, str(fence)]))

    def cas_set(self, lock_key, target_key, value, *, run_id, fence, ttl=None) -> str:
        return str(self._r.eval(_CAS_SET, keys=[lock_key, target_key],
                               args=[run_id, str(fence), value,
                                     "" if ttl is None else str(ttl)]))

    def release_lock(self, lock_key, *, run_id, fence) -> str:
        return str(self._r.eval(_RELEASE, keys=[lock_key],
                               args=[run_id, str(fence)]))

    def get(self, key) -> Optional[str]:
        v = self._r.get(key)
        return v if v is None else str(v)

    def set(self, key, value, ex=None) -> None:
        if ex is None:
            self._r.set(key, value)
        else:
            self._r.set(key, value, ex=ex)

    def delete(self, key) -> None:
        self._r.delete(key)

    def read_lock(self, lock_key) -> Optional[dict]:
        raw = self.get(lock_key)
        return json.loads(raw) if raw else None


class InMemoryBackend:
    """Test backend: identical semantics, process-local, thread-atomic.

    No background TTL expiry — staleness is decided purely from the explicit
    now_epoch the caller passes (the same input the Lua path uses), so tests are
    deterministic.
    """

    def __init__(self):
        self._store: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._lock = threading.RLock()

    def acquire(self, lock_key, fence_key, *, run_id, owner_host, pid, now_iso,
                now_epoch, ttl, stale) -> AcquireResult:
        with self._lock:
            raw = self._store.get(lock_key)
            existed = raw is not None
            if raw:
                d = json.loads(raw)
                if d["orchestrator_run_id"] == run_id:
                    d["heartbeat_at"] = now_iso
                    d["heartbeat_epoch"] = int(now_epoch)
                    self._store[lock_key] = json.dumps(d)
                    return AcquireResult(True, int(d["fencing_token"]), "reacquired")
                hb = int(d.get("heartbeat_epoch") or 0)
                if (int(now_epoch) - hb) < int(stale):
                    return AcquireResult(False, int(d["fencing_token"]), "held")
            fence = self._counters.get(fence_key, 0) + 1
            self._counters[fence_key] = fence
            self._store[lock_key] = json.dumps({
                "orchestrator_run_id": run_id, "owner_host": owner_host, "pid": int(pid),
                "acquired_at": now_iso, "heartbeat_at": now_iso,
                "heartbeat_epoch": int(now_epoch), "fencing_token": fence,
            })
            return AcquireResult(True, fence, "stale_takeover" if existed else "acquired")

    def heartbeat(self, lock_key, *, run_id, fence, now_iso, now_epoch, ttl) -> bool:
        with self._lock:
            raw = self._store.get(lock_key)
            if not raw:
                return False
            d = json.loads(raw)
            if d["orchestrator_run_id"] != run_id or int(d["fencing_token"]) != int(fence):
                return False
            d["heartbeat_at"] = now_iso
            d["heartbeat_epoch"] = int(now_epoch)
            self._store[lock_key] = json.dumps(d)
            return True

    def check_fence(self, lock_key, *, run_id, fence) -> str:
        with self._lock:
            raw = self._store.get(lock_key)
            if not raw:
                return LOST
            d = json.loads(raw)
            if d["orchestrator_run_id"] == run_id and int(d["fencing_token"]) == int(fence):
                return VALID
            if int(d["fencing_token"]) > int(fence):
                return SUPERSEDED
            return LOST

    def cas_set(self, lock_key, target_key, value, *, run_id, fence, ttl=None) -> str:
        with self._lock:
            verdict = self.check_fence(lock_key, run_id=run_id, fence=fence)
            if verdict == VALID:
                self._store[target_key] = value
            return verdict

    def release_lock(self, lock_key, *, run_id, fence) -> str:
        with self._lock:
            verdict = self.check_fence(lock_key, run_id=run_id, fence=fence)
            if verdict == VALID:
                self._store.pop(lock_key, None)
            return verdict

    def get(self, key) -> Optional[str]:
        with self._lock:
            return self._store.get(key)

    def set(self, key, value, ex=None) -> None:
        with self._lock:
            self._store[key] = value

    def delete(self, key) -> None:
        with self._lock:
            self._store.pop(key, None)

    def read_lock(self, lock_key) -> Optional[dict]:
        raw = self.get(lock_key)
        return json.loads(raw) if raw else None


def redis_lua_backend_from_env() -> RedisLuaBackend:
    """Build the production backend from the repo env (Upstash REST)."""
    import os
    from upstash_redis import Redis
    redis = Redis(url=os.environ["UPSTASH_REDIS_REST_URL"],
                  token=os.environ["UPSTASH_REDIS_REST_TOKEN"])
    return RedisLuaBackend(redis)
