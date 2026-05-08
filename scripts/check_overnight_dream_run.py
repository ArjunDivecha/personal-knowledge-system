#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from _memory_migration import append_report, utc_now_iso
from _validation_ledger import ValidationGateRecord, write_validation_gate

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://mcp.dancing-ganesh.com"
DEFAULT_CRON_HOUR_UTC = 7
DEFAULT_CRON_MINUTE_UTC = 10
DEFAULT_MAX_START_DELAY_MINUTES = 45
DEFAULT_EXPECTED_ARCHIVE_LIMIT = 10
DEFAULT_EXPECTED_PROMOTION_LIMIT = 10
UTC = timezone.utc


@dataclass
class ValidationResult:
    passed: bool
    issues: list[str]
    expected_boundary_utc: str
    expected_boundary_local: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether the latest scheduled Dream governance proposal was generated without live mutation.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cron-hour-utc", type=int, default=DEFAULT_CRON_HOUR_UTC)
    parser.add_argument("--cron-minute-utc", type=int, default=DEFAULT_CRON_MINUTE_UTC)
    parser.add_argument("--max-start-delay-minutes", type=int, default=DEFAULT_MAX_START_DELAY_MINUTES)
    parser.add_argument("--expected-archive-limit", type=int, default=DEFAULT_EXPECTED_ARCHIVE_LIMIT)
    parser.add_argument("--expected-promotion-limit", type=int, default=DEFAULT_EXPECTED_PROMOTION_LIMIT)
    parser.add_argument(
        "--now-utc",
        help="Override current time for testing, in ISO 8601 UTC form such as 2026-03-28T15:00:00+00:00",
    )
    parser.add_argument(
        "--allow-on-demand",
        action="store_true",
        help="Allow an operator-triggered scheduled-equivalent proposal to validate the proposal path outside the cron start window.",
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


def fetch_dream_session(base_url: str) -> tuple[requests.Session, str, str]:
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
    return session, access_token, session_id


def fetch_latest_scheduled_proposal(base_url: str) -> dict[str, Any]:
    session, access_token, session_id = fetch_dream_session(base_url)
    proposals = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=2,
        name="list_dream_runs",
        arguments={"limit": 10, "status_filter": "proposal_ready"},
    )
    for summary in proposals.get("runs", []):
        run_id = summary.get("run_id")
        if not isinstance(run_id, str):
            continue
        run = call_mcp_tool(
            session,
            base_url,
            access_token,
            session_id,
            rpc_id=3,
            name="get_dream_run",
            arguments={"run_id": run_id},
        )
        if run.get("actor_id") == "scheduled:dream-governance":
            return run
    return {
        "error": "scheduled_proposal_not_found",
        "recent_proposals": proposals.get("runs", []),
    }


def fetch_dream_summary(base_url: str) -> dict[str, Any]:
    session, access_token, session_id = fetch_dream_session(base_url)
    return call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=2,
        name="get_dream_summary",
        arguments={},
    )


def validate_dream_proposal(
    *,
    health: dict[str, Any],
    dream_proposal: dict[str, Any],
    now_utc: datetime,
    cron_hour_utc: int,
    cron_minute_utc: int,
    max_start_delay_minutes: int,
    expected_archive_limit: int,
    expected_promotion_limit: int,
    allow_on_demand: bool = False,
) -> ValidationResult:
    issues: list[str] = []
    expected_boundary = most_recent_scheduled_boundary(now_utc, cron_hour_utc, cron_minute_utc)
    expected_latest_start = expected_boundary + timedelta(minutes=max_start_delay_minutes)

    run_at_raw = dream_proposal.get("run_at")
    timestamp_field = "run_at"
    if not isinstance(run_at_raw, str):
        run_at_raw = dream_proposal.get("created_at")
        timestamp_field = "created_at"
    if not isinstance(run_at_raw, str):
        issues.append("Dream proposal is missing run_at or created_at")
        run_at = None
    else:
        run_at = parse_iso_datetime(run_at_raw)

    if dream_proposal.get("status") != "proposal_ready":
        issues.append(f"Dream proposal status is not proposal_ready: {dream_proposal.get('status')}")

    if dream_proposal.get("actor_id") != "scheduled:dream-governance":
        issues.append(f"Dream proposal actor is not scheduled governance: {dream_proposal.get('actor_id')}")

    if dream_proposal.get("trigger") != "manual":
        issues.append(f"Dream proposal trigger is not manual proposal path: {dream_proposal.get('trigger')}")

    if dream_proposal.get("dry_run") is not True:
        issues.append("Dream proposal is not marked dry_run=true; scheduled governance should generate proposals without live mutation")

    operations = dream_proposal.get("operations")
    if not isinstance(operations, list):
        issues.append("Dream proposal is missing operations list")
    else:
        unsupported_live_ops = [
            operation.get("operation_id")
            for operation in operations
            if isinstance(operation, dict) and operation.get("applied_at") is not None
        ]
        if unsupported_live_ops:
            issues.append(f"Dream proposal appears to include applied operations: {unsupported_live_ops}")

    counts = dream_proposal.get("counts")
    if not isinstance(counts, dict):
        issues.append("Dream proposal is missing counts")
        counts = {}

    if counts.get("archive_limit") != expected_archive_limit:
        issues.append(
            f"archive_limit is {counts.get('archive_limit')}, expected {expected_archive_limit} for nightly governance proposals",
        )
    if counts.get("promotion_limit") != expected_promotion_limit:
        issues.append(
            f"promotion_limit is {counts.get('promotion_limit')}, expected {expected_promotion_limit} for nightly governance proposals",
        )

    if run_at is not None:
        if run_at < expected_boundary:
            issues.append(
                f"Latest scheduled Dream proposal is too old: {timestamp_field}={run_at.isoformat()} expected_after={expected_boundary.isoformat()}",
            )
        if run_at > expected_latest_start and not allow_on_demand:
            issues.append(
                f"Latest scheduled Dream proposal started later than expected window: {timestamp_field}={run_at.isoformat()} latest_expected={expected_latest_start.isoformat()}",
            )

    if health.get("status") != "ok":
        issues.append(f"/health status is not ok: {health.get('status')}")

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
    dream_proposal = fetch_latest_scheduled_proposal(args.base_url)
    validation = validate_dream_proposal(
        health=health,
        dream_proposal=dream_proposal,
        now_utc=now_utc,
        cron_hour_utc=args.cron_hour_utc,
        cron_minute_utc=args.cron_minute_utc,
        max_start_delay_minutes=args.max_start_delay_minutes,
        expected_archive_limit=args.expected_archive_limit,
        expected_promotion_limit=args.expected_promotion_limit,
        allow_on_demand=args.allow_on_demand,
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
        "dream_proposal": dream_proposal,
    }
    report_path = append_report(
        f"check_overnight_dream_run_{utc_now_iso().replace(':', '').replace('+00:00', 'Z')}.json",
        report,
    )
    try:
        sys.path.insert(0, str(REPO_ROOT / "distillation"))
        from storage.redis_client import RedisClient  # noqa: PLC0415

        redis_client = RedisClient()
        write_validation_gate(
            redis_client.client,
            ValidationGateRecord(
                gate="check_overnight_dream",
                passed=validation.passed,
                issues=validation.issues,
                report_path=str(report_path),
                details={
                    "base_url": args.base_url,
                    "expected_boundary_utc": validation.expected_boundary_utc,
                    "expected_boundary_local": validation.expected_boundary_local,
                    "dream_run_at": dream_proposal.get("run_at") or dream_proposal.get("created_at"),
                    "dream_status": dream_proposal.get("status"),
                    "dream_actor_id": dream_proposal.get("actor_id"),
                },
            ),
        )
    except Exception as error:
        print(f"WARNING: could not write validation ledger: {error}")

    print(f"Expected scheduled boundary (UTC): {validation.expected_boundary_utc}")
    print(f"Expected scheduled boundary (local): {validation.expected_boundary_local}")
    print(f"Report written to {report_path}")

    if validation.passed:
        print("PASS: latest scheduled Dream governance proposal is present and no live mutation is required.")
        return 0

    print("FAIL: latest scheduled Dream proposal does not yet satisfy the overnight governance checks.")
    for issue in validation.issues:
        print(f"- {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
