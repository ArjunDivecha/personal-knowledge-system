from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_DIR = REPO_ROOT / "distillation"

if str(DISTILLATION_DIR) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_DIR))

from storage.vector_client import VectorClient


class _FakeIndex:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def fetch(self, *, ids: list[str], include_metadata: bool, include_vectors: bool):
        self.calls.append(ids)
        return [f"row:{entry_id}" for entry_id in ids]


class VectorClientTests(unittest.TestCase):
    def test_fetch_entries_batches_upstash_read_limit(self) -> None:
        client = object.__new__(VectorClient)
        client.index = _FakeIndex()

        results = client.fetch_entries(
            [f"ke_{index}" for index in range(1001)],
            include_metadata=True,
            batch_size=1000,
        )

        self.assertEqual(len(results), 1001)
        self.assertEqual(len(client.index.calls), 2)
        self.assertEqual(len(client.index.calls[0]), 1000)
        self.assertEqual(len(client.index.calls[1]), 1)


if __name__ == "__main__":
    unittest.main()
