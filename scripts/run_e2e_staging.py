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
from _validation_ledger import ValidationGateRecord, write_validation_gate

REPO_ROOT = Path(__file__).resolve().parent.parent
PHASE9_PROBE_SET_KEY = "dream:outcome_probes"
DEFAULT_PHASE9_PROBES_PATH = REPO_ROOT / "tests" / "fixtures" / "phase9_staging_outcome_probes.json"


def get_env_value(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    return None


def run_command(cmd: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return {
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def build_staging_seed_env() -> dict[str, str]:
    env = dict(os.environ)
    env["STAGING_UPSTASH_REDIS_REST_URL"] = get_env_value("STAGING_UPSTASH_REDIS_REST_URL") or ""
    env["STAGING_UPSTASH_REDIS_REST_TOKEN"] = get_env_value("STAGING_UPSTASH_REDIS_REST_TOKEN") or ""
    env["STAGING_UPSTASH_VECTOR_REST_URL"] = get_env_value("STAGING_UPSTASH_VECTOR_REST_URL") or ""
    env["STAGING_UPSTASH_VECTOR_REST_TOKEN"] = get_env_value("STAGING_UPSTASH_VECTOR_REST_TOKEN") or ""
    openai_key = get_env_value("STAGING_OPENAI_API_KEY", "OPENAI_API_KEY")
    if openai_key:
        env["STAGING_OPENAI_API_KEY"] = openai_key
    return env


def call_health(base_url: str) -> dict[str, Any]:
    response = requests.get(f"{base_url.rstrip('/')}/health", timeout=30)
    return {
        "status_code": response.status_code,
        "ok": response.ok,
        "body": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text,
    }


def call_dream_run(
    base_url: str,
    *,
    dry_run: bool,
    set_as_latest: bool,
    note: str,
    candidate_ids: list[str] | None = None,
    archive_limit: int | None = None,
    promotion_limit: int | None = None,
) -> dict[str, Any]:
    token = get_env_value("STAGING_DREAM_OPERATOR_TOKEN", "DREAM_OPERATOR_TOKEN")
    if not token:
        return {"skipped": True, "reason": "STAGING_DREAM_OPERATOR_TOKEN or DREAM_OPERATOR_TOKEN not set"}

    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "set_as_latest": set_as_latest,
        "note": note,
    }
    if candidate_ids:
        payload["candidate_ids"] = candidate_ids
    if archive_limit is not None:
        payload["archive_limit"] = archive_limit
    if promotion_limit is not None:
        payload["promotion_limit"] = promotion_limit

    response = requests.post(
        f"{base_url.rstrip('/')}/ops/dream/run",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
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

def oauth_client_flow(
    base_url: str,
    *,
    scope: str = "mcp:read",
    operator_token: str | None = None,
) -> tuple[requests.Session, str]:
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
            "scope": scope,
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
            "scope": scope,
            "state": f"staging-{uuid.uuid4().hex}",
        },
        headers={"Authorization": f"Bearer {operator_token}"} if operator_token else None,
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
    path: str = "/mcp",
) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {access_token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return session.post(f"{base_url.rstrip('/')}{path}", json=payload, headers=headers, timeout=30)


def call_mcp_tool(
    session: requests.Session,
    base_url: str,
    access_token: str,
    session_id: str,
    *,
    rpc_id: int,
    name: str,
    arguments: dict[str, Any],
    path: str = "/mcp",
) -> tuple[requests.Response, dict[str, Any], Any]:
    response = mcp_post(
        session,
        base_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        },
        session_id=session_id,
        path=path,
    )
    response_data = parse_sse_json(response.text)
    payload = json.loads(response_data["result"]["content"][0]["text"])
    return response, response_data, payload


def require_step_ok(step: dict[str, Any]) -> None:
    if not step.get("ok"):
        raise RuntimeError(f"Staging lifecycle step failed: {step.get('name')}: {step}")


def select_archive_operation(proposal: dict[str, Any]) -> dict[str, Any]:
    operations = proposal.get("operations")
    if not isinstance(operations, list):
        raise RuntimeError("Dream proposal did not include an operations list")
    for operation in operations:
        if isinstance(operation, dict) and operation.get("type") == "archive_entry":
            operation_id = operation.get("operation_id")
            entry_id = operation.get("entry_id")
            if isinstance(operation_id, str) and isinstance(entry_id, str):
                return operation
    raise RuntimeError("Dream proposal did not include an archive_entry operation for rollback drill")


def build_phase9_apply_arguments(arguments: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    if not enabled:
        return dict(arguments)
    return {
        **arguments,
        "phase9_outcome_gate": True,
        "phase9_auto_rollback": False,
        "phase9_probe_set_key": PHASE9_PROBE_SET_KEY,
        "phase9_write_validation_ledger": True,
    }


def call_openai_read_only_compatibility(base_url: str) -> list[dict[str, Any]]:
    session, access_token = oauth_client_flow(base_url, scope="mcp:read")
    steps: list[dict[str, Any]] = []
    initialize_response = mcp_post(
        session,
        base_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "staging-openai-readonly-check", "version": "1.0"},
            },
        },
        path="/openai/mcp",
    )
    initialize_data = parse_sse_json(initialize_response.text)
    session_id = initialize_response.headers.get("mcp-session-id")
    steps.append(
        {
            "name": "openai_mcp_initialize",
            "status_code": initialize_response.status_code,
            "ok": initialize_response.ok and "result" in initialize_data and bool(session_id),
            "body": initialize_data,
        }
    )
    if not session_id:
        return steps

    tools_response = mcp_post(
        session,
        base_url,
        access_token,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        session_id=session_id,
        path="/openai/mcp",
    )
    tools_data = parse_sse_json(tools_response.text)
    tool_names = [tool["name"] for tool in tools_data.get("result", {}).get("tools", [])]
    forbidden_tools = {
        "run_dream_proposal",
        "grade_dream_proposal",
        "apply_dream_proposal",
        "rollback_dream_apply",
        "rollback_dream_run",
        "restore_archived",
        "set_context_type",
        "rebuild_thin_index",
    }
    steps.append(
        {
            "name": "openai_mcp_read_only_tools",
            "status_code": tools_response.status_code,
            "ok": tools_response.ok
            and "get_index" in tool_names
            and "search" in tool_names
            and forbidden_tools.isdisjoint(tool_names),
            "body": {
                "tool_names": tool_names,
                "forbidden_present": sorted(forbidden_tools.intersection(tool_names)),
            },
        }
    )
    return steps


def call_dream_governance_lifecycle(base_url: str, *, phase9_gate: bool = True) -> list[dict[str, Any]]:
    operator_token = get_env_value("STAGING_DREAM_OPERATOR_TOKEN", "DREAM_OPERATOR_TOKEN")
    if not operator_token:
        return [
            {
                "name": "dream_governance_lifecycle",
                "ok": False,
                "status_code": None,
                "body": {"error": "STAGING_DREAM_OPERATOR_TOKEN or DREAM_OPERATOR_TOKEN not set"},
            }
        ]

    session, access_token = oauth_client_flow(
        base_url,
        scope="mcp:read mcp:write",
        operator_token=operator_token,
    )
    steps: list[dict[str, Any]] = []
    initialize_response = mcp_post(
        session,
        base_url,
        access_token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "staging-dream-governance-lifecycle", "version": "1.0"},
            },
        },
    )
    initialize_data = parse_sse_json(initialize_response.text)
    session_id = initialize_response.headers.get("mcp-session-id")
    init_step = {
        "name": "dream_lifecycle_initialize",
        "status_code": initialize_response.status_code,
        "ok": initialize_response.ok and "result" in initialize_data and bool(session_id),
        "body": initialize_data,
    }
    steps.append(init_step)
    require_step_ok(init_step)
    assert session_id is not None

    proposal_response, _, proposal = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=2,
        name="run_dream_proposal",
        arguments={
            "candidate_ids": ["ke_fixture_archive_001"],
            "archive_limit": 1,
            "promotion_limit": 0,
            "note": "Staging R5 lifecycle proposal",
        },
    )
    archive_operation = select_archive_operation(proposal)
    operation_id = str(archive_operation["operation_id"])
    archived_entry_id = str(archive_operation["entry_id"])
    proposal_step = {
        "name": "dream_lifecycle_proposal",
        "status_code": proposal_response.status_code,
        "ok": proposal_response.ok
        and proposal.get("status") == "proposal_ready"
        and proposal.get("dry_run") is True
        and proposal.get("operation_count", 0) >= 1
        and archived_entry_id == "ke_fixture_archive_001",
        "body": {
            "run_id": proposal.get("run_id"),
            "operation_id": operation_id,
            "entry_id": archived_entry_id,
            "operation_count": proposal.get("operation_count"),
            "counts": proposal.get("counts"),
        },
    }
    steps.append(proposal_step)
    require_step_ok(proposal_step)
    proposal_id = str(proposal["run_id"])

    grade_response, _, grade = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=3,
        name="grade_dream_proposal",
        arguments={"proposal_id": proposal_id},
    )
    grade_step = {
        "name": "dream_lifecycle_grade",
        "status_code": grade_response.status_code,
        "ok": grade_response.ok
        and grade.get("proposal_id") == proposal_id
        and grade.get("passed") is True
        and grade.get("status") == "passed",
        "body": {
            "proposal_id": grade.get("proposal_id"),
            "grade_id": grade.get("grade_id"),
            "passed": grade.get("passed"),
            "issues": grade.get("issues"),
        },
    }
    steps.append(grade_step)
    require_step_ok(grade_step)

    apply_mutation_id = f"staging-r5-apply-{uuid.uuid4().hex}"
    rollback_mutation_id = f"staging-r5-rollback-{uuid.uuid4().hex}"
    apply_response, _, apply_payload = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=4,
        name="apply_dream_proposal",
        arguments=build_phase9_apply_arguments({
            "proposal_id": proposal_id,
            "mutation_id": apply_mutation_id,
            "reason": "Staging R5 rollback drill apply",
            "require_grade_pass": True,
            "grade_id": str(grade.get("grade_id")),
            "operation_ids": [operation_id],
        }, enabled=phase9_gate),
    )
    phase9_payload = apply_payload.get("phase9_outcome_gate") if phase9_gate else None
    phase9_gate_ok = (
        not phase9_gate
        or (
            isinstance(phase9_payload, dict)
            and phase9_payload.get("status") == "passed"
            and isinstance(phase9_payload.get("gate_report"), dict)
            and phase9_payload["gate_report"].get("passed") is True
        )
    )
    apply_step = {
        "name": "dream_lifecycle_apply",
        "status_code": apply_response.status_code,
        "ok": apply_response.ok
        and apply_payload.get("ok") is True
        and apply_payload.get("applied_count") == 1
        and phase9_gate_ok,
        "body": {
            "proposal_id": apply_payload.get("proposal_id"),
            "mutation_id": apply_payload.get("mutation_id"),
            "applied_count": apply_payload.get("applied_count"),
            "operation_ids": apply_payload.get("operation_ids"),
            "before_revisions": apply_payload.get("before_revisions"),
            "after_revisions": apply_payload.get("after_revisions"),
            "phase9_outcome_gate": phase9_payload,
        },
    }
    steps.append(apply_step)
    require_step_ok(apply_step)

    post_apply_deep_response, _, post_apply_deep = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=5,
        name="get_deep",
        arguments={"id": archived_entry_id},
    )
    post_apply_metadata = post_apply_deep.get("metadata") or {}
    post_apply_step = {
        "name": "dream_lifecycle_verify_post_apply",
        "status_code": post_apply_deep_response.status_code,
        "ok": post_apply_deep_response.ok
        and post_apply_deep.get("id") == archived_entry_id
        and post_apply_metadata.get("archived") is True,
        "body": post_apply_deep,
    }
    steps.append(post_apply_step)
    require_step_ok(post_apply_step)

    rollback_response, _, rollback_payload = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=6,
        name="rollback_dream_apply",
        arguments={
            "proposal_id": proposal_id,
            "apply_mutation_id": apply_mutation_id,
            "rollback_mutation_id": rollback_mutation_id,
            "reason": "Staging R5 rollback drill restore",
            "operation_ids": [operation_id],
        },
    )
    rollback_step = {
        "name": "dream_lifecycle_rollback",
        "status_code": rollback_response.status_code,
        "ok": rollback_response.ok
        and rollback_payload.get("ok") is True
        and rollback_payload.get("rolled_back_count") == 1,
        "body": {
            "proposal_id": rollback_payload.get("proposal_id"),
            "apply_mutation_id": rollback_payload.get("apply_mutation_id"),
            "rollback_mutation_id": rollback_payload.get("rollback_mutation_id"),
            "rolled_back_count": rollback_payload.get("rolled_back_count"),
            "conflicts": rollback_payload.get("conflicts"),
            "error": rollback_payload.get("error"),
            "raw": rollback_payload,
        },
    }
    steps.append(rollback_step)
    require_step_ok(rollback_step)

    post_rollback_deep_response, _, post_rollback_deep = call_mcp_tool(
        session,
        base_url,
        access_token,
        session_id,
        rpc_id=7,
        name="get_deep",
        arguments={"id": archived_entry_id},
    )
    post_rollback_metadata = post_rollback_deep.get("metadata") or {}
    post_rollback_step = {
        "name": "dream_lifecycle_verify_post_rollback",
        "status_code": post_rollback_deep_response.status_code,
        "ok": post_rollback_deep_response.ok
        and post_rollback_deep.get("id") == archived_entry_id
        and post_rollback_metadata.get("archived") is False,
        "body": post_rollback_deep,
    }
    steps.append(post_rollback_step)
    require_step_ok(post_rollback_step)

    return steps


def call_mcp_sequence(base_url: str, archived_entry_id: str | None = None) -> list[dict[str, Any]]:
    operator_token = get_env_value("STAGING_DREAM_OPERATOR_TOKEN", "DREAM_OPERATOR_TOKEN")
    requested_scope = "mcp:read mcp:write" if archived_entry_id and operator_token else "mcp:read"
    session, access_token = oauth_client_flow(
        base_url,
        scope=requested_scope,
        operator_token=operator_token if archived_entry_id else None,
    )
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
            "ok": tools_list_response.ok
            and "get_index" in tool_names
            and "search" in tool_names
            and "restore_archived" in tool_names
            and "set_context_type" in tool_names,
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
    expected_topic_count = 1 if archived_entry_id else 2
    expected_archived_count = 1 if archived_entry_id else 0
    steps.append(
        {
            "name": "mcp_get_index",
            "status_code": get_index_response.status_code,
            "ok": get_index_response.ok
            and get_index_payload.get("total_topics") == expected_topic_count
            and get_index_payload.get("total_projects") == 1
            and get_index_payload.get("archived_count") == expected_archived_count,
            "expected": {
                "total_topics": expected_topic_count,
                "total_projects": 1,
                "archived_count": expected_archived_count,
            },
            "body": get_index_payload,
        }
    )

    next_rpc_id = 4
    if archived_entry_id:
        restore_response, _, restore_payload = call_mcp_tool(
            session,
            base_url,
            access_token,
            mcp_session_id,
            rpc_id=next_rpc_id,
            name="restore_archived",
            arguments={
                "id": archived_entry_id,
                "reason": "Staging end-to-end validation restore",
            },
        )
        next_rpc_id += 1
        steps.append(
            {
                "name": "mcp_restore_archived",
                "status_code": restore_response.status_code,
                "ok": restore_response.ok
                and restore_payload.get("id") == archived_entry_id
                and restore_payload.get("context_type") == "explicit_save"
                and restore_payload.get("injection_tier") == 1,
                "body": restore_payload,
            }
        )

        set_context_response, _, set_context_payload = call_mcp_tool(
            session,
            base_url,
            access_token,
            mcp_session_id,
            rpc_id=next_rpc_id,
            name="set_context_type",
            arguments={
                "id": archived_entry_id,
                "context_type": "recurring_pattern",
                "reason": "Staging end-to-end validation override",
            },
        )
        next_rpc_id += 1
        steps.append(
            {
                "name": "mcp_set_context_type",
                "status_code": set_context_response.status_code,
                "ok": set_context_response.ok
                and set_context_payload.get("id") == archived_entry_id
                and set_context_payload.get("context_type") == "recurring_pattern"
                and set_context_payload.get("injection_tier") == 2,
                "body": set_context_payload,
            }
        )

        deep_response, _, deep_payload = call_mcp_tool(
            session,
            base_url,
            access_token,
            mcp_session_id,
            rpc_id=next_rpc_id,
            name="get_deep",
            arguments={"id": archived_entry_id},
        )
        next_rpc_id += 1
        deep_metadata = deep_payload.get("metadata") or {}
        steps.append(
            {
                "name": "mcp_get_deep_after_restore",
                "status_code": deep_response.status_code,
                "ok": deep_response.ok
                and deep_payload.get("id") == archived_entry_id
                and deep_metadata.get("archived") is False
                and deep_metadata.get("context_type") == "recurring_pattern"
                and deep_metadata.get("classification_status") == "manual_override"
                and deep_metadata.get("injection_tier") == 2,
                "body": deep_payload,
            }
        )

    search_response, _, search_payload = call_mcp_tool(
        session,
        base_url,
        access_token,
        mcp_session_id,
        rpc_id=next_rpc_id,
        name="search",
        arguments={"query": "quantitative investing", "limit": 3},
    )
    next_rpc_id += 1
    top_result = (search_payload.get("results") or [{}])[0]
    steps.append(
        {
            "name": "mcp_search",
            "status_code": search_response.status_code,
            "ok": search_response.ok and top_result.get("id") == "ke_fixture_identity_001",
            "body": search_payload,
        }
    )

    context_response, _, context_payload = call_mcp_tool(
        session,
        base_url,
        access_token,
        mcp_session_id,
        rpc_id=next_rpc_id,
        name="get_context",
        arguments={"topic": "Quantitative investing background"},
    )
    next_rpc_id += 1
    steps.append(
        {
            "name": "mcp_get_context",
            "status_code": context_response.status_code,
            "ok": context_response.ok and context_payload.get("id") == "ke_fixture_identity_001",
            "body": context_payload,
        }
    )

    dream_summary_response, _, dream_summary_payload = call_mcp_tool(
        session,
        base_url,
        access_token,
        mcp_session_id,
        rpc_id=next_rpc_id,
        name="get_dream_summary",
        arguments={},
    )
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
    env["UPSTASH_REDIS_REST_URL"] = get_env_value("STAGING_UPSTASH_REDIS_REST_URL") or ""
    env["UPSTASH_REDIS_REST_TOKEN"] = get_env_value("STAGING_UPSTASH_REDIS_REST_TOKEN") or ""
    env["UPSTASH_VECTOR_REST_URL"] = get_env_value("STAGING_UPSTASH_VECTOR_REST_URL") or ""
    env["UPSTASH_VECTOR_REST_TOKEN"] = get_env_value("STAGING_UPSTASH_VECTOR_REST_TOKEN") or ""
    openai_key = get_env_value("STAGING_OPENAI_API_KEY", "OPENAI_API_KEY")
    if openai_key:
        env["OPENAI_API_KEY"] = openai_key
    return env


def write_staging_validation_gate(report_path: Path, passed: bool, issues: list[str], report: dict[str, Any]) -> None:
    env_backup = {
        "UPSTASH_REDIS_REST_URL": os.getenv("UPSTASH_REDIS_REST_URL"),
        "UPSTASH_REDIS_REST_TOKEN": os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    }
    try:
        sys.path.insert(0, str(REPO_ROOT / "distillation"))
        from storage.redis_client import RedisClient  # noqa: PLC0415

        os.environ["UPSTASH_REDIS_REST_URL"] = get_env_value("STAGING_UPSTASH_REDIS_REST_URL") or ""
        os.environ["UPSTASH_REDIS_REST_TOKEN"] = get_env_value("STAGING_UPSTASH_REDIS_REST_TOKEN") or ""
        if not os.environ["UPSTASH_REDIS_REST_URL"] or not os.environ["UPSTASH_REDIS_REST_TOKEN"]:
            raise RuntimeError("Missing staging Redis env for validation ledger write")
        redis_client = RedisClient()
        write_validation_gate(
            redis_client.client,
            ValidationGateRecord(
                gate="staging_e2e",
                passed=passed,
                issues=issues,
                report_path=str(report_path),
                details={
                    "base_url": report.get("base_url"),
                    "bundle": report.get("bundle"),
                    "step_count": len(report.get("steps", [])),
                    "rollback_drill": any(
                        step.get("name") == "dream_lifecycle_rollback" and step.get("ok") is True
                        for step in report.get("steps", [])
                    ),
                    "openai_read_only": any(
                        step.get("name") == "openai_mcp_read_only_tools" and step.get("ok") is True
                        for step in report.get("steps", [])
                    ),
                },
            ),
        )
    except Exception as error:  # noqa: BLE001
        print(f"WARNING: could not write staging validation ledger: {error}")
    finally:
        for key, value in env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a staging smoke flow for the knowledge system")
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--bundle",
        type=Path,
        default=REPO_ROOT / "tests" / "fixtures" / "sample_memory_fixture.json",
    )
    parser.add_argument("--phase9-probes", type=Path, default=DEFAULT_PHASE9_PROBES_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--skip-dream", action="store_true")
    parser.add_argument("--skip-write-path", action="store_true")
    parser.add_argument(
        "--legacy-smoke",
        action="store_true",
        help="Run the older Dream dry-run/live-archive smoke instead of the PRD R5 governance lifecycle.",
    )
    parser.add_argument("--skip-phase9-gate", action="store_true")
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
            "--phase9-probes",
            str(args.phase9_probes),
        ]
        if args.dry_run:
            cmd.append("--dry-run")
        else:
            cmd.append("--reset")
        report["steps"].append({"name": "seed_staging", **run_command(cmd, env=build_staging_seed_env())})

    if not args.dry_run:
        report["steps"].append({"name": "health_check", **call_health(args.base_url)})
        report["steps"].append({"name": "operator_unauthorized", **call_operator_unauthorized(args.base_url)})
        archived_entry_id: str | None = None
        if not args.skip_dream and not args.legacy_smoke:
            try:
                lifecycle_steps = call_dream_governance_lifecycle(
                    args.base_url,
                    phase9_gate=not args.skip_phase9_gate,
                )
            except Exception as error:  # noqa: BLE001
                lifecycle_steps = [
                    {
                        "name": "dream_governance_lifecycle",
                        "ok": False,
                        "status_code": None,
                        "body": {"error": str(error)},
                    }
                ]
            report["steps"].extend(lifecycle_steps)
            try:
                report["steps"].extend(call_openai_read_only_compatibility(args.base_url))
            except Exception as error:  # noqa: BLE001
                report["steps"].append(
                    {
                        "name": "openai_mcp_read_only_compatibility",
                        "ok": False,
                        "status_code": None,
                        "body": {"error": str(error)},
                    }
                )
        elif not args.skip_dream:
            dry_run_step = call_dream_run(
                args.base_url,
                dry_run=True,
                set_as_latest=False,
                note="Staging smoke test Dream dry run",
            )
            report["steps"].append({"name": "dream_dry_run", **dry_run_step})

            archive_candidates = dry_run_step.get("body", {}).get("archive_candidates") or []
            if archive_candidates:
                archived_entry_id = archive_candidates[0].get("id")

            if not args.skip_write_path and archived_entry_id:
                live_run_step = call_dream_run(
                    args.base_url,
                    dry_run=False,
                    set_as_latest=False,
                    note="Staging smoke test bounded live archive",
                    candidate_ids=[archived_entry_id],
                    archive_limit=1,
                )
                report["steps"].append({"name": "dream_live_archive", **live_run_step})
                report["steps"].append({"name": "health_after_archive", **call_health(args.base_url)})
            elif not args.skip_write_path:
                report["steps"].append(
                    {
                        "name": "dream_live_archive",
                        "ok": False,
                        "status_code": None,
                        "body": {"error": "Dream dry run returned no archive candidates to validate write path"},
                    }
                )

        try:
            report["steps"].extend(
                call_mcp_sequence(
                    args.base_url,
                    archived_entry_id=archived_entry_id if not args.skip_write_path else None,
                )
            )
        except Exception as error:  # noqa: BLE001
            report["steps"].append(
                {
                    "name": "mcp_sequence",
                    "ok": False,
                    "status_code": None,
                    "body": {"error": str(error)},
                }
            )
        if not args.skip_write_path and archived_entry_id:
            report["steps"].append({"name": "health_after_restore", **call_health(args.base_url)})
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
    if not args.dry_run:
        write_staging_validation_gate(
            report_path,
            passed=not failed_steps,
            issues=[str(step.get("name", "unknown_step")) for step in failed_steps],
            report=report,
        )
    return 1 if failed_steps else 0


if __name__ == "__main__":
    raise SystemExit(main())
