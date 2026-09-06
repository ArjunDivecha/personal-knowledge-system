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

    def test_recent_sessions_exclude_retrieval_validation_transcripts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project_root = root / "Memory"
            codex_root = root / "codex"
            project_root.mkdir()
            codex_root.mkdir()
            timestamp = "2026-08-11T17:23:10Z"
            validation_prompt = """Use the Personal Knowledge app and actually call its tools.
Run these checks and show a PASS/FAIL table with the raw supporting fields.
Search for sourdough fermentation recipe and report working_context_bonus,
content_checksum, and complete_source."""
            validation_report = """Personal Knowledge validation
PASS/FAIL Raw supporting fields
abstain_reason: null; minimum_final_score: 0.65;
working_context_bonus: 0.04; content_checksum: abc; complete_source: true."""
            prose_report = """9 of 10 checks pass. One fails outright: the sourdough
query does not abstain. This is a self-referential match on the test script.
Raw supporting fields follow."""
            live_acceptance = """The live acceptance checks now pass on the public MCP:
sourdough returns an explicit empty abstention, working_context is labeled,
and get_deep returns every source chunk."""
            (codex_root / "session.jsonl").write_text("\n".join([
                json.dumps({"type": "session_meta", "timestamp": timestamp, "payload": {"cwd": str(project_root), "id": "codex-meta"}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Keep recent ASADO network spillover work at top of mind."}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": validation_prompt}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": validation_report}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": prose_report}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": live_acceptance}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "A historical note quoted: sourdough fermentation recipe."}]}}),
                json.dumps({"type": "response_item", "timestamp": timestamp, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Decision: retain one source-first index and treat sessions as working context."}]}}),
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
                    "excluded_retrieval_probe_queries": ["sourdough fermentation recipe"],
                    "surfaces": [{"name": "codex", "path": str(codex_root)}],
                },
            }

            records, diagnostics = scan_recent_sessions(
                config,
                [project],
                now=datetime(2026, 8, 11, 18, 0, tzinfo=UTC),
            )
            combined = "\n".join(record.text for record in records)
            self.assertIn("ASADO network spillover", combined)
            self.assertIn("one source-first index", combined)
            self.assertNotIn("sourdough fermentation recipe", combined)
            self.assertNotIn("minimum_final_score", combined)
            self.assertEqual(diagnostics["excluded_retrieval_meta_turn_count"], 5)

    def test_configured_negative_probes_are_excluded_from_session_evidence(self):
        repo_root = Path(__file__).resolve().parents[2]
        config = json.loads((repo_root / "shared/source_first_config.json").read_text())
        negative_probes = json.loads((repo_root / "tests/probes/negative.json").read_text())
        configured = {
            query.casefold()
            for query in config["recent_sessions"]["excluded_retrieval_probe_queries"]
        }
        enabled = {
            probe["query"].casefold()
            for probe in negative_probes
            if probe.get("enabled") is True
        }
        self.assertEqual(configured, enabled)


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


class SessionFallbackMappingTests(unittest.TestCase):
    """Sessions under a source root whose folder has no authoritative file are
    named after the top-level folder instead of being dropped; the remaining
    unmapped sessions are bucketed by cause (2026-09-04)."""

    def test_unlisted_folder_sessions_are_kept_and_unmapped_are_bucketed(self) -> None:
        import tempfile
        from ingestion.source_first.models import ProjectRecord

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "A Working"
            unlisted = root / "Scratch Project"
            claude_root = Path(tmp) / "claude"
            for path in (unlisted, claude_root):
                path.mkdir(parents=True)
            timestamp = "2026-08-10T12:00:00Z"

            def session(name: str, cwd: str, sid: str) -> None:
                (claude_root / name).write_text("\n".join([
                    json.dumps({"type": "user", "timestamp": timestamp, "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": "Question about the scratch project design."}}),
                    json.dumps({"type": "assistant", "timestamp": timestamp, "cwd": cwd, "sessionId": sid, "message": {"role": "assistant", "content": [{"type": "text", "text": "Decision recorded here."}]}}),
                ]) + "\n")

            session("unlisted.jsonl", str(unlisted), "s-unlisted")
            session("tmp.jsonl", "/private/tmp/headless", "s-tmp")
            session("home.jsonl", "/", "s-root")
            session("outside.jsonl", "/opt/elsewhere/project", "s-outside")
            catalog_project = ProjectRecord("p1", "Listed", str(root / "Listed"), "active", timestamp, "summary")
            config = {
                "chunk_chars": 3200,
                "chunk_overlap_chars": 240,
                "roots": [{"path": str(root), "source_kind": "working_project", "max_depth": 4}],
                "recent_sessions": {
                    "enabled": True,
                    "retention_days": 30,
                    "max_sessions_per_surface": 100,
                    "max_total_chunks": 250,
                    "max_turn_chars": 1200,
                    "max_session_chars": 24000,
                    "require_source_roots": True,
                    "map_unlisted_folders": True,
                    "surfaces": [{"name": "claude_code", "path": str(claude_root)}],
                },
            }
            now = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
            records, diagnostics = scan_recent_sessions(config, [catalog_project], now=now)

            self.assertEqual({record.project for record in records}, {"Scratch Project"})
            self.assertEqual(diagnostics["fallback_mapped_session_count"], 1)
            self.assertEqual(diagnostics["unmapped_session_count"], 3)
            self.assertEqual(diagnostics["unmapped_by_cause"]["scratch_tmp"], 1)
            self.assertEqual(diagnostics["unmapped_by_cause"]["root_or_home"], 1)
            self.assertEqual(diagnostics["unmapped_by_cause"]["outside_source_roots"], 1)

            config["recent_sessions"]["map_unlisted_folders"] = False
            records_off, diagnostics_off = scan_recent_sessions(config, [catalog_project], now=now)
            self.assertEqual(records_off, [])
            self.assertEqual(diagnostics_off["unmapped_by_cause"]["inside_source_root_without_project"], 1)


class RootIncludeGlobTests(unittest.TestCase):
    """A root may declare include_globs so every matching file is authoritative
    even when its name is not README/PRD/REPORT-shaped (Investment Learnings)."""

    def test_root_include_globs_admit_plain_markdown(self) -> None:
        import tempfile
        from ingestion.source_first.scanner import iter_source_files

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Investment Learnings"
            root.mkdir()
            (root / "INDEX.md").write_text("- [13F](13F.md): cloning does not beat SPX\n")
            (root / "13F.md").write_text("# 13F verdict\nrejected\n")
            (root / "notes.txt").write_text("not markdown\n")
            config = {
                "recent_days": 365,
                "max_file_bytes": 750000,
                "authoritative_names": ["README.md"],
                "authoritative_name_contains": ["PRD"],
                "exclude_directories": [],
                "roots": [{"path": str(root), "source_kind": "investment_learning", "max_depth": 2, "include_globs": ["*.md"]}],
                "source_authority": {"investment_learning": 1.0},
            }
            files = iter_source_files(config, now=datetime(2026, 9, 4, tzinfo=UTC))
            names = sorted(f.path.name for f in files)
            self.assertEqual(names, ["13F.md", "INDEX.md"])
            self.assertTrue(all(f.source_kind == "investment_learning" and f.authority == 1.0 for f in files))


class ChatExportScannerTests(unittest.TestCase):
    """claude.ai export -> claude_ai_chat evidence (whole archive, no retention) plus
    pinned curated memory from memories.json (2026-09-04)."""

    def test_zip_export_becomes_chat_and_memory_evidence(self) -> None:
        import tempfile
        import zipfile
        from ingestion.source_first.chat_export_scanner import scan_chat_exports

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Anthropic"
            (root / "2026-09-04").mkdir(parents=True)
            (root / "conversations.json").write_text("[]")  # older loose export must lose to the dated dir
            convs = [
                {"uuid": "c1", "name": "Country momentum", "created_at": "2025-01-02T10:00:00Z", "updated_at": "2025-01-02T11:00:00Z",
                 "chat_messages": [
                     {"uuid": "m1", "sender": "human", "text": "What do I think about country ETF momentum? API_KEY=abc123secret", "created_at": "2025-01-02T10:00:00Z", "parent_message_uuid": None},
                     {"uuid": "m2", "sender": "assistant", "text": "Index-space alpha is roughly zero at the close.", "created_at": "2025-01-02T10:01:00Z", "parent_message_uuid": "m1"},
                     {"uuid": "m3", "sender": "assistant", "text": "Older branch that was regenerated.", "created_at": "2025-01-02T10:00:30Z", "parent_message_uuid": "m1"},
                 ]},
                {"uuid": "c2", "name": "empty", "created_at": "2025-01-03T10:00:00Z", "updated_at": "2025-01-03T10:00:00Z", "chat_messages": []},
                {"uuid": "c3", "name": "PKS validation checks", "created_at": "2025-02-01T10:00:00Z", "updated_at": "2025-02-01T10:00:00Z",
                 "chat_messages": [
                     {"uuid": "v1", "sender": "human", "text": "Run these checks with a negative control: search 'sourdough fermentation recipe' and confirm abstention.", "created_at": "2025-02-01T10:00:00Z"},
                     {"uuid": "v2", "sender": "assistant", "text": "Negative control passed: abstain_reason no_relevant_evidence, minimum_final_score 0.65, PASS/FAIL report complete.", "created_at": "2025-02-01T10:01:00Z"},
                 ]},
            ]
            with zipfile.ZipFile(root / "2026-09-04" / "conversations-000.zip", "w") as archive:
                archive.writestr("conversations.json", json.dumps(convs))
            with zipfile.ZipFile(root / "2026-09-04" / "memories-000.zip", "w") as archive:
                archive.writestr("memories/acct.json", json.dumps({"conversations_memory": "**Work context**\n\nArjun is a Senior Advisor.", "project_memories": {}, "memory_files": []}))
            config = {
                "chunk_chars": 3200, "chunk_overlap_chars": 240,
                "recent_sessions": {"excluded_retrieval_probe_queries": ["sourdough fermentation recipe"]},
                "chat_exports": {"enabled": True, "root": str(root), "authority": 0.6},
            }
            records, diagnostics = scan_chat_exports(config, now=datetime(2026, 9, 4, tzinfo=UTC))
            self.assertFalse(any("sourdough" in r.text.lower() for r in records))
            self.assertEqual(diagnostics["excluded_retrieval_meta_turn_count"], 2)

            chats = [r for r in records if r.source_kind == "claude_ai_chat"]
            memories = [r for r in records if r.source_kind == "curated_memory"]
            self.assertEqual(diagnostics["export_dir"], str(root / "2026-09-04"))
            self.assertEqual(diagnostics["conversation_count"], 1)
            self.assertEqual(diagnostics["skipped_empty_conversations"], 2)  # the empty chat + the fully-filtered validation chat
            self.assertEqual(len(chats), 1)
            self.assertEqual(chats[0].source_path, "chat://claude_ai/c1")
            self.assertEqual(chats[0].authority, 0.6)
            self.assertEqual(chats[0].evidence_role, "authoritative")
            self.assertIn("Index-space alpha", chats[0].text)
            self.assertNotIn("Older branch", chats[0].text)  # regenerated sibling dropped
            self.assertIn("[REDACTED]", chats[0].text)
            self.assertNotIn("abc123secret", chats[0].text)
            self.assertEqual(len(memories), 1)
            self.assertTrue(memories[0].pinned)
            self.assertEqual(memories[0].authority, 1.0)
            # deterministic ids across runs
            again, _ = scan_chat_exports(config, now=datetime(2026, 9, 4, tzinfo=UTC))
            self.assertEqual([r.id for r in records], [r.id for r in again])


class ChatGptExportScannerTests(unittest.TestCase):
    """OpenAI data export -> chatgpt_chat evidence via the current_node primary path;
    is_do_not_remember conversations are skipped (2026-09-05)."""

    def test_mapping_tree_follows_current_node_and_respects_do_not_remember(self) -> None:
        import tempfile
        from ingestion.source_first.chat_export_scanner import scan_chat_exports

        def msg(mid, role, text, t, ctype="text"):
            return {"id": mid, "message": {"id": mid, "author": {"role": role}, "create_time": t,
                                           "content": {"content_type": ctype, "parts": [text]}, "metadata": {}}}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ChatGPT"
            (root / "2026-09-04").mkdir(parents=True)
            mapping = {
                "root": {"id": "root", "message": None, "parent": None, "children": ["u1"]},
                "u1": {**msg("u1", "user", "What drives country ETF momentum?", 1700000000.0), "parent": "root", "children": ["a1", "a2"]},
                "a1": {**msg("a1", "assistant", "Old regenerated answer.", 1700000010.0), "parent": "u1", "children": []},
                "a2": {**msg("a2", "assistant", "Index-space alpha is near zero at the close.", 1700000020.0), "parent": "u1", "children": ["s1"]},
                "s1": {**msg("s1", "assistant", "hidden reasoning", 1700000021.0, ctype="thoughts"), "parent": "a2", "children": []},
            }
            convs = [
                {"id": "g1", "conversation_id": "g1", "title": "Momentum", "create_time": 1700000000.0, "update_time": 1700000030.0, "current_node": "s1", "mapping": mapping},
                {"id": "g2", "conversation_id": "g2", "title": "Private", "create_time": 1700000000.0, "update_time": 1700000030.0, "current_node": "u1", "mapping": mapping, "is_do_not_remember": True},
            ]
            (root / "2026-09-04" / "conversations-000.json").write_text(json.dumps(convs))
            config = {
                "chunk_chars": 3200, "chunk_overlap_chars": 240,
                "chat_exports": {"enabled": True, "surfaces": [{"name": "chatgpt", "root": str(root), "source_kind": "chatgpt_chat", "authority": 0.6}]},
            }
            records, diagnostics = scan_chat_exports(config, now=datetime(2026, 9, 5, tzinfo=UTC))
            self.assertEqual(len(records), 1)
            r = records[0]
            self.assertEqual(r.source_kind, "chatgpt_chat")
            self.assertEqual(r.source_path, "chat://chatgpt/g1")
            self.assertIn("near zero at the close", r.text)
            self.assertNotIn("Old regenerated", r.text)
            self.assertNotIn("hidden reasoning", r.text)
            self.assertEqual(r.source_modified_at, "2023-11-14T22:13:50+00:00")
            self.assertEqual(diagnostics["skipped_do_not_remember"], 1)
            self.assertEqual(diagnostics["surfaces"]["chatgpt"]["conversation_count"], 1)
