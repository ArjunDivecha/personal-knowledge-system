#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: scripts/nightly_orchestrator.py
=============================================================================

DESCRIPTION:
Production entrypoint for the PKS nightly orchestrator (Phase 1). Thin wrapper
that puts the repo root on sys.path and dispatches to orchestrator.cli.main.

The CLI (spec):
    nightly_orchestrator.py preflight
    nightly_orchestrator.py run --mode shadow|live --date YYYY-MM-DD|auto
    nightly_orchestrator.py resume --date YYYY-MM-DD|auto
    nightly_orchestrator.py report --date YYYY-MM-DD|auto

Phase 1 is shadow-only / non-mutating; see
docs/pks-nightly-orchestrator-redesign-2026-06-16.md and the orchestrator/
package.

INPUT FILES:
- ingestion/checkpoints/orchestrator_runs/{run_date}.json (resume/report)
- knowledge-system/.env (runtime keys)

OUTPUT FILES:
- ingestion/checkpoints/orchestrator_runs/{run_date}.json
- scripts/reports/pks-nightly-{run_date}.{json,md}
- Redis orchestrator keys (pks:orchestrator:*)

USAGE:
  ingestion/.venv/bin/python scripts/nightly_orchestrator.py run --mode shadow
=============================================================================
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
