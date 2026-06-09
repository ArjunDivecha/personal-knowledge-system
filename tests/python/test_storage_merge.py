"""
Tests for ingestion/core/storage.py — _merge_knowledge_entry_data and sticky
Dream lifecycle flags (PRD item 1.3).

These tests exercise the merge function directly without touching Redis/Vector.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

# Import only the StorageClient class; don't instantiate (requires live creds).
from core.storage import StorageClient


def _make_client() -> StorageClient:
    """Return a StorageClient whose network clients are None — safe for unit tests."""
    obj = object.__new__(StorageClient)
    obj.redis = None
    obj.vector = None
    obj.openai = None
    return obj


def _entry(domain: str, view: str, **meta_overrides) -> dict:
    """Minimal valid knowledge entry with optional metadata overrides."""
    meta = {
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "archived": False,
        "injection_tier": None,
        "injection_quarantine": None,
        "salience_score": None,
        "last_consolidated": None,
    }
    meta.update(meta_overrides)
    return {"id": "ke_test", "domain": domain, "current_view": view, "metadata": meta}


class StickyArchivedTests(unittest.TestCase):
    """
    1.3 — A re-ingested source must never un-archive an entry Dream archived.

    The `archived` flag in _normalize_knowledge_metadata defaults to False, so
    a fresh ingest payload always has archived=False.  Without the sticky guard
    the merge's {**existing_meta, **incoming_meta} spread silently flips
    archived:True → False and recreates the vector, defeating forgetting in a loop.
    """

    def setUp(self):
        self.sc = _make_client()

    def test_archived_true_survives_reingest(self):
        """archived=True on an existing entry is preserved after a re-ingest."""
        existing = _entry("Python", "knows Python", archived=True)
        incoming = _entry("Python", "still knows Python")  # archived=False (default)

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        self.assertTrue(merged["metadata"]["archived"],
                        "archived=True must survive re-ingest")

    def test_archived_false_is_not_forced_true(self):
        """If the existing entry is not archived, re-ingest must not change that."""
        existing = _entry("Python", "knows Python", archived=False)
        incoming = _entry("Python", "still knows Python")

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        self.assertFalse(merged["metadata"]["archived"])

    def test_injection_tier_sticky(self):
        """Dream-set injection_tier survives re-ingest."""
        existing = _entry("ML", "knows ML", injection_tier=2)
        incoming = _entry("ML", "knows ML too")  # injection_tier=None by default

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        self.assertEqual(merged["metadata"]["injection_tier"], 2)

    def test_salience_score_sticky(self):
        """Dream-computed salience_score is not overwritten to None on re-ingest."""
        existing = _entry("ML", "knows ML", salience_score=0.87)
        incoming = _entry("ML", "knows ML too")

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        self.assertAlmostEqual(merged["metadata"]["salience_score"], 0.87)

    def test_last_consolidated_sticky(self):
        """last_consolidated timestamp from Dream is preserved."""
        ts = "2026-05-01T03:00:00"
        existing = _entry("ML", "knows ML", last_consolidated=ts)
        incoming = _entry("ML", "knows ML too")

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        self.assertEqual(merged["metadata"]["last_consolidated"], ts)

    def test_injection_quarantine_sticky(self):
        """Dream quarantine flag is preserved."""
        existing = _entry("noise", "some noise", injection_quarantine=True)
        incoming = _entry("noise", "some noise again")

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        self.assertTrue(merged["metadata"]["injection_quarantine"])

    def test_incoming_archived_true_is_accepted_when_existing_false(self):
        """Incoming archived=True (e.g. from an explicit archive op) is accepted."""
        existing = _entry("topic", "view A", archived=False)
        incoming = _entry("topic", "view B", archived=True)

        merged = self.sc._merge_knowledge_entry_data(existing, incoming)
        # Incoming archived=True must win; the guard only protects existing=True
        # from being overwritten by incoming=False, not the other way around.
        self.assertTrue(merged["metadata"]["archived"])

    def test_new_entry_archived_false_not_forced(self):
        """A brand-new entry (no existing) passes through unchanged."""
        incoming = _entry("topic", "view", archived=False)
        merged = self.sc._merge_knowledge_entry_data(None, incoming)
        self.assertFalse(merged["metadata"]["archived"])


if __name__ == "__main__":
    unittest.main()
