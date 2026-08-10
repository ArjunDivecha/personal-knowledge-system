from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ingestion.source_first.models import EvidenceRecord, ProjectRecord
from ingestion.source_first.publisher import CURRENT_GENERATION_KEY, HEARTBEAT_KEY, SourceFirstPublisher
from ingestion.source_first.scanner import (
    SourceFile,
    build_projects,
    chunk_text,
    evidence_from_files,
    iter_source_files,
    strip_generated_boilerplate,
)
from ingestion.source_first.session_scanner import redact_session_text, scan_recent_sessions


UTC = timezone.utc


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, **_kwargs):
        self.store[key] = value
        return "OK"

    def mset(self, values):
        self.store.update(values)
        return "OK"

    def mget(self, *keys):
        return [self.store.get(key) for key in keys]


class FakeVector:
    def __init__(self, fail_upsert: bool = False):
        self.namespaces: dict[str, dict[str, SimpleNamespace]] = {}
        self.fail_upsert = fail_upsert

    def upsert(self, vectors, namespace=""):
        if self.fail_upsert:
            raise RuntimeError("simulated vector failure")
        target = self.namespaces.setdefault(namespace, {})
        for record_id, vector, metadata in vectors:
            target[record_id] = SimpleNamespace(id=record_id, vector=vector, metadata=metadata)
        return "OK"

    def fetch(self, ids, include_vectors=False, include_metadata=False, namespace=""):
        target = self.namespaces.get(namespace, {})
        output = []
        for record_id in ids:
            value = target.get(record_id)
            if value is None:
                output.append(None)
            else:
                output.append(SimpleNamespace(
                    id=value.id,
                    vector=value.vector if include_vectors else None,
                    metadata=value.metadata if include_metadata else None,
                ))
        return output


class FakeEmbeddings:
    def create(self, *, input, **_kwargs):
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(index), 1.0]) for index, _ in enumerate(input)])


class SourceFirstScannerTests(unittest.TestCase):
    def test_chunking_is_deterministic_and_bounded(self):
        text = ("alpha " * 300) + "\n\n" + ("beta " * 300)
        first = chunk_text(text, 500, 40)
        second = chunk_text(text, 500, 40)
        self.assertEqual(first, second)
        self.assertGreater(len(first), 2)
        self.assertTrue(all(len(chunk) <= 500 for chunk in first))

    def test_global_cross_session_boilerplate_is_removed(self):
        only_boilerplate = "## Cross-session messaging\n\nSessions can message each other directly."
        self.assertEqual(strip_generated_boilerplate(only_boilerplate), "")
        mixed = "# Tracker\n\nReal project facts.\n\n## Cross-session messaging\n\nBoilerplate."
        self.assertEqual(strip_generated_boilerplate(mixed).strip(), "# Tracker\n\nReal project facts.")

    def test_scanner_uses_authoritative_recent_files_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "Tracker"
            project.mkdir()
            (project / "README.md").write_text("# Tracker\n\nCurrent project evidence.")
            (project / "random.py").write_text("not authoritative")
            config = {
                "recent_days": 365,
                "max_file_bytes": 10000,
                "authoritative_names": ["README.md"],
                "authoritative_name_contains": ["PRD"],
                "include_relative_globs": [],
                "exclude_directories": ["node_modules"],
                "source_authority": {"working_project": 0.9},
                "roots": [{"path": str(root), "source_kind": "working_project", "max_depth": 3}],
                "explicit_files": [],
            }
            files = iter_source_files(config, now=datetime.now(UTC))
            self.assertEqual([item.path.name for item in files], ["README.md"])
            records = evidence_from_files(files, {**config, "chunk_chars": 1000, "chunk_overlap_chars": 20})
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].project, "Tracker")
            self.assertEqual(records[0].source_path, str((project / "README.md").resolve()))

    def test_required_project_without_docs_still_appears(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "Futures"
            path.mkdir()
            config = {"roots": [], "required_projects": [str(path)]}
            projects = build_projects([], config, now=datetime.now(UTC))
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0].name, "Futures")

    def test_scanner_excludes_linked_git_worktree_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            canonical = root / "ASADO"
            worktree = root / "ASADO-neural-regime"
            canonical.mkdir()
            worktree.mkdir()
            (canonical / ".git").mkdir()
            (worktree / ".git").write_text("gitdir: /tmp/main/.git/worktrees/neural\n")
            (canonical / "README.md").write_text("# Canonical")
            (worktree / "README.md").write_text("# Duplicate")
            config = {
                "recent_days": 365,
                "max_file_bytes": 10000,
                "authoritative_names": ["README.md"],
                "authoritative_name_contains": [],
                "include_relative_globs": [],
                "exclude_directories": [],
                "exclude_git_worktrees": True,
                "source_authority": {"working_project": 0.9},
                "roots": [{"path": str(root), "source_kind": "working_project", "max_depth": 3}],
                "explicit_files": [],
            }
            files = iter_source_files(config, now=datetime.now(UTC))
            self.assertEqual([item.project for item in files], ["ASADO"])

    def test_recent_sessions_are_integrated_redacted_and_role_filtered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_root = root / "Memory"
            claude_root = root / "claude"
            codex_root = root / "codex"
            project_root.mkdir()
            claude_root.mkdir()
            codex_root.mkdir()
            timestamp = "2026-08-10T12:00:00Z"
            (claude_root / "session.jsonl").write_text("\n".join([
                json.dumps({"type": "user", "timestamp": timestamp, "cwd": str(project_root), "sessionId": "claude-1", "message": {"role": "user", "content": "Tracker API_KEY=secret-canary"}}),
                json.dumps({"type": "assistant", "timestamp": timestamp, "cwd": str(project_root), "sessionId": "claude-1", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "shell", "input": "tool-secret"}]}}),
                json.dumps({"type": "assistant", "timestamp": timestamp, "cwd": str(project_root), "sessionId": "claude-1", "message": {"role": "assistant", "content": [{"type": "text", "text": "Recent decision text."}]}}),
            ]) + "\n")
            (codex_root / "session.jsonl").write_text("\n".join([
                json.dumps({"type": "session_meta", "timestamp": timestamp, "payload": {"cwd": str(project_root), "id": "codex-1"}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "developer-secret"}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Current factor timing question."}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "function_call", "role": "assistant", "content": [{"type": "output_text", "text": "tool-output-secret"}]}}),
            ]) + "\n")
            project = ProjectRecord("p1", "Memory", str(project_root), "active", timestamp, "summary")
            config = {
                "chunk_chars": 3200,
                "chunk_overlap_chars": 240,
                "recent_sessions": {
                    "enabled": True,
                    "retention_days": 30,
                    "max_sessions_per_surface": 100,
                    "max_total_chunks": 250,
                    "max_turn_chars": 1200,
                    "max_session_chars": 24000,
                    "require_source_roots": True,
                    "surfaces": [
                        {"name": "claude_code", "path": str(claude_root)},
                        {"name": "codex", "path": str(codex_root)},
                    ],
                },
            }
            now = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
            first, diagnostics = scan_recent_sessions(config, [project], now=now)
            second, _ = scan_recent_sessions(config, [project], now=now)
            combined = "\n".join(record.text for record in first)
            self.assertTrue(first, diagnostics)
            self.assertEqual([record.id for record in first], [record.id for record in second])
            self.assertTrue(all(record.evidence_role == "working_context" for record in first))
            self.assertIn("[REDACTED]", combined)
            self.assertNotIn("secret-canary", combined)
            self.assertNotIn("developer-secret", combined)
            self.assertNotIn("tool-output-secret", combined)
            self.assertNotIn("tool-secret", combined)
            self.assertEqual(diagnostics["claude_code_sessions"], 1)
            self.assertEqual(diagnostics["codex_sessions"], 1)

    def test_redaction_handles_tokens_private_keys_and_credential_urls(self):
        text = "Bearer abcdefghijklmnop https://user:password@example.com sk-abcdefghijklmnop\nPASSWORD=hunter2"
        redacted, count = redact_session_text(text)
        self.assertGreaterEqual(count, 4)
        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("password@example", redacted)
        self.assertNotIn("abcdefghijklmnop", redacted)


class SourceFirstPublisherTests(unittest.TestCase):
    def make_record(self) -> EvidenceRecord:
        return EvidenceRecord(
            id="ev_one",
            source_id="src_tracker",
            title="Tracker",
            text="Current authoritative Tracker evidence.",
            source_path="/tmp/Tracker/README.md",
            source_kind="working_project",
            project="Tracker",
            source_modified_at="2026-08-08T00:00:00+00:00",
            content_checksum="abc",
            chunk_index=0,
            chunk_count=1,
            authority=0.9,
        )

    def test_pointer_moves_only_after_candidate_is_complete(self):
        redis = FakeRedis()
        vector = FakeVector()
        publisher = SourceFirstPublisher(
            redis=redis,
            vector=vector,
            openai=SimpleNamespace(embeddings=FakeEmbeddings()),
        )
        result = publisher.publish(
            generation="sf_test",
            manifest={"generation": "sf_test"},
            records=[self.make_record()],
            projects=[ProjectRecord("p1", "Tracker", "/tmp/Tracker", "active", "2026-08-08T00:00:00+00:00", "summary")],
            suppressions={"schema_version": 1, "rules": []},
        )
        self.assertTrue(result["promoted"])
        self.assertEqual(redis.get(CURRENT_GENERATION_KEY), "sf_test")
        self.assertEqual(json.loads(redis.get("sf:sf_test:project_evidence:p1")), ["ev_one"])
        self.assertEqual(json.loads(redis.get("sf:sf_test:source_evidence:src_tracker")), ["ev_one"])
        self.assertTrue(publisher.verify_current()["passed"])

    def test_staging_does_not_move_pointer_until_explicit_promotion(self):
        redis = FakeRedis()
        redis.set(CURRENT_GENERATION_KEY, "sf_existing")
        publisher = SourceFirstPublisher(
            redis=redis,
            vector=FakeVector(),
            openai=SimpleNamespace(embeddings=FakeEmbeddings()),
        )
        result = publisher.publish(
            generation="sf_candidate",
            manifest={"generation": "sf_candidate", "built_at": "2026-08-10T00:00:00+00:00"},
            records=[self.make_record()],
            projects=[],
            suppressions={"schema_version": 1, "rules": []},
            promote=False,
        )
        self.assertFalse(result["promoted"])
        self.assertEqual(redis.get(CURRENT_GENERATION_KEY), "sf_existing")
        self.assertTrue(publisher.verify_generation("sf_candidate")["passed"])
        publisher.promote_generation("sf_candidate")
        self.assertEqual(redis.get(CURRENT_GENERATION_KEY), "sf_candidate")

    def test_failed_candidate_does_not_replace_working_pointer(self):
        redis = FakeRedis()
        redis.set(CURRENT_GENERATION_KEY, "sf_working")
        publisher = SourceFirstPublisher(
            redis=redis,
            vector=FakeVector(fail_upsert=True),
            openai=SimpleNamespace(embeddings=FakeEmbeddings()),
        )
        with self.assertRaises(RuntimeError):
            publisher.publish(
                generation="sf_broken",
                manifest={"generation": "sf_broken"},
                records=[self.make_record()],
                projects=[],
                suppressions={"schema_version": 1, "rules": []},
            )
        self.assertEqual(redis.get(CURRENT_GENERATION_KEY), "sf_working")

    def test_publish_records_a_generation_heartbeat(self):
        redis = FakeRedis()
        publisher = SourceFirstPublisher(
            redis=redis,
            vector=FakeVector(),
            openai=SimpleNamespace(embeddings=FakeEmbeddings()),
        )
        publisher.publish(
            generation="sf_heartbeat",
            manifest={"generation": "sf_heartbeat", "built_at": "2026-08-08T00:00:00+00:00"},
            records=[self.make_record()],
            projects=[],
            suppressions={"schema_version": 1, "rules": []},
        )
        heartbeat = json.loads(redis.get(HEARTBEAT_KEY))
        self.assertEqual(heartbeat["generation"], "sf_heartbeat")
        self.assertTrue(heartbeat["published_at"])

    def test_verify_current_fails_stale_generation(self):
        redis = FakeRedis()
        publisher = SourceFirstPublisher(
            redis=redis,
            vector=FakeVector(),
            openai=SimpleNamespace(embeddings=FakeEmbeddings()),
        )
        publisher.publish(
            generation="sf_old",
            manifest={"generation": "sf_old", "built_at": "2026-08-08T00:00:00+00:00"},
            records=[self.make_record()],
            projects=[],
            suppressions={"schema_version": 1, "rules": []},
        )
        redis.set(HEARTBEAT_KEY, json.dumps({
            "generation": "sf_old",
            "published_at": "2026-08-08T00:00:00+00:00",
        }))
        report = publisher.verify_current(
            max_age_seconds=60,
            now=datetime(2026, 8, 9, tzinfo=UTC),
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["freshness"]["status"], "stale")
        self.assertTrue(any(issue.startswith("generation_stale:") for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
