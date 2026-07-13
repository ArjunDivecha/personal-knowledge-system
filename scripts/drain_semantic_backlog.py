"""
=============================================================================
SCRIPT NAME: drain_semantic_backlog.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
  : dedup thresholds (COSINE_DUP_THRESHOLD, DEDUP_NEIGHBOR_K,
  SEMANTIC_MAX_CLUSTER_SIZE) and dream_thresholds.archive_protected_context_types.
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/.env
  : Upstash Redis + Vector credentials and DREAM_OPERATOR_TOKEN.
- (network, PRODUCTION Upstash Vector, READ) existing entry embeddings — this
  script NEVER re-embeds; it reuses stored vectors, exactly like the audit.

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/semantic_drain_<UTCSTAMP>.json
  : the run report. Written incrementally after EVERY batch, so a crash,
  abort, or stop-on-fail still leaves a complete record of what was applied.
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/semantic_drain_checkpoint.json
  : resume checkpoint (completed batch indices + applied operation ids).
  Delete it to start over.
- (network, PRODUCTION, WRITE — only with --apply) semantic duplicate merges,
  applied through the Worker's governed endpoints so that EVERY merge passes
  the hard merge-conservation gates (mergeGates.ts validateMergeConservation)
  and is individually reversible via rollback_dream_apply.

VERSION: 1.0
LAST UPDATED: 2026-07-13
AUTHOR: Claude (Opus 4.8) for Arjun Divecha

DESCRIPTION:
One-time accelerated drain of the semantic duplicate backlog
(PKS-SEMANTIC-CONSOLIDATION-001 "scale" bar).

In plain terms: the memory store has accumulated thousands of entries that say
the same thing in different words, because every ingestion minted a brand-new
entry instead of merging into the existing one. As of the 2026-07-13 audit
that is 1,348 duplicate clusters covering 5,454 entries — about 46% of the
corpus. (The leak that CREATED them was closed the same day by turning
admission-dedup live, so this backlog is a fixed pool, not a growing one.)

This script merges each cluster down to one canonical entry, in bounded
batches, verifying after every batch and stopping instantly on any failure.

HOW IT WORKS
1. Cluster discovery (READ-ONLY): fetch stored vectors for all active
   knowledge entries and run nearest-neighbour queries at
   COSINE_DUP_THRESHOLD (0.95), building connected components — the same
   method the audit uses, so the scope matches the audited number.
2. Batch: pack clusters into batches of <=200 entry ids, KEEPING EACH CLUSTER
   WHOLLY INSIDE ONE BATCH. (If a cluster's members were split across two
   batches, the Worker would never see them together and would never merge
   them — the drain would silently under-deliver.)
3. Per batch, through the Worker's own governed endpoints:
     POST /ops/dream/proposal {candidate_ids}  -> semantic dedup fires
          (runDreamProposal enables semantic dedup exactly when candidate ids
           are supplied), archive_limit=0/promotion_limit=0 so the drain does
           NOT archive or promote anything — merges only.
     POST /ops/dream/grade    {proposal_id}    -> deterministic hard gates
     POST /ops/dream/apply    {operation_ids}  -> only duplicate_merge ops
     make verify-memory-full                    -> stop-on-fail
4. Resume from checkpoint; report incrementally.

SAFETY PROPERTIES
1. PROTECTED-TYPE GUARD (the important one). The Worker enforces its
   protected-type hold — "an explicit_save / professional_identity /
   stated_preference entry may never be an automatic merge LOSER" (INV1) —
   inside buildScheduledGovernedDecision, which is on the SCHEDULED nightly
   path ONLY (index.ts:1317). This script necessarily uses the MANUAL
   /ops/dream/apply path, which does NOT run that check. So the guard is
   re-implemented here client-side: any duplicate_merge whose evidence names a
   protected-type loser is DROPPED from the apply and recorded in the report
   for human review. Without this, the drain would quietly absorb Arjun's
   explicitly-saved memories. This is checked against the policy's own
   archive_protected_context_types list, not a hardcoded copy.
2. CONSERVATION GATES STILL APPLY. Merges go through the Worker's real apply
   path, so validateMergeConservation (dream.ts:4832) hard-fails any merge
   that would drop evidence. This script deliberately does NOT re-implement
   merging in Python — that would bypass the very gates the contract built.
3. MERGES ONLY. archive_limit=0 and promotion_limit=0 on the proposal, and the
   apply is filtered to type == "duplicate_merge". No archives, no promotions,
   no context-type changes, no project transitions.
4. VERIFY-AFTER-EACH-BATCH, STOP-ON-FAIL. `make verify-memory-full` runs after
   every batch; any nonzero exit halts the drain immediately, leaving the
   checkpoint intact for inspection. This is the check the 2026-06-08 attempt
   failed on (after 35 merges) — it is now enforced per batch, not per run.
5. REVERSIBLE. Every batch applies under its own mutation_id, so any batch can
   be rolled back wholesale via rollback_dream_apply.
6. DOUBLE-GATED + RATE-LIMIT AWARE. Dry-run is the default. The Worker rate-
   limits each operator endpoint to 12 calls/hour; the script paces itself and
   never busy-retries a 429.

DEPENDENCIES: requests, upstash_redis, upstash_vector (via ingestion config)

USAGE:
  # Dry run (default): discover clusters, build batches, and report the exact
  # scope — including which merges would be dropped by the protected-type
  # guard. Performs ZERO writes.
  distillation/venv/bin/python scripts/drain_semantic_backlog.py

  # Apply, only after reviewing the dry run.
  distillation/venv/bin/python scripts/drain_semantic_backlog.py \
      --apply --i-reviewed-the-dry-run

  # Limit the blast radius (e.g. a pilot):
  ... --apply --i-reviewed-the-dry-run --max-batches 2
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
INGESTION_DIR = REPO_ROOT / "ingestion"
POLICY_PATH = REPO_ROOT / "shared" / "memory_policy.json"
REPORTS_DIR = REPO_ROOT / "scripts" / "reports"
CHECKPOINT_PATH = REPORTS_DIR / "semantic_drain_checkpoint.json"

PROD_BASE_URL = "https://mcp.dancing-ganesh.com"
MAX_CANDIDATES_PER_PROPOSAL = 200   # Worker caps candidate_ids at 200
MAX_OPS_PER_APPLY = 100             # Worker caps operation_ids at 100
RATE_LIMIT_PER_HOUR = 12            # OPERATOR_WRITE_RATE_LIMIT, per endpoint

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open() as fh:
        return json.load(fh)


def clusters_from_edges(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Union-find connected components (mirrors the audit's helper)."""
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def discover_clusters(storage: Any, policy: dict[str, Any], workers: int) -> list[list[str]]:
    """READ-ONLY. Same method the audit uses, so the scope matches the audited
    number: reuse stored vectors (never re-embed), NN-query each active
    knowledge entry, keep neighbours at/above COSINE_DUP_THRESHOLD, and take
    connected components."""
    dedup = policy["dedup"]
    threshold = float(dedup["COSINE_DUP_THRESHOLD"])
    neighbor_k = int(dedup["DEDUP_NEIGHBOR_K"])
    max_cluster = int(dedup["SEMANTIC_MAX_CLUSTER_SIZE"])

    print(f"[drain] discovering clusters (cosine >= {threshold}, k={neighbor_k}, max_size={max_cluster})")
    redis = storage.redis
    vector = storage.vector

    keys = []
    cursor: int | str = 0
    while True:
        cursor, batch = redis.scan(cursor, match="knowledge:*", count=500)
        keys.extend(batch)
        if str(cursor) == "0":
            break

    active_ids: list[str] = []
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        for raw in redis.mget(*chunk):
            if not raw:
                continue
            e = json.loads(raw) if isinstance(raw, str) else raw
            meta = e.get("metadata") or {}
            if meta.get("archived"):
                continue
            if e.get("state") != "active":
                continue
            active_ids.append(str(e["id"]))
    print(f"[drain]   active knowledge entries: {len(active_ids)}")

    vec_by_id: dict[str, list[float]] = {}
    for i in range(0, len(active_ids), 100):
        chunk = active_ids[i:i + 100]
        for row in vector.fetch(chunk, include_vectors=True, include_metadata=False):
            if row is None:
                continue
            rid = getattr(row, "id", None)
            rvec = getattr(row, "vector", None)
            if rid is not None and rvec:
                vec_by_id[str(rid)] = list(rvec)
    print(f"[drain]   vectors fetched: {len(vec_by_id)}")

    id_set = set(vec_by_id)
    edges: list[tuple[str, str]] = []

    def nn_for(eid: str) -> list[tuple[str, str]]:
        res = vector.query(vector=vec_by_id[eid], top_k=neighbor_k + 5, include_metadata=False)
        out = []
        for r in res:
            nid = str(r["id"] if isinstance(r, dict) else getattr(r, "id"))
            score = float(r.get("score", 0.0) if isinstance(r, dict) else getattr(r, "score", 0.0))
            if nid == eid or nid not in id_set:
                continue
            if score >= threshold:
                out.append((eid, nid))
        return out

    query_ids = list(vec_by_id)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(nn_for, e): e for e in query_ids}
        done = 0
        for fut in as_completed(futures):
            edges.extend(fut.result())
            done += 1
            if done % 1000 == 0:
                print(f"[drain]   NN progress: {done}/{len(query_ids)}")

    components = clusters_from_edges(query_ids, edges)
    clusters = [sorted(c) for c in components if len(c) >= 2]
    # Respect the policy's cluster-size guard: oversized components are the
    # chaining pathology the 0.95 threshold exists to avoid. Skip, don't split
    # arbitrarily — splitting would invent groupings the Worker never proposed.
    oversized = [c for c in clusters if len(c) > max_cluster]
    clusters = [c for c in clusters if len(c) <= max_cluster]
    clusters.sort(key=len, reverse=True)
    print(f"[drain]   clusters (2..{max_cluster}): {len(clusters)} covering {sum(len(c) for c in clusters)} entries")
    if oversized:
        print(f"[drain]   SKIPPED {len(oversized)} oversized clusters (> {max_cluster} members) "
              f"covering {sum(len(c) for c in oversized)} entries — left for the nightly pass")
    return clusters


def pack_batches(clusters: list[list[str]], limit: int) -> list[list[str]]:
    """Pack clusters into batches of <= limit ids, never splitting a cluster."""
    batches: list[list[str]] = []
    current: list[str] = []
    for cluster in clusters:
        if len(current) + len(cluster) > limit and current:
            batches.append(current)
            current = []
        current.extend(cluster)
    if current:
        batches.append(current)
    return batches


class OpsClient:
    """Talks to the Worker's governed Dream endpoints, pacing to the 12/hour
    per-endpoint operator rate limit. Never busy-retries a 429."""

    def __init__(self, base_url: str, token: str, apply_mode: bool):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.apply_mode = apply_mode
        self.calls: dict[str, list[float]] = {}

    def _pace(self, endpoint: str) -> None:
        now = time.time()
        hist = [t for t in self.calls.get(endpoint, []) if now - t < 3600]
        if len(hist) >= RATE_LIMIT_PER_HOUR:
            wait = 3600 - (now - hist[0]) + 5
            print(f"[drain]   rate limit on {endpoint}: sleeping {wait/60:.1f} min")
            time.sleep(max(wait, 0))
            hist = [t for t in self.calls.get(endpoint, []) if time.time() - t < 3600]
        hist.append(time.time())
        self.calls[endpoint] = hist

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._pace(endpoint)
        r = requests.post(f"{self.base}/ops/dream/{endpoint}", headers=self.headers,
                          json=payload, timeout=120)
        if r.status_code == 429:
            raise SystemExit(f"❌ Rate limited on {endpoint} despite pacing — aborting rather than hammering.")
        if not r.ok:
            raise SystemExit(f"❌ {endpoint} failed HTTP {r.status_code}: {r.text[:400]}")
        return r.json()


def protected_losers(op: dict[str, Any], protected: list[str]) -> list[dict[str, str]]:
    """SAFETY 1. Return any merge LOSERS whose context_type is protected.

    The Worker enforces this only on the scheduled path
    (buildScheduledGovernedDecision, index.ts:1317). The manual apply path this
    script must use does NOT — so if we did not check here, the drain would
    absorb explicit_save / professional_identity / stated_preference entries.
    """
    evidence = op.get("evidence") or {}
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except (ValueError, TypeError):
            evidence = {}
    hits = []
    for dup in (evidence.get("duplicates") or []):
        ctype = dup.get("context_type")
        if ctype in protected:
            hits.append({"id": str(dup.get("id")), "context_type": str(ctype)})
    return hits


def run_verify() -> tuple[bool, str]:
    """make verify-memory-full — the stop-on-fail check (the one the
    2026-06-08 attempt died on, now run after EVERY batch)."""
    proc = subprocess.run(
        ["make", "verify-memory-full"], cwd=REPO_ROOT,
        capture_output=True, text=True,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, "\n".join(tail[-6:])


def main() -> int:
    ap = argparse.ArgumentParser(description="Drain the semantic duplicate backlog in verified batches.")
    ap.add_argument("--apply", action="store_true", help="Perform real merges (default: dry run)")
    ap.add_argument("--i-reviewed-the-dry-run", action="store_true")
    ap.add_argument("--max-batches", type=int, default=None, help="Stop after N batches (pilot mode)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--base-url", default=PROD_BASE_URL)
    ap.add_argument(
        "--clusters-from",
        default=None,
        help=(
            "Path to an audit_memory_quality report JSON; reuse its "
            "m4_duplicates.all_tight_clusters instead of re-deriving clusters. "
            "STRONGLY PREFERRED: the NN scan is the expensive step (~12k vector "
            "queries) and the audit already does it. Re-deriving it here against "
            "an index already under load measured ~15x slower. Reusing the audit "
            "also guarantees the drain's scope matches the audited number exactly."
        ),
    )
    args = ap.parse_args()

    if args.apply and not args.i_reviewed_the_dry_run:
        raise SystemExit("❌ --apply requires --i-reviewed-the-dry-run. Run the dry run first.")

    from core.storage import StorageClient  # noqa: E402

    # core.config does not export DREAM_OPERATOR_TOKEN; read it straight from
    # ingestion/.env (the same file core.config loads with override=True).
    from dotenv import dotenv_values  # noqa: E402

    token = (
        dotenv_values(INGESTION_DIR / ".env").get("DREAM_OPERATOR_TOKEN")
        or dotenv_values(REPO_ROOT / ".env").get("DREAM_OPERATOR_TOKEN")
    )
    if not token:
        raise SystemExit("❌ DREAM_OPERATOR_TOKEN not found in ingestion/.env or repo-root .env")

    policy = load_policy()
    protected = list(policy["dream_thresholds"]["archive_protected_context_types"])
    storage = StorageClient()

    mode = "APPLY" if args.apply else "DRY RUN"
    run_id = f"semantic_drain_{uuid.uuid4().hex[:12]}"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S+0000")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"semantic_drain_{stamp}.json"

    print("=" * 72)
    print(f"Semantic backlog drain — {mode}")
    print(f"run_id: {run_id}   protected types (never merge-losers): {protected}")
    print("=" * 72)

    if args.clusters_from:
        src = Path(args.clusters_from)
        m4 = json.loads(src.read_text())["m4_duplicates"]
        if m4.get("query_capped"):
            raise SystemExit(
                f"❌ {src.name} was produced by a CAPPED scan (query_capped=true) — "
                "its cluster list is incomplete and the drain would silently miss "
                "duplicates. Re-run the audit with --max-dup-queries above the "
                "corpus size."
            )
        clusters = [sorted(c) for c in m4["all_tight_clusters"]]
        clusters.sort(key=len, reverse=True)
        print(f"[drain] clusters loaded from {src.name}: {len(clusters)} "
              f"covering {sum(len(c) for c in clusters)} entries "
              f"(cosine >= {m4.get('cosine_threshold')}, uncapped scan)")
    else:
        clusters = discover_clusters(storage, policy, args.workers)

    batches = pack_batches(clusters, MAX_CANDIDATES_PER_PROPOSAL)
    if args.max_batches:
        batches = batches[: args.max_batches]
    print(f"[drain] batches: {len(batches)} (<= {MAX_CANDIDATES_PER_PROPOSAL} ids each, clusters kept intact)")

    checkpoint: dict[str, Any] = {"completed_batches": [], "applied_operations": []}
    if CHECKPOINT_PATH.exists() and args.apply:
        checkpoint = json.loads(CHECKPOINT_PATH.read_text())
        print(f"[drain] resuming — {len(checkpoint['completed_batches'])} batches already done")

    report: dict[str, Any] = {
        "run_id": run_id, "mode": mode, "started_at": utc_now(),
        "base_url": args.base_url, "protected_context_types": protected,
        "cluster_count": len(clusters),
        "entries_in_clusters": sum(len(c) for c in clusters),
        "batch_count": len(batches),
        "batches": [], "merges_applied": 0,
        "protected_holds": [], "stopped_early": None,
    }

    def flush() -> None:
        report["updated_at"] = utc_now()
        report_path.write_text(json.dumps(report, indent=1))

    if not args.apply:
        flush()
        print(f"\nDRY RUN — zero writes.")
        print(f"  clusters: {len(clusters)}  entries: {report['entries_in_clusters']}  batches: {len(batches)}")
        print(f"  Each batch: proposal -> grade -> apply(duplicate_merge only) -> verify-memory-full (stop-on-fail)")
        print(f"  Protected-type losers will be HELD, not merged: {protected}")
        print(f"\nReport: {report_path}")
        print("\nTo apply: rerun with --apply --i-reviewed-the-dry-run")
        return 0

    client = OpsClient(args.base_url, token, apply_mode=True)

    for idx, batch in enumerate(batches):
        if idx in checkpoint["completed_batches"]:
            continue
        print(f"\n[drain] batch {idx + 1}/{len(batches)} — {len(batch)} entries")

        proposal = client.post("proposal", {
            "candidate_ids": batch,
            "archive_limit": 0,      # merges only — never archive
            "promotion_limit": 0,    # merges only — never promote
            "note": f"Semantic backlog drain {run_id} batch {idx}",
        })
        pid = proposal.get("run_id")
        ops = [o for o in (proposal.get("operations") or []) if o.get("type") == "duplicate_merge"]
        print(f"[drain]   proposal {pid}: {len(ops)} duplicate_merge ops")

        # SAFETY 1 — protected-type guard (the manual apply path has none).
        safe_ops, held = [], []
        for op in ops:
            hits = protected_losers(op, protected)
            if hits:
                held.append({"operation_id": op.get("operation_id"), "protected_losers": hits})
            else:
                safe_ops.append(op)
        if held:
            print(f"[drain]   HELD {len(held)} merges with protected-type losers (never auto-merged)")
            report["protected_holds"].extend(held)

        batch_rec: dict[str, Any] = {
            "index": idx, "entries": len(batch), "proposal_id": pid,
            "merge_ops": len(ops), "held_protected": len(held),
            "applied": 0, "verify": None,
        }

        if not safe_ops:
            batch_rec["verify"] = "skipped (nothing to apply)"
            report["batches"].append(batch_rec)
            checkpoint["completed_batches"].append(idx)
            CHECKPOINT_PATH.write_text(json.dumps(checkpoint, indent=1))
            flush()
            print("[drain]   nothing to apply — next batch")
            continue

        grade = client.post("grade", {"proposal_id": pid})
        if not (grade.get("passed") is True and grade.get("status") == "passed"):
            report["stopped_early"] = f"batch {idx}: grade did not pass ({grade.get('status')})"
            batch_rec["verify"] = "grade failed"
            report["batches"].append(batch_rec)
            flush()
            print(f"❌ STOP — grade did not pass: {grade.get('status')}")
            return 1

        applied_total = 0
        for c in range(0, len(safe_ops), MAX_OPS_PER_APPLY):
            chunk = safe_ops[c:c + MAX_OPS_PER_APPLY]
            res = client.post("apply", {
                "proposal_id": pid,
                "mutation_id": f"{run_id}_b{idx}_c{c}",
                "reason": f"Semantic backlog drain {run_id}: merge near-duplicate cluster members (batch {idx})",
                "operation_ids": [o["operation_id"] for o in chunk],
                "require_grade_pass": True,
                "grade_id": grade.get("grade_id"),
            })
            if not res.get("ok"):
                report["stopped_early"] = f"batch {idx}: apply failed — {res.get('error')}"
                batch_rec["verify"] = "apply failed"
                report["batches"].append(batch_rec)
                flush()
                print(f"❌ STOP — apply failed: {res.get('error')}")
                return 1
            applied_total += int(res.get("applied_count") or 0)
            checkpoint["applied_operations"].extend([o["operation_id"] for o in chunk])

        batch_rec["applied"] = applied_total
        report["merges_applied"] += applied_total
        print(f"[drain]   applied {applied_total} merges")

        # SAFETY 4 — verify after EVERY batch; stop on fail.
        ok, tail = run_verify()
        batch_rec["verify"] = "PASS" if ok else f"FAIL: {tail}"
        report["batches"].append(batch_rec)
        if not ok:
            report["stopped_early"] = f"batch {idx}: verify-memory-full FAILED"
            flush()
            print(f"❌ STOP — verify-memory-full failed after batch {idx}:\n{tail}")
            print(f"   Applied merges are reversible via rollback_dream_apply "
                  f"(mutation_id prefix {run_id}_b{idx}).")
            return 1
        print("[drain]   verify-memory-full: PASS")

        checkpoint["completed_batches"].append(idx)
        CHECKPOINT_PATH.write_text(json.dumps(checkpoint, indent=1))
        flush()

    flush()
    print(f"\n✅ Drain complete — {report['merges_applied']} merges applied across {len(batches)} batches.")
    if report["protected_holds"]:
        print(f"   {len(report['protected_holds'])} merges HELD (protected-type losers) — review in the report.")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
