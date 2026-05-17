#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from config import EXTRACTION_MODEL  # noqa: E402
from pipeline.extract import validate_extraction  # noqa: E402
from prompts.extraction import build_extraction_prompt  # noqa: E402
from utils.llm import call_claude_json, count_tokens  # noqa: E402


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ModelRunResult:
    success: bool
    latency_s: float
    input_tokens: int
    output_tokens: int
    knowledge_entries: int
    project_entries: int
    validation_error_count: int
    validation_errors: list[str]
    error: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Side-by-side extraction benchmark for Claude Opus and DeepSeek.",
    )
    parser.add_argument(
        "--filtered-pkl",
        default=str(REPO_ROOT / "distillation" / "checkpoints" / "filtered_conversations.pkl"),
        help="Path to filtered_conversations.pkl",
    )
    parser.add_argument("--sample-size", type=int, default=12, help="How many keep-worthy conversations to compare")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed")
    parser.add_argument("--claude-model", default=EXTRACTION_MODEL, help="Claude model name")
    parser.add_argument("--deepseek-model", default="deepseek-v4-pro", help="DeepSeek model name")
    parser.add_argument("--deepseek-base-url", default="https://api.deepseek.com", help="DeepSeek OpenAI-compatible base URL")
    parser.add_argument("--max-output-tokens", type=int, default=2500, help="Max output tokens per model call")
    parser.add_argument("--request-timeout-s", type=float, default=90.0, help="Per-request timeout for DeepSeek calls")
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "scripts" / "reports"),
        help="Directory for JSON/MD benchmark artifacts",
    )
    return parser.parse_args()


def load_conversation_sample(path: Path, sample_size: int, seed: int) -> list[Any]:
    with path.open("rb") as handle:
        filtered = pickle.load(handle)
    keep = [fc.conversation for fc in filtered if getattr(fc, "should_keep", False)]
    if not keep:
        raise RuntimeError("No keep-worthy conversations found in filtered checkpoint.")
    if len(keep) <= sample_size:
        return keep
    rng = random.Random(seed)
    return rng.sample(keep, sample_size)


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def run_deepseek_json(
    *,
    client: OpenAI,
    model: str,
    prompt: str,
    max_output_tokens: int,
    timeout_s: float,
) -> tuple[dict[str, Any], int, int]:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=max_output_tokens,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
        timeout=timeout_s,
    )
    content = response.choices[0].message.content or ""
    parsed = json.loads(clean_json_text(content))
    usage = response.usage
    input_tokens = int(usage.prompt_tokens) if usage and usage.prompt_tokens is not None else 0
    output_tokens = int(usage.completion_tokens) if usage and usage.completion_tokens is not None else 0
    return parsed, input_tokens, output_tokens


def execute_model_run(
    *,
    provider: str,
    model: str,
    conversation: Any,
    max_output_tokens: int,
    request_timeout_s: float,
    deepseek_client: OpenAI | None = None,
) -> ModelRunResult:
    prompt = build_extraction_prompt(conversation)
    t0 = time.perf_counter()
    try:
        if provider == "claude":
            data, input_tokens, output_tokens = call_claude_json(
                prompt,
                model=model,
                max_tokens=max_output_tokens,
            )
        elif provider == "deepseek":
            if deepseek_client is None:
                raise RuntimeError("DeepSeek client not initialized")
            data, input_tokens, output_tokens = run_deepseek_json(
                client=deepseek_client,
                model=model,
                prompt=prompt,
                max_output_tokens=max_output_tokens,
                timeout_s=request_timeout_s,
            )
        else:
            raise RuntimeError(f"Unknown provider: {provider}")
        latency = time.perf_counter() - t0
        validation_errors = validate_extraction(data, conversation)
        return ModelRunResult(
            success=True,
            latency_s=latency,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            knowledge_entries=len(data.get("knowledge_entries", [])),
            project_entries=len(data.get("project_entries", [])),
            validation_error_count=len(validation_errors),
            validation_errors=validation_errors[:10],
            error=None,
        )
    except Exception as exc:
        latency = time.perf_counter() - t0
        return ModelRunResult(
            success=False,
            latency_s=latency,
            input_tokens=0,
            output_tokens=0,
            knowledge_entries=0,
            project_entries=0,
            validation_error_count=0,
            validation_errors=[],
            error=str(exc),
        )


def aggregate(results: list[ModelRunResult]) -> dict[str, Any]:
    successes = [r for r in results if r.success]
    return {
        "total_runs": len(results),
        "success_count": len(successes),
        "success_rate": (len(successes) / len(results)) if results else 0.0,
        "avg_latency_s": statistics.mean([r.latency_s for r in results]) if results else 0.0,
        "avg_input_tokens_success_only": statistics.mean([r.input_tokens for r in successes]) if successes else 0.0,
        "avg_output_tokens_success_only": statistics.mean([r.output_tokens for r in successes]) if successes else 0.0,
        "avg_knowledge_entries_success_only": statistics.mean([r.knowledge_entries for r in successes]) if successes else 0.0,
        "avg_project_entries_success_only": statistics.mean([r.project_entries for r in successes]) if successes else 0.0,
        "avg_validation_errors_success_only": statistics.mean([r.validation_error_count for r in successes]) if successes else 0.0,
        "zero_validation_error_rate_success_only": (
            sum(1 for r in successes if r.validation_error_count == 0) / len(successes)
        ) if successes else 0.0,
        "errors": [r.error for r in results if r.error],
    }


def score(result: ModelRunResult) -> float:
    if not result.success:
        return -1000.0
    return (100.0 - (result.validation_error_count * 5.0) + ((result.knowledge_entries + result.project_entries) * 2.0))


def write_report_markdown(path: Path, report: dict[str, Any]) -> None:
    claude = report["summary"]["claude"]
    deepseek = report["summary"]["deepseek"]
    wins = report["summary"]["head_to_head_wins"]
    lines = [
        f"# Extraction Benchmark: {report['models']['claude']} vs {report['models']['deepseek']}",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Sample size: `{report['sample_size']}`",
        f"- Dataset source: `{report['dataset']}`",
        "",
        "## Summary",
        "",
        "| Metric | Claude | DeepSeek |",
        "|---|---:|---:|",
        f"| Success rate | {claude['success_rate']:.2%} | {deepseek['success_rate']:.2%} |",
        f"| Avg latency (s) | {claude['avg_latency_s']:.2f} | {deepseek['avg_latency_s']:.2f} |",
        f"| Avg input tokens (success only) | {claude['avg_input_tokens_success_only']:.1f} | {deepseek['avg_input_tokens_success_only']:.1f} |",
        f"| Avg output tokens (success only) | {claude['avg_output_tokens_success_only']:.1f} | {deepseek['avg_output_tokens_success_only']:.1f} |",
        f"| Avg knowledge entries | {claude['avg_knowledge_entries_success_only']:.2f} | {deepseek['avg_knowledge_entries_success_only']:.2f} |",
        f"| Avg project entries | {claude['avg_project_entries_success_only']:.2f} | {deepseek['avg_project_entries_success_only']:.2f} |",
        f"| Avg validation errors | {claude['avg_validation_errors_success_only']:.2f} | {deepseek['avg_validation_errors_success_only']:.2f} |",
        f"| Zero validation-error rate | {claude['zero_validation_error_rate_success_only']:.2%} | {deepseek['zero_validation_error_rate_success_only']:.2%} |",
        "",
        "## Head-to-Head Wins",
        "",
        f"- Claude wins: `{wins['claude']}`",
        f"- DeepSeek wins: `{wins['deepseek']}`",
        f"- Ties: `{wins['tie']}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    deepseek_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not deepseek_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required in environment.")

    filtered_path = Path(args.filtered_pkl)
    if not filtered_path.exists():
        raise FileNotFoundError(f"Filtered checkpoint not found: {filtered_path}")

    sample = load_conversation_sample(filtered_path, args.sample_size, args.seed)

    deepseek_client = OpenAI(api_key=deepseek_key, base_url=args.deepseek_base_url.rstrip("/"))

    per_conversation: list[dict[str, Any]] = []
    claude_results: list[ModelRunResult] = []
    deepseek_results: list[ModelRunResult] = []
    wins = {"claude": 0, "deepseek": 0, "tie": 0}

    for conversation in sample:
        print(f"Running conversation {conversation.id} ({conversation.source})...", flush=True)
        prompt = build_extraction_prompt(conversation)
        prompt_tokens = count_tokens(prompt)

        claude = execute_model_run(
            provider="claude",
            model=args.claude_model,
            conversation=conversation,
            max_output_tokens=args.max_output_tokens,
            request_timeout_s=args.request_timeout_s,
        )
        deepseek = execute_model_run(
            provider="deepseek",
            model=args.deepseek_model,
            conversation=conversation,
            max_output_tokens=args.max_output_tokens,
            request_timeout_s=args.request_timeout_s,
            deepseek_client=deepseek_client,
        )
        claude_results.append(claude)
        deepseek_results.append(deepseek)

        claude_score = score(claude)
        deepseek_score = score(deepseek)
        if claude_score > deepseek_score:
            winner = "claude"
            wins["claude"] += 1
        elif deepseek_score > claude_score:
            winner = "deepseek"
            wins["deepseek"] += 1
        else:
            winner = "tie"
            wins["tie"] += 1

        per_conversation.append(
            {
                "conversation_id": conversation.id,
                "title": conversation.title,
                "source": conversation.source,
                "message_count": conversation.message_count,
                "prompt_tokens_estimate": prompt_tokens,
                "winner": winner,
                "claude": {
                    "success": claude.success,
                    "latency_s": claude.latency_s,
                    "input_tokens": claude.input_tokens,
                    "output_tokens": claude.output_tokens,
                    "knowledge_entries": claude.knowledge_entries,
                    "project_entries": claude.project_entries,
                    "validation_error_count": claude.validation_error_count,
                    "validation_errors": claude.validation_errors,
                    "error": claude.error,
                },
                "deepseek": {
                    "success": deepseek.success,
                    "latency_s": deepseek.latency_s,
                    "input_tokens": deepseek.input_tokens,
                    "output_tokens": deepseek.output_tokens,
                    "knowledge_entries": deepseek.knowledge_entries,
                    "project_entries": deepseek.project_entries,
                    "validation_error_count": deepseek.validation_error_count,
                    "validation_errors": deepseek.validation_errors,
                    "error": deepseek.error,
                },
            }
        )
        print(
            f"  done | winner={winner} | claude_ok={claude.success} deepseek_ok={deepseek.success}",
            flush=True,
        )

    report = {
        "generated_at": utc_now_iso(),
        "dataset": str(filtered_path),
        "sample_size": len(sample),
        "seed": args.seed,
        "models": {
            "claude": args.claude_model,
            "deepseek": args.deepseek_model,
        },
        "summary": {
            "claude": aggregate(claude_results),
            "deepseek": aggregate(deepseek_results),
            "head_to_head_wins": wins,
        },
        "per_conversation": per_conversation,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"extraction_model_benchmark_{stamp}.json"
    md_path = output_dir / f"extraction_model_benchmark_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_report_markdown(md_path, report)

    print(json.dumps(
        {
            "ok": True,
            "json_report": str(json_path),
            "markdown_report": str(md_path),
            "summary": report["summary"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
