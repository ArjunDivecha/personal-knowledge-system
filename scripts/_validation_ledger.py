from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from _memory_migration import utc_now_iso

VALIDATION_LAST_KEY = "validation:last"
VALIDATION_GATE_STATUS_KEY = "validation:gate_status"
VALIDATION_HISTORY_PREFIX = "validation:history:"
VALIDATION_HISTORY_LIMIT = 100


@dataclass(frozen=True)
class ValidationGateRecord:
    gate: str
    passed: bool
    issues: list[Any]
    report_path: str
    details: dict[str, Any]


def _history_key(generated_at: str) -> str:
    return f"{VALIDATION_HISTORY_PREFIX}{generated_at[:10]}"


def write_validation_gate(redis_client: Any, record: ValidationGateRecord) -> dict[str, Any]:
    generated_at = utc_now_iso()
    payload = {
        "schema_version": 1,
        "generated_at": generated_at,
        "gate": record.gate,
        "passed": record.passed,
        "status": "pass" if record.passed else "fail",
        "issues": record.issues,
        "report_path": record.report_path,
        "details": record.details,
    }

    raw_status = redis_client.get(VALIDATION_GATE_STATUS_KEY)
    if isinstance(raw_status, str):
        try:
            gate_status = json.loads(raw_status)
        except json.JSONDecodeError:
            gate_status = {}
    elif isinstance(raw_status, dict):
        gate_status = dict(raw_status)
    else:
        gate_status = {}

    gates = gate_status.get("gates") if isinstance(gate_status.get("gates"), dict) else {}
    gates[record.gate] = payload

    overall_passed = all(
        isinstance(gate, dict) and gate.get("passed") is True
        for gate in gates.values()
    )
    gate_status = {
        "schema_version": 1,
        "updated_at": generated_at,
        "overall_status": "green" if overall_passed else "red",
        "overall_passed": overall_passed,
        "gates": gates,
    }

    redis_client.set(VALIDATION_LAST_KEY, json.dumps(payload))
    redis_client.set(VALIDATION_GATE_STATUS_KEY, json.dumps(gate_status))
    redis_client.lpush(_history_key(generated_at), json.dumps(payload))
    redis_client.ltrim(_history_key(generated_at), 0, VALIDATION_HISTORY_LIMIT - 1)

    return payload
