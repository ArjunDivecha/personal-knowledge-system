"""
=============================================================================
MODULE: orchestrator/engine.py
=============================================================================

DESCRIPTION:
The orchestrator engine: drives the state machine for the `preflight`, `run`,
`resume`, and `report` subcommands. It acquires the fencing lock, records each
stage through the ledger (fence-guarded terminal writes), heartbeats between
stages, and always renders a report — even on failure or supersession.

Phase-1 guarantees:
- Non-mutating: stage executors are shadow (orchestrator/stages.py); mutations
  are gated by `mutations_enabled` (False).
- A `--mode live` request while mutations are disabled is downgraded to shadow
  with a loud run-level warning (never silently mutates).
- Same-host-only resume (spec): a resume on a different host is refused.

All collaborators (backend, dream client, clock, sleep, preflight deps) are
injectable so the engine is unit/integration testable without network.

INPUT FILES:
- ingestion/checkpoints/orchestrator_runs/{run_date}.json (resume/report)

OUTPUT FILES:
- ingestion/checkpoints/orchestrator_runs/{run_date}.json (run/resume)
- scripts/reports/pks-nightly-{run_date}.{json,md} (always, incl. on failure)
- Redis orchestrator keys (via lock/backend)
=============================================================================
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Callable, Optional

from . import config, dream as dreammod, ids, report as reportmod, states
from .backends import VALID, AtomicBackend
from .ledger import RunLedger
from .lock import FencingLock, system_clock
from .preflight import PreflightDeps, run_preflight
from .stages import RUN_STAGES, STAGE_EXECUTORS, StageContext


class Orchestrator:
    def __init__(self, *, backend: Optional[AtomicBackend] = None,
                 dream_client: Optional[dreammod.DreamClient] = None,
                 preflight_deps: Optional[PreflightDeps] = None,
                 clock: Callable[[], tuple[str, float]] = system_clock,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 owner_host: Optional[str] = None,
                 mutations_enabled: bool = config.MUTATIONS_ENABLED,
                 dream_poll_interval: float = config.DREAM_POLL_INTERVAL_SECONDS,
                 dream_timeout: float = config.DREAM_FIRST_WAIT_TIMEOUT_SECONDS,
                 suffix: Optional[str] = None):
        self._backend = backend
        self.dream_client = dream_client or dreammod.Phase1ShadowDreamClient()
        self.preflight_deps = preflight_deps
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.owner_host = owner_host or config.owner_host()
        self.mutations_enabled = mutations_enabled
        self.dream_poll_interval = dream_poll_interval
        self.dream_timeout = dream_timeout
        self.suffix = suffix

    # ── backend (lazy, production) ───────────────────────────────────────────
    @property
    def backend(self) -> AtomicBackend:
        if self._backend is None:
            from .backends import redis_lua_backend_from_env
            self._backend = redis_lua_backend_from_env()
        return self._backend

    # ── helpers ──────────────────────────────────────────────────────────────
    def _resolve_run_date(self, arg: Optional[str]) -> str:
        if not arg or arg == "auto":
            return datetime.now(config.PACIFIC).date().isoformat()
        datetime.strptime(arg, "%Y-%m-%d")  # validate
        return arg

    def _effective_mode(self, requested_mode: str) -> tuple[str, list[str]]:
        if requested_mode == "live" and not self.mutations_enabled:
            return "shadow", [
                "live mode requested but mutations are disabled in Phase 1; "
                "executed in shadow (non-mutating)."]
        return requested_mode, []

    def _make_context(self, run_date, requested_mode, effective_mode, identity,
                      ledger, lock, force_notify) -> StageContext:
        ctx = StageContext(
            run_date=run_date, requested_mode=requested_mode,
            effective_mode=effective_mode, identity=identity, ledger=ledger,
            lock=lock, backend=self._backend, dream_client=self.dream_client,
            preflight_deps=self.preflight_deps,
            mutations_enabled=self.mutations_enabled, force_notify=force_notify,
            sleep=self.sleep, monotonic=self.monotonic)
        ctx.dream_poll_interval = self.dream_poll_interval
        ctx.dream_timeout = self.dream_timeout
        return ctx

    def _emit_report(self, ledger: RunLedger) -> tuple[str, str]:
        jpath, mpath = reportmod.write_reports(ledger.doc)
        try:
            ledger.set_report_paths(jpath, mpath)
        except Exception:
            pass
        return jpath, mpath

    def _exit_code(self, ledger: RunLedger) -> int:
        return 0 if ledger.doc["status"] in states.STAGE_OK_STATUSES else 1

    # ── stage loop (shared by run + resume) ──────────────────────────────────
    def _run_stages(self, ctx: StageContext, start_stage: str) -> int:
        ledger, lock = ctx.ledger, ctx.lock
        start_idx = RUN_STAGES.index(start_stage)
        for stage in RUN_STAGES[start_idx:]:
            # Heartbeat / supersession check before each stage.
            if not lock.heartbeat():
                self._abandon(ledger, stage)
                self._emit_report(ledger)
                return self._exit_code(ledger)

            if ledger.begin_stage(stage) != VALID:
                self._abandon(ledger, stage)
                self._emit_report(ledger)
                return self._exit_code(ledger)

            try:
                record = STAGE_EXECUTORS[stage](ctx)
            except Exception as exc:
                record = states.make_stage_record(
                    stage, status="failed_recoverable",
                    errors=[f"{exc.__class__.__name__}: {exc}"],
                    next_action="Investigate the stage error on M4, then resume.")

            verdict = ledger.commit_stage(stage, record)
            if verdict != VALID:
                self._emit_report(ledger)   # abandoned recorded by commit_stage
                return self._exit_code(ledger)

            if record["status"] in states.STAGE_FAIL_STATUSES:
                # Stop the run but always leave a (partial) report reflecting the
                # final status behind (finalize first, then render).
                ledger.finalize(reached_done=False)
                self._emit_report(ledger)
                return self._exit_code(ledger)

        ledger.finalize(reached_done=True)
        # Refresh the report AFTER finalize so the durable report shows the
        # terminal verdict (REPORT_WRITE itself runs before NOTIFY/DONE).
        self._emit_report(ledger)
        return self._exit_code(ledger)

    def _abandon(self, ledger: RunLedger, stage: str) -> None:
        ledger.doc["stages"][stage] = states.make_stage_record(
            stage, status="abandoned_by_newer_run",
            next_action="A newer run owns this date; this run aborted.")
        ledger.doc["status"] = "abandoned_by_newer_run"
        ledger._persist_local_only()

    # ── subcommands ──────────────────────────────────────────────────────────
    def run(self, *, mode: str = "shadow", run_date: Optional[str] = None,
            force_notify: bool = False) -> int:
        rd = self._resolve_run_date(run_date)

        existing = RunLedger.load(rd)
        if existing is not None:
            if existing.resume_stage() is None:
                print(f"[orchestrator] run {rd} already complete "
                      f"({existing.doc['status']}); nothing to do.")
                return 0
            print(f"[orchestrator] run {rd} exists and is incomplete; resuming.")
            return self.resume(run_date=rd, force_notify=force_notify)

        now = datetime.now()
        identity = ids.make_identity(datetime.now(config.PACIFIC).date(), now,
                                     suffix=self.suffix)
        lock = FencingLock(self.backend, rd, identity.orchestrator_run_id,
                           clock=self.clock, owner_host=self.owner_host)
        acq = lock.acquire()
        if not acq.acquired:
            print(f"[orchestrator] lock for {rd} held by a live run "
                  f"(fence {acq.fence}); not starting a second run.")
            return 0

        effective_mode, warns = self._effective_mode(mode)
        ledger = RunLedger.new(rd, identity, mode=effective_mode, fence=acq.fence,
                               owner_host=self.owner_host, pid=lock.pid)
        ledger.attach(self._backend, lock)
        ledger.doc["requested_mode"] = mode
        ledger.doc["warnings"] = warns
        ledger.doc["status"] = "running"
        if ledger._persist() != VALID:
            print("[orchestrator] lost lock immediately after acquire; aborting.")
            return 1

        ctx = self._make_context(rd, mode, effective_mode, identity, ledger,
                                 lock, force_notify)
        try:
            code = self._run_stages(ctx, start_stage=RUN_STAGES[0])
        finally:
            lock.release()
        return code

    def resume(self, *, run_date: Optional[str] = None,
               force_notify: bool = False) -> int:
        rd = self._resolve_run_date(run_date)
        ledger = RunLedger.load(rd)
        if ledger is None:
            # No run exists. Catch-up policy: before 08:45 Pacific start late,
            # otherwise mark missed locally and let the dead-man alert at 09:00.
            now_pac = datetime.now(config.PACIFIC)
            cutoff = now_pac.replace(hour=config.CATCHUP_CUTOFF_HHMM[0],
                                     minute=config.CATCHUP_CUTOFF_HHMM[1],
                                     second=0, microsecond=0)
            if now_pac < cutoff:
                print(f"[orchestrator] no run for {rd}; starting late.")
                return self.run(run_date=rd, force_notify=force_notify)
            print(f"[orchestrator] no run for {rd} after catch-up cutoff; marking missed.")
            self._write_missed(rd)
            return 1

        if ledger.resume_stage() is None:
            print(f"[orchestrator] run {rd} already complete ({ledger.doc['status']}).")
            return 0

        # Same-host-only resume (v1).
        if ledger.doc.get("owner_host") != self.owner_host:
            print(f"[orchestrator] refuse cross-host resume: run owned by "
                  f"{ledger.doc.get('owner_host')!r}, this host is {self.owner_host!r}.")
            return 1

        identity = ids.RunIdentity(
            orchestrator_run_id=ledger.doc["orchestrator_run_id"],
            dream_run_id=ledger.doc["dream_run_id"],
            suffix=ids.suffix_of(ledger.doc["orchestrator_run_id"]))
        lock = FencingLock(self.backend, rd, identity.orchestrator_run_id,
                           clock=self.clock, owner_host=self.owner_host)
        acq = lock.acquire()
        if not acq.acquired:
            print(f"[orchestrator] cannot reacquire lock for {rd} "
                  f"(held by fence {acq.fence}); not resuming.")
            return 0
        ledger.attach(self._backend, lock)
        ledger.doc["fencing_token"] = acq.fence

        effective_mode = ledger.doc.get("mode", "shadow")
        requested_mode = ledger.doc.get("requested_mode", effective_mode)
        ctx = self._make_context(rd, requested_mode, effective_mode, identity,
                                 ledger, lock, force_notify)
        start = ledger.resume_stage()
        if start in ("INIT", "LOCKED"):
            start = RUN_STAGES[0]
        try:
            code = self._run_stages(ctx, start_stage=start)
        finally:
            lock.release()
        return code

    def report(self, *, run_date: Optional[str] = None) -> int:
        rd = self._resolve_run_date(run_date)
        ledger = RunLedger.load(rd)
        if ledger is None:
            print(f"[orchestrator] no ledger for {rd}; nothing to report.")
            return 1
        jpath, mpath = reportmod.write_reports(ledger.doc)
        print(f"[orchestrator] report: {jpath}\n[orchestrator] report: {mpath}")
        print(f"[orchestrator] verdict: {ledger.doc.get('status')}")
        return 0

    def preflight(self) -> int:
        result = run_preflight(self.preflight_deps)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] in states.STAGE_OK_STATUSES else 1

    def _write_missed(self, run_date: str) -> None:
        doc = {
            "orchestrator_run_id": None, "dream_run_id": None, "run_date": run_date,
            "mode": "shadow", "owner_host": self.owner_host, "pid": None,
            "fencing_token": None, "status": "failed_terminal",
            "current_stage": "INIT", "started_at": None,
            "updated_at": states.utcnow_iso(),
            "completed_at": states.utcnow_iso(),
            "report": {"json": None, "md": None},
            "stages": {"INIT": states.make_stage_record(
                "INIT", status="failed_terminal",
                errors=["No orchestrator run started for this date (missed night)."],
                next_action="Investigate why the nightly never started.")},
        }
        led = RunLedger(doc)
        led._persist_local_only()
        reportmod.write_reports(doc)
