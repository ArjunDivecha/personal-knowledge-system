"""
=============================================================================
PACKAGE: orchestrator
=============================================================================

PKS nightly orchestrator (Phase 1). A single M4-owned production controller for
the nightly knowledge-system update, per
docs/pks-nightly-orchestrator-redesign-2026-06-16.md.

Phase 1 scope ONLY: ledger, fencing lock, preflight, report renderer, and
shadow (non-mutating) stage wrappers, plus tests. No stage performs ingestion
writes or Dream applyDreamProposal. The async Dream Worker (Phase 2) and the
launchd plist (Phase 4) are intentionally not built here.

The Atomicity And Race Contract (atomic acquire+fence via Lua EVAL,
equality-only fence, fence-guarded CAS terminal writes) is honored from the
start — see orchestrator/backends.py and orchestrator/lock.py.

INPUT/OUTPUT FILES: none at package level (see individual modules).
=============================================================================
"""
