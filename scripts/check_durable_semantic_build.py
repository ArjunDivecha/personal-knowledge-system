#!/usr/bin/env python3
"""Deterministic repository-native checks for the durable consolidation build."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "cloudflare-mcp" / "mcp-server"


def run(command: list[str], cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def gate(gate_id: str) -> int:
    if gate_id == "G0":
        required = [WORKER / "src" / "semanticMaintenance.ts", WORKER / "src" / "embeddingFreshness.ts"]
        if not all(path.exists() for path in required):
            raise SystemExit("G0 regression fixtures missing")
    elif gate_id == "G1":
        run(["npm", "run", "test:worker", "--", "test/durableSemanticConsolidation.test.ts", "test/dream-replay.test.ts", "--no-file-parallelism"], WORKER)
    elif gate_id == "G2":
        run(["npm", "run", "test:worker", "--", "test/maintenanceQueue.test.ts", "test/maintenanceJournal.test.ts", "--no-file-parallelism"], WORKER)
    elif gate_id == "G3":
        source = (WORKER / "src" / "dream.ts").read_text()
        for token in ("embedding_input_sha256", "derived_complete", "processSemanticCandidateTask"):
            if token not in source:
                raise SystemExit(f"G3 missing {token}")
        run(["npm", "run", "test:worker", "--", "test/durableSemanticConsolidation.test.ts", "--no-file-parallelism"], WORKER)
    elif gate_id == "G4":
        run(["python3", "scripts/maintenance_cost_harness.py"])
    elif gate_id == "G5":
        run(["python3", "tests/python/test_semantic_candidate_planner.py"])
    elif gate_id == "G6":
        source = (WORKER / "src" / "index.ts").read_text()
        if 'env.DREAM_QUEUE_MODE === "live"' not in source or "runScheduledGovernedDream" not in source:
            raise SystemExit("G6 trigger-only guard missing")
        run(["npm", "run", "type-check"], WORKER)
    elif gate_id == "G7":
        run(["npm", "run", "type-check"], WORKER)
        run(["python3", "tests/python/test_semantic_candidate_planner.py"])
        run(["git", "diff", "--check"])
        config = json.loads((WORKER / "wrangler.json").read_text())
        if not config.get("queues", {}).get("consumers"):
            raise SystemExit("queue consumer config missing")
    else:
        raise SystemExit(f"unknown deterministic gate {gate_id}")
    print(f"DURABLE_SEMANTIC_GATE_PASS {gate_id}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", required=True)
    raise SystemExit(gate(parser.parse_args().gate))
