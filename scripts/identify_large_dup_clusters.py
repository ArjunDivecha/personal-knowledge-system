#!/usr/bin/env python3
"""
=============================================================================
SCRIPT NAME: identify_large_dup_clusters.py
=============================================================================

DESCRIPTION:
Find the oversized near-duplicate clusters the Dream semantic-dedup size guard
splits to lexical (>SEMANTIC_MAX_CLUSTER_SIZE members at cosine >=
COSINE_DUP_THRESHOLD), and emit a safe merge plan for each.

Clusters are formed by union-find over vector NN edges >= threshold — which is
TRANSITIVE (A~B, B~C chains A,B,C even if A~C is low). Merging a whole
transitive chain into one canonical would over-merge. So for each cluster we:
  1. pick a canonical = highest salience, then most source_conversations,
     then most recently updated;
  2. compute every member's DIRECT cosine to the canonical's vector;
  3. mark as merge_ids only members with direct cosine >= --merge-cosine to the
     canonical (genuine near-dups); the rest are hold_ids (kept, for review).

OUTPUT:
- scripts/reports/large_dup_clusters_<ts>.json : per-cluster merge plan with
  member ids, revisions (metadata.revision), salience, direct cosine, and the
  suggested keep_id / merge_ids / hold_ids. No mutations — read-only.

USAGE:
  python scripts/identify_large_dup_clusters.py            # >=7-member clusters
  python scripts/identify_large_dup_clusters.py --min-size 5 --merge-cosine 0.95
=============================================================================
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

DISTILLATION_ROOT = Path(__file__).resolve().parent.parent / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from _memory_migration import append_report, ensure_runtime_dirs, utc_now_iso  # noqa: E402

from storage.redis_client import RedisClient  # noqa: E402
from storage.vector_client import VectorClient  # noqa: E402


def _is_archived(entry) -> bool:
    md = getattr(entry, "metadata", None)
    return bool(getattr(md, "archived", False))


def _meta_attr(entry, name, default=None):
    md = getattr(entry, "metadata", None)
    return getattr(md, name, default) if md is not None else default


class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def main() -> int:
    ap = argparse.ArgumentParser(description="Identify oversized near-dup clusters + safe merge plan")
    ap.add_argument("--min-size", type=int, default=7, help="min cluster size to report (oversized)")
    ap.add_argument("--cluster-cosine", type=float, default=0.95, help="edge threshold for union-find clustering")
    ap.add_argument("--merge-cosine", type=float, default=0.95, help="direct-to-canonical cosine required to merge")
    ap.add_argument("--neighbor-k", type=int, default=12)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    ensure_runtime_dirs()
    redis = RedisClient()
    vector = VectorClient()

    entries = [e for e in redis.get_all_knowledge_entries() if not _is_archived(e)]
    entries += [e for e in redis.get_all_project_entries() if not _is_archived(e)]
    by_id = {e.id: e for e in entries}
    ids = list(by_id.keys())
    print(f"[clusters] active entries: {len(ids)}", flush=True)

    fetched = vector.fetch_entries(ids, include_metadata=False, include_vectors=True, batch_size=200)
    vec_by_id: dict[str, list[float]] = {}
    for row in fetched:
        if row is not None and getattr(row, "vectors", None) is not None:
            vec_by_id[str(row.id)] = row.vectors
        elif row is not None and getattr(row, "vector", None) is not None:
            vec_by_id[str(row.id)] = row.vector
    print(f"[clusters] vectors fetched: {len(vec_by_id)}", flush=True)

    uf = UnionFind()
    pair_cos: dict[tuple[str, str], float] = {}

    def nn(eid):
        vec = vec_by_id.get(eid)
        if vec is None:
            return eid, []
        res = vector.index.query(vector=vec, top_k=args.neighbor_k + 1, include_metadata=False)
        out = []
        for hit in res:
            hid = str(hit.id)
            if hid != eid:
                out.append((hid, float(hit.score)))
        return eid, out

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(nn, eid) for eid in vec_by_id]
        done = 0
        for fut in as_completed(futs):
            eid, neighbors = fut.result()
            for hid, score in neighbors:
                if hid in vec_by_id and score >= args.cluster_cosine:
                    key = (eid, hid) if eid < hid else (hid, eid)
                    pair_cos[key] = max(pair_cos.get(key, 0.0), score)
                    uf.union(eid, hid)
            done += 1
            if done % 500 == 0:
                print(f"[clusters] NN {done}/{len(vec_by_id)}", flush=True)

    clusters: dict[str, list[str]] = {}
    for eid in vec_by_id:
        if eid in uf.parent:
            clusters.setdefault(uf.find(eid), []).append(eid)
    big = {root: mem for root, mem in clusters.items() if len(mem) >= args.min_size}
    print(f"[clusters] clusters >= {args.min_size}: {len(big)}", flush=True)

    def salience(eid):
        return float(_meta_attr(by_id[eid], "salience_score", 0.0) or 0.0)

    def sources(eid):
        sc = _meta_attr(by_id[eid], "source_conversations", []) or []
        return len(sc)

    def updated(eid):
        return _meta_attr(by_id[eid], "updated_at", "") or getattr(by_id[eid], "state", "") or ""

    def cosine(a, b):
        va, vb = vec_by_id.get(a), vec_by_id.get(b)
        if not va or not vb:
            return 0.0
        dot = sum(x * y for x, y in zip(va, vb))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(y * y for y in vb) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    plan = []
    for root, members in sorted(big.items(), key=lambda kv: -len(kv[1])):
        # canonical: highest salience, then most sources, then most recent
        canonical = sorted(members, key=lambda e: (salience(e), sources(e), updated(e)), reverse=True)[0]
        merge_ids, hold_ids, detail = [], [], []
        for m in members:
            if m == canonical:
                continue
            c = round(cosine(canonical, m), 4)
            row = {
                "id": m,
                "domain": getattr(by_id[m], "domain", None),
                "salience": round(salience(m), 4),
                "sources": sources(m),
                "revision": _meta_attr(by_id[m], "revision", 0),
                "cosine_to_canonical": c,
            }
            detail.append(row)
            (merge_ids if c >= args.merge_cosine else hold_ids).append(m)
        plan.append({
            "size": len(members),
            "keep_id": canonical,
            "keep_domain": getattr(by_id[canonical], "domain", None),
            "keep_salience": round(salience(canonical), 4),
            "keep_revision": _meta_attr(by_id[canonical], "revision", 0),
            "merge_ids": merge_ids,
            "hold_ids": hold_ids,
            "members_detail": sorted(detail, key=lambda r: -r["cosine_to_canonical"]),
        })

    report = {
        "generated_at": utc_now_iso(),
        "cluster_cosine": args.cluster_cosine,
        "merge_cosine": args.merge_cosine,
        "min_size": args.min_size,
        "cluster_count": len(plan),
        "total_merge_ids": sum(len(c["merge_ids"]) for c in plan),
        "total_hold_ids": sum(len(c["hold_ids"]) for c in plan),
        "clusters": plan,
    }
    path = append_report(f"large_dup_clusters_{utc_now_iso().replace(':','').replace('+00:00','Z')}.json", report)
    print("")
    for c in plan:
        print(f"  cluster size={c['size']:2d} keep={c['keep_id']} ({c['keep_domain']!r}) "
              f"merge={len(c['merge_ids'])} hold={len(c['hold_ids'])}", flush=True)
    print(f"\n[clusters] {len(plan)} clusters, merge={report['total_merge_ids']} hold={report['total_hold_ids']}")
    print(f"[clusters] report -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
