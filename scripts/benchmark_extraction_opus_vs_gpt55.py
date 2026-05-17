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
    parser = argparse.ArgumentParser(description="Benchmark extraction: Claude Opus 4.6 vs GPT-5.5 high")
    parser.add_argument(
        "--filtered-pkl",
        default=str(REPO_ROOT / "distillation" / "checkpoints" / "filtered_conversations.pkl"),
    )
    parser.add_argument("--sample-size", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--claude-model", default=EXTRACTION_MODEL)
    parser.add_argument("--openai-model", default="gpt-5.5")
    parser.add_argument("--openai-reasoning-effort", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=1600)
    parser.add_argument("--request-timeout-s", type=float, default=60.0)
    parser.add_argument("--output-dir", default=str(REPO_ROOT / "scripts" / "reports"))
    return parser.parse_args()


def clean_json_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def load_sample(path: Path, sample_size: int, seed: int) -> list[Any]:
    with path.open("rb") as handle:
        filtered = pickle.load(handle)
    keep = [fc.conversation for fc in filtered if getattr(fc, "should_keep", False)]
    if not keep:
        raise RuntimeError("No keep-worthy conversations found.")
    if len(keep) <= sample_size:
        return keep
    return random.Random(seed).sample(keep, sample_size)


def run_gpt55_json(
    *,
    client: OpenAI,
    model: str,
    prompt: str,
    max_output_tokens: int,
    timeout_s: float,
    reasoning_effort: str,
) -> tuple[dict[str, Any], int, int]:
    response = client.responses.create(
        model=model,
        input=prompt,
        reasoning={"effort": reasoning_effort},
        text={"format": {"type": "json_object"}},
        max_output_tokens=max_output_tokens,
        timeout=timeout_s,
    )
    text = response.output_text or ""
    parsed = json.loads(clean_json_text(text))
    usage = response.usage
    input_tokens = int(usage.input_tokens) if usage and usage.input_tokens is not None else 0
    output_tokens = int(usage.output_tokens) if usage and usage.output_tokens is not None else 0
    return parsed, input_tokens, output_tokens


def execute_claude(conversation: Any, model: str, max_output_tokens: int) -> ModelRunResult:
    prompt = build_extraction_prompt(conversation)
    t0 = time.perf_counter()
    try:
        data, input_tokens, output_tokens = call_claude_json(
            prompt,
            model=model,
            max_tokens=max_output_tokens,
        )
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
        return ModelRunResult(
            success=False,
            latency_s=time.perf_counter() - t0,
            input_tokens=0,
            output_tokens=0,
            knowledge_entries=0,
            project_entries=0,
            validation_error_count=0,
            validation_errors=[],
            error=str(exc),
        )


def execute_gpt55(
    conversation: Any,
    client: OpenAI,
    model: str,
    max_output_tokens: int,
    timeout_s: float,
    reasoning_effort: str,
) -> ModelRunResult:
    prompt = build_extraction_prompt(conversation)
    t0 = time.perf_counter()
    try:
        data, input_tokens, output_tokens = run_gpt55_json(
            client=client,
            model=model,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
            reasoning_effort=reasoning_effort,
        )
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
        return ModelRunResult(
            success=False,
            latency_s=time.perf_counter() - t0,
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


def score(r: ModelRunResult) -> float:
    if not r.success:
        return -1000.0
    return 100.0 - (5.0 * r.validation_error_count) + (2.0 * (r.knowledge_entries + r.project_entries))


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    c = report["summary"]["claude"]
    g = report["summary"]["gpt55"]
    w = report["summary"]["head_to_head_wins"]
    lines = [
        f"# Extraction Benchmark: {report['models']['claude']} vs {report['models']['gpt55']} ({report['models']['gpt55_reasoning_effort']})",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Sample size: `{report['sample_size']}`",
        "",
        "| Metric | Claude | GPT-5.5 |",
        "|---|---:|---:|",
        f"| Success rate | {c['success_rate']:.2%} | {g['success_rate']:.2%} |",
        f"| Avg latency (s) | {c['avg_latency_s']:.2f} | {g['avg_latency_s']:.2f} |",
        f"| Avg input tokens | {c['avg_input_tokens_success_only']:.1f} | {g['avg_input_tokens_success_only']:.1f} |",
        f"| Avg output tokens | {c['avg_output_tokens_success_only']:.1f} | {g['avg_output_tokens_success_only']:.1f} |",
        f"| Avg knowledge entries | {c['avg_knowledge_entries_success_only']:.2f} | {g['avg_knowledge_entries_success_only']:.2f} |",
        f"| Avg project entries | {c['avg_project_entries_success_only']:.2f} | {g['avg_project_entries_success_only']:.2f} |",
        f"| Avg validation errors | {c['avg_validation_errors_success_only']:.2f} | {g['avg_validation_errors_success_only']:.2f} |",
        "",
        f"- Claude wins: `{w['claude']}`",
        f"- GPT-5.5 wins: `{w['gpt55']}`",
        f"- Ties: `{w['tie']}`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY is required.")

    sample = load_sample(Path(args.filtered_pkl), args.sample_size, args.seed)
    gpt_client = OpenAI(api_key=openai_key)

    claude_results: list[ModelRunResult] = []
    gpt_results: list[ModelRunResult] = []
    per_conversation: list[dict[str, Any]] = []
    wins = {"claude": 0, "gpt55": 0, "tie": 0}

    for conv in sample:
        print(f"Running conversation {conv.id} ({conv.source})...", flush=True)
        prompt_tokens = count_tokens(build_extraction_prompt(conv))

        c = execute_claude(conv, args.claude_model, args.max_output_tokens)
        g = execute_gpt55(
            conv,
            gpt_client,
            args.openai_model,
            args.max_output_tokens,
            args.request_timeout_s,
            args.openai_reasoning_effort,
        )
        claude_results.append(c)
        gpt_results.append(g)

        cs = score(c)
        gs = score(g)
        if cs > gs:
            winner = "claude"
            wins["claude"] += 1
        elif gs > cs:
            winner = "gpt55"
            wins["gpt55"] += 1
        else:
            winner = "tie"
            wins["tie"] += 1

        per_conversation.append(
            {
                "conversation_id": conv.id,
                "title": conv.title,
                "source": conv.source,
                "message_count": conv.message_count,
                "prompt_tokens_estimate": prompt_tokens,
                "winner": winner,
                "claude": c.__dict__,
                "gpt55": g.__dict__,
            }
        )
        print(f"  done | winner={winner} | claude_ok={c.success} gpt55_ok={g.success}", flush=True)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset": str(Path(args.filtered_pkl).resolve()),
        "sample_size": len(sample),
        "seed": args.seed,
        "models": {
            "claude": args.claude_model,
            "gpt55": args.openai_model,
            "gpt55_reasoning_effort": args.openai_reasoning_effort,
        },
        "summary": {
            "claude": aggregate(claude_results),
            "gpt55": aggregate(gpt_results),
            "head_to_head_wins": wins,
        },
        "per_conversation": per_conversation,
    }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"extraction_opus_vs_gpt55_{stamp}.json"
    md_path = out_dir / f"extraction_opus_vs_gpt55_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_markdown(md_path, report)

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
