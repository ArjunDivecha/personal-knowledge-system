"""
=============================================================================
MODULE: orchestrator/stages.py
=============================================================================

DESCRIPTION:
The Phase-1 SHADOW stage executors. Each executor returns a terminal stage
record (orchestrator/states.make_stage_record). NOTHING here mutates production
state: the ingestion stages are non-mutating no-ops that record an
`executed_mode: shadow` count; the Dream stages drive the injectable Dream
client (a shadow client by default) and enforce the shadow contract; the
verify/report stages are read-only.

Live execution of the real ingestion/Dream apply is a later phase; the
`ctx.mutations_enabled` gate (False in Phase 1) guards every would-be mutation.

INPUT FILES: none directly (validate/snapshot read Redis counts best-effort).
OUTPUT FILES:
- REPORT_WRITE writes scripts/reports/pks-nightly-{run_date}.{json,md} (read-only
  rendering of the ledger; not a production mutation).
=============================================================================
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import dream as dreammod
from . import preflight as preflightmod
from . import report as reportmod
from . import states
from .ids import RunIdentity
from .ledger import RunLedger
from .lock import FencingLock


@dataclass
class StageContext:
    run_date: str
    requested_mode: str            # 'shadow' | 'live'
    effective_mode: str            # always 'shadow' in Phase 1
    identity: RunIdentity
    ledger: RunLedger
    lock: FencingLock
    backend: object
    dream_client: dreammod.DreamClient
    preflight_deps: Optional[preflightmod.PreflightDeps] = None
    mutations_enabled: bool = False
    force_notify: bool = False
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    dream_poll_interval: float = None  # type: ignore
    dream_timeout: float = None        # type: ignore


def _ok(stage, **kw):
    kw.setdefault("status", "completed")
    return states.make_stage_record(stage, **kw)


# ── stage executors ──────────────────────────────────────────────────────────
def stage_preflight(ctx: StageContext) -> dict:
    result = preflightmod.run_preflight(ctx.preflight_deps)
    return states.make_stage_record(
        "PREFLIGHT",
        status=result["status"],
        counts={"auth_route": result.get("auth_route")},
        warnings=result.get("warnings", []),
        errors=result.get("errors", []),
        retryable=result["status"] != "failed_terminal",
        next_action=result.get("next_action"))


def stage_snapshot_before(ctx: StageContext) -> dict:
    counts = {}
    try:  # read-only: capture current vector count for the before/after delta
        v = ctx.backend.get("pks:storage:vector_count") if ctx.backend else None
        if v is not None:
            counts["vector_count_before"] = v
    except Exception:
        pass
    return _ok("SNAPSHOT_BEFORE", counts=counts)


def _shadow_ingest(stage: str) -> dict:
    return _ok(stage, counts={"saved": 0, "before": 0, "executed_mode": "shadow",
                              "note": "shadow no-op (Phase 1): no ingestion writes"})


def stage_ingest_twitter(ctx): return _shadow_ingest("INGEST_TWITTER")
def stage_ingest_github(ctx): return _shadow_ingest("INGEST_GITHUB")
def stage_ingest_agent_sessions(ctx): return _shadow_ingest("INGEST_AGENT_SESSIONS")


def stage_validate_twitter(ctx): return _ok("VALIDATE_TWITTER", counts={"executed_mode": "shadow"})
def stage_validate_github(ctx): return _ok("VALIDATE_GITHUB", counts={"executed_mode": "shadow"})
def stage_validate_agent_sessions(ctx): return _ok("VALIDATE_AGENT_SESSIONS", counts={"executed_mode": "shadow"})


def _dream_start_rejection(resp: dict) -> str | None:
    """Return a Worker start rejection code if no background run exists."""
    if not resp:
        return None
    if resp.get("accepted") is False:
        return str(resp.get("error") or resp.get("status") or "worker_rejected")
    status = str(resp.get("status") or "")
    if status.startswith("rejected_"):
        return status
    error = str(resp.get("error") or "")
    if error.startswith("rejected_") or error == "date_locked":
        return error
    return None


def stage_dream_start(ctx: StageContext) -> dict:
    res = dreammod.ensure_dream_started(
        ctx.dream_client, dream_run_id=ctx.identity.dream_run_id,
        orchestrator_run_id=ctx.identity.orchestrator_run_id,
        run_date=ctx.run_date, mode=ctx.effective_mode,
        fencing_token=ctx.lock.fence or 0)
    resp = res.get("response") or {}
    rejection = _dream_start_rejection(resp)
    if rejection:
        return states.make_stage_record(
            "DREAM_START", status="failed_terminal",
            counts={
                "requested_mode": ctx.effective_mode,
                "executed_mode": resp.get("executed_mode"),
                "started": False,
                "reason": rejection,
                "accepted": False,
                "blocked_by": resp.get("blocked_by"),
            },
            errors=[f"Dream Worker rejected start: {rejection}"],
            retryable=False,
            next_action="Inspect the Worker date lock/status; do not wait on this dream_run_id.")
    return _ok("DREAM_START", counts={
        "requested_mode": ctx.effective_mode,
        "executed_mode": resp.get("executed_mode") or (res.get("status") or {}).get("executed_mode"),
        "started": res["started"], "reason": res["reason"]})


def stage_dream_wait(ctx: StageContext) -> dict:
    res = dreammod.wait_for_dream(
        ctx.dream_client, ctx.identity.dream_run_id,
        poll_interval=ctx.dream_poll_interval, timeout=ctx.dream_timeout,
        sleep=ctx.sleep, monotonic=ctx.monotonic)
    st = res.get("status") or {}
    if res["outcome"] == "timeout":
        return states.make_stage_record(
            "DREAM_WAIT", status="failed_recoverable",
            counts={"dream_status": st.get("status"), "outcome": "timeout"},
            errors=["Dream did not reach terminal within first timeout."],
            next_action="Resume reattaches to the same dream_run_id (no new start).")
    return _ok("DREAM_WAIT", counts={
        "dream_status": st.get("status"),
        "executed_mode": st.get("executed_mode"),
        "applied_count": st.get("applied_count"), "outcome": "terminal"})


def stage_dream_verify(ctx: StageContext) -> dict:
    st = ctx.dream_client.status(ctx.identity.dream_run_id)
    v = dreammod.verify_dream_status(st, ctx.effective_mode)
    counts = {
        "executed_mode": (st or {}).get("executed_mode"),
        "applied_count": (st or {}).get("applied_count"),
        "dream_status": (st or {}).get("status"),
    }
    if not v["ok"]:
        return states.make_stage_record(
            "DREAM_VERIFY", status="failed_terminal", counts=counts,
            errors=v["problems"], retryable=False,
            next_action="Dream contract violated; do not cut over.")
    if v["holds"] > 0:
        return states.make_stage_record(
            "DREAM_VERIFY", status="completed_with_holds", counts={**counts, "holds": v["holds"]},
            warnings=[f"{v['holds']} held op(s) awaiting judge."])
    return _ok("DREAM_VERIFY", counts=counts)


def stage_index_verify(ctx): return _ok("INDEX_VERIFY", counts={"mode": "verify_only"})
def stage_consistency_verify(ctx): return _ok("CONSISTENCY_VERIFY")


def stage_report_write(ctx: StageContext) -> dict:
    jpath, mpath = reportmod.write_reports(ctx.ledger.doc)
    ctx.ledger.set_report_paths(jpath, mpath)
    return _ok("REPORT_WRITE", counts={"json": jpath, "md": mpath})


def stage_notify(ctx: StageContext) -> dict:
    # skip-if-completed: do not resend unless --force-notify. In Phase 1
    # shadow_validation the orchestrator does NOT alert the user directly; the
    # old NightWatch/GitHub artifacts remain the user-facing truth.
    prior = ctx.ledger.doc["stages"].get("NOTIFY", {})
    if prior.get("status") in states.STAGE_OK_STATUSES and not ctx.force_notify:
        return _ok("NOTIFY", counts={"sent": False, "reason": "skip-if-completed"})
    return _ok("NOTIFY", counts={"sent": False, "reason": "shadow_validation: no user alert"})


def stage_done(ctx): return _ok("DONE")


STAGE_EXECUTORS: dict[str, Callable[[StageContext], dict]] = {
    "PREFLIGHT": stage_preflight,
    "SNAPSHOT_BEFORE": stage_snapshot_before,
    "INGEST_TWITTER": stage_ingest_twitter,
    "VALIDATE_TWITTER": stage_validate_twitter,
    "INGEST_GITHUB": stage_ingest_github,
    "VALIDATE_GITHUB": stage_validate_github,
    "INGEST_AGENT_SESSIONS": stage_ingest_agent_sessions,
    "VALIDATE_AGENT_SESSIONS": stage_validate_agent_sessions,
    "DREAM_START": stage_dream_start,
    "DREAM_WAIT": stage_dream_wait,
    "DREAM_VERIFY": stage_dream_verify,
    "INDEX_VERIFY": stage_index_verify,
    "CONSISTENCY_VERIFY": stage_consistency_verify,
    "REPORT_WRITE": stage_report_write,
    "NOTIFY": stage_notify,
    "DONE": stage_done,
}

# Stages executed by the engine, in order (INIT/LOCKED are setup milestones
# recorded by the ledger at creation).
RUN_STAGES = [s for s in states.STAGES if s not in ("INIT", "LOCKED")]
