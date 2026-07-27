#!/usr/bin/env python3
"""Automatic, fail-closed semantic maintenance through the bounded Worker queue.

The old in-Worker semantic slice stays disabled. This external driver performs the
corpus-scale read-only audit, submits candidate IDs only, waits for the Worker to
revalidate and apply each candidate, and places a strict verification barrier after
every small cohort. Any mutation-path failure rolls back the unverified cohort and
stops the night.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests


REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = REPO_ROOT / "scripts" / "reports"
PRODUCTION_BASE_URL = "https://mcp.dancing-ganesh.com"
LOCK_KEY = "maintenance:planner:lock"
LATEST_KEY = "maintenance:planner:latest"
RUN_KEY_PREFIX = "maintenance:planner:run:"
TERMINAL_TASK_STATUSES = {"completed", "held", "failed", "dead_lettered", "rolled_back"}
SAFE_OUTBOX_STATUSES = {"completed", "derived_complete", "rolled_back"}
SAFE_TASK_STATUSES = {"completed", "held", "rolled_back"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def require_env(names: Iterable[str]) -> None:
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"missing_required_environment:{','.join(missing)}")


def write_local_report(report: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%S+0000")
    path = REPORT_DIR / f"nightly_semantic_maintenance_{stamp}.json"
    path.write_text(json.dumps(json_safe(report), indent=2, sort_keys=True) + "\n")
    return path


def load_audit_clusters(report: dict[str, Any]) -> list[list[str]]:
    m4 = report.get("m4_duplicates")
    if not isinstance(m4, dict):
        raise RuntimeError("audit_missing_m4_duplicates")
    if m4.get("skipped") is True:
        raise RuntimeError("audit_semantic_scan_skipped")
    if m4.get("query_capped") is not False:
        raise RuntimeError("capped_or_incomplete_audit_rejected")
    raw_clusters = m4.get("all_tight_clusters")
    if not isinstance(raw_clusters, list):
        raise RuntimeError("audit_missing_full_cluster_membership")

    clusters: list[list[str]] = []
    for raw in raw_clusters:
        if not isinstance(raw, list):
            continue
        ids = sorted({str(item) for item in raw if str(item)})
        if 2 <= len(ids) <= 6:
            clusters.append(ids)
    return sorted(clusters, key=lambda ids: (len(ids), ids))


def select_candidate_clusters(report: dict[str, Any], max_candidates: int) -> list[list[str]]:
    """Prefer small disjoint components; the Worker remains duplicate authority."""
    selected: list[list[str]] = []
    seen_ids: set[str] = set()
    for cluster in load_audit_clusters(report):
        if any(entry_id in seen_ids for entry_id in cluster):
            continue
        selected.append(cluster)
        seen_ids.update(cluster)
        if len(selected) >= max_candidates:
            break
    return selected


def stable_task_id(run_id: str, candidate_ids: list[str]) -> str:
    digest = hashlib.sha256("|".join(sorted(candidate_ids)).encode()).hexdigest()[:16]
    return f"{run_id}-{digest}"[:160]


def newest_report(pattern: str, since: float) -> Path:
    candidates = [path for path in REPORT_DIR.glob(pattern) if path.stat().st_mtime >= since]
    if not candidates:
        raise RuntimeError(f"expected_report_missing:{pattern}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def run_uncapped_audit(
    *,
    max_queries: int,
    workers: int,
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[dict[str, Any], Path]:
    started = time.time() - 1
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "audit_memory_quality.py"),
        "--skip-recall",
        "--skip-temporal",
        "--max-dup-queries",
        str(max_queries),
        "--dup-workers",
        str(workers),
    ]
    result = command_runner(command, cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"uncapped_audit_failed:rc={result.returncode}")
    path = newest_report("audit_memory_quality_*.json", started)
    report = json.loads(path.read_text())
    load_audit_clusters(report)
    return report, path


def run_strict_verification(
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> dict[str, Any]:
    started = time.time() - 1
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "verify_memory_consistency.py"),
        "--full",
        "--strict",
    ]
    result = command_runner(command, cwd=REPO_ROOT, check=False)
    path: Path | None = None
    try:
        path = newest_report("verify_memory_consistency_*.json", started)
    except RuntimeError:
        pass
    return {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "report_path": str(path) if path else None,
    }


class OperatorClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @staticmethod
    def _json_object(response: requests.Response, *, operation: str) -> dict[str, Any]:
        """Reject route fallthroughs and proxy pages with an actionable error."""
        try:
            body = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError) as error:
            content_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
            raise RuntimeError(
                f"worker_non_json_response:{operation}:status={response.status_code}:content_type={content_type}"
            ) from error
        if not isinstance(body, dict):
            raise RuntimeError(f"worker_invalid_json_object:{operation}:status={response.status_code}")
        return body

    def _request(self, method: str, path: str, *, attempts: int = 5, **kwargs: Any) -> requests.Response:
        last_status: int | None = None
        for attempt in range(attempts):
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=self.headers,
                timeout=30,
                **kwargs,
            )
            last_status = response.status_code
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(30, 2 ** attempt)
                self.sleep(delay)
        raise RuntimeError(f"worker_request_failed:{method}:{path}:status={last_status}")

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/health")
        return self._json_object(response, operation="health")

    def enqueue(self, *, task_id: str, candidate_ids: list[str], plan_id: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/ops/maintenance/enqueue",
            json={"task_id": task_id, "candidate_ids": candidate_ids, "plan_id": plan_id},
        )
        body = self._json_object(response, operation="enqueue")
        if response.status_code != 202 or body.get("accepted") is not True:
            raise RuntimeError(f"queue_enqueue_rejected:{task_id}")
        return body

    def task_status(self, task_id: str) -> dict[str, Any] | None:
        response = self._request("GET", "/ops/maintenance/task", params={"task_id": task_id})
        body = self._json_object(response, operation="task_status")
        status = body.get("status")
        return status if isinstance(status, dict) else None

    def wait_terminal(self, task_id: str, *, timeout_seconds: int, poll_seconds: float) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            status = self.task_status(task_id)
            if status and status.get("status") in TERMINAL_TASK_STATUSES:
                return status
            self.sleep(poll_seconds)
        raise RuntimeError(f"queue_task_timeout:{task_id}")

    def rollback(self, task_id: str) -> dict[str, Any]:
        response = self._request("POST", "/ops/maintenance/rollback", json={"task_id": task_id})
        body = self._json_object(response, operation="rollback")
        if body.get("status") != "rolled_back":
            raise RuntimeError(f"rollback_failed:{task_id}:{body.get('error') or body.get('status')}")
        return body


class RedisRunStore:
    def __init__(self, redis: Any | None = None) -> None:
        if redis is None:
            require_env(("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"))
            from upstash_redis import Redis

            redis = Redis(
                url=os.environ["UPSTASH_REDIS_REST_URL"],
                token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
            )
        self.redis = redis

    def acquire(self, run_id: str, ttl_seconds: int) -> bool:
        result = self.redis.set(LOCK_KEY, run_id, nx=True, ex=ttl_seconds)
        return result is True or result == "OK" or result == 1

    def release(self, run_id: str) -> None:
        script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
        self.redis.eval(script, keys=[LOCK_KEY], args=[run_id])

    def save(self, report: dict[str, Any]) -> None:
        encoded = json.dumps(json_safe(report), sort_keys=True)
        self.redis.set(f"{RUN_KEY_PREFIX}{report['run_id']}", encoded, ex=60 * 60 * 24 * 90)
        self.redis.set(LATEST_KEY, encoded)

    def latest(self) -> dict[str, Any] | None:
        value = self.redis.get(LATEST_KEY)
        if isinstance(value, str):
            return json.loads(value)
        return value if isinstance(value, dict) else None

    def maintenance_status_counts(self) -> dict[str, dict[str, int]]:
        output: dict[str, dict[str, int]] = {}
        for pattern in ("maintenance:task:*", "maintenance:outbox:*"):
            cursor: int | str = 0
            keys: list[str] = []
            while True:
                cursor, batch = self.redis.scan(cursor, match=pattern, count=1000)
                keys.extend(str(key) for key in batch)
                if str(cursor) == "0":
                    break
            counts: dict[str, int] = {}
            for start in range(0, len(keys), 100):
                values = self.redis.mget(*keys[start : start + 100]) if keys[start : start + 100] else []
                for value in values:
                    try:
                        parsed = json.loads(value) if isinstance(value, str) else value
                        status = str(parsed.get("status")) if isinstance(parsed, dict) else "invalid"
                    except (TypeError, ValueError):
                        status = "invalid"
                    counts[status] = counts.get(status, 0) + 1
            output[pattern] = counts
        return output

    def list_prepared_outbox(self) -> list[dict[str, Any]]:
        """Every maintenance:outbox:* journal currently in `prepared` state.

        A `prepared` journal is a merge whose before-snapshots were written but
        whose apply never reached a terminal state — an orphan from a crashed
        prior run. At run start (before this run enqueues anything) it can only
        be someone else's leftover. The barrier treats it as unsafe, so it must
        be reconciled or every night fails on it.
        """
        cursor: int | str = 0
        keys: list[str] = []
        while True:
            cursor, batch = self.redis.scan(cursor, match="maintenance:outbox:*", count=1000)
            keys.extend(str(key) for key in batch)
            if str(cursor) == "0":
                break
        prepared: list[dict[str, Any]] = []
        for start in range(0, len(keys), 100):
            chunk = keys[start : start + 100]
            values = self.redis.mget(*chunk) if chunk else []
            for value in values:
                try:
                    parsed = json.loads(value) if isinstance(value, str) else value
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict) and parsed.get("status") == "prepared":
                    prepared.append(parsed)
        return prepared

    def entry_revision(self, entry_id: str) -> int | None:
        """Current revision of a live entry. None means the entry key is absent
        (a genuine divergence — never blindly restore over a missing entry).
        A present entry with no revision field reads as 0, matching the
        Worker's `metadata.revision ?? 0` convention."""
        key = f"project:{entry_id}" if entry_id.startswith("pe_") else f"knowledge:{entry_id}"
        raw = self.redis.get(key)
        if raw is None:
            return None
        try:
            entry = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        metadata = entry.get("metadata") if isinstance(entry, dict) else None
        revision = metadata.get("revision") if isinstance(metadata, dict) else None
        return revision if isinstance(revision, int) and not isinstance(revision, bool) else 0


def _normalized_revision(value: Any) -> int:
    """The system treats an absent/None revision as 0 (Worker: revision ?? 0).
    A stale orphan's duplicate often has no revision field yet its journal
    expects 0 — without this they would look diverged and never reconcile."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def reconcile_orphan_outbox(
    *,
    store: "RedisRunStore",
    operator: OperatorClient,
) -> dict[str, list[Any]]:
    """Roll back orphaned `prepared` outbox entries, revision-guarded.

    For each `prepared` journal: if every current entry revision still equals
    the journal's expected revision (nothing touched the entries since prepare),
    roll it back through the EXISTING /ops/maintenance/rollback endpoint — for a
    never-applied orphan this restores identical state and just flips the status
    to `rolled_back`, clearing the barrier poison. If any entry diverged or is
    missing, DO NOT roll back (that would clobber newer state); record it for
    human review. Never raises: a skipped orphan is strictly safer than a
    corrupted entry, and the barrier will simply still fail that night.
    """
    reconciled: list[str] = []
    skipped: list[dict[str, Any]] = []
    for journal in store.list_prepared_outbox():
        task_id = journal.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            skipped.append({"task_id": None, "reason": "missing_task_id"})
            continue
        expected = journal.get("expected_revisions")
        expected = expected if isinstance(expected, dict) else {}
        ids = list(expected)
        if not ids:
            canonical = journal.get("canonical_id")
            dups = journal.get("duplicate_ids")
            ids = [canonical, *(dups if isinstance(dups, list) else [])]
        divergence: dict[str, str] | None = None
        for entry_id in ids:
            if not isinstance(entry_id, str) or not entry_id:
                continue
            current = store.entry_revision(entry_id)
            if current is None:
                divergence = {"reason": "entry_missing", "detail": entry_id}
                break
            if current != _normalized_revision(expected.get(entry_id)):
                divergence = {"reason": "revision_diverged", "detail": entry_id}
                break
        if divergence is not None:
            skipped.append({"task_id": task_id, **divergence})
            continue
        try:
            operator.rollback(task_id)
            reconciled.append(task_id)
        except Exception as exc:  # noqa: BLE001 — surface as skip, never abort the run
            skipped.append({"task_id": task_id, "reason": "rollback_failed", "detail": str(exc)})
    return {"reconciled": reconciled, "skipped": skipped}


def barrier(
    *,
    operator: OperatorClient,
    store: RedisRunStore,
    verifier: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    health = operator.health()
    consistency = verifier()
    statuses = store.maintenance_status_counts()
    unsafe_tasks = sorted(status for status in statuses.get("maintenance:task:*", {}) if status not in SAFE_TASK_STATUSES)
    unsafe_outbox = sorted(status for status in statuses.get("maintenance:outbox:*", {}) if status not in SAFE_OUTBOX_STATUSES)
    passed = (
        health.get("status") == "ok"
        and consistency.get("passed") is True
        and not unsafe_tasks
        and not unsafe_outbox
    )
    return {
        "passed": passed,
        "health_status": health.get("status"),
        "consistency": consistency,
        "maintenance_statuses": statuses,
        "unsafe_task_statuses": unsafe_tasks,
        "unsafe_outbox_statuses": unsafe_outbox,
    }


def rollback_tasks(
    task_ids: list[str],
    *,
    operator: OperatorClient,
) -> list[dict[str, Any]]:
    return [operator.rollback(task_id) for task_id in reversed(task_ids)]


@dataclass
class RunOptions:
    live: bool
    max_applied: int
    max_candidates: int
    cohort_size: int
    max_queries: int
    audit_workers: int
    task_timeout_seconds: int
    poll_seconds: float
    lock_ttl_seconds: int
    rollback_after_run: bool = False


def run_night(
    options: RunOptions,
    *,
    operator: OperatorClient,
    store: RedisRunStore,
    audit_runner: Callable[..., tuple[dict[str, Any], Path]] = run_uncapped_audit,
    verifier: Callable[[], dict[str, Any]] = run_strict_verification,
) -> tuple[int, dict[str, Any]]:
    run_id = f"nsm-{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": utc_iso(),
        "mode": "live" if options.live else "plan_only",
        "status": "starting",
        "tasks": [],
        "barriers": [],
        "rollbacks": [],
        "config": json_safe(options.__dict__),
    }
    lock_acquired = False
    completed_unverified: list[str] = []
    all_completed: list[str] = []
    exit_code = 1
    try:
        if options.live:
            lock_acquired = store.acquire(run_id, options.lock_ttl_seconds)
            if not lock_acquired:
                report.update(status="skipped_locked", completed_at=utc_iso())
                return 0, report
            store.save(report)

        initial_health = operator.health()
        if initial_health.get("status") != "ok":
            raise RuntimeError(f"worker_unhealthy:{initial_health.get('status')}")
        report["preflight_health"] = {"status": initial_health.get("status")}

        # Reconcile orphaned `prepared` outbox entries from crashed prior runs
        # BEFORE the barrier can trip on them. Live only (it mutates via the
        # existing rollback endpoint); at this point this run has enqueued
        # nothing, so any `prepared` entry is necessarily a prior-run orphan.
        if options.live:
            reconcile = reconcile_orphan_outbox(store=store, operator=operator)
            report["reconciled"] = reconcile["reconciled"]
            if reconcile["skipped"]:
                report["reconcile_skipped"] = reconcile["skipped"]
                report.setdefault("warnings", []).extend(
                    "orphan_outbox_unreconciled:%s:%s" % (item.get("task_id"), item.get("reason"))
                    for item in reconcile["skipped"]
                )
            store.save(report)

        pre_verification = verifier()
        report["pre_verification"] = pre_verification
        if pre_verification.get("passed") is not True:
            raise RuntimeError("preflight_strict_verification_failed")

        pre_audit, pre_audit_path = audit_runner(
            max_queries=options.max_queries,
            workers=options.audit_workers,
        )
        clusters = select_candidate_clusters(pre_audit, options.max_candidates)
        pre_m4 = pre_audit["m4_duplicates"]
        report["pre_audit"] = {
            "report_path": str(pre_audit_path),
            "query_capped": pre_m4.get("query_capped"),
            "multi_member_clusters": pre_m4.get("multi_member_clusters"),
            "entries_in_clusters": pre_m4.get("entries_in_clusters"),
            "tight_clusters": len(pre_m4.get("all_tight_clusters") or []),
        }
        report["planned_candidate_count"] = len(clusters)

        if not options.live:
            report.update(status="planned", completed_at=utc_iso(), candidate_preview=clusters[:20])
            return 0, report

        report["status"] = "applying"
        store.save(report)
        applied = 0
        attempted = 0
        for cluster in clusters:
            if applied >= options.max_applied:
                break
            task_id = stable_task_id(run_id, cluster)
            operator.enqueue(task_id=task_id, candidate_ids=cluster, plan_id=run_id)
            task_status = operator.wait_terminal(
                task_id,
                timeout_seconds=options.task_timeout_seconds,
                poll_seconds=options.poll_seconds,
            )
            attempted += 1
            task_record = {
                "task_id": task_id,
                "candidate_ids": cluster,
                "status": task_status.get("status"),
                "reason": task_status.get("reason"),
                "component": task_status.get("component"),
                "journal_key": task_status.get("journal_key"),
            }
            report["tasks"].append(task_record)
            if task_status.get("status") == "completed":
                applied += 1
                completed_unverified.append(task_id)
                all_completed.append(task_id)
            elif task_status.get("status") == "held":
                pass
            else:
                if completed_unverified:
                    report["rollbacks"].extend(rollback_tasks(completed_unverified, operator=operator))
                    completed_unverified.clear()
                raise RuntimeError(f"terminal_task_failure:{task_id}:{task_status.get('status')}")

            if len(completed_unverified) >= options.cohort_size:
                cohort_barrier = barrier(operator=operator, store=store, verifier=verifier)
                report["barriers"].append(cohort_barrier)
                if not cohort_barrier["passed"]:
                    report["rollbacks"].extend(rollback_tasks(completed_unverified, operator=operator))
                    completed_unverified.clear()
                    raise RuntimeError("cohort_verification_failed")
                completed_unverified.clear()
            store.save(report)

        if completed_unverified:
            final_cohort_barrier = barrier(operator=operator, store=store, verifier=verifier)
            report["barriers"].append(final_cohort_barrier)
            if not final_cohort_barrier["passed"]:
                report["rollbacks"].extend(rollback_tasks(completed_unverified, operator=operator))
                completed_unverified.clear()
                raise RuntimeError("final_cohort_verification_failed")
            completed_unverified.clear()
        elif not report["barriers"]:
            no_op_barrier = barrier(operator=operator, store=store, verifier=verifier)
            report["barriers"].append(no_op_barrier)
            if not no_op_barrier["passed"]:
                raise RuntimeError("no_op_verification_failed")

        report["attempted_count"] = attempted
        report["applied_count"] = applied
        report["held_count"] = sum(1 for task in report["tasks"] if task["status"] == "held")
        if clusters and applied == 0:
            report["progress_status"] = "no_candidate_applied"
            report.setdefault("warnings", []).append("semantic_maintenance_no_candidate_applied")
        else:
            report["progress_status"] = "applied" if applied else "no_candidates_planned"

        # Persist the verified mutation state before the slower uncapped post-audit.
        # If the runner is interrupted during that read-only scan, operators can see
        # that the cohort barrier passed and exactly which effects were verified.
        report["status"] = "verified"
        store.save(report)

        post_audit, post_audit_path = audit_runner(
            max_queries=options.max_queries,
            workers=options.audit_workers,
        )
        post_m4 = post_audit["m4_duplicates"]
        report["post_audit"] = {
            "report_path": str(post_audit_path),
            "query_capped": post_m4.get("query_capped"),
            "multi_member_clusters": post_m4.get("multi_member_clusters"),
            "entries_in_clusters": post_m4.get("entries_in_clusters"),
            "tight_clusters": len(post_m4.get("all_tight_clusters") or []),
            "cluster_delta": int(post_m4.get("multi_member_clusters", 0)) - int(pre_m4.get("multi_member_clusters", 0)),
            "entry_delta": int(post_m4.get("entries_in_clusters", 0)) - int(pre_m4.get("entries_in_clusters", 0)),
        }

        if options.rollback_after_run and all_completed:
            report["rollbacks"].extend(rollback_tasks(all_completed, operator=operator))
            rollback_barrier = barrier(operator=operator, store=store, verifier=verifier)
            report["barriers"].append(rollback_barrier)
            if not rollback_barrier["passed"]:
                raise RuntimeError("post_test_rollback_verification_failed")
            status = "completed_rolled_back"
        else:
            status = "completed"

        report.update(status=status, completed_at=utc_iso())
        exit_code = 0
        return exit_code, report
    except Exception as error:
        if completed_unverified:
            try:
                report["rollbacks"].extend(rollback_tasks(completed_unverified, operator=operator))
            except Exception as rollback_error:
                report["rollback_error"] = str(rollback_error)
        report.update(status="failed", error=str(error), completed_at=utc_iso())
        return 1, report
    finally:
        if options.live:
            try:
                store.save(report)
            finally:
                if lock_acquired:
                    store.release(run_id)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp_missing_timezone")
    return parsed.astimezone(timezone.utc)


def check_latest(store: RedisRunStore, *, max_age_hours: float) -> tuple[int, dict[str, Any]]:
    latest = store.latest()
    now = utc_now()
    issues: list[str] = []
    if not latest:
        issues.append("nightly_semantic_maintenance_latest_missing")
    else:
        status = latest.get("status")
        if status != "completed":
            issues.append(f"latest_status_is_{status}")
        completed_at = latest.get("completed_at")
        try:
            age = now - parse_datetime(str(completed_at))
            if age > timedelta(hours=max_age_hours):
                issues.append(f"latest_run_stale:{age.total_seconds() / 3600:.2f}h")
        except (TypeError, ValueError):
            issues.append("latest_completed_at_invalid")
        barriers = latest.get("barriers") or []
        if not barriers or any(barrier.get("passed") is not True for barrier in barriers):
            issues.append("latest_verification_barrier_missing_or_failed")
    result = {
        "checked_at": now.replace(microsecond=0).isoformat(),
        "passed": not issues,
        "issues": issues,
        "latest_run": latest,
    }
    return (0 if not issues else 1), result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or verify automatic nightly semantic maintenance.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="enqueue and apply bounded production/staging cohorts")
    mode.add_argument("--plan-only", action="store_true", help="audit and plan without external writes")
    mode.add_argument("--check-latest", action="store_true", help="validate the latest durable nightly run")
    parser.add_argument(
        "--base-url",
        default=os.getenv("DREAM_WORKER_BASE_URL") or os.getenv("STAGING_WORKER_BASE_URL") or PRODUCTION_BASE_URL,
    )
    parser.add_argument("--max-applied", type=int, default=5)
    parser.add_argument("--max-candidates", type=int, default=100)
    parser.add_argument("--cohort-size", type=int, default=5)
    parser.add_argument("--max-queries", type=int, default=20000)
    parser.add_argument("--audit-workers", type=int, default=4)
    parser.add_argument("--task-timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--lock-ttl-seconds", type=int, default=7200)
    parser.add_argument("--rollback-after-run", action="store_true")
    parser.add_argument("--max-age-hours", type=float, default=4.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_env(("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"))
    store = RedisRunStore()
    if args.check_latest:
        code, report = check_latest(store, max_age_hours=args.max_age_hours)
    else:
        require_env(("UPSTASH_VECTOR_REST_URL", "UPSTASH_VECTOR_REST_TOKEN", "DREAM_OPERATOR_TOKEN"))
        operator = OperatorClient(args.base_url, os.environ["DREAM_OPERATOR_TOKEN"])
        options = RunOptions(
            live=args.live,
            max_applied=max(1, args.max_applied),
            max_candidates=max(1, args.max_candidates),
            cohort_size=max(1, args.cohort_size),
            max_queries=max(1, args.max_queries),
            audit_workers=max(1, args.audit_workers),
            task_timeout_seconds=max(10, args.task_timeout_seconds),
            poll_seconds=max(0.1, args.poll_seconds),
            lock_ttl_seconds=max(300, args.lock_ttl_seconds),
            rollback_after_run=args.rollback_after_run,
        )
        code, report = run_night(options, operator=operator, store=store)
    path = write_local_report(report)
    print(json.dumps({
        "status": report.get("status"),
        "run_id": report.get("run_id"),
        "applied_count": report.get("applied_count"),
        "held_count": report.get("held_count"),
        "progress_status": report.get("progress_status"),
        "warnings": report.get("warnings"),
        "report_path": str(path),
        "issues": report.get("issues"),
        "error": report.get("error"),
    }, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
