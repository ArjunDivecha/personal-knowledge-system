#!/usr/bin/env python3
"""Offline, blockwise candidate planner; never writes Redis/Vector."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    den = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / den if den else 0.0


def plan_candidates(rows: list[dict[str, Any]], threshold: float, max_cluster_size: int, block_size: int = 512) -> list[list[str]]:
    parent = {str(row["id"]): str(row["id"]) for row in rows}
    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item
    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root
    for start in range(0, len(rows), block_size):
        for left in rows[start : start + block_size]:
            for right in rows[start:]:
                if left["id"] >= right["id"]:
                    continue
                if cosine(left["vector"], right["vector"]) >= threshold:
                    union(str(left["id"]), str(right["id"]))
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(find(str(row["id"])), []).append(str(row["id"]))
    return sorted(sorted(ids) for ids in groups.values() if 1 < len(ids) <= max_cluster_size)


def build_manifest(rows: list[dict[str, Any]], clusters: list[list[str]], *, threshold: float, algorithm: str = "blockwise-cosine-v1", query_capped: bool = False) -> dict[str, Any]:
    if query_capped:
        raise ValueError("capped_or_incomplete_audit_rejected")
    watermark = hashlib.sha256("|".join(sorted(str(row["id"]) for row in rows)).encode()).hexdigest()
    return {"schema_version": 1, "algorithm": algorithm, "threshold": threshold, "query_capped": False, "corpus_watermark": watermark, "clusters": clusters}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--threshold", type=float, default=0.95)
    parser.add_argument("--max-cluster-size", type=int, default=6)
    args = parser.parse_args()
    rows = json.loads(args.fixture.read_text())
    print(json.dumps(build_manifest(rows, plan_candidates(rows, args.threshold, args.max_cluster_size), threshold=args.threshold), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
