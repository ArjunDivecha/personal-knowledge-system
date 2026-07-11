"""
=============================================================================
SCRIPT NAME: test_admission_router.py
=============================================================================

INPUT FILES: None. All storage/vector interactions go through in-memory fake
doubles (FakeStorage); no network, no real Redis/Vector/OpenAI credentials.
One test reads the real, checked-in
/Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
to confirm the default policy loaded from disk is dry_run=true.

OUTPUT FILES: None.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Covers the invariants of contract PKS-ADMISSION-DEDUP-001
(contracts/admission-dedup.spec.md):

INV1 - evidence is conserved on append: the Evidence dict persisted onto the
       neighbor's new key_insight equals, field-for-field, the candidate's
       own evidence (conversation_id, message_ids, snippet, asserted_by,
       assertion_kind) — nothing dropped, nothing rewritten.
INV2 - append targets are only active, non-archived, same-type entries.
       Real enforcement happens server-side via the Upstash Vector filter
       string constructed in StorageClient.query_top_neighbor(); this suite
       proves that string is correct (mocking self.vector.query) and
       separately documents, via a route()-level test, that AdmissionRouter
       trusts its storage's filtering rather than re-checking state/archived
       itself (the filter is the single enforcement point by design).
INV3 - dry-run mode performs zero writes while the router's decision log
       (AdmissionRouter.decisions) contains the full decision; the config
       default is off (enabled=false, dry_run=true).
INV4 - below-threshold / disabled admission is byte-identical to current
       behavior: with admission_dedup.enabled=False,
       StorageClient.save_knowledge_entry_with_dedup() calls
       save_knowledge_entry() with identical arguments to a direct call.
INV5 - thresholds live in the injected policy dict, not hardcoded: a policy
       override changes routing without any code change.

DEPENDENCIES: Python 3.14 stdlib unittest + unittest.mock only.
USAGE:
  python -m unittest tests.python.test_admission_router -v
=============================================================================
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
INGESTION_DIR = REPO_ROOT / "ingestion"
POLICY_PATH = REPO_ROOT / "shared" / "memory_policy.json"

if str(INGESTION_DIR) not in sys.path:
    sys.path.insert(0, str(INGESTION_DIR))

from core import admission_router as admission_router_module  # noqa: E402
from core.admission_router import AdmissionDecision, AdmissionRouter  # noqa: E402
from core.storage import StorageClient  # noqa: E402


DEFAULT_POLICY = {
    "enabled": True,
    "dry_run": True,
    "append_threshold": 0.85,
    "link_threshold": 0.70,
}


class FakeStorage:
    """Duck-typed stand-in for StorageClient exposing exactly the surface
    AdmissionRouter and save_knowledge_entry_with_dedup need. No network."""

    def __init__(self, neighbor: dict | None = None, embedding: list[float] | None = None):
        self.neighbor = neighbor
        self.embedding = embedding or [0.1, 0.2, 0.3]
        self.entries: dict[str, dict] = {}
        self.embed_calls: list[str] = []
        self.query_calls: list[tuple[list[float], str, str | None]] = []
        self.get_calls: list[str] = []
        self.save_calls: list[tuple[dict, str | None]] = []
        self.batch_save_calls: list[tuple[list[dict], list[str] | None]] = []

    def generate_embedding(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return self.embedding

    def query_top_neighbor(self, embedding: list[float], entry_type: str = "knowledge", exclude_id: str | None = None):
        self.query_calls.append((embedding, entry_type, exclude_id))
        # Mirrors the real StorageClient.query_top_neighbor's self-exclusion:
        # a configured neighbor whose id matches exclude_id is filtered out,
        # simulating a candidate that already exists in the vector index from
        # a prior partial run (PKS-ADMISSION-DEDUP-001 self-match regression).
        if self.neighbor is not None and exclude_id is not None and self.neighbor.get("id") == exclude_id:
            return None
        return self.neighbor

    def get_knowledge_entry(self, entry_id: str):
        self.get_calls.append(entry_id)
        return self.entries.get(entry_id)

    def save_knowledge_entry(self, entry: dict, embedding_text: str = None):
        self.save_calls.append((entry, embedding_text))
        self.entries[entry["id"]] = entry

    def save_knowledge_entries_batch(self, entries: list[dict], embedding_texts: list[str] = None):
        self.batch_save_calls.append((entries, embedding_texts))
        for entry in entries:
            self.entries[entry["id"]] = entry


def _candidate(entry_id: str = "ke_candidate", domain: str = "Python") -> dict:
    return {"id": entry_id, "domain": domain, "current_view": f"knows {domain}", "metadata": {}}


def _neighbor(entry_id: str, score: float, **metadata_overrides) -> dict:
    metadata = {"mention_count": 1, "source_conversations": []}
    metadata.update(metadata_overrides)
    return {
        "id": entry_id,
        "domain": "Python",
        "key_insights": [],
        "metadata": metadata,
        "_similarity_score": score,
    }


class CosineBandRoutingTests(unittest.TestCase):
    """Append / link / new banding against policy thresholds."""

    def test_score_above_append_threshold_routes_append(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.90))
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision.decision, "append")
        self.assertEqual(decision.neighbor_id, "ke_n")
        self.assertEqual(decision.neighbor_score, 0.90)

    def test_score_in_mid_band_routes_link(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.75))
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision.decision, "link")
        self.assertEqual(decision.neighbor_id, "ke_n")
        self.assertEqual(decision.neighbor_score, 0.75)

    def test_score_below_link_threshold_routes_new_and_drops_neighbor(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.50))
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision.decision, "new")
        self.assertIsNone(decision.neighbor_id, "below-threshold neighbor must not be reported")
        self.assertIsNone(decision.neighbor_score)

    def test_route_passes_the_candidates_own_id_as_exclude_id(self):
        # Regression: route() must thread the candidate's own id through so
        # storage can filter out a self-match (see QueryTopNeighborFilterTests
        # for the storage-level behavior this enables).
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.90))
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        router.route(_candidate(entry_id="ke_candidate_123"), "text")
        self.assertEqual(len(storage.query_calls), 1)
        _embedding, _entry_type, exclude_id = storage.query_calls[0]
        self.assertEqual(exclude_id, "ke_candidate_123")

    def test_no_neighbor_at_all_routes_new(self):
        storage = FakeStorage(neighbor=None)
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision.decision, "new")
        self.assertIsNone(decision.neighbor_id)

    def test_route_never_writes_regardless_of_dry_run(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.95))
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "dry_run": False})
        router.route(_candidate(), "text")
        self.assertEqual(storage.save_calls, [])


class DisabledPolicyTests(unittest.TestCase):
    """When admission_dedup.enabled is False, route() is a cheap no-op."""

    def test_disabled_policy_returns_new_without_embedding_or_query(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.99))
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "enabled": False})
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision, AdmissionDecision("new", "Python", None, None, "admission_dedup disabled"))
        self.assertEqual(storage.embed_calls, [], "disabled router must not embed")
        self.assertEqual(storage.query_calls, [], "disabled router must not query")


class PolicyOverrideNotHardcodedTests(unittest.TestCase):
    """INV5: thresholds come from the injected policy dict."""

    def test_strict_append_threshold_override_prevents_default_append(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.90))  # would append at default 0.85
        strict_policy = {**DEFAULT_POLICY, "append_threshold": 0.99}
        router = AdmissionRouter(storage, strict_policy)
        decision = router.route(_candidate(), "text")
        self.assertNotEqual(decision.decision, "append")
        self.assertIn(decision.decision, ("link", "new"))

    def test_permissive_link_threshold_override_widens_the_link_band(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.30))  # "new" at default 0.70
        permissive_policy = {**DEFAULT_POLICY, "link_threshold": 0.10}
        router = AdmissionRouter(storage, permissive_policy)
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision.decision, "link")


class DecisionLogTests(unittest.TestCase):
    """INV3 (decision-log half): every route() call is recorded."""

    def test_decisions_accumulate_across_multiple_routes(self):
        storage = FakeStorage(neighbor=_neighbor("ke_n", 0.90))
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        router.route(_candidate("ke_a"), "text a")
        router.route(_candidate("ke_b"), "text b")
        self.assertEqual(len(router.decisions), 2)
        self.assertEqual([d["candidate_id"] for d in router.decisions], ["ke_a", "ke_b"])
        for record in router.decisions:
            self.assertIn("decision", record)
            self.assertIn("neighbor_id", record)
            self.assertIn("neighbor_score", record)
            self.assertIn("reason", record)
            self.assertIn("timestamp", record)


class RouteTrustsFilteringByDesignTests(unittest.TestCase):
    """INV2 (router half): route() trusts query_top_neighbor's filtering
    rather than re-checking state/archived itself. The actual enforcement —
    the Upstash filter string excluding contested/archived entries — is
    proven server-side-construction-wise in QueryTopNeighborFilterTests
    below. This test documents the design decision: if a misbehaving storage
    ever returned a disqualified neighbor, route() would still act on it,
    because filtering is not route()'s job."""

    def test_route_acts_on_whatever_neighbor_storage_returns(self):
        disqualified_neighbor = _neighbor("ke_contested", 0.95, archived=True)
        disqualified_neighbor["state"] = "contested"
        storage = FakeStorage(neighbor=disqualified_neighbor)
        router = AdmissionRouter(storage, DEFAULT_POLICY)
        decision = router.route(_candidate(), "text")
        self.assertEqual(decision.decision, "append")
        self.assertEqual(decision.neighbor_id, "ke_contested")


class QueryTopNeighborFilterTests(unittest.TestCase):
    """INV2 (storage half): StorageClient.query_top_neighbor constructs the
    exact filter string that excludes non-active/archived/other-type
    entries, and returns None cleanly when nothing matches."""

    class _FakeQueryResult:
        def __init__(self, id_: str, score: float):
            self.id = id_
            self.score = score

    class _FakeVector:
        def __init__(self, results):
            self._results = results
            self.query_kwargs = None

        def query(self, **kwargs):
            self.query_kwargs = kwargs
            return self._results

    class _FakeRedis:
        def __init__(self, values: dict[str, str]):
            self._values = values

        def get(self, key: str):
            return self._values.get(key)

    def _client(self, vector_results, redis_values):
        client = object.__new__(StorageClient)
        client.vector = self._FakeVector(vector_results)
        client.redis = self._FakeRedis(redis_values)
        return client

    def test_filter_string_excludes_non_active_archived_or_other_type(self):
        client = self._client(
            [self._FakeQueryResult("ke_x", 0.87)],
            {"knowledge:ke_x": json.dumps({"id": "ke_x", "domain": "d"})},
        )
        result = client.query_top_neighbor([0.1, 0.2, 0.3], entry_type="knowledge")
        self.assertEqual(
            client.vector.query_kwargs["filter"],
            "type = 'knowledge' AND state = 'active' AND archived = false",
        )
        self.assertEqual(result["_similarity_score"], 0.87)
        self.assertEqual(result["id"], "ke_x")

    def test_returns_none_when_vector_query_is_empty(self):
        client = self._client([], {})
        result = client.query_top_neighbor([0.1, 0.2, 0.3])
        self.assertIsNone(result)

    def test_returns_none_when_top_match_entry_is_missing_from_redis(self):
        client = self._client([self._FakeQueryResult("ke_ghost", 0.99)], {})
        result = client.query_top_neighbor([0.1, 0.2, 0.3])
        self.assertIsNone(result)

    def test_exclude_id_filters_out_a_self_match_and_falls_through_to_the_real_second_neighbor(self):
        # Regression: adversarial review 2026-07-11 — a candidate already
        # present in the vector index (e.g. a retried ingestion run
        # re-processing a candidate whose id was minted and saved on a prior
        # partial attempt) could self-match at top-1 (cosine ~1.0) and be
        # routed as an append-to-itself. exclude_id must filter that out and
        # still surface a genuine second neighbor when one exists.
        client = self._client(
            [self._FakeQueryResult("ke_self", 0.999), self._FakeQueryResult("ke_real_neighbor", 0.88)],
            {
                "knowledge:ke_self": json.dumps({"id": "ke_self", "domain": "d"}),
                "knowledge:ke_real_neighbor": json.dumps({"id": "ke_real_neighbor", "domain": "d"}),
            },
        )
        result = client.query_top_neighbor([0.1, 0.2, 0.3], entry_type="knowledge", exclude_id="ke_self")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "ke_real_neighbor")
        self.assertEqual(result["_similarity_score"], 0.88)
        # top_k widens to 2 specifically so a real second neighbor survives
        # the self-match being filtered — assert that widening happened.
        self.assertEqual(client.vector.query_kwargs["top_k"], 2)

    def test_exclude_id_with_no_other_neighbor_returns_none_not_the_self_match(self):
        client = self._client(
            [self._FakeQueryResult("ke_self", 0.999)],
            {"knowledge:ke_self": json.dumps({"id": "ke_self", "domain": "d"})},
        )
        result = client.query_top_neighbor([0.1, 0.2, 0.3], entry_type="knowledge", exclude_id="ke_self")
        self.assertIsNone(result)

    def test_no_exclude_id_requests_top_k_one_unchanged(self):
        client = self._client(
            [self._FakeQueryResult("ke_x", 0.87)],
            {"knowledge:ke_x": json.dumps({"id": "ke_x", "domain": "d"})},
        )
        client.query_top_neighbor([0.1, 0.2, 0.3], entry_type="knowledge")
        self.assertEqual(client.vector.query_kwargs["top_k"], 1)


class AppendEvidenceConservationTests(unittest.TestCase):
    """INV1: evidence appended to the neighbor is exactly the candidate's
    own evidence — nothing dropped, nothing rewritten."""

    def _apply_append(self, evidence: dict):
        candidate = _candidate("ke_candidate")
        candidate["key_insights"] = [{"insight": "uses Python heavily", "evidence": evidence}]
        neighbor_stub = _neighbor("ke_neighbor", 0.91)
        storage = FakeStorage(neighbor=neighbor_stub)
        storage.entries["ke_neighbor"] = {
            "id": "ke_neighbor",
            "domain": "Python",
            "key_insights": [],
            "metadata": {"mention_count": 2, "source_conversations": ["conv_old"]},
        }
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "dry_run": False})
        decision = router.route(candidate, "embedding text")
        result = router.apply_decision(decision, candidate, evidence)
        return storage, result

    def test_full_evidence_including_provenance_fields_is_conserved(self):
        evidence = {
            "conversation_id": "conv_new",
            "message_ids": ["m1", "m2"],
            "snippet": "Arjun said he uses Python for everything",
            "asserted_by": "arjun",
            "assertion_kind": "correction",
        }
        storage, result = self._apply_append(evidence)
        self.assertEqual(result, {"action": "appended", "entry_id": "ke_neighbor"})
        saved_neighbor = storage.entries["ke_neighbor"]
        persisted_evidence = saved_neighbor["key_insights"][-1]["evidence"]
        self.assertEqual(persisted_evidence, evidence, "evidence must be conserved field-for-field")

    def test_evidence_without_optional_provenance_fields_is_conserved_as_is(self):
        evidence = {
            "conversation_id": "conv_new",
            "message_ids": ["m1"],
            "snippet": "some snippet",
        }
        storage, result = self._apply_append(evidence)
        saved_neighbor = storage.entries["ke_neighbor"]
        persisted_evidence = saved_neighbor["key_insights"][-1]["evidence"]
        self.assertEqual(persisted_evidence, evidence)
        self.assertNotIn("asserted_by", persisted_evidence, "must not invent a field that wasn't present")
        self.assertNotIn("assertion_kind", persisted_evidence, "must not invent a field that wasn't present")

    def test_candidate_never_gets_its_own_new_entry_on_append(self):
        evidence = {"conversation_id": "conv_new", "message_ids": ["m1"], "snippet": "s"}
        storage, _ = self._apply_append(evidence)
        self.assertNotIn("ke_candidate", storage.entries)


class AppendMetadataMathTests(unittest.TestCase):
    """mention_count/last_seen/source_conversations bookkeeping on append."""

    def _apply(self, evidence, existing_metadata):
        candidate = _candidate("ke_candidate")
        candidate["key_insights"] = [{"insight": "x", "evidence": evidence}]
        storage = FakeStorage(neighbor=_neighbor("ke_neighbor", 0.92))
        storage.entries["ke_neighbor"] = {
            "id": "ke_neighbor",
            "domain": "Python",
            "key_insights": [],
            "metadata": dict(existing_metadata),
        }
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "dry_run": False})
        decision = router.route(candidate, "text")
        router.apply_decision(decision, candidate, evidence)
        return storage.entries["ke_neighbor"]

    def test_mention_count_increments_by_exactly_one(self):
        evidence = {"conversation_id": "conv_new", "message_ids": [], "snippet": ""}
        saved = self._apply(evidence, {"mention_count": 5, "source_conversations": []})
        self.assertEqual(saved["metadata"]["mention_count"], 6)

    def test_mention_count_defaults_to_one_when_absent(self):
        evidence = {"conversation_id": "conv_new", "message_ids": [], "snippet": ""}
        saved = self._apply(evidence, {"source_conversations": []})
        self.assertEqual(saved["metadata"]["mention_count"], 1)

    def test_last_seen_is_refreshed(self):
        evidence = {"conversation_id": "conv_new", "message_ids": [], "snippet": ""}
        saved = self._apply(evidence, {"mention_count": 1, "source_conversations": [], "last_seen": "2020-01-01T00:00:00"})
        self.assertNotEqual(saved["metadata"]["last_seen"], "2020-01-01T00:00:00")

    def test_source_conversations_gains_new_conversation_id(self):
        evidence = {"conversation_id": "conv_new", "message_ids": [], "snippet": ""}
        saved = self._apply(evidence, {"mention_count": 1, "source_conversations": ["conv_old"]})
        self.assertEqual(saved["metadata"]["source_conversations"], ["conv_old", "conv_new"])

    def test_source_conversations_does_not_duplicate_existing_id(self):
        evidence = {"conversation_id": "conv_old", "message_ids": [], "snippet": ""}
        saved = self._apply(evidence, {"mention_count": 1, "source_conversations": ["conv_old"]})
        self.assertEqual(saved["metadata"]["source_conversations"], ["conv_old"])


class LinkAppliesRelatedKnowledgeTests(unittest.TestCase):
    def test_link_decision_adds_related_knowledge_and_saves_candidate_as_new_entry(self):
        candidate = _candidate("ke_candidate")
        storage = FakeStorage(neighbor=_neighbor("ke_neighbor", 0.75))
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "dry_run": False})
        decision = router.route(candidate, "text")
        result = router.apply_decision(decision, candidate, {"conversation_id": "c", "message_ids": [], "snippet": ""})
        self.assertEqual(result, {"action": "linked_new", "entry_id": "ke_candidate"})
        saved_candidate = storage.entries["ke_candidate"]
        self.assertEqual(saved_candidate["related_knowledge"], [{"knowledge_id": "ke_neighbor", "relationship": "related"}])

    def test_link_does_not_duplicate_an_already_present_link(self):
        candidate = _candidate("ke_candidate")
        candidate["related_knowledge"] = [{"knowledge_id": "ke_neighbor", "relationship": "related"}]
        storage = FakeStorage(neighbor=_neighbor("ke_neighbor", 0.75))
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "dry_run": False})
        decision = router.route(candidate, "text")
        router.apply_decision(decision, candidate, {"conversation_id": "c", "message_ids": [], "snippet": ""})
        saved_candidate = storage.entries["ke_candidate"]
        self.assertEqual(len(saved_candidate["related_knowledge"]), 1)


class NewDecisionUnchangedTests(unittest.TestCase):
    def test_new_decision_saves_candidate_unchanged(self):
        candidate = _candidate("ke_candidate")
        storage = FakeStorage(neighbor=None)
        router = AdmissionRouter(storage, {**DEFAULT_POLICY, "dry_run": False})
        decision = router.route(candidate, "text")
        result = router.apply_decision(decision, candidate, {})
        self.assertEqual(result, {"action": "new", "entry_id": "ke_candidate"})
        self.assertEqual(storage.entries["ke_candidate"], candidate)


class SaveKnowledgeEntryWithDedupTests(unittest.TestCase):
    """StorageClient.save_knowledge_entry_with_dedup, exercised as an
    unbound method against FakeStorage (no real StorageClient instance is
    ever constructed — no credentials required)."""

    def test_disabled_policy_calls_save_knowledge_entry_with_identical_args(self):
        # INV4: byte-identical below the feature toggle.
        entry = _candidate("ke_candidate")
        with mock.patch.object(
            admission_router_module,
            "load_admission_dedup_policy",
            return_value={**DEFAULT_POLICY, "enabled": False},
        ):
            fake_via_dedup = FakeStorage()
            StorageClient.save_knowledge_entry_with_dedup(fake_via_dedup, entry)

        fake_direct = FakeStorage()
        fake_direct.save_knowledge_entry(entry)

        self.assertEqual(fake_via_dedup.save_calls, fake_direct.save_calls)
        self.assertEqual(fake_via_dedup.embed_calls, [], "disabled path must not embed for routing")
        self.assertEqual(fake_via_dedup.query_calls, [], "disabled path must not query for routing")

    def test_dry_run_true_performs_zero_writes(self):
        # INV3: dry-run is write-free.
        entry = _candidate("ke_candidate")
        with mock.patch.object(
            admission_router_module,
            "load_admission_dedup_policy",
            return_value={**DEFAULT_POLICY, "enabled": True, "dry_run": True},
        ):
            fake = FakeStorage(neighbor=_neighbor("ke_neighbor", 0.95))
            result = StorageClient.save_knowledge_entry_with_dedup(fake, entry)

        self.assertEqual(fake.save_calls, [])
        self.assertEqual(fake.batch_save_calls, [])
        self.assertEqual(result["action"], "dry_run")
        self.assertEqual(result["decision"], "append")

    def test_live_mode_applies_the_decision(self):
        entry = _candidate("ke_candidate")
        with mock.patch.object(
            admission_router_module,
            "load_admission_dedup_policy",
            return_value={**DEFAULT_POLICY, "enabled": True, "dry_run": False},
        ):
            fake = FakeStorage(neighbor=None)  # forces "new"
            result = StorageClient.save_knowledge_entry_with_dedup(fake, entry)

        self.assertEqual(result, {"action": "new", "entry_id": "ke_candidate"})
        self.assertEqual(fake.entries["ke_candidate"], entry)


class CheckedInPolicyDefaultsTests(unittest.TestCase):
    """The real, on-disk shared/memory_policy.json defaults must be safe."""

    def test_real_policy_file_defaults_off(self):
        with POLICY_PATH.open() as handle:
            full_policy = json.load(handle)
        block = full_policy["admission_dedup"]
        self.assertIs(block["enabled"], False)
        self.assertIs(block["dry_run"], True)


if __name__ == "__main__":
    unittest.main()
