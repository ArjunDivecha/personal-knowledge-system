from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTILLATION_DIR = REPO_ROOT / "distillation"

if str(DISTILLATION_DIR) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_DIR))

from storage.redis_client import RedisClient


class _FakeRedis:
    def __init__(self) -> None:
        self.mget_calls: list[tuple[str, ...]] = []

    def scan(self, cursor: int | str, *, match: str, count: int):
        return 0, [f"knowledge:{index}" for index in range(3)]

    def mget(self, *keys: str):
        self.mget_calls.append(keys)
        return [
            json.dumps(
                {
                    "id": key.replace("knowledge:", "ke_"),
                    "type": "knowledge",
                    "domain": "Batching",
                    "current_view": "Batched Redis reads",
                    "metadata": {
                        "created_at": "2026-05-07T00:00:00+00:00",
                        "updated_at": "2026-05-07T00:00:00+00:00",
                        "source_conversations": [],
                        "source_messages": [],
                    },
                }
            )
            for key in keys
        ]


class RedisClientBatchingTests(unittest.TestCase):
    def test_get_all_knowledge_entries_uses_batched_mget(self) -> None:
        client = object.__new__(RedisClient)
        client.client = _FakeRedis()

        entries = client.get_all_knowledge_entries(batch_size=2)

        self.assertEqual(len(entries), 3)
        self.assertEqual(client.client.mget_calls, [
            ("knowledge:0", "knowledge:1"),
            ("knowledge:2",),
        ])


if __name__ == "__main__":
    unittest.main()
