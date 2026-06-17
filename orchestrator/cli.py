"""
=============================================================================
MODULE: orchestrator/cli.py
=============================================================================

DESCRIPTION:
Argument parsing for the orchestrator CLI (spec "The orchestrator CLI must
support"):

    nightly_orchestrator.py preflight
    nightly_orchestrator.py run --mode shadow|live --date YYYY-MM-DD|auto
    nightly_orchestrator.py resume --date YYYY-MM-DD|auto
    nightly_orchestrator.py report --date YYYY-MM-DD|auto

INPUT/OUTPUT FILES: none directly (delegates to orchestrator.engine.Orchestrator).
=============================================================================
"""
from __future__ import annotations

import argparse
from typing import Optional

from .engine import Orchestrator


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nightly_orchestrator",
                                description="PKS nightly orchestrator (Phase 1, shadow-only).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight", help="Verify env + auth (never opens a browser).")

    pr = sub.add_parser("run", help="Run the nightly state machine.")
    pr.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    pr.add_argument("--date", default="auto", help="YYYY-MM-DD or auto")
    pr.add_argument("--force-notify", action="store_true")

    ps = sub.add_parser("resume", help="Resume (same-host) the run for a date.")
    ps.add_argument("--date", default="auto", help="YYYY-MM-DD or auto")
    ps.add_argument("--force-notify", action="store_true")

    rp = sub.add_parser("report", help="Render the report for a date from the ledger.")
    rp.add_argument("--date", default="auto", help="YYYY-MM-DD or auto")

    sub.add_parser("supervise", help="launchd-driven: pick the overnight window's "
                                     "run date and run/resume/skip (Phase 4 sidecar).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    orch = Orchestrator()
    if args.cmd == "preflight":
        return orch.preflight()
    if args.cmd == "run":
        return orch.run(mode=args.mode, run_date=args.date,
                        force_notify=args.force_notify)
    if args.cmd == "resume":
        return orch.resume(run_date=args.date, force_notify=args.force_notify)
    if args.cmd == "report":
        return orch.report(run_date=args.date)
    if args.cmd == "supervise":
        return orch.supervise()
    return 2
