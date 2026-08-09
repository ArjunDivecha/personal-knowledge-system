#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ingestion.source_first.models import EvidenceRecord, SourceFirstManifest  # noqa: E402
from ingestion.source_first.publisher import SourceFirstPublisher  # noqa: E402
from ingestion.source_first.scanner import (  # noqa: E402
    build_projects,
    evidence_from_files,
    iter_source_files,
    load_json,
)

UTC = timezone.utc
DEFAULT_CONFIG = REPO_ROOT / "shared" / "source_first_config.json"
DEFAULT_CURATED = REPO_ROOT / "shared" / "source_first_curated_memory.json"
DEFAULT_SUPPRESSIONS = REPO_ROOT / "shared" / "source_first_suppressions.json"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "scripts" / "reports" / "source_first"


def generation_id(now: datetime) -> str:
    return now.strftime("sf_%Y%m%dT%H%M%SZ")


def curated_records(payload: dict[str, Any], now: datetime) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for raw in payload.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        entry_id = str(raw.get("id") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not entry_id or not text:
            raise ValueError("curated entries require id and text")
        checksum = hashlib.sha256(text.encode()).hexdigest()
        records.append(EvidenceRecord(
            id=f"ev_curated_{hashlib.sha256(entry_id.encode()).hexdigest()[:16]}",
            title=str(raw.get("title") or entry_id)[:240],
            text=text,
            source_path=f"curated://{entry_id}",
            source_kind="curated_memory",
            project=None,
            source_modified_at=str(raw.get("updated_at") or now.replace(microsecond=0).isoformat()),
            content_checksum=checksum,
            chunk_index=0,
            chunk_count=1,
            authority=1.0,
            pinned=True,
        ))
    return records


def write_artifacts(
    root: Path,
    generation: str,
    manifest: dict[str, Any],
    records: list[EvidenceRecord],
    projects: list[Any],
    suppressions: dict[str, Any],
) -> Path:
    output = root / generation
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output / "evidence.jsonl").write_text("".join(json.dumps(record.to_dict(), sort_keys=True) + "\n" for record in records))
    (output / "projects.json").write_text(json.dumps([project.to_dict() for project in projects], indent=2, sort_keys=True) + "\n")
    (output / "suppressions.json").write_text(json.dumps(suppressions, indent=2, sort_keys=True) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and atomically publish the source-first PKS index.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--curated", type=Path, default=DEFAULT_CURATED)
    parser.add_argument("--suppressions", type=Path, default=DEFAULT_SUPPRESSIONS)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--publish", action="store_true", help="publish verified generation to Upstash and promote it")
    parser.add_argument("--verify-current", action="store_true", help="verify the currently promoted remote generation")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=36.0,
        help="with --verify-current, fail if the promoted generation is older than this many hours",
    )
    args = parser.parse_args()

    if args.verify_current:
        if args.max_age_hours <= 0:
            raise ValueError("--max-age-hours must be positive")
        report = SourceFirstPublisher().verify_current(max_age_seconds=int(args.max_age_hours * 3600))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("passed") else 1

    now = datetime.now(UTC)
    config = load_json(args.config)
    files = iter_source_files(config, now=now)
    records = evidence_from_files(files, config)
    records.extend(curated_records(load_json(args.curated), now))
    records.sort(key=lambda record: (record.source_path, record.chunk_index))
    projects = build_projects(files, config, now=now)
    suppressions = load_json(args.suppressions)

    required = [str(Path(path).expanduser().resolve()) for path in config.get("required_projects") or []]
    present_paths = {project.path for project in projects}
    present = [path for path in required if path in present_paths]
    missing = [path for path in required if path not in present_paths]
    if missing:
        raise RuntimeError(f"required_projects_missing:{','.join(missing)}")
    if not records:
        raise RuntimeError("no_evidence_records_built")

    generation = generation_id(now)
    source_checksum = hashlib.sha256(
        "\n".join(f"{record.id}:{record.content_checksum}" for record in records).encode()
    ).hexdigest()
    manifest = SourceFirstManifest(
        schema_version=1,
        generation=generation,
        built_at=now.replace(microsecond=0).isoformat(),
        evidence_count=len(records),
        project_count=len(projects),
        source_file_count=len(files),
        source_checksum=source_checksum,
        required_projects_present=present,
        required_projects_missing=missing,
    ).to_dict()
    output = write_artifacts(args.artifact_root, generation, manifest, records, projects, suppressions)
    report: dict[str, Any] = {"artifact_dir": str(output), "manifest": manifest, "published": False}
    if args.publish:
        result = SourceFirstPublisher().publish(
            generation=generation,
            manifest=manifest,
            records=records,
            projects=projects,
            suppressions=suppressions,
        )
        report["published"] = True
        report["publish_result"] = result
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
