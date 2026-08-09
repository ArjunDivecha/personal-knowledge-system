from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from openai import OpenAI
from dotenv import load_dotenv
from upstash_redis import Redis
from upstash_vector import Index

from .models import EvidenceRecord, ProjectRecord


CURRENT_GENERATION_KEY = "sf:current_generation"
MANIFEST_KEY_PREFIX = "sf:manifest:"
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 3072

# Keep this replacement independent of the legacy ingestion package, whose
# __init__ imports the old extractor and its much larger dependency tree.
_INGESTION_ROOT = Path(__file__).resolve().parents[1]
for _env_path in (
    _INGESTION_ROOT / ".env",
    _INGESTION_ROOT.parent / ".env",
    _INGESTION_ROOT.parent / "distillation" / ".env",
):
    if _env_path.exists():
        load_dotenv(_env_path, override=False)
        break

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
UPSTASH_VECTOR_REST_URL = os.getenv("UPSTASH_VECTOR_REST_URL", "")
UPSTASH_VECTOR_REST_TOKEN = os.getenv("UPSTASH_VECTOR_REST_TOKEN", "")


def batched(values: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def embedding_text(record: EvidenceRecord) -> str:
    project = f"Project: {record.project}\n" if record.project else ""
    return f"{record.title}\n{project}Source: {record.source_path}\n\n{record.text}"


class SourceFirstPublisher:
    def __init__(
        self,
        *,
        redis: Redis | None = None,
        vector: Index | None = None,
        openai: OpenAI | None = None,
    ) -> None:
        self.redis = redis or Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
        self.vector = vector or Index(url=UPSTASH_VECTOR_REST_URL, token=UPSTASH_VECTOR_REST_TOKEN)
        self.openai = openai or OpenAI(api_key=OPENAI_API_KEY)

    def current_generation(self) -> str | None:
        value = self.redis.get(CURRENT_GENERATION_KEY)
        return str(value) if value else None

    def _previous_vectors(self, records: list[EvidenceRecord]) -> dict[str, list[float]]:
        previous = self.current_generation()
        if not previous:
            return {}
        raw = self.redis.get(f"{MANIFEST_KEY_PREFIX}{previous}")
        try:
            manifest = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return {}
        if not isinstance(manifest, dict):
            return {}
        prior_checksums = manifest.get("record_checksums")
        if not isinstance(prior_checksums, dict):
            return {}
        reusable_ids = [record.id for record in records if prior_checksums.get(record.id) == record.content_checksum]
        vectors: dict[str, list[float]] = {}
        for ids in batched(reusable_ids, 200):
            for item in self.vector.fetch(ids=ids, include_vectors=True, namespace=previous):
                if item is not None and isinstance(item.vector, list):
                    vectors[str(item.id)] = item.vector
        return vectors

    def _embed_missing(
        self,
        records: list[EvidenceRecord],
        vectors: dict[str, list[float]],
    ) -> int:
        missing = [record for record in records if record.id not in vectors]
        for batch in batched(missing, 64):
            response = self.openai.embeddings.create(
                model=EMBEDDING_MODEL,
                input=[embedding_text(record) for record in batch],
                dimensions=EMBEDDING_DIMENSIONS,
            )
            if len(response.data) != len(batch):
                raise RuntimeError("embedding_batch_count_mismatch")
            for record, item in zip(batch, response.data, strict=True):
                vectors[record.id] = item.embedding
        return len(missing)

    def publish(
        self,
        *,
        generation: str,
        manifest: dict[str, Any],
        records: list[EvidenceRecord],
        projects: list[ProjectRecord],
        suppressions: dict[str, Any],
    ) -> dict[str, Any]:
        previous = self.current_generation()
        vectors = self._previous_vectors(records)
        embedded_count = self._embed_missing(records, vectors)

        for batch in batched(records, 100):
            payload = []
            for record in batch:
                payload.append((record.id, vectors[record.id], {
                    "source_first": True,
                    "source_kind": record.source_kind,
                    "project": record.project or "",
                    "source_modified_at": record.source_modified_at,
                    "authority": record.authority,
                    "pinned": record.pinned,
                }))
            self.vector.upsert(vectors=payload, namespace=generation)

        for batch in batched(records, 100):
            values = {
                f"sf:{generation}:evidence:{record.id}": json.dumps(record.to_dict(), sort_keys=True)
                for record in batch
            }
            self.redis.mset(values)

        full_manifest = dict(manifest)
        full_manifest["record_ids"] = [record.id for record in records]
        full_manifest["record_checksums"] = {record.id: record.content_checksum for record in records}
        full_manifest["previous_generation"] = previous
        full_manifest["embedding_model"] = EMBEDDING_MODEL
        full_manifest["embedding_dimensions"] = EMBEDDING_DIMENSIONS
        self.redis.set(f"{MANIFEST_KEY_PREFIX}{generation}", json.dumps(full_manifest, sort_keys=True))
        self.redis.set(f"sf:{generation}:projects", json.dumps([project.to_dict() for project in projects], sort_keys=True))

        # Exact project lookup is deterministic and does not depend on whether
        # a broad semantic query happened to place that project in its top-K.
        evidence_ids_by_project: dict[str, list[str]] = {}
        project_id_by_name = {project.name: project.id for project in projects}
        for record in records:
            project_id = project_id_by_name.get(record.project or "")
            if project_id:
                evidence_ids_by_project.setdefault(project_id, []).append(record.id)
        for project in projects:
            self.redis.set(
                f"sf:{generation}:project_evidence:{project.id}",
                json.dumps(evidence_ids_by_project.get(project.id, []), sort_keys=True),
            )
        self.redis.set(f"sf:{generation}:suppressions", json.dumps(suppressions, sort_keys=True))

        missing_vectors: list[str] = []
        for ids in batched([record.id for record in records], 200):
            fetched = self.vector.fetch(ids=ids, include_vectors=False, namespace=generation)
            missing_vectors.extend(record_id for record_id, value in zip(ids, fetched, strict=True) if value is None)
        missing_redis: list[str] = []
        for batch in batched(records, 100):
            keys = [f"sf:{generation}:evidence:{record.id}" for record in batch]
            values = self.redis.mget(*keys)
            missing_redis.extend(record.id for record, value in zip(batch, values, strict=True) if value is None)
        if missing_vectors or missing_redis:
            raise RuntimeError(
                f"candidate_generation_incomplete:missing_vectors={len(missing_vectors)}:missing_redis={len(missing_redis)}"
            )

        missing_project_maps = [
            project.id
            for project in projects
            if self.redis.get(f"sf:{generation}:project_evidence:{project.id}") is None
        ]
        if missing_project_maps:
            raise RuntimeError(f"candidate_generation_incomplete:missing_project_maps={len(missing_project_maps)}")

        # This is the only serving-state mutation. It happens after the candidate
        # generation is complete in both stores and has passed strict count checks.
        self.redis.set(CURRENT_GENERATION_KEY, generation)
        return {
            "generation": generation,
            "previous_generation": previous,
            "record_count": len(records),
            "project_count": len(projects),
            "embedded_count": embedded_count,
            "reused_embedding_count": len(records) - embedded_count,
            "promoted": True,
        }

    def verify_current(self) -> dict[str, Any]:
        generation = self.current_generation()
        if not generation:
            return {"passed": False, "issues": ["current_generation_missing"]}
        raw = self.redis.get(f"{MANIFEST_KEY_PREFIX}{generation}")
        manifest = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(manifest, dict):
            return {"passed": False, "generation": generation, "issues": ["manifest_missing"]}
        ids = [str(value) for value in manifest.get("record_ids") or []]
        vector_count = 0
        redis_count = 0
        for batch_ids in batched(ids, 200):
            vector_count += sum(value is not None for value in self.vector.fetch(ids=batch_ids, namespace=generation))
        for batch_ids in batched(ids, 100):
            keys = [f"sf:{generation}:evidence:{record_id}" for record_id in batch_ids]
            redis_count += sum(value is not None for value in self.redis.mget(*keys))

        raw_projects = self.redis.get(f"sf:{generation}:projects")
        try:
            projects = json.loads(raw_projects) if isinstance(raw_projects, str) else raw_projects
        except (TypeError, ValueError):
            projects = []
        project_ids = [str(project["id"]) for project in projects or [] if isinstance(project, dict) and project.get("id")]
        project_map_count = 0
        for batch_ids in batched(project_ids, 100):
            keys = [f"sf:{generation}:project_evidence:{project_id}" for project_id in batch_ids]
            project_map_count += sum(value is not None for value in self.redis.mget(*keys))
        issues: list[str] = []
        if vector_count != len(ids):
            issues.append(f"vector_count:{vector_count}!={len(ids)}")
        if redis_count != len(ids):
            issues.append(f"redis_count:{redis_count}!={len(ids)}")
        if project_map_count != len(project_ids):
            issues.append(f"project_map_count:{project_map_count}!={len(project_ids)}")
        return {
            "passed": not issues,
            "generation": generation,
            "expected_count": len(ids),
            "vector_count": vector_count,
            "redis_count": redis_count,
            "expected_project_map_count": len(project_ids),
            "project_map_count": project_map_count,
            "issues": issues,
        }
