#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
DISTILLATION_ROOT = REPO_ROOT / "distillation"
if str(DISTILLATION_ROOT) not in sys.path:
    sys.path.insert(0, str(DISTILLATION_ROOT))

from pipeline.extract import validate_extraction  # noqa: E402
from prompts.extraction import build_extraction_prompt  # noqa: E402
from utils.llm import call_claude_json, count_tokens  # noqa: E402


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL = f"{OPENROUTER_BASE_URL}/models"

DEFAULT_MODELS: tuple[tuple[str, str], ...] = (
    ("deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
    ("glm5.2", "z-ai/glm-5.2"),
    ("kimi-2.7-code", "moonshotai/kimi-k2.7-code"),
    ("qwen3.7-plus", "qwen/qwen3.7-plus"),
)


@dataclass(frozen=True)
class ModelSpec:
    alias: str
    model_id: str
    provider: str = "openrouter"


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
    score: float
    attempt_count: int
    json_mode_used: bool
    text_fallback_used: bool
    error: str | None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark OpenRouter challenger models against the PKS extraction "
            "prompt and validator. This is report-only and never writes memory storage."
        ),
    )
    parser.add_argument(
        "--filtered-pkl",
        default=str(REPO_ROOT / "distillation" / "checkpoints" / "filtered_conversations.pkl"),
        help="Path to filtered_conversations.pkl",
    )
    parser.add_argument("--sample-size", type=int, default=4, help="How many keep-worthy conversations to test")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed")
    parser.add_argument(
        "--include-sonnet",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include the current production Claude Sonnet baseline in the same-sample benchmark",
    )
    parser.add_argument(
        "--sonnet-model",
        default="sonnet",
        help="Claude Sonnet model to use when --include-sonnet is enabled",
    )
    parser.add_argument(
        "--sonnet-route",
        choices=("claude-cli", "anthropic-api"),
        default="claude-cli",
        help="How to run the Sonnet baseline. claude-cli uses subscription auth and scrubs stale API keys.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help=(
            "Model to test as alias=model_id or model_id. Can be passed more than once. "
            "Defaults to DeepSeek V4 Pro, GLM 5.2, Kimi K2.7 Code, and Qwen3.7 Plus."
        ),
    )
    parser.add_argument(
        "--models",
        default="",
        help="Comma-separated model specs as alias=model_id or model_id. Added after --model values.",
    )
    parser.add_argument(
        "--no-default-openrouter-models",
        action="store_true",
        help="Do not include the default OpenRouter challenger set when no --model/--models values are provided",
    )
    parser.add_argument("--max-output-tokens", type=int, default=2500, help="Max output tokens per model call")
    parser.add_argument("--request-timeout-s", type=float, default=120.0, help="Per-request timeout")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    parser.add_argument("--max-attempts", type=int, default=2, help="Max attempts per model per conversation")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load the sample, build prompts, count tokens, and fetch model metadata without model calls",
    )
    parser.add_argument(
        "--allow-text-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retry without JSON mode when JSON-mode calls fail or return unparsable text",
    )
    parser.add_argument(
        "--list-openrouter-models",
        action="store_true",
        help="Fetch OpenRouter model metadata for the configured/default model set, then exit",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "scripts" / "reports"),
        help="Directory for JSON/MD benchmark artifacts",
    )
    return parser.parse_args()


def parse_model_specs(model_args: list[str], models_arg: str, *, use_defaults: bool = True) -> list[ModelSpec]:
    raw_specs: list[str] = []
    raw_specs.extend(model_args)
    if models_arg.strip():
        raw_specs.extend(part.strip() for part in models_arg.split(",") if part.strip())

    if not raw_specs and use_defaults:
        return [ModelSpec(alias=alias, model_id=model_id) for alias, model_id in DEFAULT_MODELS]
    if not raw_specs:
        return []

    specs: list[ModelSpec] = []
    seen_aliases: set[str] = set()
    seen_ids: set[str] = set()
    for raw in raw_specs:
        if "=" in raw:
            alias, model_id = raw.split("=", 1)
            alias = alias.strip()
            model_id = model_id.strip()
        else:
            model_id = raw.strip()
            alias = model_id.split("/")[-1]
        if not alias or not model_id:
            raise ValueError(f"Invalid model spec: {raw!r}")
        if alias in seen_aliases:
            raise ValueError(f"Duplicate model alias: {alias}")
        if model_id in seen_ids:
            raise ValueError(f"Duplicate model id: {model_id}")
        seen_aliases.add(alias)
        seen_ids.add(model_id)
        specs.append(ModelSpec(alias=alias, model_id=model_id))
    return specs


def fetch_openrouter_model_metadata() -> dict[str, dict[str, Any]]:
    with urllib.request.urlopen(OPENROUTER_MODELS_URL, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return {item.get("id", ""): item for item in payload.get("data", []) if item.get("id")}


def model_metadata_summary(specs: list[ModelSpec]) -> dict[str, Any]:
    metadata = fetch_openrouter_model_metadata()
    summary: dict[str, Any] = {}
    for spec in specs:
        if spec.provider == "anthropic":
            summary[spec.alias] = {
                "model_id": spec.model_id,
                "available": True,
                "name": spec.model_id,
                "provider": "anthropic",
            }
            continue
        item = metadata.get(spec.model_id)
        if not item:
            summary[spec.alias] = {"model_id": spec.model_id, "available": False, "provider": spec.provider}
            continue
        summary[spec.alias] = {
            "model_id": spec.model_id,
            "available": True,
            "name": item.get("name"),
            "provider": spec.provider,
            "context_length": item.get("context_length"),
            "architecture": item.get("architecture"),
            "pricing": item.get("pricing"),
        }
    return summary


def load_conversation_sample(path: Path, sample_size: int, seed: int) -> list[Any]:
    if sample_size < 1:
        raise ValueError("--sample-size must be at least 1")
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


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = clean_json_text(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("Model response parsed as JSON, but not as an object.")
    return parsed


def openrouter_client(api_key: str) -> OpenAI:
    return OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "https://github.com/arjundivecha/personal-knowledge-system",
            "X-Title": "PKS extraction model evaluator",
        },
    )


def load_openrouter_key_from_repo_env() -> None:
    if os.getenv("OPENROUTER_API_KEY"):
        return
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or not line.startswith("OPENROUTER_API_KEY="):
                continue
            _name, raw_value = line.split("=", 1)
            value = raw_value.strip().strip('"').strip("'")
            if value:
                os.environ["OPENROUTER_API_KEY"] = value
            return


def call_openrouter_json(
    *,
    client: OpenAI,
    model_id: str,
    prompt: str,
    max_output_tokens: int,
    timeout_s: float,
    temperature: float,
    use_json_mode: bool,
) -> tuple[dict[str, Any], int, int, str]:
    kwargs: dict[str, Any] = {
        "model": model_id,
        "messages": [
            {
                "role": "system",
                "content": "Return only a valid JSON object matching the requested schema. Do not use markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_output_tokens,
        "timeout": timeout_s,
    }
    if use_json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content or ""
    parsed = parse_json_response(content)
    usage = response.usage
    input_tokens = int(usage.prompt_tokens) if usage and usage.prompt_tokens is not None else 0
    output_tokens = int(usage.completion_tokens) if usage and usage.completion_tokens is not None else 0
    return parsed, input_tokens, output_tokens, content


def call_claude_cli_json(
    *,
    prompt: str,
    model: str,
    timeout_s: float,
) -> tuple[dict[str, Any], int, int]:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("Claude CLI not found on PATH")
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        [claude_bin, "--print", "--model", model],
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(f"Claude CLI failed with exit {result.returncode}: {(stderr or stdout)[:500]}")
    parsed = parse_json_response(result.stdout or "")
    return parsed, count_tokens(prompt), count_tokens(result.stdout or "")


def score_result(result: ModelRunResult) -> float:
    if not result.success:
        return -1000.0
    entry_count = result.knowledge_entries + result.project_entries
    retry_penalty = max(0, result.attempt_count - 1) * 2.0
    fallback_penalty = 2.0 if result.text_fallback_used else 0.0
    return 100.0 + (entry_count * 2.0) - (result.validation_error_count * 10.0) - retry_penalty - fallback_penalty


def execute_model_run(
    *,
    client: OpenAI | None,
    spec: ModelSpec,
    conversation: Any,
    max_output_tokens: int,
    request_timeout_s: float,
    temperature: float,
    max_attempts: int,
    allow_text_fallback: bool,
) -> ModelRunResult:
    prompt = build_extraction_prompt(conversation)
    if spec.provider == "anthropic":
        t0 = time.perf_counter()
        try:
            if spec.model_id.startswith("claude-cli:"):
                data, input_tokens, output_tokens = call_claude_cli_json(
                    prompt=prompt,
                    model=spec.model_id.removeprefix("claude-cli:"),
                    timeout_s=request_timeout_s,
                )
            else:
                data, input_tokens, output_tokens = call_claude_json(
                    prompt=prompt,
                    model=spec.model_id,
                    max_tokens=max_output_tokens,
                    temperature=temperature,
                )
            latency = time.perf_counter() - t0
            validation_errors = validate_extraction(data, conversation)
            result = ModelRunResult(
                success=True,
                latency_s=latency,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                knowledge_entries=len(data.get("knowledge_entries", [])),
                project_entries=len(data.get("project_entries", [])),
                validation_error_count=len(validation_errors),
                validation_errors=validation_errors[:10],
                score=0.0,
                attempt_count=1,
                json_mode_used=False,
                text_fallback_used=False,
                error=None,
            )
            result.score = score_result(result)
            return result
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
                score=-1000.0,
                attempt_count=1,
                json_mode_used=False,
                text_fallback_used=False,
                error=str(exc),
            )

    if client is None:
        raise RuntimeError("OpenRouter client is required for OpenRouter model runs")

    attempts: list[tuple[bool, str]] = [(True, "json-mode")]
    if allow_text_fallback:
        attempts.append((False, "text-fallback"))
    attempts = attempts[: max(1, max_attempts)]

    first_error: str | None = None
    t0 = time.perf_counter()
    for attempt_index, (use_json_mode, _label) in enumerate(attempts, start=1):
        try:
            data, input_tokens, output_tokens, _raw_content = call_openrouter_json(
                client=client,
                model_id=spec.model_id,
                prompt=prompt,
                max_output_tokens=max_output_tokens,
                timeout_s=request_timeout_s,
                temperature=temperature,
                use_json_mode=use_json_mode,
            )
            latency = time.perf_counter() - t0
            validation_errors = validate_extraction(data, conversation)
            result = ModelRunResult(
                success=True,
                latency_s=latency,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                knowledge_entries=len(data.get("knowledge_entries", [])),
                project_entries=len(data.get("project_entries", [])),
                validation_error_count=len(validation_errors),
                validation_errors=validation_errors[:10],
                score=0.0,
                attempt_count=attempt_index,
                json_mode_used=use_json_mode,
                text_fallback_used=not use_json_mode,
                error=None,
            )
            result.score = score_result(result)
            return result
        except Exception as exc:
            if first_error is None:
                first_error = str(exc)
            continue

    latency = time.perf_counter() - t0
    result = ModelRunResult(
        success=False,
        latency_s=latency,
        input_tokens=0,
        output_tokens=0,
        knowledge_entries=0,
        project_entries=0,
        validation_error_count=0,
        validation_errors=[],
        score=-1000.0,
        attempt_count=len(attempts),
        json_mode_used=any(use_json for use_json, _label in attempts),
        text_fallback_used=any(not use_json for use_json, _label in attempts),
        error=first_error or "Unknown model failure",
    )
    return result


def aggregate(results: list[ModelRunResult]) -> dict[str, Any]:
    successes = [r for r in results if r.success]
    return {
        "total_runs": len(results),
        "success_count": len(successes),
        "success_rate": (len(successes) / len(results)) if results else 0.0,
        "avg_score": statistics.mean([r.score for r in results]) if results else 0.0,
        "avg_latency_s": statistics.mean([r.latency_s for r in results]) if results else 0.0,
        "avg_input_tokens_success_only": statistics.mean([r.input_tokens for r in successes]) if successes else 0.0,
        "avg_output_tokens_success_only": statistics.mean([r.output_tokens for r in successes]) if successes else 0.0,
        "avg_knowledge_entries_success_only": statistics.mean([r.knowledge_entries for r in successes]) if successes else 0.0,
        "avg_project_entries_success_only": statistics.mean([r.project_entries for r in successes]) if successes else 0.0,
        "avg_validation_errors_success_only": statistics.mean([r.validation_error_count for r in successes]) if successes else 0.0,
        "zero_validation_error_rate_success_only": (
            sum(1 for r in successes if r.validation_error_count == 0) / len(successes)
        )
        if successes
        else 0.0,
        "text_fallback_count": sum(1 for r in results if r.text_fallback_used),
        "errors": [r.error for r in results if r.error],
    }


def ranked_aliases(summary: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        summary,
        key=lambda alias: (
            summary[alias]["success_rate"],
            summary[alias]["zero_validation_error_rate_success_only"],
            summary[alias]["avg_score"],
            -summary[alias]["avg_latency_s"],
        ),
        reverse=True,
    )


def write_report_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    ranked = report["ranking"]
    lines = [
        "# OpenRouter Extraction Model Benchmark",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Dataset source: `{report['dataset']}`",
        f"- Sample size: `{report['sample_size']}`",
        f"- Seed: `{report['seed']}`",
        f"- Prompt: `distillation/prompts/extraction.py`",
        f"- Validator: `distillation/pipeline/extract.py::validate_extraction`",
        "- Storage writes: `none`",
        "",
        "## Ranking",
        "",
        "| Rank | Alias | Provider | Model ID | Success | Zero-error | Avg score | Avg latency | Avg entries | Fallbacks | Wins |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for idx, alias in enumerate(ranked, start=1):
        row = summary[alias]
        entries = row["avg_knowledge_entries_success_only"] + row["avg_project_entries_success_only"]
        lines.append(
            "| "
            f"{idx} | {alias} | {report['providers'][alias]} | `{report['models'][alias]}` | "
            f"{row['success_rate']:.0%} | "
            f"{row['zero_validation_error_rate_success_only']:.0%} | "
            f"{row['avg_score']:.1f} | "
            f"{row['avg_latency_s']:.1f}s | "
            f"{entries:.1f} | "
            f"{row['text_fallback_count']} | "
            f"{report['wins'].get(alias, 0)} |"
        )

    lines.extend(
        [
            "",
            "## Model Metadata",
            "",
            "| Alias | Provider | Available | Context | Model name |",
            "|---|---|---:|---:|---|",
        ],
    )
    for alias, metadata in report["model_metadata"].items():
        lines.append(
            f"| {alias} | {metadata.get('provider', '')} | {metadata.get('available')} | "
            f"{metadata.get('context_length', '') or ''} | {metadata.get('name', '') or ''} |"
        )

    lines.extend(
        [
            "",
            "## Per Conversation",
            "",
            "| Conversation | Winner | Prompt tokens | " + " | ".join(ranked) + " |",
            "|---|---:|---:|" + "|".join("---:" for _ in ranked) + "|",
        ],
    )
    for row in report["per_conversation"]:
        scores = " | ".join(f"{row['results'][alias]['score']:.1f}" for alias in ranked)
        lines.append(f"| `{row['conversation_id']}` | {row['winner']} | {row['prompt_tokens']} | {scores} |")

    lines.extend(["", "## Errors"])
    any_error = False
    for alias in ranked:
        errors = summary[alias]["errors"]
        if errors:
            any_error = True
            lines.append("")
            lines.append(f"### {alias}")
            for error in errors[:5]:
                lines.append(f"- `{error[:300]}`")
    if not any_error:
        lines.append("")
        lines.append("No request-level errors.")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    openrouter_specs = parse_model_specs(
        args.model,
        args.models,
        use_defaults=not args.no_default_openrouter_models,
    )
    specs: list[ModelSpec] = []
    if args.include_sonnet:
        sonnet_model_id = args.sonnet_model
        if args.sonnet_route == "claude-cli":
            sonnet_model_id = f"claude-cli:{sonnet_model_id}"
        specs.append(ModelSpec(alias="sonnet", model_id=sonnet_model_id, provider="anthropic"))
    specs.extend(openrouter_specs)
    if not specs:
        raise ValueError("No models selected.")

    if args.list_openrouter_models:
        print(json.dumps(model_metadata_summary(specs), indent=2, sort_keys=True))
        return 0

    filtered_path = Path(args.filtered_pkl)
    if not filtered_path.exists():
        raise FileNotFoundError(f"Filtered checkpoint not found: {filtered_path}")

    sample = load_conversation_sample(filtered_path, args.sample_size, args.seed)
    metadata = model_metadata_summary(specs)

    if args.dry_run:
        payload = {
            "status": "dry_run",
            "generated_at": utc_now_iso(),
            "dataset": str(filtered_path),
            "sample_size": len(sample),
            "seed": args.seed,
            "models": {spec.alias: spec.model_id for spec in specs},
            "model_metadata": metadata,
            "sample": [
                {
                    "conversation_id": conversation.id,
                    "source": conversation.source,
                    "prompt_tokens": count_tokens(build_extraction_prompt(conversation)),
                }
                for conversation in sample
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    load_openrouter_key_from_repo_env()
    needs_openrouter = any(spec.provider == "openrouter" for spec in specs)
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if needs_openrouter and not api_key:
        print(
            "OPENROUTER_API_KEY is required in the environment for live model calls. "
            "Run with --dry-run for non-credential validation.",
            file=sys.stderr,
        )
        return 2

    client = openrouter_client(api_key) if needs_openrouter else None

    results_by_alias: dict[str, list[ModelRunResult]] = {spec.alias: [] for spec in specs}
    per_conversation: list[dict[str, Any]] = []
    wins: dict[str, int] = {spec.alias: 0 for spec in specs}
    wins["tie"] = 0

    for conversation in sample:
        print(f"Running conversation {conversation.id} ({conversation.source})...", flush=True)
        prompt = build_extraction_prompt(conversation)
        prompt_tokens = count_tokens(prompt)
        row_results: dict[str, Any] = {}
        best_aliases: list[str] = []
        best_score: float | None = None

        for spec in specs:
            print(f"  - {spec.alias}: {spec.model_id}", flush=True)
            result = execute_model_run(
                client=client,
                spec=spec,
                conversation=conversation,
                max_output_tokens=args.max_output_tokens,
                request_timeout_s=args.request_timeout_s,
                temperature=args.temperature,
                max_attempts=args.max_attempts,
                allow_text_fallback=args.allow_text_fallback,
            )
            results_by_alias[spec.alias].append(result)
            row_results[spec.alias] = asdict(result)
            if best_score is None or result.score > best_score:
                best_score = result.score
                best_aliases = [spec.alias]
            elif result.score == best_score:
                best_aliases.append(spec.alias)

        if len(best_aliases) == 1:
            wins[best_aliases[0]] += 1
            winner = best_aliases[0]
        else:
            wins["tie"] += 1
            winner = "tie"

        per_conversation.append(
            {
                "conversation_id": conversation.id,
                "source": conversation.source,
                "prompt_tokens": prompt_tokens,
                "winner": winner,
                "results": row_results,
            },
        )

    summary = {alias: aggregate(results) for alias, results in results_by_alias.items()}
    report = {
        "generated_at": utc_now_iso(),
        "dataset": str(filtered_path),
        "sample_size": len(sample),
        "seed": args.seed,
        "models": {spec.alias: spec.model_id for spec in specs},
        "providers": {spec.alias: spec.provider for spec in specs},
        "model_metadata": metadata,
        "ranking": ranked_aliases(summary),
        "wins": wins,
        "summary": summary,
        "per_conversation": per_conversation,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"openrouter_extraction_model_benchmark_{stamp}.json"
    md_path = output_dir / f"openrouter_extraction_model_benchmark_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    write_report_markdown(md_path, report)

    print(f"Wrote JSON report: {json_path}", flush=True)
    print(f"Wrote Markdown report: {md_path}", flush=True)
    print(json.dumps({"ranking": report["ranking"], "wins": wins, "summary": summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
