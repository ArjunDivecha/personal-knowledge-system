"""
=============================================================================
MODULE: orchestrator/states.py
=============================================================================

DESCRIPTION:
The orchestrator state machine: the ordered STAGES, the per-stage record
schema, stage/terminal status vocabularies, and transition validation. This is
the single source of truth the ledger and engine validate against.

Stage order and terminal statuses are copied verbatim from
docs/pks-nightly-orchestrator-redesign-2026-06-16.md.

INPUT/OUTPUT FILES: none (definitions + pure functions).
=============================================================================
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

# Ordered stages (spec "The state machine is:").
STAGES: list[str] = [
    "INIT",
    "LOCKED",
    "PREFLIGHT",
    "SNAPSHOT_BEFORE",
    "INGEST_TWITTER",
    "VALIDATE_TWITTER",
    "INGEST_GITHUB",
    "VALIDATE_GITHUB",
    "INGEST_AGENT_SESSIONS",
    "VALIDATE_AGENT_SESSIONS",
    "DREAM_START",
    "DREAM_WAIT",
    "DREAM_VERIFY",
    "INDEX_VERIFY",
    "CONSISTENCY_VERIFY",
    "REPORT_WRITE",
    "NOTIFY",
    "DONE",
]
_STAGE_INDEX = {name: i for i, name in enumerate(STAGES)}

# Run-level terminal statuses (spec "Terminal statuses:").
TERMINAL_STATUSES: frozenset[str] = frozenset({
    "completed",
    "completed_with_warnings",
    "completed_with_holds",
    "failed_recoverable",
    "failed_terminal",
    "abandoned_by_newer_run",
})

# Per-stage record statuses. The "completed*" / "failed*" / "abandoned" terms
# reuse the terminal vocabulary at stage granularity; "pending"/"running" are
# transient and never appear as a run terminal status.
STAGE_PENDING = "pending"
STAGE_RUNNING = "running"
STAGE_OK_STATUSES = frozenset({
    "completed", "completed_with_warnings", "completed_with_holds",
})
STAGE_FAIL_STATUSES = frozenset({
    "failed_recoverable", "failed_terminal", "abandoned_by_newer_run",
})
STAGE_TERMINAL_STATUSES = STAGE_OK_STATUSES | STAGE_FAIL_STATUSES


def stage_index(stage: str) -> int:
    if stage not in _STAGE_INDEX:
        raise KeyError(f"unknown stage {stage!r}")
    return _STAGE_INDEX[stage]


def next_stage(stage: str) -> Optional[str]:
    """The stage that follows `stage`, or None if `stage` is DONE."""
    i = stage_index(stage)
    return STAGES[i + 1] if i + 1 < len(STAGES) else None


def is_valid_transition(from_stage: str, to_stage: str) -> bool:
    """A run may stay on a stage (retry) or advance exactly one stage.

    Regressing to an earlier stage or skipping a stage is invalid. This guards
    the ledger against corrupt/forged progressions.
    """
    fi, ti = stage_index(from_stage), stage_index(to_stage)
    return ti == fi or ti == fi + 1


def make_stage_record(stage: str, *, status: str = STAGE_PENDING, attempt: int = 0,
                      started_at: Optional[str] = None,
                      completed_at: Optional[str] = None,
                      counts: Optional[dict] = None,
                      warnings: Optional[list] = None,
                      errors: Optional[list] = None,
                      retryable: bool = True,
                      next_action: Optional[str] = None) -> dict:
    """Build a stage record in the spec's schema (Each stage record includes …)."""
    if stage not in _STAGE_INDEX:
        raise KeyError(f"unknown stage {stage!r}")
    return {
        "stage": stage,
        "status": status,
        "attempt": attempt,
        "started_at": started_at,
        "completed_at": completed_at,
        "counts": counts or {},
        "warnings": warnings or [],
        "errors": errors or [],
        "retryable": retryable,
        "next_action": next_action,
    }


def derive_run_status(stage_records: dict[str, dict], reached_done: bool) -> str:
    """Derive the run terminal status from the stage records.

    Precedence: any failed_terminal -> failed_terminal; any
    abandoned_by_newer_run -> abandoned_by_newer_run; any failed_recoverable ->
    failed_recoverable; else if DONE reached, fold warnings/holds into the
    completed_* family; otherwise the run is not terminal (returns "running").
    """
    statuses = {r.get("status") for r in stage_records.values()}
    if "failed_terminal" in statuses:
        return "failed_terminal"
    if "abandoned_by_newer_run" in statuses:
        return "abandoned_by_newer_run"
    if "failed_recoverable" in statuses:
        return "failed_recoverable"
    if not reached_done:
        return "running"
    if "completed_with_holds" in statuses:
        return "completed_with_holds"
    if "completed_with_warnings" in statuses:
        return "completed_with_warnings"
    return "completed"


def utcnow_iso() -> str:
    """Timezone-aware ISO-8601 timestamp (used for record stamps)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
