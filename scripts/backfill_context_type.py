#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from _memory_migration import (
    CHECKPOINT_DIR,
    DEFAULT_CLASSIFICATION_MODEL,
    VALID_CONTEXT_TYPES,
    append_report,
    build_classification_summary,
    ensure_runtime_dirs,
    get_entry_label,
    get_entry_mention_count,
    get_entry_source_hint,
    load_checkpoint,
    load_entries,
    normalize_entry_for_phase2,
    save_checkpoint,
    utc_now_iso,
)

import sys

DISTILLATION_ROOT = Path(__file__).resolve().parent.parent / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from storage.redis_client import RedisClient  # noqa: E402
from utils.llm import call_claude_json  # noqa: E402


CLASSIFICATION_SYSTEM = """
You classify personal knowledge entries for long-term memory retention.
Choose exactly one label per entry.
When unsure, prefer task_query over passing_reference.
Output only valid JSON.
""".strip()


def build_batch_prompt(entries: list[Any]) -> str:
    payload = []
    for entry in entries:
        payload.append(
            {
                "id": entry.id,
                "entry_type": entry.type,
                "domain": get_entry_label(entry),
                "summary": build_classification_summary(entry),
                "source": get_entry_source_hint(entry),
                "mention_count": get_entry_mention_count(entry),
            }
        )

    return (
        "Classify each entry into exactly one context_type.\n\n"
        "Allowed labels:\n"
        "- professional_identity\n"
        "- stated_preference\n"
        "- explicit_save\n"
        "- active_project\n"
        "- recurring_pattern\n"
        "- task_query\n"
        "- passing_reference\n\n"
        "Return JSON in this exact shape:\n"
        "{\"results\": [{\"id\": \"...\", \"context_type\": \"task_query\"}]}\n\n"
        f"Entries:\n{json.dumps(payload, indent=2)}"
    )


def validate_batch_response(entries: list[Any], response: dict[str, Any]) -> dict[str, str]:
    results = response.get("results")
    if not isinstance(results, list):
        raise ValueError("response missing results list")

    by_id: dict[str, str] = {}
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("result item is not an object")
        entry_id = item.get("id")
        label = item.get("context_type")
        if not isinstance(entry_id, str) or not isinstance(label, str):
            raise ValueError("result item missing id or context_type")
        if label not in VALID_CONTEXT_TYPES:
            raise ValueError(f"invalid context_type: {label}")
        by_id[entry_id] = label

    expected_ids = {entry.id for entry in entries}
    actual_ids = set(by_id)
    if expected_ids != actual_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(f"batch response id mismatch missing={missing} extra={extra}")

    return by_id


def classify_batch(entries: list[Any], model: str, max_tokens: int) -> tuple[dict[str, str], int, int, int]:
    response, input_tokens, output_tokens = call_claude_json(
        prompt=build_batch_prompt(entries),
        system=CLASSIFICATION_SYSTEM,
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return validate_batch_response(entries, response), input_tokens, output_tokens, 1


def classify_with_retries(entries: list[Any], model: str, max_tokens: int) -> tuple[dict[str, str], int, int, int]:
    try:
        return classify_batch(entries, model=model, max_tokens=max_tokens)
    except Exception:
        if len(entries) == 1:
            raise

    midpoint = len(entries) // 2
    left_results, left_in, left_out, left_calls = classify_with_retries(entries[:midpoint], model=model, max_tokens=max_tokens)
    right_results, right_in, right_out, right_calls = classify_with_retries(entries[midpoint:], model=model, max_tokens=max_tokens)
    merged = dict(left_results)
    merged.update(right_results)
    return merged, left_in + right_in, left_out + right_out, left_calls + right_calls


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill context_type via Claude Haiku")
    parser.add_argument("--model", default=DEFAULT_CLASSIFICATION_MODEL)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-response-tokens", type=int, default=2048)
    parser.add_argument("--entry-type", choices=["all", "knowledge", "project"], default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--checkpoint", type=Path, default=CHECKPOINT_DIR / "backfill_context_type.json")
    parser.add_argument("--max-api-calls", type=int, default=500)
    parser.add_argument("--max-input-tokens", type=int, default=250000)
    parser.add_argument("--max-output-tokens", type=int, default=50000)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    args = parser.parse_args()

    ensure_runtime_dirs()
    checkpoint = load_checkpoint(args.checkpoint, name="backfill_context_type")
    redis_client = RedisClient()

    entries = load_entries(redis_client, entry_type=args.entry_type)
    entries.sort(key=lambda entry: entry.id)
    entries = [normalize_entry_for_phase2(entry) for entry in entries]

    pending_entries = []
    completed_ids = set(checkpoint["completed_ids"])
    for entry in entries:
        metadata = entry.metadata
        already_classified = bool(metadata and metadata.context_type and (metadata.classification_status or "") != "pending")
        if entry.id in completed_ids:
            continue
        if not args.force and already_classified:
            continue
        pending_entries.append(entry)

    if args.limit is not None:
        pending_entries = pending_entries[: args.limit]

    stats = checkpoint.setdefault("stats", {})
    stats.setdefault("api_calls", 0)
    stats.setdefault("entries_examined", 0)
    stats.setdefault("entries_classified", 0)
    stats.setdefault("input_tokens", 0)
    stats.setdefault("output_tokens", 0)

    print(f"Loaded {len(entries)} entries, {len(pending_entries)} pending classification")
    if args.dry_run:
        print("Dry run enabled: Redis will not be mutated")

    consecutive_errors = 0

    for batch in [pending_entries[i:i + args.batch_size] for i in range(0, len(pending_entries), args.batch_size)]:
        if stats["api_calls"] >= args.max_api_calls:
            print("Stopping: max API calls reached")
            break
        if stats["input_tokens"] >= args.max_input_tokens:
            print("Stopping: max input token budget reached")
            break
        if stats["output_tokens"] >= args.max_output_tokens:
            print("Stopping: max output token budget reached")
            break

        try:
            labels_by_id, input_tokens, output_tokens, api_calls_used = classify_with_retries(
                batch,
                model=args.model,
                max_tokens=args.max_response_tokens,
            )
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            checkpoint["errors"].append(
                {
                    "at": utc_now_iso(),
                    "entry_ids": [entry.id for entry in batch],
                    "error": str(exc),
                }
            )
            save_checkpoint(args.checkpoint, checkpoint)
            print(f"Batch failed for {[entry.id for entry in batch]}: {exc}")
            if consecutive_errors >= args.max_consecutive_errors:
                print("Stopping: too many consecutive classification failures")
                break
            continue

        stats["api_calls"] += api_calls_used
        stats["input_tokens"] += input_tokens
        stats["output_tokens"] += output_tokens
        stats["entries_examined"] += len(batch)

        for entry in batch:
            label = labels_by_id[entry.id]
            if not args.dry_run:
                entry.metadata.context_type = label
                entry.metadata.classification_status = "complete"
                if entry.type == "knowledge":
                    redis_client.save_knowledge_entry(entry)
                else:
                    redis_client.save_project_entry(entry)
            stats["entries_classified"] += 1
            if not args.dry_run:
                checkpoint["completed_ids"].append(entry.id)

        if not args.dry_run:
            checkpoint["completed_ids"] = list(dict.fromkeys(checkpoint["completed_ids"]))
            save_checkpoint(args.checkpoint, checkpoint)
        print(
            f"Processed {stats['entries_classified']}/{len(pending_entries)} "
            f"classifications, api_calls={stats['api_calls']}, "
            f"input_tokens={stats['input_tokens']}, output_tokens={stats['output_tokens']}"
        )

        if args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    report_payload = {
        "generated_at": utc_now_iso(),
        "checkpoint": str(args.checkpoint),
        "dry_run": args.dry_run,
        "model": args.model,
        "stats": checkpoint["stats"],
        "errors": checkpoint["errors"],
    }
    report_name = f"backfill_context_type_{datetime_safe_stamp()}.json"
    report_path = append_report(report_name, report_payload)
    print(f"Report written to {report_path}")
    return 0


def datetime_safe_stamp() -> str:
    return utc_now_iso().replace(":", "").replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
