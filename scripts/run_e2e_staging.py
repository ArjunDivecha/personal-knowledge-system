#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

from _memory_migration import append_report, utc_now_iso

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_command(cmd: list[str]) -> dict[str, Any]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def call_health(base_url: str) -> dict[str, Any]:
    response = requests.get(f"{base_url.rstrip('/')}/health", timeout=30)
    return {
        "status_code": response.status_code,
        "ok": response.ok,
        "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
    }


def call_dream_dry_run(base_url: str) -> dict[str, Any]:
    token = os.getenv("STAGING_DREAM_OPERATOR_TOKEN")
    if not token:
        return {"skipped": True, "reason": "STAGING_DREAM_OPERATOR_TOKEN not set"}

    response = requests.post(
        f"{base_url.rstrip('/')}/ops/dream/run",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "dry_run": True,
            "set_as_latest": False,
            "note": "Staging smoke test Dream dry run",
        },
        timeout=60,
    )
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
    return {
        "status_code": response.status_code,
        "ok": response.ok,
        "body": body,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a staging smoke flow for the knowledge system")
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--bundle",
        type=Path,
        default=REPO_ROOT / "tests" / "fixtures" / "sample_memory_fixture.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--skip-dream", action="store_true")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "generated_at": utc_now_iso(),
        "base_url": args.base_url,
        "bundle": str(args.bundle),
        "dry_run": args.dry_run,
        "steps": [],
    }

    if not args.skip_seed:
        cmd = [
            str(REPO_ROOT / "distillation" / "venv" / "bin" / "python"),
            str(REPO_ROOT / "scripts" / "seed_staging_env.py"),
            "--bundle",
            str(args.bundle),
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        else:
            cmd.append("--reset")
        report["steps"].append({"name": "seed_staging", **run_command(cmd)})

    if not args.dry_run:
        report["steps"].append({"name": "health_check", **call_health(args.base_url)})
        if not args.skip_dream:
            report["steps"].append({"name": "dream_dry_run", **call_dream_dry_run(args.base_url)})

    report_path = append_report(
        f"run_e2e_staging_{utc_now_iso().replace(':', '').replace('+00:00', 'Z')}.json",
        report,
    )
    print(f"Report written to {report_path}")

    failed_steps = [
        step for step in report["steps"]
        if ("returncode" in step and step["returncode"] != 0) or ("ok" in step and not step["ok"])
    ]
    return 1 if failed_steps else 0


if __name__ == "__main__":
    raise SystemExit(main())
