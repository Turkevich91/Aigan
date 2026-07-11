#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openai import AsyncOpenAI

from memory_extraction import (
    OUTPUT_SCHEMA_VERSION,
    aggregate_report_has_private_fields,
    deterministic_extract,
    evaluate_predictions,
    fixture_case_set_sha256,
    fixture_sha256,
    load_fixture,
    memory_extraction_output_schema,
    provider_model_matches,
    public_model_input,
    validate_prediction,
)
from model_pricing import TokenUsage, estimate_token_cost


PROMPT_PATH = ROOT / "prompts" / "memory_extraction_eval_v4.md"


def load_system_prompt() -> tuple[str, str]:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CallResult:
    prediction: Mapping[str, Any] | None
    actual_model: str
    actual_effort: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    latency_ms: int
    failure: str


def stratified_cases(cases: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or limit >= len(cases):
        return list(cases)
    groups: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for case in cases:
        groups[str(case["tags"][0])].append(case)
    selected: list[dict[str, Any]] = []
    names = deque(sorted(groups))
    while names and len(selected) < limit:
        name = names.popleft()
        if groups[name]:
            selected.append(groups[name].popleft())
        if groups[name]:
            names.append(name)
    return selected


def expanded_cases(cases: Sequence[dict[str, Any]], repeats: int) -> list[tuple[str, dict[str, Any]]]:
    expanded: list[tuple[str, dict[str, Any]]] = []
    for repeat in range(1, repeats + 1):
        for case in cases:
            clone = copy.deepcopy(case)
            original_id = str(case["id"])
            clone["id"] = original_id if repeats == 1 else f"{original_id}_r{repeat}"
            expanded.append((original_id, clone))
    return expanded


def model_matches(requested: str, actual: str) -> bool:
    return provider_model_matches(requested, actual)


def response_effort(response: Any) -> str:
    reasoning = getattr(response, "reasoning", None)
    return str(getattr(reasoning, "effort", "") or "").strip()


def response_usage(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    return input_tokens, cached_tokens, output_tokens


async def evaluate_case(
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    case: Mapping[str, Any],
    *,
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    timeout_seconds: float,
    system_prompt: str,
) -> CallResult:
    started = time.monotonic()
    try:
        request: dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                public_model_input(case),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        }
                    ],
                },
            ],
            "max_output_tokens": max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": OUTPUT_SCHEMA_VERSION,
                    "strict": True,
                    "schema": memory_extraction_output_schema(),
                }
            },
        }
        if reasoning_effort != "omit":
            request["reasoning"] = {"effort": reasoning_effort}
        async with semaphore:
            async with asyncio.timeout(timeout_seconds):
                response = await client.responses.create(**request)
        latency_ms = int((time.monotonic() - started) * 1000)
        status = str(getattr(response, "status", "") or "").casefold()
        actual_model = str(getattr(response, "model", "") or "")
        actual_effort = response_effort(response)
        input_tokens, cached_tokens, output_tokens = response_usage(response)
        if status != "completed":
            return CallResult(
                None,
                actual_model,
                actual_effort,
                input_tokens,
                cached_tokens,
                output_tokens,
                latency_ms,
                "provider_status",
            )
        output_text = str(getattr(response, "output_text", "") or "").strip()
        if not output_text:
            return CallResult(
                None,
                actual_model,
                actual_effort,
                input_tokens,
                cached_tokens,
                output_tokens,
                latency_ms,
                "empty_output",
            )
        try:
            prediction = json.loads(output_text)
        except json.JSONDecodeError:
            prediction = None
            failure = "invalid_json"
        else:
            failure = ""
        return CallResult(
            prediction,
            actual_model,
            actual_effort,
            input_tokens,
            cached_tokens,
            output_tokens,
            latency_ms,
            failure,
        )
    except TimeoutError:
        return CallResult(
            None,
            "",
            "",
            0,
            0,
            0,
            int((time.monotonic() - started) * 1000),
            "timeout",
        )
    except Exception:
        return CallResult(
            None,
            "",
            "",
            0,
            0,
            0,
            int((time.monotonic() - started) * 1000),
            "provider_error",
        )


async def run_api(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    *,
    model: str,
    reasoning_effort: str,
    max_output_tokens: int,
    timeout_seconds: float,
    concurrency: int,
    system_prompt: str,
) -> list[CallResult]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for --mode api")
    semaphore = asyncio.Semaphore(max(1, concurrency))
    async with AsyncOpenAI(
        api_key=api_key,
        timeout=timeout_seconds,
        max_retries=0,
    ) as client:
        tasks = [
            evaluate_case(
                client,
                semaphore,
                case,
                model=model,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
                system_prompt=system_prompt,
            )
            for _original_id, case in jobs
        ]
        return list(await asyncio.gather(*tasks))


def prediction_signature(
    case: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    *,
    full_contract: bool = False,
) -> str:
    if prediction is None:
        return "provider_failure"
    normalized, errors = validate_prediction(case, prediction)
    if errors:
        return "invalid:" + ",".join(errors)
    bounded = []
    for candidate in normalized["candidates"]:
        item = {
                "candidate_type": candidate["candidate_type"],
                "subject_key": candidate["subject_key"],
                "source_role": candidate["source_role"],
                "epistemic": candidate["epistemic"],
                "durability": candidate["durability"],
                "lifecycle": candidate["lifecycle"],
                "evidence_refs": candidate["evidence_refs"],
                "supersedes_row_keys": candidate["supersedes_row_keys"],
                "conflicts_row_keys": candidate["conflicts_row_keys"],
                "valid_from": candidate["valid_from"],
                "valid_until": candidate["valid_until"],
                "evidence_span": candidate["evidence_span"],
                "reason_codes": candidate["reason_codes"],
            }
        if full_contract:
            item["confidence"] = candidate["confidence"]
        bounded.append(item)
    return json.dumps(
        {
            "candidates": bounded,
            "no_candidate_reason": (
                normalized["no_candidate_reason"] if full_contract else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def validation_error_bucket(error: str) -> str:
    parts = str(error).split(":")
    if parts[0].startswith("candidate_") and len(parts) > 1:
        return parts[1]
    if len(parts) > 1:
        return "_".join(parts[:2])
    return parts[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate bounded memory-candidate extraction without persisting payloads."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/memory_extraction_v1.jsonl"),
    )
    parser.add_argument("--mode", choices=("baseline", "api"), default="baseline")
    parser.add_argument(
        "--split",
        choices=("development", "holdout", "all"),
        default="development",
    )
    parser.add_argument("--model", default="gpt-5.4-nano-2026-03-17")
    parser.add_argument(
        "--reasoning-effort",
        choices=("omit", "none", "low", "medium", "high", "xhigh", "max"),
        default="none",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--require-gates", action="store_true")
    args = parser.parse_args()

    if args.repeats < 1 or args.repeats > 5:
        parser.error("--repeats must be between 1 and 5")
    if args.limit < 0:
        parser.error("--limit cannot be negative")
    if args.max_output_tokens < 200:
        parser.error("--max-output-tokens must be at least 200")

    all_cases = load_fixture(args.fixture)
    system_prompt, prompt_hash = load_system_prompt()
    split_cases = (
        all_cases
        if args.split == "all"
        else [case for case in all_cases if case["split"] == args.split]
    )
    if not split_cases:
        raise RuntimeError("selected fixture split is empty")
    selected = stratified_cases(split_cases, args.limit)
    jobs = expanded_cases(selected, args.repeats)
    results: list[CallResult] = []

    if args.mode == "baseline":
        for _original_id, case in jobs:
            prediction = deterministic_extract(case)
            results.append(CallResult(prediction, "deterministic-baseline-v1", "none", 0, 0, 0, 0, ""))
        model = "deterministic-baseline-v1"
        effort = "none"
    else:
        results = asyncio.run(
            run_api(
                jobs,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout_seconds,
                concurrency=args.concurrency,
                system_prompt=system_prompt,
            )
        )
        model = args.model
        effort = args.reasoning_effort

    failures = Counter(result.failure for result in results if result.failure)
    model_mismatches = sum(
        not result.actual_model or not model_matches(args.model, result.actual_model)
        for result in results
        if args.mode == "api"
    )
    effort_mismatches = sum(
        args.reasoning_effort != "omit"
        and (not result.actual_effort or result.actual_effort != args.reasoning_effort)
        for result in results
        if args.mode == "api"
    )
    metadata_missing = sum(
        not result.actual_model
        or (args.reasoning_effort != "omit" and not result.actual_effort)
        for result in results
        if args.mode == "api"
    )
    usages = [
        TokenUsage(
            input_tokens=result.input_tokens,
            cached_input_tokens=result.cached_input_tokens,
            output_tokens=result.output_tokens,
        )
        for result in results
        if result.input_tokens or result.output_tokens
    ]
    usage_metadata_missing = sum(
        result.input_tokens <= 0 or result.output_tokens <= 0
        for result in results
        if args.mode == "api"
    )
    cost = estimate_token_cost(args.model, usages) if args.mode == "api" else None
    behavior_signatures: dict[str, list[str]] = defaultdict(list)
    contract_signatures: dict[str, list[str]] = defaultdict(list)
    all_structured_valid = 0
    repeat_error_counts: Counter[str] = Counter()
    for (original_id, case), result in zip(jobs, results):
        behavior_signatures[original_id].append(
            prediction_signature(case, result.prediction)
        )
        contract_signatures[original_id].append(
            prediction_signature(case, result.prediction, full_contract=True)
        )
        if result.prediction is not None:
            _normalized, validation_errors = validate_prediction(case, result.prediction)
            if not validation_errors:
                all_structured_valid += 1
            else:
                repeat_error_counts.update(
                    validation_error_bucket(error) for error in validation_errors
                )
        else:
            repeat_error_counts["provider_failure"] += 1
    stable = sum(len(set(items)) == 1 for items in behavior_signatures.values())
    contract_stable = sum(
        len(set(items)) == 1 for items in contract_signatures.values()
    )
    stability_rate = stable / len(behavior_signatures) if behavior_signatures else None
    contract_stability_rate = (
        contract_stable / len(contract_signatures) if contract_signatures else None
    )
    all_structured_rate = all_structured_valid / len(jobs) if jobs else 0.0

    quality_predictions = {
        str(case["id"]): results[index].prediction
        for index, case in enumerate(selected)
    }
    report = evaluate_predictions(
        selected,
        quality_predictions,
        fixture_hash=fixture_sha256(args.fixture),
        model=model,
        reasoning_effort=effort,
        prompt_hash=prompt_hash,
        repeat_count=args.repeats,
        stability_rate=stability_rate,
        full_contract_stability_rate=contract_stability_rate,
        evaluation_instance_count=len(jobs),
        evaluation_structured_valid_rate=all_structured_rate,
        provider_model_mismatches=model_mismatches,
        provider_effort_mismatches=effort_mismatches,
        provider_metadata_missing=metadata_missing,
        provider_actual_models=[result.actual_model for result in results],
        provider_actual_efforts=[result.actual_effort for result in results],
        input_tokens=sum(result.input_tokens for result in results),
        cached_input_tokens=sum(result.cached_input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        estimated_cost_nano_usd=cost.nano_usd if cost is not None else 0,
        pricing_status=cost.status if cost is not None else "not_applicable",
        pricing_complete=cost.complete if cost is not None else args.mode == "baseline",
        usage_metadata_missing=usage_metadata_missing,
        pricing_snapshot_version=cost.snapshot_version if cost is not None else None,
        pricing_basis_model=cost.basis_model if cost is not None else None,
        input_rate_nano_usd=cost.input_rate_nano_usd if cost is not None else None,
        cached_input_rate_nano_usd=cost.cached_input_rate_nano_usd if cost is not None else None,
        output_rate_nano_usd=cost.output_rate_nano_usd if cost is not None else None,
        concurrency=args.concurrency if args.mode == "api" else 0,
        timeout_seconds=args.timeout_seconds if args.mode == "api" else 0.0,
        max_output_tokens=args.max_output_tokens if args.mode == "api" else 0,
        store=False,
        latency_ms=[result.latency_ms for result in results],
        failure_counts=failures,
        repeat_error_counts=repeat_error_counts,
    )
    report["run"]["fixture_split"] = args.split
    report["fixture"]["available_split_sha256"] = fixture_case_set_sha256(split_cases)
    report["fixture"]["available_split_case_count"] = len(split_cases)
    report["stability"]["eligible_case_count"] = len(behavior_signatures)
    report["stability"]["candidate_behavior_match_count"] = stable
    report["stability"]["full_contract_match_count"] = contract_stable
    if aggregate_report_has_private_fields(report):
        raise RuntimeError("aggregate report contained a forbidden payload field")
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.require_gates and report["verdict"] != "GO_FOR_RUNTIME_SHADOW_PR":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
