#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from _memory_migration import REPORT_DIR, append_report, ensure_runtime_dirs, utc_now_iso
from _validation_ledger import ValidationGateRecord, write_validation_gate

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://mcp.dancing-ganesh.com"
DEFAULT_CRON_HOUR_UTC = 7
DEFAULT_CRON_MINUTE_UTC = 10
DEFAULT_MAX_START_DELAY_MINUTES = 45
DEFAULT_EXPECTED_PROMOTION_LIMIT = 10
UTC = timezone.utc
PACIFIC = ZoneInfo("America/Los_Angeles")


@dataclass
class ValidationResult:
    passed: bool
    issues: list[str]
    expected_boundary_utc: str
    expected_boundary_local: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether the latest scheduled Dream governed live run executed.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cron-hour-utc", type=int, default=DEFAULT_CRON_HOUR_UTC)
    parser.add_argument("--cron-minute-utc", type=int, default=DEFAULT_CRON_MINUTE_UTC)
    parser.add_argument("--max-start-delay-minutes", type=int, default=DEFAULT_MAX_START_DELAY_MINUTES)
    parser.add_argument(
        "--expected-archive-limit",
        type=int,
        default=None,
        help="Expected scheduled archive limit. Defaults to shared/memory_policy.json dream_thresholds.scheduled_archive_limit.",
    )
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


def load_scheduled_archive_limit(policy_path: Path | None = None) -> int:
    path = policy_path or REPO_ROOT / "shared" / "memory_policy.json"
    with path.open("r", encoding="utf-8") as handle:
        policy = json.load(handle)
    value = policy.get("dream_thresholds", {}).get("scheduled_archive_limit")
    if not isinstance(value, int):
        raise ValueError(f"Missing integer dream_thresholds.scheduled_archive_limit in {path}")
    return value


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


def is_scheduled_governed_run(record: dict[str, Any]) -> bool:
    run_id = record.get("run_id")
    return (
        record.get("trigger") == "scheduled"
        and (
            record.get("auto_apply_mode") == "governed"
            or (isinstance(run_id, str) and run_id.startswith("dga_"))
        )
    )


def fetch_latest_scheduled_governed_run(base_url: str) -> dict[str, Any]:
    """Fetch the newest scheduled governed Dream attempt from the run ledger.

    `dream:last_run` intentionally skips fully-held governed runs, so using only
    get_dream_summary can make a healthy cautious run look stale. The run index
    is the source of truth for scheduled governed attempts.
    """
    session, access_token, session_id = fetch_dream_session(base_url)
    runs = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=2,
        name="list_dream_runs",
        arguments={"limit": 50},
    )
    recent_runs = runs.get("runs", [])
    if not isinstance(recent_runs, list):
        recent_runs = []

    for summary in recent_runs:
        if not isinstance(summary, dict):
            continue
        run_id = summary.get("run_id")
        if not isinstance(run_id, str):
            continue
        if not is_scheduled_governed_run(summary) and not run_id.startswith("dga_"):
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
        if is_scheduled_governed_run(run):
            run["_report_source"] = "dream_run_index"
            return run

    latest_summary = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=4,
        name="get_dream_summary",
        arguments={},
    )
    return {
        "error": "scheduled_governed_run_not_found",
        "recent_runs": recent_runs,
        "latest_dream_summary": latest_summary,
        "_report_source": "dream_run_index",
    }


def fetch_tripwire_status(base_url: str) -> dict[str, Any] | None:
    operator_token = os.getenv("DREAM_OPERATOR_TOKEN", "")
    if not operator_token:
        return None
    response = requests.get(
        f"{base_url.rstrip('/')}/ops/dream/tripwire_status",
        headers={"Authorization": f"Bearer {operator_token}"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def validate_dream_run(
    *,
    health: dict[str, Any],
    dream_run: dict[str, Any],
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

    if isinstance(dream_run.get("error"), str):
        issues.append(f"Dream run lookup failed: {dream_run.get('error')}")

    run_at_raw = dream_run.get("run_at")
    timestamp_field = "run_at"
    if not isinstance(run_at_raw, str):
        run_at_raw = dream_run.get("created_at")
        timestamp_field = "created_at"
    if not isinstance(run_at_raw, str):
        issues.append("Dream run is missing run_at or created_at")
        run_at = None
    else:
        run_at = parse_iso_datetime(run_at_raw)

    valid_statuses = {"completed", "completed_with_holds", "held"}
    if dream_run.get("status") not in valid_statuses:
        issues.append(f"Dream run status is not governed-live successful/held: {dream_run.get('status')}")

    if dream_run.get("trigger") != "scheduled":
        issues.append(f"Dream run trigger is not scheduled: {dream_run.get('trigger')}")

    if dream_run.get("dry_run") is not False:
        issues.append("Dream run is not marked dry_run=false; scheduled Dream should be live governed apply")

    if dream_run.get("auto_apply_mode") != "governed":
        issues.append(f"Dream run auto_apply_mode is not governed: {dream_run.get('auto_apply_mode')}")

    counts = dream_run.get("counts")
    if not isinstance(counts, dict):
        issues.append("Dream run is missing counts")
        counts = {}

    if counts.get("archive_limit") != expected_archive_limit:
        issues.append(
            f"archive_limit is {counts.get('archive_limit')}, expected {expected_archive_limit} for nightly governance proposals",
        )
    if counts.get("promotion_limit") != expected_promotion_limit:
        issues.append(
            f"promotion_limit is {counts.get('promotion_limit')}, expected {expected_promotion_limit} for nightly governance proposals",
        )

    selected_count = counts.get("selected_operation_count")
    held_count = counts.get("held_operation_count")
    operation_count = counts.get("operation_count")
    applied_count = counts.get("applied_count")
    if all(isinstance(value, int) for value in (selected_count, held_count, operation_count)):
        if selected_count + held_count != operation_count:
            issues.append(
                f"selected_operation_count + held_operation_count is {selected_count + held_count}, expected operation_count {operation_count}",
            )
    if isinstance(selected_count, int) and isinstance(applied_count, int) and applied_count != selected_count:
        issues.append(
            f"applied_count is {applied_count}, expected selected_operation_count {selected_count}",
        )

    verification = dream_run.get("verification")
    if isinstance(verification, dict) and verification.get("passed") is not True:
        issues.append("Dream governed apply verification did not pass")

    if run_at is not None:
        if run_at < expected_boundary:
            issues.append(
                f"Latest scheduled Dream run is too old: {timestamp_field}={run_at.isoformat()} expected_after={expected_boundary.isoformat()}",
            )
        if run_at > expected_latest_start and not allow_on_demand:
            issues.append(
                f"Latest scheduled Dream run started later than expected window: {timestamp_field}={run_at.isoformat()} latest_expected={expected_latest_start.isoformat()}",
            )

    if health.get("status") != "ok":
        issues.append(f"/health status is not ok: {health.get('status')}")

    return ValidationResult(
        passed=len(issues) == 0,
        issues=issues,
        expected_boundary_utc=expected_boundary.isoformat(),
        expected_boundary_local=expected_boundary.astimezone(PACIFIC).isoformat(),
    )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _format_count_map(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "_none_"
    parts = [f"`{key}`: {value[key]}" for key in sorted(value)]
    return ", ".join(parts)


def _markdown_table(rows: list[tuple[str, Any]]) -> list[str]:
    lines = ["| Field | Value |", "|---|---|"]
    for field, value in rows:
        lines.append(f"| {field} | {value} |")
    return lines


def _render_verification(verification: Any) -> list[str]:
    if not isinstance(verification, dict):
        return ["_No verification object recorded on the Dream run._"]

    checks = verification.get("checks")
    lines = [f"Overall verification passed: `{verification.get('passed')}`", ""]
    if not isinstance(checks, list) or not checks:
        return lines

    lines.extend(["| Check | Passed | Expected | Actual |", "|---|---:|---|---|"])
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            "| "
            f"{check.get('name', '?')} | "
            f"`{check.get('passed')}` | "
            f"{json.dumps(check.get('expected'), sort_keys=True) if 'expected' in check else ''} | "
            f"{json.dumps(check.get('actual'), sort_keys=True) if 'actual' in check else ''} |"
        )
    return lines


def _render_tripwire_status(tripwire_status: Any) -> list[str]:
    if tripwire_status is None:
        return ["_Tripwire status was not fetched because DREAM_OPERATOR_TOKEN was not set._"]
    if not isinstance(tripwire_status, dict):
        return [f"_Unexpected tripwire payload: `{type(tripwire_status).__name__}`_"]
    if "error" in tripwire_status:
        return [f"Tripwire fetch error: `{tripwire_status.get('error')}`"]

    lines: list[str] = []
    modes = tripwire_status.get("modes")
    if isinstance(modes, dict) and modes:
        lines.extend(["| Mode | Effective | Tripped |", "|---|---|---:|"])
        for name, info in sorted(modes.items()):
            info_dict = info if isinstance(info, dict) else {}
            lines.append(
                f"| {name} | `{info_dict.get('effective')}` | `{info_dict.get('tripped')}` |"
            )
    else:
        lines.append("_No mode status returned._")

    tripwires = tripwire_status.get("tripwires")
    if isinstance(tripwires, dict) and tripwires:
        lines.extend(["", "| Tripwire | Tripped | Breaches | Threshold |", "|---|---:|---:|---|"])
        for name, info in sorted(tripwires.items()):
            info_dict = info if isinstance(info, dict) else {}
            threshold = info_dict.get("threshold", info_dict.get("threshold_ratio", ""))
            lines.append(
                f"| {name} | `{info_dict.get('tripped')}` | {info_dict.get('consecutive_breaches', '')} | {threshold} |"
            )
    return lines


def render_sleep_report(report: dict[str, Any]) -> str:
    dream_run = report.get("dream_run") if isinstance(report.get("dream_run"), dict) else {}
    health = report.get("health") if isinstance(report.get("health"), dict) else {}
    tripwire_status = report.get("tripwire_status")
    counts = dream_run.get("counts") if isinstance(dream_run.get("counts"), dict) else {}
    local_date = parse_iso_datetime(str(report["expected_boundary_utc"])).astimezone(PACIFIC).date().isoformat()

    selected_count = _as_int(counts.get("selected_operation_count"))
    held_count = _as_int(counts.get("held_operation_count"))
    applied_count = _as_int(counts.get("applied_count"))
    operation_count = _as_int(counts.get("operation_count"))

    lines: list[str] = []
    lines.append(f"# Dream Sleep Report - {local_date}")
    lines.append("")
    lines.append(f"Generated: `{report.get('generated_at')}`")
    lines.append(f"Expected scheduled boundary: `{report.get('expected_boundary_utc')}` UTC / `{report.get('expected_boundary_local')}` Pacific")
    lines.append(f"Verdict: `{'PASS' if report.get('passed') else 'FAIL'}`")
    lines.append("")

    issues = report.get("issues")
    if isinstance(issues, list) and issues:
        lines.append("## Issues")
        lines.append("")
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")

    lines.append("## Run Summary")
    lines.append("")
    lines.extend(
        _markdown_table(
            [
                ("Run ID", f"`{dream_run.get('run_id')}`"),
                ("Source", f"`{dream_run.get('_report_source', 'unknown')}`"),
                ("Status", f"`{dream_run.get('status')}`"),
                ("Trigger", f"`{dream_run.get('trigger')}`"),
                ("Auto-apply mode", f"`{dream_run.get('auto_apply_mode')}`"),
                ("Dry run", f"`{dream_run.get('dry_run')}`"),
                ("Run at", f"`{dream_run.get('run_at') or dream_run.get('created_at')}`"),
                ("Completed at", f"`{dream_run.get('completed_at')}`"),
                ("Proposal ID", f"`{dream_run.get('proposal_id')}`"),
                ("Risk score", f"`{dream_run.get('risk_score')}`"),
                ("Grade status", f"`{dream_run.get('grade_status')}`"),
                ("Apply run ID", f"`{dream_run.get('apply_run_id')}`"),
            ]
        )
    )
    lines.append("")

    lines.append("## Operation Counts")
    lines.append("")
    lines.extend(
        _markdown_table(
            [
                ("Operations proposed", operation_count if operation_count is not None else ""),
                ("Operations selected", selected_count if selected_count is not None else ""),
                ("Operations applied", applied_count if applied_count is not None else ""),
                ("Operations held", held_count if held_count is not None else ""),
                ("Archive cap", counts.get("archive_limit", "")),
                ("Promotion cap", counts.get("promotion_limit", "")),
                ("Duplicate merge cap", counts.get("duplicate_merge_limit", "")),
                ("Mark-contested cap", counts.get("mark_contested_limit", "")),
                ("Proposed by type", _format_count_map(counts.get("operation_counts"))),
                ("Selected by type", _format_count_map(counts.get("selected_counts"))),
            ]
        )
    )
    lines.append("")

    held_operations = dream_run.get("held_operations")
    lines.append("## Held Operations")
    lines.append("")
    if isinstance(held_operations, list) and held_operations:
        lines.extend(["| Operation | Type | Reason |", "|---|---|---|"])
        for operation in held_operations[:25]:
            if not isinstance(operation, dict):
                continue
            lines.append(
                f"| `{operation.get('operation_id')}` | `{operation.get('type')}` | {operation.get('reason')} |"
            )
        if len(held_operations) > 25:
            lines.append(f"| _plus {len(held_operations) - 25} more_ |  |  |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append("## Apply Verification")
    lines.append("")
    lines.extend(_render_verification(dream_run.get("verification")))
    lines.append("")

    lines.append("## Tripwires")
    lines.append("")
    lines.extend(_render_tripwire_status(tripwire_status))
    lines.append("")

    lines.append("## Worker Health")
    lines.append("")
    lines.extend(
        _markdown_table(
            [
                ("Health status", f"`{health.get('status')}`"),
                ("Last Dream run in health", f"`{health.get('last_dream_run')}`"),
                ("Total topics", health.get("total_topic_count", "")),
                ("Total projects", health.get("total_project_count", "")),
                ("Archived count", health.get("archived_count", "")),
            ]
        )
    )
    lines.append("")

    next_action = dream_run.get("next_action")
    if isinstance(next_action, str) and next_action:
        lines.append("## Next Action")
        lines.append("")
        lines.append(next_action)
        lines.append("")

    return "\n".join(lines)


def write_sleep_report(report: dict[str, Any]) -> Path:
    ensure_runtime_dirs()
    boundary = parse_iso_datetime(str(report["expected_boundary_utc"]))
    local_date = boundary.astimezone(PACIFIC).date().isoformat()
    report_path = REPORT_DIR / f"dream-sleep-{local_date}.md"
    report_path.write_text(render_sleep_report(report), encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    now_utc = parse_iso_datetime(args.now_utc) if args.now_utc else datetime.now(UTC)
    expected_archive_limit = (
        args.expected_archive_limit
        if args.expected_archive_limit is not None
        else load_scheduled_archive_limit()
    )

    health = fetch_health(args.base_url)
    dream_run = fetch_latest_scheduled_governed_run(args.base_url)
    try:
        tripwire_status = fetch_tripwire_status(args.base_url)
    except Exception as error:
        tripwire_status = {"error": str(error)}
    validation = validate_dream_run(
        health=health,
        dream_run=dream_run,
        now_utc=now_utc,
        cron_hour_utc=args.cron_hour_utc,
        cron_minute_utc=args.cron_minute_utc,
        max_start_delay_minutes=args.max_start_delay_minutes,
        expected_archive_limit=expected_archive_limit,
        expected_promotion_limit=args.expected_promotion_limit,
        allow_on_demand=args.allow_on_demand,
    )

    generated_at = utc_now_iso()
    report = {
        "generated_at": generated_at,
        "base_url": args.base_url,
        "now_utc": now_utc.isoformat(),
        "expected_boundary_utc": validation.expected_boundary_utc,
        "expected_boundary_local": validation.expected_boundary_local,
        "passed": validation.passed,
        "issues": validation.issues,
        "health": health,
        "dream_run": dream_run,
        "tripwire_status": tripwire_status,
    }
    report_path = append_report(
        f"check_overnight_dream_run_{generated_at.replace(':', '').replace('+00:00', 'Z')}.json",
        report,
    )
    sleep_report_path = write_sleep_report(report)
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
                    "dream_run_at": dream_run.get("run_at") or dream_run.get("created_at"),
                    "dream_status": dream_run.get("status"),
                    "dream_auto_apply_mode": dream_run.get("auto_apply_mode"),
                },
            ),
        )
    except Exception as error:
        print(f"WARNING: could not write validation ledger: {error}")

    print(f"Expected scheduled boundary (UTC): {validation.expected_boundary_utc}")
    print(f"Expected scheduled boundary (local): {validation.expected_boundary_local}")
    print(f"Report written to {report_path}")
    print(f"Sleep report written to {sleep_report_path}")

    if validation.passed:
        print("PASS: latest scheduled Dream governed live run is present.")
        return 0

    print("FAIL: latest scheduled Dream governed live run does not satisfy the overnight checks.")
    for issue in validation.issues:
        print(f"- {issue}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
