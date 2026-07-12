"""
=============================================================================
SCRIPT NAME: admission_router.py
=============================================================================

INPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/shared/memory_policy.json
  : read-only, for the `admission_dedup` policy block (enabled, dry_run,
  append_threshold, link_threshold) when no policy dict is injected.

OUTPUT FILES:
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/ingestion/logs/admission_decisions.jsonl
  : append-only decision log, one JSON line per routing decision, written by
  _record() on every route() call (so shadow/dry-run runs leave a durable,
  reviewable artifact even though storage.py constructs a fresh router per
  candidate and discards it). Path overridable via the
  PKS_ADMISSION_DECISION_LOG environment variable (tests point it at a
  tempdir). Gitignored via ingestion/logs/.gitignore.
- /Users/arjundivecha/Dropbox/AAA Backup/A Working/Memory/knowledge-system/scripts/reports/admission_dedup_decisions_<UTCSTAMP>.json
  : optional, only written when AdmissionRouter.write_report() is called
  explicitly (e.g. by a batch shadow-run entry point); never written
  automatically by route() or apply_decision().
- (network, production Redis + Vector, write — ONLY when apply_decision() is
  called by a caller in live, non-dry-run mode) updates or creates knowledge
  entries via the injected storage's save_knowledge_entry(). This module
  never talks to Redis/Vector directly; all writes go through the injected
  storage object, so tests can inject a fake and issue zero real network
  calls.

VERSION: 1.0
LAST UPDATED: 2026-07-10
AUTHOR: Claude (Sonnet 5, high effort) for Arjun Divecha

DESCRIPTION:
Implements the "admission dedup" routing decision from contract
PKS-ADMISSION-DEDUP-001 (contracts/admission-dedup.spec.md).

Background: ingestion currently mints a brand-new knowledge entry for every
extracted candidate, even when an active entry already holds the same
knowledge under a slightly different domain name. This module adds a
retrieve-before-admit step: given a not-yet-saved candidate entry and its
embedding text, find the single best-matching active, non-archived,
same-type neighbor and route the candidate as one of:
  - "append": cosine similarity >= append_threshold — the candidate is
    absorbed into the neighbor as a new key_insight; no new entry is minted.
  - "link": link_threshold <= cosine similarity < append_threshold — the
    candidate is admitted as a new entry but linked `related` to the
    neighbor.
  - "new": cosine similarity < link_threshold, or no qualifying neighbor
    exists — the candidate is admitted unchanged, exactly as today.

AdmissionRouter.route() is a pure decision function: it never writes to
storage, regardless of dry_run. Writes only ever happen inside
apply_decision(), which callers are expected to call only when the policy's
`dry_run` flag is False. This split is what makes dry-run mode structurally
write-free (INV3 of the contract) rather than write-free by convention.

Both the storage dependency and the policy dict are constructor-injected
(mirroring scripts/sweep_contested_fossils.py's FossilSweep idiom), so tests
never touch real OpenAI/Upstash credentials.

DEPENDENCIES: Python 3.14 stdlib only (dataclasses, json, uuid, pathlib,
datetime). No import from distillation/ (out of scope for this contract);
the memory_policy.json loading pattern is intentionally re-implemented here
rather than imported from distillation/utils/salience.py.

USAGE:
  from core.admission_router import AdmissionRouter, AdmissionDecision, load_admission_dedup_policy
  router = AdmissionRouter(storage)  # policy=None loads shared/memory_policy.json
  decision = router.route(candidate_entry, embedding_text)
  if not policy["dry_run"]:
      router.apply_decision(decision, candidate_entry, candidate_evidence)

NOTES:
- This module is never called directly by ingestion runners; they call
  StorageClient.save_knowledge_entry_with_dedup() (ingestion/core/storage.py),
  which owns the policy-gated wiring (enabled/dry_run branching) and
  constructs an AdmissionRouter internally when the feature is enabled.
- Automated tests (tests/python/test_admission_router.py) exercise this
  class against an injected FakeStorage double, never real Redis/Vector/OpenAI.
=============================================================================
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "shared" / "memory_policy.json"
REPORTS_DIR = REPO_ROOT / "scripts" / "reports"
DEFAULT_DECISION_LOG_PATH = REPO_ROOT / "ingestion" / "logs" / "admission_decisions.jsonl"


def decision_log_path() -> Path:
    """Resolve the durable decision-log path.

    PKS_ADMISSION_DECISION_LOG overrides the default (tests point it at a
    tempdir so unit runs never append to the real log; CI could point it
    into a run-artifacts dir)."""
    override = os.environ.get("PKS_ADMISSION_DECISION_LOG")
    return Path(override) if override else DEFAULT_DECISION_LOG_PATH

_ADMISSION_POLICY_CACHE: Optional[dict[str, Any]] = None


def load_admission_dedup_policy() -> dict[str, Any]:
    """Load the `admission_dedup` block from shared/memory_policy.json.

    Mirrors distillation/utils/salience.py's load_memory_policy() caching
    pattern (module-level cache, lazily populated on first call), but is
    re-implemented here rather than imported since this module may not
    depend on distillation/ (contract scope).
    """
    global _ADMISSION_POLICY_CACHE
    if _ADMISSION_POLICY_CACHE is None:
        with POLICY_PATH.open() as handle:
            full_policy = json.load(handle)
        _ADMISSION_POLICY_CACHE = full_policy["admission_dedup"]
    return _ADMISSION_POLICY_CACHE


@dataclass
class AdmissionDecision:
    """One routing verdict for a not-yet-saved candidate entry."""

    decision: str  # "append" | "link" | "new"
    candidate_domain: str
    neighbor_id: Optional[str]        # None when decision == "new"
    neighbor_score: Optional[float]   # cosine similarity to the chosen neighbor, None if none reported
    reason: str                       # short human-readable why


class AdmissionRouter:
    """Decides append/link/new for candidate knowledge entries before they
    are saved, and (when asked) applies that decision to storage.

    storage: anything exposing generate_embedding(text), query_top_neighbor
    (embedding, entry_type) -> dict|None, get_knowledge_entry(entry_id), and
    save_knowledge_entry(entry). Constructor-injected so tests never touch
    real Vector/Redis (same pattern as scripts/sweep_contested_fossils.py's
    FossilSweep).

    policy: the admission_dedup config dict; if None, loaded from
    shared/memory_policy.json via load_admission_dedup_policy().
    """

    def __init__(self, storage: Any, policy: Optional[dict[str, Any]] = None):
        self.storage = storage
        self.policy = policy if policy is not None else load_admission_dedup_policy()
        self.decisions: list[dict[str, Any]] = []

    def _record(self, decision: AdmissionDecision, candidate_entry: dict) -> None:
        record = asdict(decision)
        record["candidate_id"] = candidate_entry.get("id")
        record["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.decisions.append(record)
        # Durable decision log (contract scale bar: "G5 decision log reviewed
        # by Arjun"). The in-memory list alone is useless in production —
        # save_knowledge_entry_with_dedup constructs a fresh router per entry
        # and discards it, so without this append a shadow run leaves no
        # reviewable artifact. One JSON line per decision; a write failure
        # raises rather than passing silently, because in shadow mode the log
        # IS the deliverable.
        log_path = decision_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def route(self, candidate_entry: dict, embedding_text: str) -> AdmissionDecision:
        """Decide append/link/new for a not-yet-saved candidate. Pure
        decision — never writes, regardless of dry_run. Writes happen only
        in apply_decision()."""
        domain = candidate_entry.get("domain")

        if not self.policy.get("enabled"):
            decision = AdmissionDecision(
                decision="new",
                candidate_domain=domain,
                neighbor_id=None,
                neighbor_score=None,
                reason="admission_dedup disabled",
            )
            self._record(decision, candidate_entry)
            return decision

        embedding = self.storage.generate_embedding(embedding_text)
        neighbor = self.storage.query_top_neighbor(
            embedding, entry_type="knowledge", exclude_id=candidate_entry.get("id"),
        )

        if neighbor is None:
            decision = AdmissionDecision(
                decision="new",
                candidate_domain=domain,
                neighbor_id=None,
                neighbor_score=None,
                reason="no active same-type neighbor found",
            )
            self._record(decision, candidate_entry)
            return decision

        score = neighbor["_similarity_score"]
        append_threshold = self.policy["append_threshold"]
        link_threshold = self.policy["link_threshold"]

        if score >= append_threshold:
            decision = AdmissionDecision(
                decision="append",
                candidate_domain=domain,
                neighbor_id=neighbor["id"],
                neighbor_score=score,
                reason=f"cosine {score:.4f} >= append_threshold {append_threshold}",
            )
        elif score >= link_threshold:
            decision = AdmissionDecision(
                decision="link",
                candidate_domain=domain,
                neighbor_id=neighbor["id"],
                neighbor_score=score,
                reason=(
                    f"cosine {score:.4f} >= link_threshold {link_threshold} "
                    f"(< append_threshold {append_threshold})"
                ),
            )
        else:
            # Below-band: a neighbor existed but is irrelevant to the
            # candidate, so it is not reported in the decision.
            decision = AdmissionDecision(
                decision="new",
                candidate_domain=domain,
                neighbor_id=None,
                neighbor_score=None,
                reason=f"cosine {score:.4f} < link_threshold {link_threshold}",
            )

        self._record(decision, candidate_entry)
        return decision

    def apply_decision(
        self,
        decision: AdmissionDecision,
        candidate_entry: dict,
        candidate_evidence: dict,
        embedding_text: Optional[str] = None,
    ) -> dict:
        """Apply a routing decision to storage. Only meaningful to call when
        the policy is in live (non-dry_run) mode; callers are responsible
        for that gating.

        embedding_text: the same text route() already embedded once for the
        neighbor search. Threaded through to save_knowledge_entry for the
        "new"/"link" paths so storage doesn't silently re-derive (and
        re-embed via a second OpenAI call) the same text from scratch."""
        if decision.decision == "append":
            neighbor = self.storage.get_knowledge_entry(decision.neighbor_id)
            insight = {
                "insight": candidate_entry.get("current_view") or candidate_entry.get("domain"),
                "evidence": candidate_evidence,
            }
            neighbor.setdefault("key_insights", []).append(insight)

            metadata = neighbor.setdefault("metadata", {})
            metadata["mention_count"] = (metadata.get("mention_count") or 0) + 1
            metadata["last_seen"] = datetime.utcnow().isoformat()

            source_conversations = metadata.setdefault("source_conversations", [])
            conversation_id = candidate_evidence.get("conversation_id")
            if conversation_id and conversation_id not in source_conversations:
                source_conversations.append(conversation_id)

            self.storage.save_knowledge_entry(neighbor)
            return {"action": "appended", "entry_id": neighbor["id"]}

        if decision.decision == "link":
            links = candidate_entry.setdefault("related_knowledge", [])
            already_linked = any(
                link.get("knowledge_id") == decision.neighbor_id
                and link.get("relationship") == "related"
                for link in links
            )
            if not already_linked:
                links.append({"knowledge_id": decision.neighbor_id, "relationship": "related"})
            self.storage.save_knowledge_entry(candidate_entry, embedding_text)
            return {"action": "linked_new", "entry_id": candidate_entry["id"]}

        # "new": candidate gets a new entry unchanged.
        self.storage.save_knowledge_entry(candidate_entry, embedding_text)
        return {"action": "new", "entry_id": candidate_entry["id"]}

    def write_report(self, path: Optional[Path] = None) -> Path:
        """Write self.decisions as a JSON report, mirroring
        scripts/sweep_contested_fossils.py's report shape. Returns the path
        written."""
        if path is None:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S+0000")
            path = REPORTS_DIR / f"admission_dedup_decisions_{stamp}.json"

        report = {
            "run_id": f"admission_dedup_{uuid.uuid4().hex[:12]}",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "decisions": self.decisions,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=1))
        return path
