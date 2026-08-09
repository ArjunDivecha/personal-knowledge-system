from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ingestion.source_first.models import EvidenceRecord, ProjectRecord
from ingestion.source_first.publisher import CURRENT_GENERATION_KEY, SourceFirstPublisher
from ingestion.source_first.scanner import (
    SourceFile,
    build_projects,
    chunk_text,
    evidence_from_files,
    iter_source_files,
    strip_generated_boilerplate,
)


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


class SourceFirstPublisherTests(unittest.TestCase):
    def make_record(self) -> EvidenceRecord:
        return EvidenceRecord(
            id="ev_one",
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
        self.assertTrue(publisher.verify_current()["passed"])

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


if __name__ == "__main__":
    unittest.main()
