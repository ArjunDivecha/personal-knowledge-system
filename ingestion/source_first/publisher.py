from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openai import OpenAI
from dotenv import load_dotenv
from upstash_redis import Redis
from upstash_vector import Index

from .models import EvidenceRecord, ProjectRecord


CURRENT_GENERATION_KEY = "sf:current_generation"
HEARTBEAT_KEY = "sf:heartbeat"
GENERATION_HISTORY_KEY = "sf:generation_history"
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


def lexical_terms(record: EvidenceRecord) -> set[str]:
    """Identifier/path terms for exact candidate lookup beside vector search."""
    haystack = "\n".join((record.title, record.project or "", record.source_path, record.text))
    terms: set[str] = set()
    for value in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}", haystack):
        terms.update(part for part in re.split(r"[^a-z0-9]+", value.lower()) if len(part) >= 3)
        normalized = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        terms.update(token for token in normalized.split() if len(token) >= 3)
        compact = re.sub(r"[^a-z0-9]", "", value.lower())
        if len(compact) >= 3:
            terms.add(compact)
    return terms


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
        promote: bool = True,
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
                    "evidence_role": record.evidence_role,
                    "session_surface": record.session_surface or "",
                    "attention_observed_at": record.attention_observed_at or "",
                }))
            self.vector.upsert(vectors=payload, namespace=generation)

        for batch in batched(records, 100):
            values = {
                f"sf:{generation}:evidence:{record.id}": json.dumps(record.to_dict(), sort_keys=True)
                for record in batch
            }
            self.redis.mset(values)

        staged_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        full_manifest = dict(manifest)
        full_manifest["record_ids"] = [record.id for record in records]
        full_manifest["record_checksums"] = {record.id: record.content_checksum for record in records}
        full_manifest["previous_generation"] = previous
        full_manifest["staged_at"] = staged_at
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

        evidence_ids_by_source: dict[str, list[str]] = {}
        for record in records:
            evidence_ids_by_source.setdefault(record.source_id, []).append(record.id)
        for batch in batched(list(evidence_ids_by_source.items()), 100):
            self.redis.mset({
                f"sf:{generation}:source_evidence:{source_id}": json.dumps(ids, sort_keys=True)
                for source_id, ids in batch
            })
        # Embeddings routinely miss opaque identifiers (for example 1MTR or a
        # DuckDB filename). Publish a bounded inverted index so the Worker can
        # add exact source candidates without scanning the evidence store.
        lexical_ids: dict[str, list[str]] = {}
        for record in records:
            for term in lexical_terms(record):
                lexical_ids.setdefault(term, []).append(record.id)
        for batch in batched(list(lexical_ids.items()), 100):
            self.redis.mset({
                f"sf:{generation}:lex:{term}": json.dumps(ids[:200], sort_keys=True)
                for term, ids in batch
            })
        lexical_term_names = sorted(lexical_ids)
        missing_lexical_maps: list[str] = []
        for terms in batched(lexical_term_names, 100):
            values = self.redis.mget(*(f"sf:{generation}:lex:{term}" for term in terms))
            missing_lexical_maps.extend(term for term, value in zip(terms, values, strict=True) if value is None)
        if missing_lexical_maps:
            raise RuntimeError(
                f"candidate_generation_incomplete:missing_lexical_maps={len(missing_lexical_maps)}"
            )
        full_manifest["lexical_term_count"] = len(lexical_term_names)
        full_manifest["lexical_terms_checksum"] = hashlib.sha256("\n".join(lexical_term_names).encode()).hexdigest()
        full_manifest["lexical_verification_sample"] = lexical_term_names[::max(1, len(lexical_term_names) // 200)][:200]
        self.redis.set(f"{MANIFEST_KEY_PREFIX}{generation}", json.dumps(full_manifest, sort_keys=True))
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

        missing_source_maps = [
            source_id
            for source_id in evidence_ids_by_source
            if self.redis.get(f"sf:{generation}:source_evidence:{source_id}") is None
        ]
        if missing_source_maps:
            raise RuntimeError(f"candidate_generation_incomplete:missing_source_maps={len(missing_source_maps)}")

        if promote:
            self.promote_generation(generation)
        return {
            "generation": generation,
            "previous_generation": previous,
            "record_count": len(records),
            "project_count": len(projects),
            "embedded_count": embedded_count,
            "reused_embedding_count": len(records) - embedded_count,
            "promoted": promote,
            "staged": True,
        }

    def promote_generation(self, generation: str) -> dict[str, Any]:
        """Make a fully staged generation live with the only serving pointer write."""
        verification = self.verify_generation(generation)
        if not verification.get("passed"):
            raise RuntimeError(
                "candidate_generation_invalid:" + ",".join(verification.get("issues") or ["unknown"])
            )
        raw = self.redis.get(f"{MANIFEST_KEY_PREFIX}{generation}")
        manifest = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(manifest, dict):
            raise RuntimeError("candidate_generation_manifest_missing")
        published_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        manifest["published_at"] = published_at
        self.redis.set(f"{MANIFEST_KEY_PREFIX}{generation}", json.dumps(manifest, sort_keys=True))
        self.redis.set(HEARTBEAT_KEY, json.dumps({
            "generation": generation,
            "built_at": manifest.get("built_at"),
            "published_at": published_at,
            "source_checksum": manifest.get("source_checksum"),
            "evidence_count": manifest.get("evidence_count"),
        }, sort_keys=True))
        self.redis.set(CURRENT_GENERATION_KEY, generation)
        retention = self._record_and_prune_generations(generation, manifest)
        return {
            "generation": generation,
            "published_at": published_at,
            "promoted": True,
            "retention": retention,
        }

    def _record_and_prune_generations(
        self,
        generation: str,
        manifest: dict[str, Any],
        retain: int = 3,
    ) -> dict[str, Any]:
        """Retain live plus two rollback generations from the verified chain."""
        raw = self.redis.get(GENERATION_HISTORY_KEY)
        try:
            existing = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            existing = []
        history = [generation]
        if isinstance(existing, list):
            history.extend(str(value) for value in existing if value)
        previous = manifest.get("previous_generation")
        if isinstance(previous, str) and previous:
            history.append(previous)
        history = list(dict.fromkeys(history))
        keep = history[:retain]
        prune = history[retain:]
        self.redis.set(GENERATION_HISTORY_KEY, json.dumps(keep, sort_keys=True))

        pruned: list[str] = []
        if hasattr(self.redis, "scan") and hasattr(self.redis, "delete") and hasattr(self.vector, "delete_namespace"):
            for old_generation in prune:
                if old_generation in keep or old_generation == generation:
                    continue
                cursor = 0
                keys: list[str] = []
                while True:
                    cursor, batch_keys = self.redis.scan(
                        cursor,
                        match=f"sf:{old_generation}:*",
                        count=500,
                    )
                    keys.extend(str(key) for key in batch_keys)
                    if int(cursor) == 0:
                        break
                keys.append(f"{MANIFEST_KEY_PREFIX}{old_generation}")
                for batch_keys in batched(list(dict.fromkeys(keys)), 100):
                    self.redis.delete(*batch_keys)
                self.vector.delete_namespace(namespace=old_generation)
                pruned.append(old_generation)
        return {"retained_generations": keep, "pruned_generations": pruned}

    def verify_generation(
        self,
        generation: str,
        *,
        max_age_seconds: int | None = None,
        now: datetime | None = None,
        require_heartbeat: bool = False,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        raw = self.redis.get(f"{MANIFEST_KEY_PREFIX}{generation}")
        manifest = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(manifest, dict):
            return {"passed": False, "generation": generation, "issues": ["manifest_missing"]}
        ids = [str(value) for value in manifest.get("record_ids") or []]
        vector_count = 0
        redis_count = 0
        source_ids: set[str] = set()
        for batch_ids in batched(ids, 200):
            vector_count += sum(value is not None for value in self.vector.fetch(ids=batch_ids, namespace=generation))
        for batch_ids in batched(ids, 100):
            keys = [f"sf:{generation}:evidence:{record_id}" for record_id in batch_ids]
            values = self.redis.mget(*keys)
            redis_count += sum(value is not None for value in values)
            for value in values:
                try:
                    evidence = json.loads(value) if isinstance(value, str) else value
                except (TypeError, ValueError):
                    evidence = None
                if isinstance(evidence, dict) and evidence.get("source_id"):
                    source_ids.add(str(evidence["source_id"]))

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
        source_map_count = 0
        for batch_ids in batched(sorted(source_ids), 100):
            keys = [f"sf:{generation}:source_evidence:{source_id}" for source_id in batch_ids]
            source_map_count += sum(value is not None for value in self.redis.mget(*keys))
        lexical_sample = [str(term) for term in manifest.get("lexical_verification_sample") or []]
        lexical_sample_count = 0
        for terms in batched(lexical_sample, 100):
            keys = [f"sf:{generation}:lex:{term}" for term in terms]
            lexical_sample_count += sum(value is not None for value in self.redis.mget(*keys))
        issues: list[str] = []
        if vector_count != len(ids):
            issues.append(f"vector_count:{vector_count}!={len(ids)}")
        if redis_count != len(ids):
            issues.append(f"redis_count:{redis_count}!={len(ids)}")
        if project_map_count != len(project_ids):
            issues.append(f"project_map_count:{project_map_count}!={len(project_ids)}")
        raw_heartbeat = self.redis.get(HEARTBEAT_KEY)
        try:
            heartbeat = json.loads(raw_heartbeat) if isinstance(raw_heartbeat, str) else raw_heartbeat
        except (TypeError, ValueError):
            heartbeat = None
        if require_heartbeat and (
            not isinstance(heartbeat, dict) or heartbeat.get("generation") != generation
        ):
            issues.append("heartbeat_missing_or_mismatched")
        matching_heartbeat = (
            heartbeat if isinstance(heartbeat, dict) and heartbeat.get("generation") == generation else None
        )
        if source_map_count != len(source_ids):
            issues.append(f"source_map_count:{source_map_count}!={len(source_ids)}")
        if lexical_sample_count != len(lexical_sample):
            issues.append(f"lexical_sample_count:{lexical_sample_count}!={len(lexical_sample)}")
        recent_sessions = manifest.get("recent_sessions")
        if isinstance(recent_sessions, dict) and recent_sessions.get("enabled"):
            required_session_fields = (
                "claude_code_sessions", "claude_code_chunks",
                "codex_sessions", "codex_chunks", "last_successful_scan_at",
            )
            missing_session_fields = [field for field in required_session_fields if field not in recent_sessions]
            if missing_session_fields:
                issues.append("recent_sessions_manifest_missing:" + ",".join(missing_session_fields))
        freshness: dict[str, Any] = {"status": "unmeasured"}
        freshness_at = (
            matching_heartbeat.get("published_at") if matching_heartbeat else None
        ) or manifest.get("published_at") or manifest.get("staged_at") or manifest.get("built_at")
        if isinstance(freshness_at, str):
            try:
                age_seconds = max(0, int((now - datetime.fromisoformat(freshness_at)).total_seconds()))
                freshness = {"status": "fresh", "age_seconds": age_seconds, "as_of": freshness_at}
                if max_age_seconds is not None and age_seconds > max_age_seconds:
                    freshness["status"] = "stale"
                    freshness["max_age_seconds"] = max_age_seconds
                    issues.append(f"generation_stale:{age_seconds}>{max_age_seconds}")
            except (TypeError, ValueError):
                freshness = {"status": "invalid", "as_of": freshness_at}
                issues.append("generation_timestamp_invalid")
        else:
            freshness = {"status": "missing"}
            if max_age_seconds is not None:
                issues.append("generation_timestamp_missing")
        return {
            "passed": not issues,
            "generation": generation,
            "expected_count": len(ids),
            "vector_count": vector_count,
            "redis_count": redis_count,
            "expected_project_map_count": len(project_ids),
            "project_map_count": project_map_count,
            "expected_source_map_count": len(source_ids),
            "source_map_count": source_map_count,
            "heartbeat": matching_heartbeat,
            "freshness": freshness,
            "issues": issues,
        }

    def verify_current(
        self,
        *,
        max_age_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        generation = self.current_generation()
        if not generation:
            return {"passed": False, "issues": ["current_generation_missing"]}
        return self.verify_generation(
            generation,
            max_age_seconds=max_age_seconds,
            now=now,
            require_heartbeat=True,
        )
