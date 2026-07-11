"""
=============================================================================
SCRIPT NAME: report_salience_v2_distribution.py
=============================================================================

INPUT FILES:
- Default (offline gate G3): /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/tests/fixtures/salience_v2_shadow_corpus_fixture.json
    a bundled, synthetic ~40-entry corpus with hand-constructed, varied
    metadata.salience_v2 values (NOT live production data) used to exercise
    this script's thresholds offline, with no network access.
- Any other JSON file passed via --fixture-file <path>: a list of entry-like
    dicts, each with a numeric metadata.salience_v2 field. In the eventual
    staging/production run (contract PKS-INJECTION-RANKING-002 gate G5, out
    of scope for this offline build), --fixture-file would point at a JSON
    export of the live shadow-pass corpus rather than this bundled fixture —
    this script itself performs no network I/O either way.

OUTPUT FILES:
- None. Prints a summary to stdout and communicates pass/fail via exit code.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Implements the Phase A shadow-distribution report for contract
PKS-INJECTION-RANKING-002
(/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/contracts/injection-ranking-v2.spec.md),
gate G3 / INV1's discrimination proof. v1 salience (utils/salience.py's
compute_salience) is degenerate: the bulk of the corpus ties bit-identically
around 0.48-0.49 after 4-decimal rounding, and a wide clamp basin pins many
unrelated entries at exactly 1.0. salience_v2 (utils/salience_v2.py) is
supposed to fix this by construction (additive, five-component, no clamp
pile-up). This script proves that claim on stored data rather than assuming
it: given a corpus of entries carrying metadata.salience_v2, it computes

  1. 4-decimal tie rate: the fraction of entries whose salience_v2 value
     (rounded to 4 decimals, matching compute_salience_v2's own rounding)
     is shared with at least one other entry in the corpus.
  2. Decile occupancy: bucketing every entry's salience_v2 into one of 10
     equal-width bins over the fixed [0, 1] range (salience_v2 is defined
     on that range by construction — every component is clamped to [0,1]
     before weighting), and checking every bin holds at least one entry.

Exit code 0 iff tie_rate < 0.01 AND all 10 deciles are occupied; otherwise
exit code 1, printing which threshold(s) failed and by how much. This
script deliberately does NOT compute salience_v2 itself — it reads
already-stored values, so it can validate a real shadow pass (nightly Dream
run over the live corpus, out of scope here) as faithfully as it validates
the offline bundled fixture.

A second, deliberately degenerate fixture
(tests/fixtures/salience_v2_shadow_corpus_degenerate_fixture.json, ~40
entries clustered tightly around a single value) proves this script's gate
actually detects bad discrimination rather than rubber-stamping any input —
see tests/python/test_salience_v2_shadow_report.py.

DEPENDENCIES: Python 3.14 stdlib only (argparse, json, pathlib, sys).

USAGE:
  python3 scripts/report_salience_v2_distribution.py
      (uses the bundled healthy fixture; expect exit 0)
  python3 scripts/report_salience_v2_distribution.py --fixture-file /path/to/corpus.json
  python3 scripts/report_salience_v2_distribution.py --fixture-file tests/fixtures/salience_v2_shadow_corpus_degenerate_fixture.json
      (expect exit 1 — this is the negative-control proof)

NOTES:
- Read-only; performs no writes and no network calls.
- No-fake-zero rule: an empty or unparseable corpus is a hard error (exit 2
  with a message), never silently reported as tie_rate=0 / decile 0 occupied.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "salience_v2_shadow_corpus_fixture.json"

TIE_RATE_THRESHOLD = 0.01
NUM_DECILES = 10


def load_corpus(fixture_path: Path) -> list[dict]:
    with fixture_path.open() as handle:
        entries = json.load(handle)
    if not isinstance(entries, list):
        raise ValueError(f"{fixture_path} must contain a JSON list of entries")
    return entries


def extract_salience_values(entries: list[dict]) -> list[float]:
    values: list[float] = []
    for entry in entries:
        metadata = entry.get("metadata") or {}
        value = metadata.get("salience_v2")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"entry {entry.get('id', '<unknown>')!r} has no numeric metadata.salience_v2")
        values.append(round(float(value), 4))
    return values


def compute_tie_rate(values: list[float]) -> float:
    """Fraction of entries whose 4-decimal salience_v2 is shared with at
    least one other entry in the corpus."""
    if not values:
        raise ValueError("cannot compute tie rate over an empty corpus")
    counts: dict[float, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    tied = sum(count for count in counts.values() if count > 1)
    return tied / len(values)


def decile_of(value: float) -> int:
    # Fixed [0, 1] bins (salience_v2 is defined on [0,1] by construction).
    # A value of exactly 1.0 falls in the last bin rather than an 11th.
    bucket = int(value * NUM_DECILES)
    return min(bucket, NUM_DECILES - 1)


def compute_decile_occupancy(values: list[float]) -> dict[int, int]:
    occupancy = {i: 0 for i in range(NUM_DECILES)}
    for v in values:
        occupancy[decile_of(v)] += 1
    return occupancy


def evaluate(fixture_path: Path) -> dict:
    entries = load_corpus(fixture_path)
    if not entries:
        raise ValueError(f"{fixture_path} contains zero entries")
    values = extract_salience_values(entries)
    tie_rate = compute_tie_rate(values)
    occupancy = compute_decile_occupancy(values)
    empty_deciles = [i for i, count in occupancy.items() if count == 0]

    tie_rate_ok = tie_rate < TIE_RATE_THRESHOLD
    deciles_ok = len(empty_deciles) == 0
    return {
        "fixture_path": str(fixture_path),
        "n_entries": len(entries),
        "tie_rate": tie_rate,
        "tie_rate_threshold": TIE_RATE_THRESHOLD,
        "tie_rate_ok": tie_rate_ok,
        "decile_occupancy": occupancy,
        "empty_deciles": empty_deciles,
        "deciles_ok": deciles_ok,
        "passed": tie_rate_ok and deciles_ok,
    }


def format_report(report: dict) -> str:
    lines = [
        f"salience_v2 shadow-distribution report: {report['fixture_path']}",
        f"  entries: {report['n_entries']}",
        f"  tie_rate: {report['tie_rate']:.4f} (threshold < {report['tie_rate_threshold']}) "
        f"-> {'PASS' if report['tie_rate_ok'] else 'FAIL'}",
        f"  decile occupancy: {report['decile_occupancy']}",
        f"  empty deciles: {report['empty_deciles'] or 'none'} "
        f"-> {'PASS' if report['deciles_ok'] else 'FAIL'}",
        f"  overall: {'PASS' if report['passed'] else 'FAIL'}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture-file",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help="Path to a JSON list of entries with metadata.salience_v2 (default: bundled offline fixture)",
    )
    args = parser.parse_args(argv)

    try:
        report = evaluate(args.fixture_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(format_report(report))
    if not report["passed"]:
        failed = []
        if not report["tie_rate_ok"]:
            failed.append(f"tie_rate {report['tie_rate']:.4f} >= {report['tie_rate_threshold']}")
        if not report["deciles_ok"]:
            failed.append(f"empty deciles: {report['empty_deciles']}")
        print(f"FAILED: {'; '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
