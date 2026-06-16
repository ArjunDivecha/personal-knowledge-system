"""
=============================================================================
MODULE: orchestrator/dream.py
=============================================================================

DESCRIPTION:
Orchestrator-side Dream logic: the DreamClient interface, a Phase-1 in-process
SHADOW client (never mutates), the contract verifier, and the
start/wait/reattach helpers. The real async Cloudflare Worker endpoints are
Phase 2; in Phase 1 the orchestrator drives Dream against an injectable client
so the wait/timeout/reattach and shadow-contract logic are fully testable
without a Worker.

Contract enforced here (spec "Shadow mode" / "Dream wait and resume"):
- DREAM_START is issued at most once per dream_run_id (skip if a status exists).
- Resume reattaches to the existing dream_run_id; never starts a second run.
- Status must include executed_mode and applied_count; missing either is a
  terminal failure.
- A shadow run with applied_count > 0 is a terminal failure.

INPUT/OUTPUT FILES: none (network is Phase 2; Phase 1 client is in-process).
=============================================================================
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Protocol

from . import config


class DreamClient(Protocol):
    def start(self, *, dream_run_id: str, orchestrator_run_id: str, run_date: str,
              mode: str, fencing_token: int) -> dict: ...
    def status(self, dream_run_id: str) -> Optional[dict]: ...


class Phase1ShadowDreamClient:
    """In-process, non-mutating Dream simulator for Phase 1.

    `start` records a TERMINAL shadow status with applied_count == 0 (nothing is
    applied in shadow). `status` reattaches by dream_run_id. This client never
    contacts the Worker and never mutates anything.
    """

    def __init__(self):
        self._statuses: dict[str, dict] = {}

    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode,
              fencing_token) -> dict:
        if dream_run_id not in self._statuses:
            self._statuses[dream_run_id] = {
                "state": "terminal",
                "status": "completed_shadow",
                "requested_mode": mode,
                "executed_mode": "shadow",
                "dream_run_id": dream_run_id,
                "orchestrator_run_id": orchestrator_run_id,
                "run_date": run_date,
                "fencing_token": fencing_token,
                "applied_count": 0,
                "held_count": 0,
            }
        s = self._statuses[dream_run_id]
        return {
            "accepted": True,
            "requested_mode": mode,
            "executed_mode": s["executed_mode"],
            "dream_run_id": dream_run_id,
            "orchestrator_run_id": orchestrator_run_id,
            "run_date": run_date,
            "status_url": f"/ops/dream/scheduled_governed/status?run_id={dream_run_id}",
        }

    def status(self, dream_run_id: str) -> Optional[dict]:
        return self._statuses.get(dream_run_id)


def ensure_dream_started(client: DreamClient, *, dream_run_id: str,
                         orchestrator_run_id: str, run_date: str, mode: str,
                         fencing_token: int) -> dict:
    """Idempotent start: only call start() if no status exists for the id."""
    existing = client.status(dream_run_id)
    if existing is not None:
        return {"started": False, "reason": "status_exists",
                "response": None, "status": existing}
    response = client.start(
        dream_run_id=dream_run_id, orchestrator_run_id=orchestrator_run_id,
        run_date=run_date, mode=mode, fencing_token=fencing_token)
    return {"started": True, "reason": "started", "response": response,
            "status": client.status(dream_run_id)}


def wait_for_dream(client: DreamClient, dream_run_id: str, *,
                   poll_interval: float = config.DREAM_POLL_INTERVAL_SECONDS,
                   timeout: float = config.DREAM_FIRST_WAIT_TIMEOUT_SECONDS,
                   sleep: Callable[[float], None] = time.sleep,
                   monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Poll the existing dream_run_id until terminal or first-timeout.

    Reattach is implicit: we only ever read status for the SAME dream_run_id;
    no start is issued here. Returns {"outcome": "terminal"|"timeout",
    "status": <last status or None>}.
    """
    deadline = monotonic() + timeout
    while True:
        st = client.status(dream_run_id)
        if st is not None and st.get("state") == "terminal":
            return {"outcome": "terminal", "status": st}
        if monotonic() >= deadline:
            return {"outcome": "timeout", "status": st}
        sleep(poll_interval)


def verify_dream_status(status: Optional[dict], requested_mode: str) -> dict:
    """Apply the shadow/contract checks. Returns {ok, problems, holds}."""
    problems: list[str] = []
    if status is None:
        return {"ok": False, "problems": ["no dream status"], "holds": 0}
    if not status.get("executed_mode"):
        problems.append("missing executed_mode")
    if status.get("applied_count") is None:
        problems.append("missing applied_count")
    is_shadow = requested_mode == "shadow" or status.get("executed_mode") == "shadow"
    if is_shadow and (status.get("applied_count") or 0) > 0:
        problems.append("shadow run applied_count>0")
    # A Worker-side rejection terminal status is also a problem to surface.
    term = status.get("status") or ""
    if term.startswith("rejected_"):
        problems.append(f"worker rejected: {term}")
    return {"ok": not problems, "problems": problems,
            "holds": int(status.get("held_count") or 0)}
