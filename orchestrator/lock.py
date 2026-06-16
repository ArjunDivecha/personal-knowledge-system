"""
=============================================================================
MODULE: orchestrator/lock.py
=============================================================================

DESCRIPTION:
The orchestrator fencing lock (Atomicity And Race Contract). Wraps an
AtomicBackend and exposes the operations the engine/ledger need:

- acquire(): atomic lock-acquire + fence-increment (one backend op). Captures
  the fence token for this run.
- heartbeat(): fence-guarded refresh; returns False the instant a newer run has
  taken over (so the engine can abort instead of mutating).
- check(): equality-only fence verdict (valid | superseded | lost).
- cas_set(): fence-guarded compare-and-set of an arbitrary key (used by the
  ledger for terminal stage writes).
- release(): atomic, fence-guarded lock release.

Lock parameters come from orchestrator/config.py: key
`pks:orchestrator:lock:{run_date}`, TTL 2h, heartbeat 60s, stale 90min.

INPUT/OUTPUT FILES: none directly (operates on the backend's Redis keys).
=============================================================================
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Callable, Optional

from . import config
from .backends import AcquireResult, AtomicBackend, VALID


def system_clock() -> tuple[str, float]:
    """(iso8601, epoch_seconds) from the wall clock."""
    now = datetime.now().astimezone()
    return now.isoformat(timespec="seconds"), now.timestamp()


class FencingLock:
    def __init__(self, backend: AtomicBackend, run_date: str, run_id: str, *,
                 clock: Callable[[], tuple[str, float]] = system_clock,
                 owner_host: Optional[str] = None, pid: Optional[int] = None,
                 ttl: int = config.LOCK_TTL_SECONDS,
                 stale: int = config.STALE_THRESHOLD_SECONDS):
        self.backend = backend
        self.run_date = run_date
        self.run_id = run_id
        self.lock_key = config.KEY_LOCK.format(run_date=run_date)
        self.fence_key = config.KEY_FENCE.format(run_date=run_date)
        self.clock = clock
        self.owner_host = owner_host or config.owner_host()
        self.pid = pid if pid is not None else os.getpid()
        self.ttl = ttl
        self.stale = stale
        self.fence: Optional[int] = None

    def acquire(self) -> AcquireResult:
        now_iso, now_epoch = self.clock()
        res = self.backend.acquire(
            self.lock_key, self.fence_key, run_id=self.run_id,
            owner_host=self.owner_host, pid=self.pid, now_iso=now_iso,
            now_epoch=now_epoch, ttl=self.ttl, stale=self.stale)
        if res.acquired:
            self.fence = res.fence
        return res

    def heartbeat(self) -> bool:
        if self.fence is None:
            return False
        now_iso, now_epoch = self.clock()
        return self.backend.heartbeat(
            self.lock_key, run_id=self.run_id, fence=self.fence,
            now_iso=now_iso, now_epoch=now_epoch, ttl=self.ttl)

    def check(self) -> str:
        if self.fence is None:
            return "lost"
        return self.backend.check_fence(self.lock_key, run_id=self.run_id,
                                        fence=self.fence)

    def cas_set(self, target_key: str, value: str, ttl: Optional[int] = None) -> str:
        if self.fence is None:
            return "lost"
        return self.backend.cas_set(self.lock_key, target_key, value,
                                    run_id=self.run_id, fence=self.fence, ttl=ttl)

    def release(self) -> bool:
        """Delete the lock only if it is still ours (fence valid)."""
        if self.fence is None:
            return False
        return self.backend.release_lock(self.lock_key, run_id=self.run_id,
                                         fence=self.fence) == VALID
