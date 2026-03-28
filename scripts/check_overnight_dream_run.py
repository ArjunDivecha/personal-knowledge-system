#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from _memory_migration import append_report, utc_now_iso

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://mcp.dancing-ganesh.com"
DEFAULT_CRON_HOUR_UTC = 7
DEFAULT_CRON_MINUTE_UTC = 10
DEFAULT_MAX_START_DELAY_MINUTES = 45
EXPECTED_ARCHIVE_LIMIT = 5
EXPECTED_PROMOTION_LIMIT = 10


@dataclass
class ValidationResult:
    passed: bool
    issues: list[str]
    expected_boundary_utc: str
    expected_boundary_local: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether the latest scheduled Dream run executed in bounded live mode.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cron-hour-utc", type=int, default=DEFAULT_CRON_HOUR_UTC)
    parser.add_argument("--cron-minute-utc", type=int, default=DEFAULT_CRON_MINUTE_UTC)
    parser.add_argument("--max-start-delay-minutes", type=int, default=DEFAULT_MAX_START_DELAY_MINUTES)
    parser.add_argument(
        "--now-utc",
        help="Override current time for testing, in ISO 8601 UTC form such as 2026-03-28T15:00:00+00:00",
    )
    return parser.parse_args()


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def most_recent_scheduled_boundary(now_utc: datetime, cron_hour_utc: int, cron_minute_utc: int) -> datetime:
    boundary = now_utc.replace(hour=cron_hour_utc, minute=cron_minute_utc, second=0, microsecond=0)
    if now_utc < boundary:
        boundary -= timedelta(days=1)
    return boundary


def parse_sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError("No JSON payload found in SSE response")


def oauth_client_flow(base_url: str) -> tuple[requests.Session, str]:
    session = requests.Session()
    metadata = session.get(f"{base_url.rstrip('/')}/.well-known/oauth-authorization-server", timeout=30).json()

    redirect_uri = "http://127.0.0.1:9883/callback"
    registration = session.post(
        metadata["registration_endpoint"],
        json={
            "client_name": "dream-overnight-check",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp:read",
        },
        timeout=30,
    )
    registration.raise_for_status()
    client = registration.json()

    authorize = session.get(
        metadata["authorization_endpoint"],
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "scope": "mcp:read",
            "state": f"dream-check-{uuid.uuid4().hex}",
        },
        allow_redirects=False,
        timeout=30,
    )
    authorize.raise_for_status()

    location = authorize.headers.get("location", "")
    parsed_location = urllib.parse.urlparse(location)
    code = urllib.parse.parse_qs(parsed_location.query).get("code", [None])[0]
    if not code:
        raise RuntimeError("OAuth authorization redirect did not include a code")

    token = session.post(
        metadata["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
        },
        timeout=30,
    )
    token.raise_for_status()
    access_token = token.json()["access_token"]
    return session, access_token


def call_mcp_tool(
    session: requests.Session,
    base_url: str,
    access_token: str,
    session_id: str,
    *,
    rpc_id: int,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {access_token}",
        "Mcp-Session-Id": session_id,
    }
    response = session.post(
        f"{base_url.rstrip('/')}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    envelope = parse_sse_json(response.text)
    return json.loads(envelope["result"]["content"][0]["text"])


def fetch_health(base_url: str) -> dict[str, Any]:
    response = requests.get(f"{base_url.rstrip('/')}/health", timeout=30)
    response.raise_for_status()
    return response.json()


def fetch_dream_summary(base_url: str) -> dict[str, Any]:
    session, access_token = oauth_client_flow(base_url)
    init_response = session.post(
        f"{base_url.rstrip('/')}/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {access_token}",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "dream-overnight-check", "version": "1.0"},
            },
        },
        timeout=30,
    )
    init_response.raise_for_status()
    session_id = init_response.headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("MCP initialize response did not include a session id")

    return call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=2,
        name="get_dream_summary",
        arguments={},
    )


def validate_dream_run(
    *,
    health: dict[str, Any],
    dream_summary: dict[str, Any],
    now_utc: datetime,
    cron_hour_utc: int,
    cron_minute_utc: int,
    max_start_delay_minutes: int,
) -> ValidationResult:
    issues: list[str] = []
    expected_boundary = most_recent_scheduled_boundary(now_utc, cron_hour_utc, cron_minute_utc)
    expected_latest_start = expected_boundary + timedelta(minutes=max_start_delay_minutes)

    run_at_raw = dream_summary.get("run_at")
    if not isinstance(run_at_raw, str):
        issues.append("Dream summary is missing run_at")
        run_at = None
    else:
        run_at = parse_iso_datetime(run_at_raw)

    if dream_summary.get("status") != "completed":
        issues.append(f"Dream status is not completed: {dream_summary.get('status')}")

    if dream_summary.get("trigger") != "scheduled":
        issues.append(f"Latest Dream run trigger is not scheduled: {dream_summary.get('trigger')}")

    if dream_summary.get("dry_run") is not False:
        issues.append(f"Latest Dream run is not live mode: dry_run={dream_summary.get('dry_run')}")

    counts = dream_summary.get("counts")
    if not isinstance(counts, dict):
        issues.append("Dream summary is missing counts")
        counts = {}

    if counts.get("archive_limit") != EXPECTED_ARCHIVE_LIMIT:
        issues.append(
            f"archive_limit is {counts.get('archive_limit')}, expected {EXPECTED_ARCHIVE_LIMIT}",
        )
    if counts.get("promotion_limit") != EXPECTED_PROMOTION_LIMIT:
        issues.append(
            f"promotion_limit is {counts.get('promotion_limit')}, expected {EXPECTED_PROMOTION_LIMIT}",
        )

    archived_count = counts.get("archived")
    if isinstance(archived_count, int) and archived_count > EXPECTED_ARCHIVE_LIMIT:
        issues.append(f"archived count {archived_count} exceeds cap {EXPECTED_ARCHIVE_LIMIT}")

    promoted_count = counts.get("promoted")
    if isinstance(promoted_count, int) and promoted_count > EXPECTED_PROMOTION_LIMIT:
        issues.append(f"promoted count {promoted_count} exceeds cap {EXPECTED_PROMOTION_LIMIT}")

    if run_at is not None:
        if run_at < expected_boundary:
            issues.append(
                f"Latest Dream run is too old: run_at={run_at.isoformat()} expected_after={expected_boundary.isoformat()}",
            )
        if run_at > expected_latest_start:
            issues.append(
                f"Latest Dream run started later than expected window: run_at={run_at.isoformat()} latest_expected={expected_latest_start.isoformat()}",
            )

    health_last_run = health.get("last_dream_run")
    if isinstance(health_last_run, str) and run_at_raw and health_last_run != run_at_raw:
        issues.append(
            f"/health last_dream_run ({health_last_run}) does not match Dream summary run_at ({run_at_raw})",
        )

    health_last_dry_run = health.get("last_dream_dry_run")
    if health_last_dry_run is not None and health_last_dry_run != dream_summary.get("dry_run"):
        issues.append(
            f"/health last_dream_dry_run ({health_last_dry_run}) does not match Dream summary dry_run ({dream_summary.get('dry_run')})",
        )

    return ValidationResult(
        passed=len(issues) == 0,
        issues=issues,
        expected_boundary_utc=expected_boundary.isoformat(),
        expected_boundary_local=expected_boundary.astimezone().isoformat(),
    )


def main() -> int:
    args = parse_args()
    now_utc = parse_iso_datetime(args.now_utc) if args.now_utc else datetime.now(UTC)

    health = fetch_health(args.base_url)
    dream_summary = fetch_dream_summary(args.base_url)
    validation = validate_dream_run(
        health=health,
        dream_summary=dream_summary,
        now_utc=now_utc,
        cron_hour_utc=args.cron_hour_utc,
        cron_minute_utc=args.cron_minute_utc,
        max_start_delay_minutes=args.max_start_delay_minutes,
    )

    report = {
        "generated_at": utc_now_iso(),
        "base_url": args.base_url,
        "now_utc": now_utc.isoformat(),
        "expected_boundary_utc": validation.expected_boundary_utc,
        "expected_boundary_local": validation.expected_boundary_local,
        "passed": validation.passed,
        "issues": validation.issues,
        "health": health,
        "dream_summary": dream_summary,
    }
    report_path = append_report(
        f"check_overnight_dream_run_{utc_now_iso().replace(':', '').replace('+00:00', 'Z')}.json",
        report,
    )

    print(f"Expected scheduled boundary (UTC): {validation.expected_boundary_utc}")
    print(f"Expected scheduled boundary (local): {validation.expected_boundary_local}")
    print(f"Report written to {report_path}")

    if validation.passed:
        print("PASS: latest scheduled Dream run is present and in bounded live mode.")
        return 0

    print("FAIL: latest scheduled Dream run does not yet satisfy the overnight checks.")
    for issue in validation.issues:
        print(f"- {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
