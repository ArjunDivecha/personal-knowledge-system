#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import requests

from _memory_migration import append_report, utc_now_iso

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_command(cmd: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
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


def call_operator_unauthorized(base_url: str) -> dict[str, Any]:
    response = requests.post(
        f"{base_url.rstrip('/')}/ops/dream/run",
        headers={"Content-Type": "application/json"},
        json={"dry_run": True, "set_as_latest": False, "note": "Unauthorized staging smoke test"},
        timeout=30,
    )
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
    return {
        "status_code": response.status_code,
        "ok": response.status_code == 401,
        "body": body,
    }


def parse_sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError("No JSON payload found in SSE response")


def oauth_client_flow(base_url: str) -> tuple[requests.Session, str]:
    session = requests.Session()
    metadata = session.get(f"{base_url.rstrip('/')}/.well-known/oauth-authorization-server", timeout=30).json()

    redirect_uri = "http://127.0.0.1:9876/callback"
    registration = session.post(
        metadata["registration_endpoint"],
        json={
            "client_name": "staging-e2e-smoke",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": "mcp:read mcp:write",
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
            "scope": "mcp:read mcp:write",
            "state": f"staging-{uuid.uuid4().hex}",
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


def mcp_post(
    session: requests.Session,
    base_url: str,
    access_token: str,
    payload: dict[str, Any],
    session_id: str | None = None,
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {access_token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return session.post(f"{base_url.rstrip('/')}/mcp", json=payload, headers=headers, timeout=30)


def call_mcp_sequence(base_url: str) -> list[dict[str, Any]]:
    session, access_token = oauth_client_flow(base_url)
    steps: list[dict[str, Any]] = []

    initialize_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "staging-e2e-smoke", "version": "1.0"},
        },
    }
    initialize_response = mcp_post(session, base_url, access_token, initialize_payload)
    initialize_data = parse_sse_json(initialize_response.text)
    mcp_session_id = initialize_response.headers.get("mcp-session-id")
    if not mcp_session_id:
        raise RuntimeError("MCP initialize response did not include a session id")
    steps.append(
        {
            "name": "mcp_initialize",
            "status_code": initialize_response.status_code,
            "ok": initialize_response.ok and "result" in initialize_data,
            "body": initialize_data,
        }
    )

    tools_list_response = mcp_post(
        session,
        base_url,
        access_token,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        session_id=mcp_session_id,
    )
    tools_list_data = parse_sse_json(tools_list_response.text)
    tool_names = [tool["name"] for tool in tools_list_data.get("result", {}).get("tools", [])]
    steps.append(
        {
            "name": "mcp_tools_list",
            "status_code": tools_list_response.status_code,
            "ok": tools_list_response.ok and "get_index" in tool_names and "search" in tool_names,
            "body": {"tool_names": tool_names},
        }
    )

    get_index_response = mcp_post(
        session,
        base_url,
        access_token,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_index", "arguments": {}}},
        session_id=mcp_session_id,
    )
    get_index_data = parse_sse_json(get_index_response.text)
    get_index_payload = json.loads(get_index_data["result"]["content"][0]["text"])
    steps.append(
        {
            "name": "mcp_get_index",
            "status_code": get_index_response.status_code,
            "ok": get_index_response.ok
            and get_index_payload.get("total_topics") == 2
            and get_index_payload.get("total_projects") == 1,
            "body": get_index_payload,
        }
    )

    search_response = mcp_post(
        session,
        base_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search",
                "arguments": {"query": "quantitative investing", "limit": 3},
            },
        },
        session_id=mcp_session_id,
    )
    search_data = parse_sse_json(search_response.text)
    search_payload = json.loads(search_data["result"]["content"][0]["text"])
    top_result = (search_payload.get("results") or [{}])[0]
    steps.append(
        {
            "name": "mcp_search",
            "status_code": search_response.status_code,
            "ok": search_response.ok and top_result.get("id") == "ke_fixture_identity_001",
            "body": search_payload,
        }
    )

    context_response = mcp_post(
        session,
        base_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_context",
                "arguments": {"topic": "Quantitative investing background"},
            },
        },
        session_id=mcp_session_id,
    )
    context_data = parse_sse_json(context_response.text)
    context_payload = json.loads(context_data["result"]["content"][0]["text"])
    steps.append(
        {
            "name": "mcp_get_context",
            "status_code": context_response.status_code,
            "ok": context_response.ok and context_payload.get("id") == "ke_fixture_identity_001",
            "body": context_payload,
        }
    )

    dream_summary_response = mcp_post(
        session,
        base_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "get_dream_summary", "arguments": {}},
        },
        session_id=mcp_session_id,
    )
    dream_summary_data = parse_sse_json(dream_summary_response.text)
    dream_summary_payload = json.loads(dream_summary_data["result"]["content"][0]["text"])
    steps.append(
        {
            "name": "mcp_get_dream_summary",
            "status_code": dream_summary_response.status_code,
            "ok": dream_summary_response.ok and (
                dream_summary_payload.get("counts", {}).get("total_entries") == 3
                or dream_summary_payload.get("message") == "No Dream runs recorded yet."
            ),
            "body": dream_summary_payload,
        }
    )

    return steps


def build_verify_env() -> dict[str, str]:
    env = dict(os.environ)
    env["UPSTASH_REDIS_REST_URL"] = os.environ["STAGING_UPSTASH_REDIS_REST_URL"]
    env["UPSTASH_REDIS_REST_TOKEN"] = os.environ["STAGING_UPSTASH_REDIS_REST_TOKEN"]
    env["UPSTASH_VECTOR_REST_URL"] = os.environ["STAGING_UPSTASH_VECTOR_REST_URL"]
    env["UPSTASH_VECTOR_REST_TOKEN"] = os.environ["STAGING_UPSTASH_VECTOR_REST_TOKEN"]
    if os.getenv("STAGING_OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = os.environ["STAGING_OPENAI_API_KEY"]
    return env


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
        report["steps"].append({"name": "operator_unauthorized", **call_operator_unauthorized(args.base_url)})
        if not args.skip_dream:
            report["steps"].append({"name": "dream_dry_run", **call_dream_dry_run(args.base_url)})
        report["steps"].extend(call_mcp_sequence(args.base_url))
        time.sleep(1)
        verify_command = [
            str(REPO_ROOT / "distillation" / "venv" / "bin" / "python"),
            str(REPO_ROOT / "scripts" / "verify_memory_consistency.py"),
            "--full",
            "--strict",
        ]
        report["steps"].append(
            {
                "name": "verify_memory_consistency",
                **run_command(verify_command, env=build_verify_env()),
            }
        )

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
