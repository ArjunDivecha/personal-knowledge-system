"""
=============================================================================
MODULE: orchestrator/ledger.py
=============================================================================

DESCRIPTION:
The durable run ledger. One document per run_date holding the run identity,
fence, current stage, and every stage record. It is the state from which a
report can be rendered at any point and from which a same-host resume
continues.

Two durable copies:
- Local (authoritative for same-host resume; agent-session checkpoints are
  local-first per the spec):
  ingestion/checkpoints/orchestrator_runs/{run_date}.json  (atomic temp-rename)
- Redis mirror (shared visibility / dead-man):
  pks:orchestrator:run:{run_date}  plus last_started/heartbeat/completed/status/report.

Atomicity And Race Contract: every write of the Redis run document goes through
the fencing lock's compare-and-set, so a superseded run can never commit. A
rejected terminal write flips the stage record to `abandoned_by_newer_run`.

INPUT FILES:
- ingestion/checkpoints/orchestrator_runs/{run_date}.json  (on resume)

OUTPUT FILES:
- ingestion/checkpoints/orchestrator_runs/{run_date}.json
- Redis keys listed above (via the lock/backend)
=============================================================================
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import config, states
from .backends import VALID
from .ids import RunIdentity
from .lock import FencingLock


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class RunLedger:
    def __init__(self, doc: dict):
        self.doc = doc
        self._backend = None
        self._lock: Optional[FencingLock] = None

    # ── construction / load ──────────────────────────────────────────────────
    @classmethod
    def new(cls, run_date: str, identity: RunIdentity, *, mode: str, fence: int,
            owner_host: str, pid: int) -> "RunLedger":
        now = states.utcnow_iso()
        doc = {
            "orchestrator_run_id": identity.orchestrator_run_id,
            "dream_run_id": identity.dream_run_id,
            "run_date": run_date,
            "mode": mode,
            "owner_host": owner_host,
            "pid": pid,
            "fencing_token": fence,
            "status": "running",
            "current_stage": "INIT",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "report": {"json": None, "md": None},
            "stages": {},
        }
        led = cls(doc)
        # INIT and LOCKED are setup milestones, recorded completed up front.
        for stg in ("INIT", "LOCKED"):
            doc["stages"][stg] = states.make_stage_record(
                stg, status="completed", attempt=1,
                started_at=now, completed_at=now)
        doc["current_stage"] = "LOCKED"
        return led

    @classmethod
    def local_path(cls, run_date: str) -> Path:
        return config.LEDGER_DIR / f"{run_date}.json"

    @classmethod
    def load(cls, run_date: str) -> Optional["RunLedger"]:
        p = cls.local_path(run_date)
        if not p.exists():
            return None
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def attach(self, backend, lock: FencingLock) -> "RunLedger":
        self._backend = backend
        self._lock = lock
        return self

    # ── stage lifecycle ──────────────────────────────────────────────────────
    def begin_stage(self, stage: str) -> str:
        """Mark `stage` running (validates the transition); persist (CAS)."""
        cur = self.doc["current_stage"]
        if not states.is_valid_transition(cur, stage):
            raise ValueError(f"invalid stage transition {cur} -> {stage}")
        prior = self.doc["stages"].get(stage)
        attempt = (prior["attempt"] + 1) if prior else 1
        self.doc["stages"][stage] = states.make_stage_record(
            stage, status=states.STAGE_RUNNING, attempt=attempt,
            started_at=states.utcnow_iso())
        self.doc["current_stage"] = stage
        return self._persist()

    def commit_stage(self, stage: str, record: dict) -> str:
        """Write a terminal stage record via a fence-guarded CAS.

        Returns the fence verdict ('valid' | 'superseded' | 'lost'). On anything
        but 'valid', the stage is recorded `abandoned_by_newer_run` locally and
        the caller must abort the run.
        """
        record = dict(record)
        record["stage"] = stage
        record.setdefault("completed_at", states.utcnow_iso())
        if record.get("status") not in states.STAGE_TERMINAL_STATUSES:
            raise ValueError(f"commit_stage needs a terminal status, got {record.get('status')!r}")
        self.doc["stages"][stage] = record
        self.doc["status"] = states.derive_run_status(
            self.doc["stages"], reached_done=(stage == "DONE"))
        verdict = self._persist()
        if verdict != VALID:
            # We have been superseded mid-commit: do not advance, record it.
            record["status"] = "abandoned_by_newer_run"
            record["next_action"] = "A newer run owns this date; this run aborts."
            self.doc["stages"][stage] = record
            self.doc["status"] = "abandoned_by_newer_run"
            self._persist_local_only()
        return verdict

    # ── persistence ──────────────────────────────────────────────────────────
    def _persist(self) -> str:
        """CAS the Redis run doc (fence-guarded); write local + last_* on valid."""
        self.doc["updated_at"] = states.utcnow_iso()
        payload = json.dumps(self.doc, indent=2, sort_keys=True)
        verdict = VALID
        if self._lock is not None:
            verdict = self._lock.cas_set(
                config.KEY_RUN.format(run_date=self.doc["run_date"]),
                payload, ttl=config.LOCK_TTL_SECONDS * 12)
        if verdict == VALID:
            _atomic_write(self.local_path(self.doc["run_date"]), payload)
            self._mirror_last()
        return verdict

    def _persist_local_only(self) -> None:
        self.doc["updated_at"] = states.utcnow_iso()
        _atomic_write(self.local_path(self.doc["run_date"]),
                      json.dumps(self.doc, indent=2, sort_keys=True))

    def _mirror_last(self) -> None:
        if self._backend is None:
            return
        b = self._backend
        b.set(config.KEY_LAST_STARTED, self.doc["started_at"])
        b.set(config.KEY_LAST_HEARTBEAT, self.doc["updated_at"])
        b.set(config.KEY_LAST_STATUS, self.doc["status"])
        if self.doc.get("completed_at"):
            b.set(config.KEY_LAST_COMPLETED, self.doc["completed_at"])
        rep = self.doc.get("report", {}).get("md")
        if rep:
            b.set(config.KEY_LAST_REPORT, rep)

    # ── finalize / report ────────────────────────────────────────────────────
    def set_report_paths(self, json_path: str, md_path: str) -> str:
        self.doc["report"] = {"json": json_path, "md": md_path}
        return self._persist()

    def finalize(self, reached_done: bool) -> str:
        self.doc["status"] = states.derive_run_status(self.doc["stages"], reached_done)
        if self.doc["status"] in states.TERMINAL_STATUSES and self.doc["status"] != "running":
            self.doc["completed_at"] = states.utcnow_iso()
        return self._persist()

    # ── resume ───────────────────────────────────────────────────────────────
    def resume_stage(self) -> Optional[str]:
        """Earliest stage not completed-OK (the stage to (re)run on resume).

        None means the run already reached a clean terminal state.
        """
        for stg in states.STAGES:
            rec = self.doc["stages"].get(stg)
            if rec is None or rec.get("status") not in states.STAGE_OK_STATUSES:
                return stg
        return None
