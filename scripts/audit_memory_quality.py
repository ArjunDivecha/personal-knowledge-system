#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: audit_memory_quality.py
=============================================================================

PURPOSE:
Read-only memory-quality audit (PRD Phase 0, spec
docs/pks-phase0-audit-script-spec-2026-05-29.md). Computes seven metrics
(M1-M7) over the live store and writes a JSON report to scripts/reports/.
Optionally records a `verify_memory_quality` validation-ledger gate.

STRICTLY READ-ONLY over the corpus: it never mutates knowledge/project
entries or vectors. The only write it can make is the explicit validation
ledger entry, and only when invoked with --write-gate. A runtime guard
wraps the Redis and Vector clients to forbid corpus-write methods.

INPUT:
- Upstash Redis + Vector via distillation storage clients (read-only).
- shared/memory_policy.json (dedup thresholds + quality_gate thresholds).
- tests/fixtures/recall_probes.json (M6 oracle).

OUTPUT:
- scripts/reports/audit_memory_quality_<ISO8601>.json
- (optional) validation:* ledger entry for gate `verify_memory_quality`.

USAGE:
    python scripts/audit_memory_quality.py                # audit only
    python scripts/audit_memory_quality.py --write-gate   # audit + ledger gate
    python scripts/audit_memory_quality.py --max-dup-queries 6000
    python scripts/audit_memory_quality.py --skip-recall  # skip M6 (no OpenAI)

VERSION: 1.0
LAST UPDATED: 2026-05-29
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from _memory_migration import append_report, ensure_runtime_dirs, utc_now_iso  # noqa: E402
from _validation_ledger import ValidationGateRecord, write_validation_gate  # noqa: E402

from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402

POLICY_PATH = REPO_ROOT / "shared" / "memory_policy.json"
RECALL_PROBES_PATH = REPO_ROOT / "tests" / "fixtures" / "recall_probes.json"

# ---------------------------------------------------------------------------
# Read-only guards. Wrap the storage clients so any corpus-write method call
# raises immediately. The validation-ledger write uses the raw client and is
# the single, explicit exception (only under --write-gate).
# ---------------------------------------------------------------------------
_FORBIDDEN_REDIS = {
    "set", "delete", "sadd", "srem", "lpush", "rpush", "ltrim",
    "save_knowledge_entry", "save_project_entry", "save_thin_index",
    "delete_knowledge_entry", "delete_project_entry", "increment_access_count",
    "mark_conversation_processed",
}
_FORBIDDEN_VECTOR = {
    "upsert", "upsert_entry", "upsert_entries_batch", "delete", "delete_entry",
    "delete_entries_batch", "update_entry_metadata",
}


class _ReadOnly:
    """Attribute proxy that blocks a denylist of mutating method names."""

    def __init__(self, wrapped: Any, forbidden: set[str], label: str):
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_forbidden", forbidden)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, name: str) -> Any:
        if name in object.__getattribute__(self, "_forbidden"):
            raise RuntimeError(
                f"READ-ONLY GUARD: {object.__getattribute__(self, '_label')}.{name}() "
                f"is a corpus-write method and is forbidden in the audit."
            )
        return getattr(object.__getattribute__(self, "_wrapped"), name)


# ---------------------------------------------------------------------------
# Policy + probe loading
# ---------------------------------------------------------------------------
def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open() as fh:
        return json.load(fh)


def load_recall_probes() -> list[dict[str, Any]]:
    if not RECALL_PROBES_PATH.exists():
        return []
    with RECALL_PROBES_PATH.open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Entry helpers (work on KnowledgeEntry | ProjectEntry dataclasses)
# ---------------------------------------------------------------------------
def entry_type(entry: Any) -> str:
    eid = str(getattr(entry, "id", ""))
    if eid.startswith("pe_"):
        return "project"
    if eid.startswith("ke_"):
        return "knowledge"
    # fall back to attribute shape
    return "project" if hasattr(entry, "name") and not hasattr(entry, "domain") else "knowledge"


def entry_label(entry: Any) -> str:
    return getattr(entry, "domain", None) or getattr(entry, "name", None) or str(getattr(entry, "id", ""))


def entry_meta(entry: Any) -> Any:
    return getattr(entry, "metadata", None)


def is_archived(entry: Any) -> bool:
    meta = entry_meta(entry)
    return bool(getattr(meta, "archived", False))


# ---------------------------------------------------------------------------
# Union-find (pure; unit-tested)
# ---------------------------------------------------------------------------
class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # path compression
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for node in self.parent:
            out[self.find(node)].append(node)
        return out


def clusters_from_edges(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Return connected components (each of size >= 1) over the given edges."""
    uf = UnionFind()
    for n in nodes:
        uf.add(n)
    for a, b in edges:
        uf.union(a, b)
    return [sorted(members) for members in uf.clusters().values()]


# ---------------------------------------------------------------------------
# Metric calculators (pure where possible; unit-tested)
# ---------------------------------------------------------------------------
def compute_m1_tiers(active: list[Any]) -> dict[str, Any]:
    counts = {1: 0, 2: 0, 3: 0}
    for e in active:
        tier = getattr(entry_meta(e), "injection_tier", None)
        if tier in (1, 2, 3):
            counts[tier] += 1
    total = max(1, len(active))
    return {
        "tier_1": counts[1],
        "tier_2": counts[2],
        "tier_3": counts[3],
        "tier_1_share": round(counts[1] / total, 4),
        "tier_2_share": round(counts[2] / total, 4),
        "tier_3_share": round(counts[3] / total, 4),
    }


def compute_m3_salience(active: list[Any], top_n: int = 15) -> dict[str, Any]:
    values: list[float] = []
    for e in active:
        s = getattr(entry_meta(e), "salience_score", None)
        if isinstance(s, (int, float)):
            values.append(round(float(s), 4))
    total = max(1, len(values))
    value_counts = Counter(values)
    top = [
        {"value": v, "count": c, "share": round(c / total, 4)}
        for v, c in value_counts.most_common(top_n)
    ]
    max_share = max((t["share"] for t in top), default=0.0)
    # histogram at bin width 0.01
    hist_counter: Counter[float] = Counter()
    for v in values:
        hist_counter[round(math.floor(v * 100) / 100, 2)] += 1
    histogram = [
        {"bin": b, "count": c} for b, c in sorted(hist_counter.items())
    ]
    return {
        "max_single_value_share": round(max_share, 4),
        "top_values": top,
        "histogram": histogram,
        "values_scored": len(values),
    }


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------
def run_audit(
    *,
    max_dup_queries: int,
    dup_workers: int,
    skip_dup: bool,
    skip_recall: bool,
    recall_k: int,
) -> dict[str, Any]:
    policy = load_policy()
    dedup_cfg = policy.get("dedup", {})
    cosine_threshold = float(dedup_cfg.get("COSINE_DUP_THRESHOLD", 0.86))
    neighbor_k = int(dedup_cfg.get("DEDUP_NEIGHBOR_K", 10))

    redis = _ReadOnly(RedisClient(), _FORBIDDEN_REDIS, "redis")
    vector = _ReadOnly(VectorClient(), _FORBIDDEN_VECTOR, "vector")

    # --- Load active entries ---
    knowledge = [e for e in redis.get_all_knowledge_entries() if not is_archived(e)]
    projects = [e for e in redis.get_all_project_entries() if not is_archived(e)]
    active = knowledge + projects
    active_total = len(active)
    by_id = {str(e.id): e for e in active}

    print(f"[audit] active entries: knowledge={len(knowledge)} project={len(projects)} total={active_total}")

    # --- M1 / M2 ---
    m1 = compute_m1_tiers(active)
    print(f"[audit] M1 tiers: T1={m1['tier_1']} ({m1['tier_1_share']:.1%}) "
          f"T2={m1['tier_2']} ({m1['tier_2_share']:.1%}) T3={m1['tier_3']}")

    # --- M3 ---
    m3 = compute_m3_salience(active)
    print(f"[audit] M3 salience: max single-value share={m3['max_single_value_share']:.1%} "
          f"(top value {m3['top_values'][0]['value'] if m3['top_values'] else 'n/a'})")

    # --- M4 duplicate clusters (vector NN, read-only) ---
    m4 = compute_m4_duplicates(
        vector=vector,
        by_id=by_id,
        cosine_threshold=cosine_threshold,
        neighbor_k=neighbor_k,
        max_queries=max_dup_queries,
        workers=dup_workers,
        skip=skip_dup,
    )
    print(f"[audit] M4 dup clusters>=2: {m4['multi_member_clusters']} "
          f"covering {m4['entries_in_clusters']} entries "
          f"(threshold {cosine_threshold}, capped={m4['query_capped']})")

    # --- M5 growth ---
    m5 = compute_m5_growth(redis, window_days=14)
    print(f"[audit] M5 growth: net_active_delta={m5['net_active_delta']} "
          f"archived={m5['archived']} (partial={m5['window_partial']})")

    # --- M6 recall ---
    if skip_recall:
        m6 = {"recall_at_5": None, "probes": [], "skipped": True}
        print("[audit] M6 recall: skipped")
    else:
        m6 = compute_m6_recall(vector, recall_k=recall_k)
        print(f"[audit] M6 recall@{recall_k}: {m6['recall_at_5']}")

    # --- M7 access coverage ---
    m7 = compute_m7_access(active, m6)
    print(f"[audit] M7 access coverage: {m7['active_with_access_share']:.1%} of active entries")

    report = {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "active_counts": {
            "knowledge": len(knowledge),
            "project": len(projects),
            "total": active_total,
        },
        "m1_tiers": m1,
        "m3_salience": m3,
        "m4_duplicates": m4,
        "m5_growth": m5,
        "m6_recall": m6,
        "m7_access": m7,
    }
    return report


def compute_m4_duplicates(
    *,
    vector: Any,
    by_id: dict[str, Any],
    cosine_threshold: float,
    neighbor_k: int,
    max_queries: int,
    workers: int,
    skip: bool,
) -> dict[str, Any]:
    base = {
        "cosine_threshold": cosine_threshold,
        "neighbor_k": neighbor_k,
        "multi_member_clusters": 0,
        "entries_in_clusters": 0,
        "largest_clusters": [],
        "query_capped": False,
        "skipped": skip,
    }
    if skip:
        return base

    ids = list(by_id.keys())
    # Fetch each entry's stored vector (reuse existing embeddings; never re-embed).
    fetched = vector.fetch_entries(ids, include_metadata=False, include_vectors=True)
    vec_by_id: dict[str, list[float]] = {}
    for row in fetched:
        rid = getattr(row, "id", None)
        rvec = getattr(row, "vector", None)
        if rid is not None and rvec:
            vec_by_id[str(rid)] = list(rvec)

    query_ids = [i for i in ids if i in vec_by_id]
    capped = False
    if len(query_ids) > max_queries:
        query_ids = query_ids[:max_queries]
        capped = True

    edges: list[tuple[str, str]] = []
    pair_cosine: dict[tuple[str, str], float] = {}

    def nn_for(eid: str) -> list[tuple[str, str, float]]:
        etype = entry_type(by_id[eid])
        # Query a few extra to absorb self-match + cross-type neighbours we drop.
        results = vector.query(
            vector=vec_by_id[eid],
            top_k=neighbor_k + 5,
            include_metadata=True,
        )
        local: list[tuple[str, str, float]] = []
        for r in results:
            nid = str(r["id"])
            if nid == eid or nid not in by_id:
                continue
            if entry_type(by_id[nid]) != etype:
                continue
            score = float(r.get("score", 0.0))
            if score >= cosine_threshold:
                local.append((eid, nid, score))
        return local

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(nn_for, eid): eid for eid in query_ids}
        done = 0
        for fut in as_completed(futures):
            for a, b, score in fut.result():
                key = (a, b) if a < b else (b, a)
                edges.append((a, b))
                pair_cosine[key] = max(pair_cosine.get(key, 0.0), score)
            done += 1
            if done % 500 == 0:
                print(f"[audit]   M4 NN progress: {done}/{len(query_ids)}")

    components = clusters_from_edges(query_ids, edges)
    multi = [c for c in components if len(c) >= 2]
    multi.sort(key=len, reverse=True)
    entries_in_clusters = sum(len(c) for c in multi)

    largest = []
    for members in multi[:10]:
        max_cos = 0.0
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                key = (a, b) if a < b else (b, a)
                max_cos = max(max_cos, pair_cosine.get(key, 0.0))
        # canonical guess = member with the most neighbours (proxy: appears most)
        largest.append({
            "member_ids": members,
            "member_domains": [entry_label(by_id[m]) for m in members],
            "max_pairwise_cosine": round(max_cos, 4),
        })

    base.update({
        "multi_member_clusters": len(multi),
        "entries_in_clusters": entries_in_clusters,
        "largest_clusters": largest,
        "query_capped": capped,
        "queries_run": len(query_ids),
    })
    return base


def run_threshold_sweep(
    *,
    thresholds: list[float],
    neighbor_k: int,
    max_queries: int,
    workers: int,
) -> dict[str, Any]:
    """Read-only: one NN pass collecting all (a,b,score) edges, then re-cluster
    at several cosine thresholds to show how cluster sizes change. Informs
    COSINE_DUP_THRESHOLD tuning (PRD open question #1)."""
    redis = _ReadOnly(RedisClient(), _FORBIDDEN_REDIS, "redis")
    vector = _ReadOnly(VectorClient(), _FORBIDDEN_VECTOR, "vector")

    knowledge = [e for e in redis.get_all_knowledge_entries() if not is_archived(e)]
    projects = [e for e in redis.get_all_project_entries() if not is_archived(e)]
    active = knowledge + projects
    by_id = {str(e.id): e for e in active}
    ids = list(by_id.keys())

    fetched = vector.fetch_entries(ids, include_metadata=False, include_vectors=True)
    vec_by_id: dict[str, list[float]] = {}
    for row in fetched:
        rid = getattr(row, "id", None)
        rvec = getattr(row, "vector", None)
        if rid is not None and rvec:
            vec_by_id[str(rid)] = list(rvec)
    query_ids = [i for i in ids if i in vec_by_id][:max_queries]

    raw_edges: list[tuple[str, str, float]] = []

    def nn_for(eid: str):
        etype = entry_type(by_id[eid])
        results = vector.query(vector=vec_by_id[eid], top_k=neighbor_k + 5, include_metadata=False)
        out = []
        for r in results:
            nid = str(r["id"])
            if nid == eid or nid not in by_id:
                continue
            if entry_type(by_id[nid]) != etype:
                continue
            out.append((eid, nid, float(r.get("score", 0.0))))
        return out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed({pool.submit(nn_for, eid) for eid in query_ids}):
            raw_edges.extend(fut.result())

    sweep = []
    for thr in thresholds:
        edges = [(a, b) for (a, b, s) in raw_edges if s >= thr]
        comps = [c for c in clusters_from_edges(query_ids, edges) if len(c) >= 2]
        sizes = sorted((len(c) for c in comps), reverse=True)
        sweep.append({
            "threshold": thr,
            "multi_member_clusters": len(comps),
            "entries_in_clusters": sum(sizes),
            "max_cluster_size": sizes[0] if sizes else 0,
            "clusters_size_2_to_6": sum(1 for s in sizes if 2 <= s <= 6),
            "clusters_over_6": sum(1 for s in sizes if s > 6),
            "top_sizes": sizes[:10],
        })
    return {"queries_run": len(query_ids), "sweep": sweep}


def compute_m5_growth(redis: Any, *, window_days: int) -> dict[str, Any]:
    """Estimate net active growth + archived count over a trailing window from
    the Dream run ledger. Marks window_partial when ledger data is thin."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    # Run index is a list of run_ids; records live at dream:run:{run_id}.
    raw_index = redis.get("dream:runs:index")
    run_ids: list[str] = []
    if isinstance(raw_index, str):
        try:
            parsed = json.loads(raw_index)
            if isinstance(parsed, list):
                run_ids = [str(x) for x in parsed]
        except json.JSONDecodeError:
            run_ids = []
    elif isinstance(raw_index, list):
        run_ids = [str(x) for x in raw_index]

    archived_total = 0
    totals_in_window: list[tuple[str, int]] = []  # (run_at, total_entries)
    seen = 0
    for run_id in run_ids:
        raw = redis.get(f"dream:run:{run_id}")
        if not raw:
            continue
        try:
            rec = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        run_at = rec.get("run_at") or rec.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        seen += 1
        counts = rec.get("counts", {}) or {}
        archived_total += int(counts.get("archived", 0) or 0)
        if "total_entries" in counts:
            totals_in_window.append((str(run_at), int(counts["total_entries"])))

    totals_in_window.sort(key=lambda x: x[0])
    net_delta: Optional[int] = None
    if len(totals_in_window) >= 2:
        net_delta = totals_in_window[-1][1] - totals_in_window[0][1]

    return {
        "window_days": window_days,
        "net_active_delta": net_delta,
        "intake": None,  # not directly tracked in ledger; see window_partial
        "archived": archived_total,
        "runs_in_window": seen,
        "window_partial": net_delta is None or seen < 2,
    }


def compute_m6_recall(vector: Any, *, recall_k: int) -> dict[str, Any]:
    """Recall@k against the probe set. Approximates production search by
    embedding the query (text-embedding-3-large) and querying Upstash Vector
    directly — the same index and embedding model the live search uses."""
    from utils.embedding import get_embedding  # local import; needs OpenAI key

    probes = load_recall_probes()
    if not probes:
        return {"recall_at_5": None, "probes": [], "note": "no recall_probes.json"}

    results: list[dict[str, Any]] = []
    hits = 0
    for probe in probes:
        query = probe.get("query", "")
        expect = [str(x).lower() for x in probe.get("expect_any_of", [])]
        try:
            vec, _tokens = get_embedding(query)
            hitlist = vector.query(vector=vec, top_k=recall_k, include_metadata=True)
        except Exception as exc:  # noqa: BLE001
            results.append({"query": query, "hit": False, "error": str(exc), "returned_ids": []})
            continue
        returned_ids = [str(r["id"]) for r in hitlist]
        returned_domains = [
            str((r.get("metadata") or {}).get("domain", "")).lower() for r in hitlist
        ]
        hit = False
        for target in expect:
            if any(target == rid.lower() for rid in returned_ids):
                hit = True
                break
            if any(target in dom for dom in returned_domains):
                hit = True
                break
        if hit:
            hits += 1
        results.append({
            "query": query,
            "hit": hit,
            "returned_ids": returned_ids,
            "returned_domains": returned_domains,
            "expect_any_of": probe.get("expect_any_of", []),
        })

    recall = round(hits / len(probes), 4) if probes else None
    return {"recall_at_5": recall, "probes": results}


def compute_m7_access(active: list[Any], m6: dict[str, Any]) -> dict[str, Any]:
    total = max(1, len(active))
    with_access = 0
    for e in active:
        meta = entry_meta(e)
        ac = getattr(meta, "access_count", 0) or 0
        la = getattr(meta, "last_accessed", None)
        if ac > 0 and la:
            with_access += 1

    # Among entries the M6 probes returned, how many have access signals?
    returned_ids: set[str] = set()
    for p in m6.get("probes", []) or []:
        returned_ids.update(p.get("returned_ids", []) or [])
    by_id = {str(e.id): e for e in active}
    probe_with_access = 0
    probe_total = 0
    for rid in returned_ids:
        e = by_id.get(rid)
        if e is None:
            continue
        probe_total += 1
        meta = entry_meta(e)
        if (getattr(meta, "access_count", 0) or 0) > 0 and getattr(meta, "last_accessed", None):
            probe_with_access += 1

    return {
        "active_with_access_share": round(with_access / total, 4),
        "active_with_access_count": with_access,
        "probe_returned_with_access_share": round(probe_with_access / probe_total, 4) if probe_total else None,
        "probe_returned_total": probe_total,
    }


# ---------------------------------------------------------------------------
# Validation gate
# ---------------------------------------------------------------------------
def evaluate_gate(report: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, list[str]]:
    gate = policy.get("quality_gate", {})
    t_tier1 = float(gate.get("threshold_tier1", 0.40))
    t_dup = float(gate.get("threshold_dup", 0.20))
    t_recall = float(gate.get("threshold_recall", 0.60))

    issues: list[str] = []

    tier1_share = report["m1_tiers"]["tier_1_share"]
    if tier1_share > t_tier1:
        issues.append(f"M1 tier_1_share {tier1_share:.3f} > threshold {t_tier1}")

    active_total = max(1, report["active_counts"]["total"])
    dup_share = report["m4_duplicates"]["entries_in_clusters"] / active_total
    if not report["m4_duplicates"].get("skipped") and dup_share > t_dup:
        issues.append(f"M4 duplicate-entry share {dup_share:.3f} > threshold {t_dup}")

    recall = report["m6_recall"].get("recall_at_5")
    if recall is not None and recall < t_recall:
        issues.append(f"M6 recall@5 {recall:.3f} < threshold {t_recall}")

    return (len(issues) == 0), issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only memory-quality audit (PRD Phase 0).")
    parser.add_argument("--write-gate", action="store_true",
                        help="record verify_memory_quality in the validation ledger")
    parser.add_argument("--max-dup-queries", type=int, default=6000,
                        help="cap on M4 nearest-neighbour queries")
    parser.add_argument("--dup-workers", type=int, default=8)
    parser.add_argument("--skip-dup", action="store_true", help="skip M4 (no vector NN)")
    parser.add_argument("--skip-recall", action="store_true", help="skip M6 (no OpenAI embed)")
    parser.add_argument("--recall-k", type=int, default=5)
    parser.add_argument("--cosine-sweep", type=str, default=None,
                        help="comma-separated thresholds, e.g. '0.86,0.90,0.92,0.94,0.96' — runs a read-only sweep and exits")
    args = parser.parse_args()

    ensure_runtime_dirs()
    policy = load_policy()

    if args.cosine_sweep:
        dedup_cfg = policy.get("dedup", {})
        thresholds = [float(x) for x in args.cosine_sweep.split(",") if x.strip()]
        result = run_threshold_sweep(
            thresholds=thresholds,
            neighbor_k=int(dedup_cfg.get("DEDUP_NEIGHBOR_K", 10)),
            max_queries=args.max_dup_queries,
            workers=args.dup_workers,
        )
        print(f"[sweep] queries_run={result['queries_run']}")
        for row in result["sweep"]:
            print(f"  thr={row['threshold']:.2f}  clusters>=2={row['multi_member_clusters']:>4}  "
                  f"in_clusters={row['entries_in_clusters']:>5}  max={row['max_cluster_size']:>5}  "
                  f"tight(2-6)={row['clusters_size_2_to_6']:>4}  over6={row['clusters_over_6']:>4}  "
                  f"top={row['top_sizes'][:5]}")
        ts = utc_now_iso().replace(":", "").replace("-", "")
        path = append_report(f"dedup_threshold_sweep_{ts}.json", {"schema_version": 1, "generated_at": utc_now_iso(), **result})
        print(f"[sweep] report → {path}")
        return 0

    report = run_audit(
        max_dup_queries=args.max_dup_queries,
        dup_workers=args.dup_workers,
        skip_dup=args.skip_dup,
        skip_recall=args.skip_recall,
        recall_k=args.recall_k,
    )

    passed, issues = evaluate_gate(report, policy)
    report["gate"] = {
        "name": "verify_memory_quality",
        "passed": passed,
        "issues": issues,
        "thresholds": policy.get("quality_gate", {}),
    }

    ts = report["generated_at"].replace(":", "").replace("-", "")
    report_path = append_report(f"audit_memory_quality_{ts}.json", report)
    print(f"\n[audit] report → {report_path}")
    print(f"[audit] gate verify_memory_quality: {'PASS' if passed else 'FAIL'}")
    for issue in issues:
        print(f"        - {issue}")

    if args.write_gate:
        # The single, explicit write: the validation-ledger gate entry.
        redis = RedisClient()
        write_validation_gate(
            redis.client,
            ValidationGateRecord(
                gate="verify_memory_quality",
                passed=passed,
                issues=issues,
                report_path=str(report_path),
                details={
                    "tier_1_share": report["m1_tiers"]["tier_1_share"],
                    "tier_2_share": report["m1_tiers"]["tier_2_share"],
                    "max_single_salience_share": report["m3_salience"]["max_single_value_share"],
                    "duplicate_entries": report["m4_duplicates"]["entries_in_clusters"],
                    "multi_member_clusters": report["m4_duplicates"]["multi_member_clusters"],
                    "recall_at_5": report["m6_recall"].get("recall_at_5"),
                    "active_with_access_share": report["m7_access"]["active_with_access_share"],
                },
            ),
        )
        print("[audit] validation ledger updated (verify_memory_quality).")

    # Exit non-zero when the gate fails AND we were asked to enforce it.
    if args.write_gate and not passed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
