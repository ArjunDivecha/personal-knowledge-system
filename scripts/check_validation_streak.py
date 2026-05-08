#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _memory_migration import append_report, utc_now_iso
from _validation_ledger import VALIDATION_HISTORY_PREFIX

REPO_ROOT = Path(__file__).resolve().parent.parent


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def history_key(day: datetime) -> str:
    return f"{VALIDATION_HISTORY_PREFIX}{day.date().isoformat()}"


def parse_record(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def day_has_passing_gate(records: list[Any], gate: str) -> tuple[bool, dict[str, Any] | None]:
    parsed_records = [record for record in (parse_record(raw) for raw in records) if record]
    matching_records = [
        record for record in parsed_records
        if record.get("gate") == gate
    ]
    passing_records = [
        record for record in matching_records
        if record.get("passed") is True
    ]
    if not passing_records:
        return False, matching_records[0] if matching_records else None
    passing_records.sort(key=lambda record: str(record.get("generated_at", "")), reverse=True)
    return True, passing_records[0]


def evaluate_streak(
    redis_client: Any,
    *,
    gate: str,
    required_days: int,
    now_utc: datetime,
) -> dict[str, Any]:
    days: list[dict[str, Any]] = []
    passed = True
    for offset in range(required_days):
        day = now_utc - timedelta(days=offset)
        key = history_key(day)
        records = redis_client.lrange(key, 0, 99)
        day_passed, latest_record = day_has_passing_gate(records, gate)
        if not day_passed:
            passed = False
        days.append(
            {
                "date": day.date().isoformat(),
                "history_key": key,
                "passed": day_passed,
                "latest_record": latest_record,
            }
        )
    return {
        "schema_version": 1,
        "gate": gate,
        "required_days": required_days,
        "passed": passed,
        "days": days,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check consecutive passing validation ledger days for a gate.")
    parser.add_argument("--gate", default="check_overnight_dream")
    parser.add_argument("--required-days", type=int, default=7)
    parser.add_argument(
        "--now-utc",
        help="Override current time for testing, in ISO 8601 UTC form such as 2026-03-28T15:00:00+00:00",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now_utc = parse_iso_datetime(args.now_utc) if args.now_utc else datetime.now(UTC)
    sys.path.insert(0, str(REPO_ROOT / "distillation"))
    from storage.redis_client import RedisClient  # noqa: PLC0415

    result = evaluate_streak(
        RedisClient().client,
        gate=args.gate,
        required_days=args.required_days,
        now_utc=now_utc,
    )
    report_path = append_report(
        f"check_validation_streak_{args.gate}_{utc_now_iso().replace(':', '').replace('+00:00', 'Z')}.json",
        result,
    )
    print(f"Gate: {args.gate}")
    print(f"Required days: {args.required_days}")
    print(f"Report written to {report_path}")
    if result["passed"]:
        print("PASS: validation streak requirement is satisfied.")
        return 0
    missing = [day["date"] for day in result["days"] if not day["passed"]]
    print(f"FAIL: validation streak requirement is not satisfied. Missing passing days: {', '.join(missing)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
