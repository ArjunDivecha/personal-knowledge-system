#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

from _memory_migration import append_report, utc_now_iso
from check_overnight_dream_run import (
    DEFAULT_BASE_URL,
    DEFAULT_CRON_HOUR_UTC,
    DEFAULT_CRON_MINUTE_UTC,
    DEFAULT_EXPECTED_PROMOTION_LIMIT,
    DEFAULT_MAX_START_DELAY_MINUTES,
    fetch_health,
    fetch_latest_scheduled_governed_run,
    load_scheduled_archive_limit,
    most_recent_scheduled_boundary,
    parse_iso_datetime,
    validate_dream_run,
)

UTC = timezone.utc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure the latest overnight scheduled-governed Dream run exists before rendering the sleep report.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cron-hour-utc", type=int, default=DEFAULT_CRON_HOUR_UTC)
    parser.add_argument("--cron-minute-utc", type=int, default=DEFAULT_CRON_MINUTE_UTC)
    parser.add_argument("--max-start-delay-minutes", type=int, default=DEFAULT_MAX_START_DELAY_MINUTES)
    parser.add_argument("--expected-archive-limit", type=int, default=None)
    parser.add_argument("--expected-promotion-limit", type=int, default=DEFAULT_EXPECTED_PROMOTION_LIMIT)
    parser.add_argument("--now-utc")
    parser.add_argument("--post-repair-timeout-seconds", type=int, default=600)
    parser.add_argument("--poll-interval-seconds", type=int, default=20)
    return parser.parse_args()


def _operator_headers() -> dict[str, str]:
    token = os.getenv("DREAM_OPERATOR_TOKEN", "")
    if not token:
        raise RuntimeError("DREAM_OPERATOR_TOKEN is required to repair a missing scheduled Dream run")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def trigger_scheduled_governed_repair(
    *,
    base_url: str,
    cron: str,
    scheduled_time_ms: int,
) -> dict[str, Any]:
    response = requests.post(
        f"{base_url.rstrip('/')}/ops/dream/run_scheduled_governed",
        headers=_operator_headers(),
        json={
            "cron": cron,
            "scheduled_time": scheduled_time_ms,
        },
        timeout=240,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected repair response type: {type(payload).__name__}")
    return payload


def main() -> int:
    args = parse_args()
    now_utc = parse_iso_datetime(args.now_utc) if args.now_utc else datetime.now(UTC)
    expected_archive_limit = (
        args.expected_archive_limit
        if args.expected_archive_limit is not None
        else load_scheduled_archive_limit()
    )
    expected_boundary = most_recent_scheduled_boundary(
        now_utc,
        args.cron_hour_utc,
        args.cron_minute_utc,
    )

    health = fetch_health(args.base_url)
    dream_run = fetch_latest_scheduled_governed_run(args.base_url)
    before = validate_dream_run(
        health=health,
        dream_run=dream_run,
        now_utc=now_utc,
        cron_hour_utc=args.cron_hour_utc,
        cron_minute_utc=args.cron_minute_utc,
        max_start_delay_minutes=args.max_start_delay_minutes,
        expected_archive_limit=expected_archive_limit,
        expected_promotion_limit=args.expected_promotion_limit,
        allow_on_demand=True,
    )

    repair_result: dict[str, Any] | None = None
    if before.passed:
        print("PASS: overnight Dream run already present.")
    else:
        print("Scheduled Dream run is missing or invalid; triggering governed repair.")
        for issue in before.issues:
            print(f"- {issue}")
        repair_result = trigger_scheduled_governed_repair(
            base_url=args.base_url,
            cron=f"operator-repair {args.cron_minute_utc} {args.cron_hour_utc} * * *",
            scheduled_time_ms=int(expected_boundary.timestamp() * 1000),
        )
        print(f"Repair run status: {repair_result.get('status')}")

    deadline = time.time() + max(0, args.post_repair_timeout_seconds)
    dream_run_after: dict[str, Any] = {}
    after = before
    while True:
        health_after = fetch_health(args.base_url)
        dream_run_after = fetch_latest_scheduled_governed_run(args.base_url)
        after = validate_dream_run(
            health=health_after,
            dream_run=dream_run_after,
            now_utc=now_utc,
            cron_hour_utc=args.cron_hour_utc,
            cron_minute_utc=args.cron_minute_utc,
            max_start_delay_minutes=args.max_start_delay_minutes,
            expected_archive_limit=expected_archive_limit,
            expected_promotion_limit=args.expected_promotion_limit,
            allow_on_demand=True,
        )
        if after.passed or repair_result is None or time.time() >= deadline:
            break
        print("Waiting for scheduled Dream repair to become visible...")
        time.sleep(max(1, args.poll_interval_seconds))

    generated_at = utc_now_iso()
    report = {
        "generated_at": generated_at,
        "base_url": args.base_url,
        "now_utc": now_utc.isoformat(),
        "expected_boundary_utc": before.expected_boundary_utc,
        "before_passed": before.passed,
        "before_issues": before.issues,
        "repair_triggered": repair_result is not None,
        "repair_result": repair_result,
        "after_passed": after.passed,
        "after_issues": after.issues,
        "dream_run_after": dream_run_after,
    }
    report_path = append_report(
        f"ensure_overnight_dream_run_{generated_at.replace(':', '').replace('+00:00', 'Z')}.json",
        report,
    )
    print(f"Ensure report written to {report_path}")

    if after.passed:
        print("PASS: overnight Dream run is present after ensure step.")
        return 0

    print("FAIL: overnight Dream run is still invalid after ensure step.")
    for issue in after.issues:
        print(f"- {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
