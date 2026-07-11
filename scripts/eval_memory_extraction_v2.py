#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openai import AsyncOpenAI

from memory_extraction_v2 import (
    API_ARMS,
    BASELINE_MODEL,
    FROZEN_DETERMINISTIC_BASELINE_SHA256,
    FROZEN_DEVELOPMENT_CASE_SHA256,
    FROZEN_DEVELOPMENT_FILE_SHA256,
    FROZEN_HOLDOUT_FILE_SHA256,
    FROZEN_HOLDOUT_CASE_SHA256,
    FROZEN_MANIFEST_SHA256,
    FROZEN_OUTPUT_SCHEMA_SHA256,
    FROZEN_PRICING_SNAPSHOT_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_RUN_MATRIX_SHA256,
    FROZEN_SCREEN_CASE_SHA256,
    FROZEN_EVALUATION_BUNDLE_SHA256,
    OUTPUT_SCHEMA_VERSION,
    aggregate_report_has_private_fields,
    deterministic_baseline_sha256,
    deterministic_extract,
    evaluate_predictions,
    evaluation_bundle_sha256,
    fixture_case_set_sha256,
    fixture_sha256,
    load_fixture,
    manifest_sha256,
    memory_extraction_output_schema,
    output_schema_sha256,
    pricing_snapshot_sha256,
    public_model_input,
    repeat_jobs,
    run_matrix_sha256,
    select_screen_cases,
    validate_prediction,
)
from memory_extraction_selection_v2 import (
    current_clean_source_commit,
    load_screen_admission,
    load_selection_attestation,
)
from model_pricing import TokenUsage, estimate_token_cost
from scripts.eval_memory_extraction import (
    model_matches,
    response_effort,
    stratified_cases,
    validation_error_bucket,
)


PROMPT_PATH = ROOT / "prompts" / "memory_extraction_eval_v6.md"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "memory_extraction_v2_development.jsonl"
HOLDOUT_RECEIPT_VERSION = "memory_extraction_holdout_receipt_v1"
HOLDOUT_RESULT_VERSION = "memory_extraction_holdout_result_v1"


@dataclass(frozen=True)
class CallResult:
    prediction: Mapping[str, Any] | None
    actual_model: str
    actual_effort: str
    input_tokens: int
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int
    latency_ms: int
    failure: str


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def response_usage_v2(response: Any) -> tuple[int, int | None, int | None, int]:
    usage = _field(response, "usage")
    details = _field(usage, "input_tokens_details") or _field(
        usage,
        "input_token_details",
    )
    try:
        input_tokens = int(_field(usage, "input_tokens") or 0)
        output_tokens = int(_field(usage, "output_tokens") or 0)
        cached_raw = _field(details, "cached_tokens")
        cache_write_raw = _field(details, "cache_write_tokens")
        cached_tokens = int(cached_raw) if cached_raw is not None else None
        cache_write_tokens = (
            int(cache_write_raw) if cache_write_raw is not None else None
        )
    except (TypeError, ValueError):
        return 0, None, None, 0
    return input_tokens, cached_tokens, cache_write_tokens, output_tokens


def _validate_external_artifact_path(path: Path, field: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"holdout:{field}:absolute_path_required")
    resolved = path.resolve()
    if resolved.is_relative_to(ROOT.resolve()):
        raise ValueError(f"holdout:{field}:must_be_outside_repository")
    if not resolved.parent.is_dir():
        raise ValueError(f"holdout:{field}:parent_missing")
    if not os.access(resolved.parent, os.W_OK | os.X_OK):
        raise ValueError(f"holdout:{field}:parent_not_writable")
    if resolved.exists():
        raise ValueError(f"holdout:{field}:already_exists")


def _validate_external_input_file(path: Path, field: str) -> None:
    if (
        not path.is_absolute()
        or path.resolve().is_relative_to(ROOT.resolve())
        or not path.resolve().is_file()
    ):
        raise ValueError(f"evaluation:{field}:external_file_required")


def _fsync_parent_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("holdout:exclusive_artifact_exists") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_parent_directory(path)


def reserve_holdout_once(
    path: Path,
    *,
    attestation_sha256: str,
    selected: Mapping[str, Any],
    source_commit: str,
) -> dict[str, Any]:
    receipt = {
        "schema_version": HOLDOUT_RECEIPT_VERSION,
        "status": "claimed_before_api",
        "run_id": secrets.token_hex(16),
        "claimed_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": FROZEN_MANIFEST_SHA256,
        "attestation_sha256": attestation_sha256,
        "source_commit": source_commit,
        "model": selected["model"],
        "reasoning_effort": selected["reasoning_effort"],
        "actual_models": copy.deepcopy(selected["actual_models"]),
        "actual_efforts": copy.deepcopy(selected["actual_efforts"]),
        "holdout_file_sha256": FROZEN_HOLDOUT_FILE_SHA256,
        "holdout_case_sha256": FROZEN_HOLDOUT_CASE_SHA256,
        "repeats": 3,
        "concurrency": 6,
        "timeout_seconds": 45,
        "max_output_tokens": 1200,
        "store": False,
    }
    _write_exclusive_private_json(path, receipt)
    return receipt


def finalize_holdout_result(
    path: Path,
    *,
    receipt: Mapping[str, Any],
    report: Mapping[str, Any],
) -> None:
    canonical_report = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = {
        "schema_version": HOLDOUT_RESULT_VERSION,
        "receipt_run_id": receipt["run_id"],
        "manifest_sha256": receipt["manifest_sha256"],
        "attestation_sha256": receipt["attestation_sha256"],
        "report_sha256": hashlib.sha256(canonical_report).hexdigest(),
        "verdict": report["verdict"],
        "runtime_authorized": report["runtime_authorized"],
        "report": copy.deepcopy(dict(report)),
    }
    _write_exclusive_private_json(path, payload)


def load_system_prompt() -> tuple[str, str]:
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


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
        actual_model = str(getattr(response, "model", "") or "")
        actual_effort = response_effort(response)
        input_tokens, cached_tokens, cache_write_tokens, output_tokens = response_usage_v2(
            response
        )
        status = str(getattr(response, "status", "") or "").casefold()
        if status != "completed":
            return CallResult(
                None,
                actual_model,
                actual_effort,
                input_tokens,
                cached_tokens,
                cache_write_tokens,
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
                cache_write_tokens,
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
            cache_write_tokens,
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
            None,
            None,
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
            None,
            None,
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
    async with AsyncOpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0) as client:
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
            "no_candidate_reason": normalized["no_candidate_reason"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def first_repeat_predictions(
    jobs: Sequence[tuple[str, Mapping[str, Any]]],
    results: Sequence[CallResult],
) -> dict[str, Mapping[str, Any] | None]:
    if len(jobs) != len(results):
        raise ValueError("evaluation:job_result_count")
    first: dict[str, Mapping[str, Any] | None] = {}
    for (original_id, _case), result in zip(jobs, results):
        first.setdefault(original_id, result.prediction)
    return first


def _preflight_fixture_access(
    args: argparse.Namespace,
    fixture_hash: str,
    prompt_hash: str,
    source_commit: str,
    parser: argparse.ArgumentParser,
) -> tuple[dict[str, Any] | None, str | None]:
    is_holdout = fixture_hash == FROZEN_HOLDOUT_FILE_SHA256
    if args.mode != "api":
        if is_holdout:
            parser.error("holdout cannot run in baseline mode")
        return None, None
    if not os.getenv("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is required for --mode api")
    artifact_checks = {
        "manifest": manifest_sha256() == FROZEN_MANIFEST_SHA256,
        "prompt": prompt_hash == FROZEN_PROMPT_SHA256,
        "schema": output_schema_sha256() == FROZEN_OUTPUT_SCHEMA_SHA256,
        "baseline": deterministic_baseline_sha256()
        == FROZEN_DETERMINISTIC_BASELINE_SHA256,
        "pricing": pricing_snapshot_sha256() == FROZEN_PRICING_SNAPSHOT_SHA256,
        "bundle": evaluation_bundle_sha256() == FROZEN_EVALUATION_BUNDLE_SHA256,
        "run_matrix": run_matrix_sha256() == FROZEN_RUN_MATRIX_SHA256,
        "fixture": fixture_hash
        in {FROZEN_DEVELOPMENT_FILE_SHA256, FROZEN_HOLDOUT_FILE_SHA256},
    }
    failed = [name for name, passed in artifact_checks.items() if not passed]
    if failed:
        parser.error("frozen artifact preflight failed: " + ",".join(failed))
    if not is_holdout:
        if args.require_gates:
            if args.screen_admission is None:
                parser.error("locked development requires a passing screen report")
            try:
                _validate_external_input_file(
                    args.screen_admission,
                    "screen_admission",
                )
                _screen_report, screen_hash = load_screen_admission(
                    args.screen_admission,
                    source_commit=source_commit,
                    expected_arm=(args.model, args.reasoning_effort),
                )
            except ValueError as exc:
                parser.error(str(exc))
            if args.acknowledge_screen_admission_sha256 != screen_hash:
                parser.error("locked development requires the exact screen report hash")
        return None, None
    if args.acknowledge_holdout_manifest_sha256 != FROZEN_MANIFEST_SHA256:
        parser.error("holdout requires the exact frozen manifest hash acknowledgement")
    if args.mode != "api" or not args.require_gates:
        parser.error("holdout requires --mode api and --require-gates")
    if args.development_attestation is None:
        parser.error("holdout requires a matrix-wide development attestation")
    attestation_path = args.development_attestation
    try:
        _validate_external_input_file(
            attestation_path,
            "development_attestation",
        )
        attestation, attestation_hash = load_selection_attestation(attestation_path)
    except ValueError as exc:
        parser.error(str(exc))
    if args.acknowledge_development_attestation_sha256 != attestation_hash:
        parser.error("holdout requires the exact development attestation hash")
    selected = attestation.get("selected")
    if attestation.get("verdict") != "CANDIDATE_SELECTED" or not isinstance(
        selected,
        Mapping,
    ):
        parser.error("development attestation selected no holdout candidate")
    if attestation.get("source_commit") != source_commit:
        parser.error("development attestation source commit mismatch")
    if (selected.get("model"), selected.get("reasoning_effort")) != (
        args.model,
        args.reasoning_effort,
    ):
        parser.error("holdout arm does not match the selected development candidate")
    if args.holdout_receipt is None or args.holdout_result is None:
        parser.error("holdout requires external receipt and result paths")
    if args.holdout_receipt.resolve() == args.holdout_result.resolve():
        parser.error("holdout receipt and result paths must differ")
    try:
        _validate_external_artifact_path(args.holdout_receipt, "receipt")
        _validate_external_artifact_path(args.holdout_result, "result")
    except ValueError as exc:
        parser.error(str(exc))
    return attestation, attestation_hash


def _validate_run_contract(
    args: argparse.Namespace,
    split: str,
    fixture_hash: str,
    parser: argparse.ArgumentParser,
) -> None:
    if args.mode == "api" and (args.model, args.reasoning_effort) not in API_ARMS:
        parser.error("API model/effort is outside the preregistered v2 matrix")
    if split == "holdout":
        if fixture_hash != FROZEN_HOLDOUT_FILE_SHA256:
            parser.error("holdout fixture bytes do not match the frozen manifest")
        if args.acknowledge_holdout_manifest_sha256 != FROZEN_MANIFEST_SHA256:
            parser.error("holdout requires the exact frozen manifest hash acknowledgement")
        if args.mode != "api" or not args.require_gates:
            parser.error("holdout requires --mode api and --require-gates")
    if args.mode == "api" and not args.require_gates:
        if (
            split != "development"
            or args.limit != 48
            or args.repeats != 1
            or args.concurrency != 6
            or args.timeout_seconds != 45
            or args.max_output_tokens != 1200
        ):
            parser.error("screen run configuration does not match preregistration")
    if args.require_gates:
        if (
            args.limit != 0
            or args.repeats != 3
            or args.concurrency != 6
            or args.timeout_seconds != 45
            or args.max_output_tokens != 1200
        ):
            parser.error("locked gate run configuration does not match preregistration")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate versioned v2 memory extraction without persisting payloads."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--mode", choices=("baseline", "api"), default="baseline")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument(
        "--reasoning-effort",
        choices=("omit", "none", "low", "medium", "high", "xhigh", "max"),
        default="none",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--max-output-tokens", type=int, default=1200)
    parser.add_argument("--acknowledge-holdout-manifest-sha256", default="")
    parser.add_argument("--screen-admission", type=Path)
    parser.add_argument("--acknowledge-screen-admission-sha256", default="")
    parser.add_argument("--development-attestation", type=Path)
    parser.add_argument("--acknowledge-development-attestation-sha256", default="")
    parser.add_argument("--holdout-receipt", type=Path)
    parser.add_argument("--holdout-result", type=Path)
    parser.add_argument("--require-gates", action="store_true")
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 3:
        parser.error("--repeats must be between 1 and 3")
    if args.limit < 0:
        parser.error("--limit cannot be negative")

    frozen_fixture_hash = fixture_sha256(args.fixture)
    system_prompt, prompt_hash = load_system_prompt()
    source_commit = ""
    if args.mode == "api":
        try:
            source_commit = current_clean_source_commit(ROOT)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            parser.error(str(exc))
    attestation, attestation_hash = _preflight_fixture_access(
        args,
        frozen_fixture_hash,
        prompt_hash,
        source_commit,
        parser,
    )
    cases = load_fixture(args.fixture)
    splits = {str(case["split"]) for case in cases}
    if len(splits) != 1:
        raise RuntimeError("fixture must contain exactly one split")
    split = next(iter(splits))
    _validate_run_contract(args, split, frozen_fixture_hash, parser)
    if args.mode == "api":
        expected_case_hash = (
            FROZEN_HOLDOUT_CASE_SHA256
            if split == "holdout"
            else FROZEN_DEVELOPMENT_CASE_SHA256
        )
        if fixture_case_set_sha256(cases) != expected_case_hash:
            parser.error("frozen fixture case-set hash mismatch")
    selected = (
        select_screen_cases(cases)
        if split == "development" and args.limit == 48
        else stratified_cases(cases, args.limit)
    )
    if (
        args.mode == "api"
        and args.limit == 48
        and fixture_case_set_sha256(selected) != FROZEN_SCREEN_CASE_SHA256
    ):
        parser.error("frozen screen case-set hash mismatch")
    jobs = repeat_jobs(selected, args.repeats)
    holdout_receipt: dict[str, Any] | None = None

    if args.mode == "baseline":
        results = [
            CallResult(
                deterministic_extract(case),
                BASELINE_MODEL,
                "none",
                0,
                0,
                0,
                0,
                0,
                "",
            )
            for _original_id, case in jobs
        ]
        model = BASELINE_MODEL
        effort = "none"
    else:
        if split == "holdout":
            assert attestation is not None and attestation_hash is not None
            assert args.holdout_receipt is not None
            try:
                if current_clean_source_commit(ROOT) != source_commit:
                    raise ValueError("holdout:source_changed_after_preflight")
                assert args.holdout_result is not None
                _validate_external_artifact_path(args.holdout_receipt, "receipt")
                _validate_external_artifact_path(args.holdout_result, "result")
                holdout_receipt = reserve_holdout_once(
                    args.holdout_receipt,
                    attestation_sha256=attestation_hash,
                    selected=attestation["selected"],
                    source_commit=source_commit,
                )
            except ValueError as exc:
                parser.error(str(exc))
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
            cache_write_tokens=result.cache_write_tokens,
            output_tokens=result.output_tokens,
        )
        for result in results
        if result.input_tokens or result.output_tokens
    ]
    usage_metadata_missing = sum(
        result.input_tokens <= 0
        or result.output_tokens <= 0
        or result.cached_input_tokens is None
        or result.cache_write_tokens is None
        for result in results
        if args.mode == "api"
    )
    cost = estimate_token_cost(args.model, usages) if args.mode == "api" else None
    sol_cost = estimate_token_cost("gpt-5.6-sol", usages) if args.mode == "api" else None

    behavior_signatures: dict[str, list[str]] = defaultdict(list)
    contract_signatures: dict[str, list[str]] = defaultdict(list)
    repeat_error_counts: Counter[str] = Counter()
    all_structured_valid = 0
    for (original_id, case), result in zip(jobs, results):
        behavior_signatures[original_id].append(prediction_signature(case, result.prediction))
        contract_signatures[original_id].append(
            prediction_signature(case, result.prediction, full_contract=True)
        )
        if result.prediction is None:
            repeat_error_counts["provider_failure"] += 1
            continue
        _normalized, errors = validate_prediction(case, result.prediction)
        if errors:
            repeat_error_counts.update(validation_error_bucket(error) for error in errors)
        else:
            all_structured_valid += 1
    stable = sum(len(set(items)) == 1 for items in behavior_signatures.values())
    contract_stable = sum(len(set(items)) == 1 for items in contract_signatures.values())
    stability_rate = stable / len(behavior_signatures) if behavior_signatures else None
    contract_stability_rate = contract_stable / len(contract_signatures) if contract_signatures else None
    all_structured_rate = all_structured_valid / len(jobs) if jobs else 0.0
    quality_predictions = first_repeat_predictions(jobs, results)
    report = evaluate_predictions(
        selected,
        quality_predictions,
        fixture_hash=frozen_fixture_hash,
        model=model,
        reasoning_effort=effort,
        prompt_hash=prompt_hash,
        repeat_count=args.repeats,
        stability_rate=stability_rate,
        full_contract_stability_rate=contract_stability_rate,
        evaluation_instance_count=len(jobs),
        evaluation_structured_valid_rate=all_structured_rate,
        sol_counterfactual_nano_usd=sol_cost.nano_usd if sol_cost is not None else 0,
        provider_model_mismatches=model_mismatches,
        provider_effort_mismatches=effort_mismatches,
        provider_metadata_missing=metadata_missing,
        provider_actual_models=[result.actual_model for result in results],
        provider_actual_efforts=[result.actual_effort for result in results],
        input_tokens=sum(result.input_tokens for result in results),
        cached_input_tokens=sum(result.cached_input_tokens or 0 for result in results),
        cache_write_tokens=sum(result.cache_write_tokens or 0 for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        estimated_cost_nano_usd=cost.nano_usd if cost is not None else 0,
        pricing_status=cost.status if cost is not None else "not_applicable",
        pricing_complete=cost.complete if cost is not None else args.mode == "baseline",
        usage_metadata_missing=usage_metadata_missing,
        pricing_snapshot_version=cost.snapshot_version if cost is not None else None,
        pricing_basis_model=cost.basis_model if cost is not None else None,
        input_rate_nano_usd=cost.input_rate_nano_usd if cost is not None else None,
        cached_input_rate_nano_usd=cost.cached_input_rate_nano_usd if cost is not None else None,
        cache_write_rate_nano_usd=cost.cache_write_rate_nano_usd if cost is not None else None,
        output_rate_nano_usd=cost.output_rate_nano_usd if cost is not None else None,
        concurrency=args.concurrency if args.mode == "api" else 0,
        timeout_seconds=args.timeout_seconds if args.mode == "api" else 0.0,
        max_output_tokens=args.max_output_tokens if args.mode == "api" else 0,
        store=False,
        latency_ms=[result.latency_ms for result in results],
        failure_counts=failures,
        repeat_error_counts=repeat_error_counts,
        attested_actual_models=(
            attestation["selected"]["actual_models"]
            if attestation is not None and attestation.get("selected")
            else None
        ),
        source_commit=source_commit,
    )
    report["stability"]["eligible_case_count"] = len(behavior_signatures)
    report["stability"]["candidate_behavior_match_count"] = stable
    report["stability"]["full_contract_match_count"] = contract_stable
    if aggregate_report_has_private_fields(report):
        raise RuntimeError("aggregate report contained a forbidden payload field")
    print(json.dumps(report, indent=2, sort_keys=True))
    if holdout_receipt is not None:
        assert args.holdout_result is not None
        try:
            finalize_holdout_result(
                args.holdout_result,
                receipt=holdout_receipt,
                report=report,
            )
        except ValueError as exc:
            print(f"holdout result persistence failed: {exc}", file=sys.stderr)
            return 3
    accepted = {"GO_FOR_HOLDOUT_CANDIDATE", "GO_FOR_RUNTIME_SHADOW_PR"}
    if args.require_gates and report["verdict"] not in accepted:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
