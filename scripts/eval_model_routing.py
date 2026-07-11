from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_pricing import TokenUsage, estimate_token_cost  # noqa: E402
from model_routing import (  # noqa: E402
    MODEL_POLICY_ROUTER_SYSTEM_PROMPT,
    MODEL_TIERS,
    model_routing_schema,
    normalize_model_routing_decision,
)


DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "model_routing_v1.jsonl"
TIER_RANK = {tier: index for index, tier in enumerate(MODEL_TIERS)}


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    expected_class: str
    observed_class: str
    selected_tier: str
    minimum_tier: str
    structured_valid: bool
    fallback: bool
    unsafe_downgrade: bool
    actual_model_mismatch: bool
    actual_model: str
    actual_effort_mismatch: bool
    actual_effort: str
    confidence: float
    fallback_reason: str
    latency_ms: int
    estimated_cost_nano_usd: int | None
    failure_class: str


def fixture_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def router_metadata(case: dict[str, Any]) -> dict[str, Any]:
    item = case["input"]
    short_unanchored = bool(item.get("short_unanchored_followup", False))
    return {
        "trusted_text": str(item["text"]),
        "request_route": str(item.get("request_route", "normal")),
        "chat_type": "private",
        "has_reply": False,
        "reply_to_bot": False,
        "has_reference": bool(item.get("has_reference", False)),
        "has_attachment": False,
        "has_url": False,
        "short_followup": short_unanchored,
        "short_unanchored_followup": short_unanchored,
        "mutation_capability": bool(item.get("mutation_capability", False)),
        "tool_intent": "manage" if item.get("mutation_capability") else "none",
    }


def usage_cost(model: str, response: Any) -> int | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    details = getattr(usage, "input_tokens_details", None)
    estimate = estimate_token_cost(
        model,
        [
            TokenUsage(
                input_tokens=getattr(usage, "input_tokens", None),
                cached_input_tokens=getattr(details, "cached_tokens", None),
                cache_write_tokens=getattr(details, "cache_write_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
            )
        ],
    )
    return estimate.nano_usd


def provider_model_matches(requested: str, actual: str) -> bool:
    requested = str(requested or "").strip().casefold()
    actual = str(actual or "").strip().casefold()
    return bool(
        actual == requested
        or re.fullmatch(rf"{re.escape(requested)}-\d{{4}}-\d{{2}}-\d{{2}}", actual)
    )


def evaluate_case(
    client: OpenAI,
    case: dict[str, Any],
    *,
    model: str,
    effort: str,
    threshold: float,
    timeout_seconds: float,
) -> EvalResult:
    started = time.monotonic()
    expected = case["expected"]
    metadata = router_metadata(case)
    try:
        response = client.with_options(timeout=timeout_seconds).responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": [
                        {"type": "input_text", "text": MODEL_POLICY_ROUTER_SYSTEM_PROMPT}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        }
                    ],
                },
            ],
            reasoning={"effort": effort},
            max_output_tokens=240,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "model_policy_v1",
                    "strict": True,
                    "schema": model_routing_schema(),
                }
            },
        )
        payload = json.loads(response.output_text)
        decision = normalize_model_routing_decision(
            payload,
            confidence_threshold=threshold,
            route_bucket=str(metadata["request_route"]),
            mutation_capability=bool(metadata["mutation_capability"]),
            short_unanchored_followup=bool(metadata["short_unanchored_followup"]),
            source_context=bool(metadata["has_reference"]),
        )
        actual_model = str(getattr(response, "model", "") or "").casefold()
        actual_reasoning = str(
            getattr(getattr(response, "reasoning", None), "effort", "") or ""
        ).casefold()
        minimum_tier = str(expected["minimum_tier"])
        return EvalResult(
            case_id=str(case["id"]),
            expected_class=str(expected["task_class"]),
            observed_class=str(payload["task_class"]),
            selected_tier=decision.selected_tier,
            minimum_tier=minimum_tier,
            structured_valid=decision.outcome not in {"invalid", "failed"},
            fallback=decision.degraded,
            unsafe_downgrade=(
                TIER_RANK[decision.selected_tier] < TIER_RANK[minimum_tier]
            ),
            actual_model_mismatch=bool(
                actual_model and not provider_model_matches(model, actual_model)
            ),
            actual_model=actual_model,
            actual_effort_mismatch=bool(actual_reasoning and actual_reasoning != effort.casefold()),
            actual_effort=actual_reasoning,
            confidence=float(payload["confidence"]),
            fallback_reason=decision.fallback_reason,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            estimated_cost_nano_usd=usage_cost(model, response),
            failure_class="",
        )
    except Exception as exc:
        return EvalResult(
            case_id=str(case["id"]),
            expected_class=str(expected["task_class"]),
            observed_class="unclassified",
            selected_tier="premium",
            minimum_tier=str(expected["minimum_tier"]),
            structured_valid=False,
            fallback=True,
            unsafe_downgrade=False,
            actual_model_mismatch=False,
            actual_model="",
            actual_effort_mismatch=False,
            actual_effort="",
            confidence=0.0,
            fallback_reason="router_failed",
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            estimated_cost_nano_usd=None,
            failure_class=type(exc).__name__,
        )


def macro_f1(results: list[EvalResult]) -> float:
    classes = sorted({result.expected_class for result in results})
    scores: list[float] = []
    for name in classes:
        true_positive = sum(
            result.expected_class == name and result.observed_class == name
            for result in results
        )
        false_positive = sum(
            result.expected_class != name and result.observed_class == name
            for result in results
        )
        false_negative = sum(
            result.expected_class == name and result.observed_class != name
            for result in results
        )
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return statistics.fmean(scores) if scores else 0.0


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def aggregate(
    results: list[EvalResult],
    *,
    fixture_hash: str,
    model: str,
    effort: str,
    repeats: int,
    threshold: float,
) -> dict[str, Any]:
    total = len(results)
    by_case: dict[str, list[EvalResult]] = defaultdict(list)
    for result in results:
        by_case[result.case_id].append(result)
    stable = sum(
        len({(item.observed_class, item.selected_tier) for item in items}) == 1
        for items in by_case.values()
    )
    confusion = Counter(
        f"{result.expected_class}->{result.observed_class}" for result in results
    )
    known_cost = sum(
        result.estimated_cost_nano_usd or 0 for result in results
    )
    known_cost_count = sum(
        result.estimated_cost_nano_usd is not None for result in results
    )
    valid_rate = (
        sum(result.structured_valid for result in results) / total if total else 0.0
    )
    classification_score = macro_f1(results)
    unsafe_count = sum(result.unsafe_downgrade for result in results)
    router_gate_passed = (
        valid_rate >= 0.99
        and classification_score >= 0.90
        and unsafe_count == 0
    )
    economy_expected = sum(
        result.minimum_tier == "economy" for result in results
    )
    economy_selected = sum(
        result.selected_tier == "economy" for result in results
    )
    economy_true_positive = sum(
        result.minimum_tier == "economy" and result.selected_tier == "economy"
        for result in results
    )
    over_escalations = sum(
        TIER_RANK[result.selected_tier] > TIER_RANK[result.minimum_tier]
        for result in results
    )
    return {
        "fixture_sha256": fixture_hash,
        "fixture_cases": len(by_case),
        "repeats": repeats,
        "decisions": total,
        "router_model": model,
        "router_effort": effort,
        "router_confidence_threshold": threshold,
        "structured_valid": sum(result.structured_valid for result in results),
        "structured_valid_rate": valid_rate,
        "macro_f1": classification_score,
        "router_classification_gate": "PASS" if router_gate_passed else "NO_GO",
        "active_routing_verdict": "INCONCLUSIVE",
        "stable_cases": stable,
        "stability_rate": (
            stable / len(by_case) if by_case and repeats > 1 else None
        ),
        "unsafe_downgrades": unsafe_count,
        "over_escalations": over_escalations,
        "economy_selection": {
            "selected": economy_selected,
            "expected": economy_expected,
            "true_positive": economy_true_positive,
            "precision": (
                economy_true_positive / economy_selected if economy_selected else None
            ),
            "recall": (
                economy_true_positive / economy_expected if economy_expected else None
            ),
        },
        "fallbacks": sum(result.fallback for result in results),
        "fallback_reasons": dict(
            Counter(result.fallback_reason or "none" for result in results)
        ),
        "unsafe_case_ids": sorted(
            result.case_id for result in results if result.unsafe_downgrade
        ),
        "actual_model_mismatches": sum(result.actual_model_mismatch for result in results),
        "provider_models": dict(
            Counter(result.actual_model or "not_reported" for result in results)
        ),
        "confidence": {
            "mean": statistics.fmean(result.confidence for result in results) if results else 0.0,
            "below_threshold": sum(result.confidence < threshold for result in results),
        },
        "actual_effort_mismatches": sum(result.actual_effort_mismatch for result in results),
        "provider_efforts": dict(
            Counter(result.actual_effort or "not_reported" for result in results)
        ),
        "selected_tiers": dict(Counter(result.selected_tier for result in results)),
        "outcomes": dict(
            Counter("valid" if result.structured_valid else result.failure_class or "invalid" for result in results)
        ),
        "confusion": dict(sorted(confusion.items())),
        "latency_ms": {
            "p50": percentile([result.latency_ms for result in results], 0.50),
            "p95": percentile([result.latency_ms for result in results], 0.95),
            "max": max((result.latency_ms for result in results), default=0),
        },
        "router_estimated_cost_usd": (
            known_cost / 1_000_000_000 if known_cost_count else None
        ),
        "priced_decisions": known_cost_count,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the sanitized frozen model-policy router evaluation."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--model", default=os.getenv("MODEL_ROUTER_MODEL", "gpt-5.4-nano"))
    parser.add_argument("--effort", default=os.getenv("MODEL_ROUTER_REASONING_EFFORT", "none"))
    parser.add_argument("--threshold", type=float, default=0.75)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise SystemExit("OPENAI_API_KEY is required")
    fixture_bytes = args.fixture.read_bytes()
    cases = fixture_cases(args.fixture)
    if args.limit > 0:
        cases = cases[: args.limit]
    jobs = [case for case in cases for _ in range(max(1, args.repeats))]
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    results: list[EvalResult] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [
                executor.submit(
                    evaluate_case,
                    client,
                    case,
                    model=args.model,
                    effort=args.effort,
                    threshold=args.threshold,
                    timeout_seconds=args.timeout_seconds,
                )
                for case in jobs
            ]
            for future in as_completed(futures):
                results.append(future.result())
    finally:
        client.close()
    summary = aggregate(
        results,
        fixture_hash=hashlib.sha256(fixture_bytes).hexdigest(),
        model=args.model,
        effort=args.effort,
        repeats=max(1, args.repeats),
        threshold=args.threshold,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    if summary["router_classification_gate"] != "PASS":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
