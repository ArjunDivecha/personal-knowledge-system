"""
=============================================================================
MODULE: orchestrator/ids.py
=============================================================================

DESCRIPTION:
Run-identity generation for the orchestrator and the Dream run, per the
Atomicity And Race Contract:

    orchestrator_run_id = pksn_YYYYMMDD_HHMMSS_<8hex>
    dream_run_id        = dga_YYYYMMDD_<8hex>

The <8hex> suffix is SHARED between the two ids for operator traceability, and
`dream_run_id` is the Cloudflare Dream idempotency key.

INPUT/OUTPUT FILES: none (pure functions).
=============================================================================
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime

_PKSN_RE = re.compile(r"^pksn_\d{8}_\d{6}_[0-9a-f]{8}$")
_DGA_RE = re.compile(r"^dga_\d{8}_[0-9a-f]{8}$")


@dataclass(frozen=True)
class RunIdentity:
    orchestrator_run_id: str
    dream_run_id: str
    suffix: str  # the shared 8-hex


def new_suffix() -> str:
    """8 lowercase hex characters."""
    return secrets.token_hex(4)


def make_identity(run_date: date, now: datetime, suffix: str | None = None) -> RunIdentity:
    """Build a shared-suffix orchestrator+dream identity for `run_date`.

    `now` supplies HH:MM:SS for the orchestrator id; `run_date` supplies the
    YYYYMMDD for both ids (so resumes on the same date keep the date stable).
    """
    sfx = suffix or new_suffix()
    if not re.fullmatch(r"[0-9a-f]{8}", sfx):
        raise ValueError(f"suffix must be 8 lowercase hex chars, got {sfx!r}")
    ymd = run_date.strftime("%Y%m%d")
    hms = now.strftime("%H%M%S")
    return RunIdentity(
        orchestrator_run_id=f"pksn_{ymd}_{hms}_{sfx}",
        dream_run_id=f"dga_{ymd}_{sfx}",
        suffix=sfx,
    )


def is_orchestrator_run_id(value: str) -> bool:
    return bool(_PKSN_RE.match(value or ""))


def is_dream_run_id(value: str) -> bool:
    return bool(_DGA_RE.match(value or ""))


def suffix_of(run_id: str) -> str:
    """Extract the trailing 8-hex suffix from either id form."""
    return (run_id or "").rsplit("_", 1)[-1]
