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


# ── Phase 2: real Worker-backed Dream client ─────────────────────────────────
import json as _json
import os as _os
import urllib.error as _urlerror
import urllib.request as _urlrequest

#: cron label the orchestrator stamps on Worker start requests.
DEFAULT_ORCH_CRON = "m4-orchestrator"


class DreamClientError(RuntimeError):
    """A recoverable transport/HTTP error talking to the Dream Worker.

    Raised for non-terminal HTTP failures so the orchestrator marks the Dream
    stage failed_recoverable (and a later resume reattaches to the same
    dream_run_id). Known terminal rejection payloads are returned, not raised.
    """


def _urllib_transport(method, url, headers, body, timeout):
    """Default HTTP transport: (status_code, parsed_json_or_text_dict)."""
    data = _json.dumps(body).encode("utf-8") if body is not None else None
    req = _urlrequest.Request(url, data=data, method=method, headers=headers)
    try:
        with _urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted base url)
            raw = resp.read().decode("utf-8")
            return resp.status, (_json.loads(raw) if raw else {})
    except _urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = _json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": raw}
        return exc.code, payload


class HttpDreamClient:
    """DreamClient backed by the async Worker endpoints (Phase 2).

    start  -> POST /ops/dream/scheduled_governed/start
    status -> GET  /ops/dream/scheduled_governed/status?run_id=...

    - Operator Bearer token from DREAM_OPERATOR_TOKEN.
    - Base URL from DREAM_MCP_BASE_URL (config.dream_base_url()).
    - Finite, explicit timeouts.
    - HTTP 404 status -> None.
    - Non-2xx raises DreamClientError UNLESS the payload is a known terminal
      rejection (so DREAM_VERIFY can fail it terminally rather than loop).
    Transport is injectable for tests.
    """

    def __init__(self, *, base_url: Optional[str] = None, token: Optional[str] = None,
                 timeout: float = 30.0, cron: str = DEFAULT_ORCH_CRON,
                 clock: Callable[[], float] = time.time, transport=None):
        self.base_url = (base_url or config.dream_base_url()).rstrip("/")
        self.token = token if token is not None else _os.environ.get("DREAM_OPERATOR_TOKEN", "")
        self.timeout = timeout
        self.cron = cron
        self.clock = clock
        self.transport = transport or _urllib_transport

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def start(self, *, dream_run_id, orchestrator_run_id, run_date, mode,
              fencing_token) -> dict:
        body = {
            "run_id": dream_run_id,
            "orchestrator_run_id": orchestrator_run_id,
            "run_date": run_date,
            "mode": mode,
            "fencing_token": fencing_token,
            "cron": self.cron,
            "scheduled_time": int(self.clock() * 1000),
        }
        code, payload = self.transport(
            "POST", f"{self.base_url}/ops/dream/scheduled_governed/start",
            self._headers(), body, self.timeout)
        if code in (200, 202):
            return payload
        # Terminal-ish rejections (live disabled / date locked) carry a payload
        # the caller should see rather than retry on.
        if code in (400, 403, 409) and isinstance(payload, dict):
            return payload
        raise DreamClientError(f"Dream start HTTP {code}: {payload}")

    def status(self, dream_run_id: str) -> Optional[dict]:
        code, payload = self.transport(
            "GET",
            f"{self.base_url}/ops/dream/scheduled_governed/status?run_id={dream_run_id}",
            self._headers(), None, self.timeout)
        if code == 404:
            return None
        if 200 <= code < 300:
            return payload
        if isinstance(payload, dict) and str(payload.get("status") or "").startswith("rejected_"):
            return payload
        raise DreamClientError(f"Dream status HTTP {code}: {payload}")


def default_dream_client() -> DreamClient:
    """Select the Dream client. Phase 2 keeps the shadow client by default;
    set PKS_ORCH_DREAM_CLIENT=http to use the Worker-backed HttpDreamClient.
    """
    if _os.environ.get("PKS_ORCH_DREAM_CLIENT") == "http":
        return HttpDreamClient()
    return Phase1ShadowDreamClient()
