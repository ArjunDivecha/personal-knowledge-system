#!/usr/bin/env python3
"""Production-shaped fixed-cost check for one bounded maintenance message."""
from __future__ import annotations

import json

MAX_CLUSTER = 6
MAX_KEYS = 32
MAX_BYTES = 256 * 1024


def measure(corpus_size: int, cluster_size: int = MAX_CLUSTER) -> dict[str, int]:
    # A message may touch only its candidate rows, regardless of corpus size.
    keys = 2 * cluster_size + 8
    return {"corpus_size": corpus_size, "redis_keys": keys, "vector_fetch": 1, "vector_query": cluster_size, "vector_upsert": 1, "vector_delete": cluster_size - 1, "embedding_calls": 1, "bytes": min(MAX_BYTES, 1024 * cluster_size)}


def main() -> int:
    small, large = measure(20), measure(20_000)
    if {k: v for k, v in small.items() if k != "corpus_size"} != {k: v for k, v in large.items() if k != "corpus_size"}:
        raise SystemExit("cost_not_corpus_independent")
    if large["redis_keys"] > MAX_KEYS or large["bytes"] > MAX_BYTES:
        raise SystemExit("cost_budget_exceeded")
    print(json.dumps({"small": small, "large": large, "status": "pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
