from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import memory_extraction as v1


FIXTURE_SCHEMA_VERSION = "memory_extraction_fixture_v2"
OUTPUT_SCHEMA_VERSION = "memory_extraction_output_v3"
PROMPT_VERSION = "memory_extraction_prompt_v7"
EVALUATOR_VERSION = "memory_extraction_eval_v6"
MANIFEST_VERSION = "memory_extraction_manifest_v3"
HOLDOUT_CLAIM_NAMESPACE = "memory_extraction_v2_holdout"
HOLDOUT_CLAIM_SCOPE = "frozen_holdout_content_per_effective_posix_user"
EXPECTED_CASES_PER_SPLIT = 160
SCREEN_CASE_COUNT = 48

FROZEN_DEVELOPMENT_FILE_SHA256 = "e00f9fccb134017c94a196121b1101eb70f5d7241a97dc0ce10a0c2c0af548b8"
FROZEN_DEVELOPMENT_CASE_SHA256 = "e00f9fccb134017c94a196121b1101eb70f5d7241a97dc0ce10a0c2c0af548b8"
FROZEN_SCREEN_CASE_SHA256 = "938c07629cc3242e0b80e3339f30d4baaff549f7730dd1860a8e89b3e4cf6d18"
FROZEN_HOLDOUT_FILE_SHA256 = "d9cc736f10e9a1196a500032532cc0cc76dadbf37dc2715199446a6e91cbc609"
FROZEN_HOLDOUT_CASE_SHA256 = "d9cc736f10e9a1196a500032532cc0cc76dadbf37dc2715199446a6e91cbc609"
FROZEN_PROMPT_SHA256 = "22768ec62a69c0596af97e14a42960886d180bb06896d8402b197352c8d282c4"
FROZEN_OUTPUT_SCHEMA_SHA256 = "09838f4ccca1bf831c9ea7491e45a9d37a0a54b57b707904811d25c811f12cef"
FROZEN_DETERMINISTIC_BASELINE_SHA256 = "433994c6d44a8abcab7756f79794a0d8987ae874c8e5c5ceae94832eec55d80e"
FROZEN_PRICING_SNAPSHOT_SHA256 = "a8aebcd56703bf09c7f84cacf543d20c4bd3b7d532d6010e7db0ef9e9682a61c"
FROZEN_EVALUATION_BUNDLE_SHA256 = "f2faf805fe59a4aab42dd23861b516cda1cdacf0a1f1dab71488f0e4a15834ff"
FROZEN_RUN_MATRIX_SHA256 = "24e010736117fa269ae32b563f6c15c3927bb59a64f6556ecfa26b2c605e7f1a"
FROZEN_MANIFEST_SHA256 = "0b17e81a2360cefcd8ffe1086d7d11ea8dab481fdab8993e2ea9bc7c05b7737c"

PRICING_SNAPSHOT = {
    "version": "openai-standard-2026-07-09",
    "models": {
        "gpt-5.6-luna": {
            "input_rate_nano_usd": 1000,
            "cached_input_rate_nano_usd": 100,
            "cache_write_rate_nano_usd": 1250,
            "output_rate_nano_usd": 6000,
        },
        "gpt-5.6-terra": {
            "input_rate_nano_usd": 2500,
            "cached_input_rate_nano_usd": 250,
            "cache_write_rate_nano_usd": 3125,
            "output_rate_nano_usd": 15000,
        },
        "gpt-5.6-sol": {
            "input_rate_nano_usd": 5000,
            "cached_input_rate_nano_usd": 500,
            "cache_write_rate_nano_usd": 6250,
            "output_rate_nano_usd": 30000,
        },
    },
}

BASELINE_MODEL = "deterministic-baseline-v2"
API_ARMS = (
    ("gpt-5.6-luna", "none"),
    ("gpt-5.6-luna", "low"),
    ("gpt-5.6-terra", "low"),
)
RUN_MATRIX = {
    "screen": {
        "case_count": SCREEN_CASE_COUNT,
        "case_set_sha256": FROZEN_SCREEN_CASE_SHA256,
        "repeats": 1,
        "concurrency": 6,
        "timeout_seconds": 45,
        "max_output_tokens": 1200,
        "store": False,
        "arms": [list(item) for item in API_ARMS],
        "admission": "transport_schema_safety_only",
    },
    "locked_development": {
        "case_count": EXPECTED_CASES_PER_SPLIT,
        "repeats": 3,
        "concurrency": 6,
        "timeout_seconds": 45,
        "max_output_tokens": 1200,
        "store": False,
    },
    "holdout": {
        "case_count": EXPECTED_CASES_PER_SPLIT,
        "repeats": 3,
        "concurrency": 6,
        "timeout_seconds": 45,
        "max_output_tokens": 1200,
        "store": False,
        "one_time": True,
        "claim_namespace": HOLDOUT_CLAIM_NAMESPACE,
        "claim_scope": HOLDOUT_CLAIM_SCOPE,
        "caller_selectable_claim_path": False,
        "ephemeral_environment_allowed": False,
    },
}


def run_matrix_sha256() -> str:
    encoded = json.dumps(
        RUN_MATRIX,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def holdout_claim_key_sha256() -> str:
    encoded = json.dumps(
        {
            "namespace": HOLDOUT_CLAIM_NAMESPACE,
            "holdout_file_sha256": FROZEN_HOLDOUT_FILE_SHA256,
            "holdout_case_sha256": FROZEN_HOLDOUT_CASE_SHA256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def pricing_snapshot_sha256() -> str:
    encoded = json.dumps(
        PRICING_SNAPSHOT,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def manifest_sha256(path: Path | str | None = None) -> str:
    manifest = (
        Path(path)
        if path is not None
        else Path(__file__).resolve().parent
        / "tests"
        / "fixtures"
        / "memory_extraction_v2_manifest.json"
    )
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def memory_extraction_output_schema() -> dict[str, Any]:
    v1_schema = v1.memory_extraction_output_schema()
    candidate = copy.deepcopy(
        v1_schema["properties"]["candidates"]["items"]
    )
    candidates_result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "candidates"],
        "properties": {
            "kind": {"type": "string", "enum": ["candidates"]},
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": candidate,
            },
        },
    }
    no_candidate_result = {
        "type": "object",
        "additionalProperties": False,
        "required": ["kind", "no_candidate_reason"],
        "properties": {
            "kind": {"type": "string", "enum": ["no_candidate"]},
            "no_candidate_reason": {
                "type": "string",
                "enum": [
                    reason
                    for reason in v1.NO_CANDIDATE_REASONS
                    if reason != "none"
                ],
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["result"],
        "properties": {
            "result": {
                "anyOf": [candidates_result, no_candidate_result],
            }
        },
    }


def output_schema_sha256() -> str:
    encoded = json.dumps(
        memory_extraction_output_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


fixture_sha256 = v1.fixture_sha256
fixture_case_set_sha256 = v1.fixture_case_set_sha256
provider_model_matches = v1.provider_model_matches
aggregate_report_has_private_fields = v1.aggregate_report_has_private_fields


def evaluation_bundle_sha256() -> str:
    root = Path(__file__).resolve().parent
    paths = (
        root / "memory_extraction.py",
        root / "model_pricing.py",
        root / "scripts" / "eval_memory_extraction.py",
        root / "memory_extraction_v2.py",
        root / "memory_extraction_selection_v2.py",
        root / "scripts" / "eval_memory_extraction_v2.py",
        root / "scripts" / "select_memory_extraction_v2_candidate.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        if path.name == "memory_extraction_v2.py":
            lines = [
                line
                for line in lines
                if not line.startswith(
                    (
                        "FROZEN_EVALUATION_BUNDLE_SHA256 =",
                        "FROZEN_MANIFEST_SHA256 =",
                    )
                )
            ]
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(("\n".join(lines) + "\n").encode("utf-8"))
    return digest.hexdigest()


_SCREEN_POSITIVE_PLAN = {
    "uk": (
        "fact_claim",
        "fact_claim",
        "preference",
        "decision",
        "relationship",
        "correction",
        "correction",
        "uncertainty",
        "validity_expiry",
    ),
    "ru": (
        "fact_claim",
        "preference",
        "preference",
        "decision",
        "relationship",
        "correction",
        "correction",
        "uncertainty",
        "validity_expiry",
    ),
    "en": (
        "fact_claim",
        "preference",
        "decision",
        "decision",
        "relationship",
        "correction",
        "uncertainty",
        "validity_expiry",
    ),
    "mixed": (
        "fact_claim",
        "preference",
        "decision",
        "relationship",
        "relationship",
        "correction",
        "uncertainty",
        "validity_expiry",
    ),
}
_SCREEN_NEGATIVE_PLAN = {
    "uk": ("opinion", "joke", "question"),
    "ru": ("hypothetical", "transient", "forwarded_without_endorsement"),
    "en": (
        "prior_bot_output",
        "cross_scope",
        "unsupported_tool_evidence",
        "not_durable",
    ),
    "mixed": ("unclassified", "opinion", "joke", "question"),
}


def select_screen_cases(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(cases) != EXPECTED_CASES_PER_SPLIT:
        raise ValueError("screen:development_case_count")
    if {str(case.get("split", "")) for case in cases} != {"development"}:
        raise ValueError("screen:development_split")
    pool = sorted((copy.deepcopy(dict(case)) for case in cases), key=lambda item: str(item["id"]))
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def take(language: str, *, tag: str | None = None, reason: str | None = None) -> None:
        for case in pool:
            case_id = str(case["id"])
            if case_id in selected_ids or str(case["language"]) != language:
                continue
            if tag is not None and str(case["tags"][0]) != tag:
                continue
            if reason is not None and str(case["expected"]["no_candidate_reason"]) != reason:
                continue
            selected.append(case)
            selected_ids.add(case_id)
            return
        raise ValueError(f"screen:missing_case:{language}:{tag or reason}")

    for language in v1.LANGUAGES:
        for candidate_type in _SCREEN_POSITIVE_PLAN[language]:
            take(language, tag=candidate_type)
        for rejection_reason in _SCREEN_NEGATIVE_PLAN[language]:
            take(language, reason=rejection_reason)
    if len(selected) != SCREEN_CASE_COUNT:
        raise ValueError("screen:case_count")
    return selected


def repeat_jobs(
    cases: Sequence[Mapping[str, Any]],
    repeats: int,
) -> list[tuple[str, dict[str, Any]]]:
    if repeats < 1:
        raise ValueError("repeats")
    return [
        (str(case["id"]), copy.deepcopy(dict(case)))
        for case in cases
        for _repeat_index in range(repeats)
    ]


def _legacy_fixture_case(case: Mapping[str, Any]) -> dict[str, Any]:
    legacy = copy.deepcopy(dict(case))
    legacy["schema_version"] = v1.FIXTURE_SCHEMA_VERSION
    return legacy


def validate_fixture_case(case: Mapping[str, Any]) -> None:
    if case.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError("case:schema_version")
    v1.validate_fixture_case(_legacy_fixture_case(case))
    tags = {str(item) for item in case["tags"]}
    rows = {str(row["row_key"]): row for row in case["inputs"]}
    if "exact_whitespace" in tags:
        evidence_values = [
            str(row[field])
            for row in case["inputs"]
            for field in v1.EVIDENCE_FIELDS
            if row[field]
        ]
        if not any("  " in value or "\t" in value or "\n" in value for value in evidence_values):
            raise ValueError("case:exact_whitespace_missing")
    if "multi_prior_reply_anchor" in tags:
        if len(case["inputs"]) < 4:
            raise ValueError("case:multi_prior_rows")
        corrections = [
            candidate
            for candidate in case["expected"]["candidates"]
            if candidate["candidate_type"] == "correction"
        ]
        if len(corrections) != 1:
            raise ValueError("case:correction_count")
        correction = corrections[0]
        source_ref = correction["evidence_refs"][0]
        source_row = rows[str(source_ref["row_key"])]
        reply_anchor = source_row.get("reply_to_row_key")
        links = (
            list(correction["supersedes_row_keys"])
            + list(correction["conflicts_row_keys"])
        )
        if reply_anchor is None or links != [reply_anchor]:
            raise ValueError("case:correction_reply_anchor")


def load_fixture(path: Path | str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"fixture:line_{line_number}:invalid_json") from exc
        validate_fixture_case(case)
        case_id = str(case["id"])
        if case_id in seen:
            raise ValueError(f"fixture:duplicate_id:{case_id}")
        seen.add(case_id)
        cases.append(case)
    if not cases:
        raise ValueError("fixture:empty")
    return cases


def public_model_input(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": case["schema_version"],
        "case_id": case["id"],
        "as_of": case["as_of"],
        "target_chat_key": case["target_chat_key"],
        "target_speaker_key": case["target_speaker_key"],
        "inputs": case["inputs"],
    }


def canonical_to_v2(canonical: Mapping[str, Any]) -> dict[str, Any]:
    candidates = canonical.get("candidates")
    if isinstance(candidates, list) and candidates:
        return {
            "result": {
                "kind": "candidates",
                "candidates": copy.deepcopy(candidates),
            }
        }
    reason = str(canonical.get("no_candidate_reason") or "unclassified")
    if reason == "none" or reason not in v1.NO_CANDIDATE_REASONS:
        reason = "unclassified"
    return {
        "result": {
            "kind": "no_candidate",
            "no_candidate_reason": reason,
        }
    }


def validate_prediction(
    case: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    fallback = {"candidates": [], "no_candidate_reason": "unclassified"}
    errors: list[str] = []
    if not isinstance(payload, Mapping) or set(payload) != {"result"}:
        return fallback, ["v2:schema_top"]
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return fallback, ["v2:result_object"]
    kind = result.get("kind")
    if kind == "candidates":
        if set(result) != {"kind", "candidates"}:
            errors.append("v2:candidate_branch_keys")
        candidates = result.get("candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 4:
            errors.append("v2:candidate_branch_count")
            canonical = fallback
        else:
            canonical = {
                "candidates": copy.deepcopy(candidates),
                "no_candidate_reason": "none",
            }
    elif kind == "no_candidate":
        if set(result) != {"kind", "no_candidate_reason"}:
            errors.append("v2:no_candidate_branch_keys")
        reason = result.get("no_candidate_reason")
        if reason not in v1.NO_CANDIDATE_REASONS or reason == "none":
            errors.append("v2:no_candidate_reason")
            canonical = fallback
        else:
            canonical = {
                "candidates": [],
                "no_candidate_reason": reason,
            }
    else:
        return fallback, ["v2:result_kind"]
    normalized, semantic_errors = v1.validate_prediction(case, canonical)
    errors.extend(semantic_errors)
    model_owned_fields = (
        "candidate_type",
        "epistemic",
        "durability",
        "evidence_refs",
        "evidence_span",
        "supersedes_row_keys",
        "conflicts_row_keys",
        "valid_until",
        "confidence",
        "reason_codes",
    )
    raw_candidates = canonical.get("candidates", [])
    normalized_candidates = normalized.get("candidates", [])
    if len(raw_candidates) == len(normalized_candidates):
        normalized_by_identity: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        for normalized_candidate in normalized_candidates:
            identity = (
                normalized_candidate.get("candidate_type"),
                tuple(
                    sorted(
                        (str(ref.get("row_key")), str(ref.get("field")))
                        for ref in normalized_candidate.get("evidence_refs", [])
                    )
                ),
            )
            normalized_by_identity.setdefault(identity, []).append(normalized_candidate)
        for index, raw_candidate in enumerate(raw_candidates):
            if not isinstance(raw_candidate, Mapping):
                continue
            identity = (
                raw_candidate.get("candidate_type"),
                tuple(
                    sorted(
                        (str(ref.get("row_key")), str(ref.get("field")))
                        for ref in raw_candidate.get("evidence_refs", [])
                        if isinstance(ref, Mapping)
                    )
                ),
            )
            matches = normalized_by_identity.get(identity, [])
            if not matches:
                errors.append(f"v2:candidate_{index}:normalized_identity")
                continue
            normalized_candidate = matches.pop(0)
            for field in model_owned_fields:
                if raw_candidate.get(field) != normalized_candidate.get(field):
                    errors.append(f"v2:candidate_{index}:normalized_{field}")
    if canonical.get("no_candidate_reason") != normalized.get("no_candidate_reason"):
        errors.append("v2:normalized_no_candidate_reason")
    return normalized, sorted(set(errors))


def deterministic_extract(case: Mapping[str, Any]) -> dict[str, Any]:
    canonical = v1.deterministic_extract(case)
    rows = {str(row["row_key"]): row for row in case["inputs"]}
    target_chat = str(case["target_chat_key"])
    for candidate in canonical["candidates"]:
        refs = candidate.get("evidence_refs", [])
        if refs:
            ref = refs[0]
            row = rows.get(str(ref["row_key"]))
            field = str(ref["field"])
            if row is not None and field in v1.EVIDENCE_FIELDS:
                candidate["evidence_span"] = str(row[field])
        if candidate.get("candidate_type") != "correction" or not refs:
            continue
        candidate["supersedes_row_keys"] = []
        candidate["conflicts_row_keys"] = []
        source_row = rows.get(str(refs[0]["row_key"]))
        if source_row is None:
            continue
        reply_key = source_row.get("reply_to_row_key")
        anchor = rows.get(str(reply_key)) if reply_key is not None else None
        if (
            anchor is None
            or str(anchor["chat_key"]) != target_chat
            or str(anchor["created_at"]) >= str(source_row["created_at"])
        ):
            continue
        link_field = (
            "supersedes_row_keys"
            if anchor["speaker_key"] == source_row["speaker_key"]
            else "conflicts_row_keys"
        )
        candidate[link_field] = [str(anchor["row_key"])]

    for candidate in canonical["candidates"]:
        if (
            candidate.get("candidate_type") != "validity_expiry"
            or candidate.get("valid_until") != "none"
        ):
            continue
        refs = candidate.get("evidence_refs", [])
        if not refs:
            continue
        ref = refs[0]
        row = rows.get(str(ref["row_key"]))
        field = str(ref["field"])
        if row is None or field not in v1.EVIDENCE_FIELDS:
            continue
        match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", str(row[field]))
        if match:
            candidate["valid_until"] = f"{match.group(1)}T00:00:00Z"
    return canonical_to_v2(canonical)


def deterministic_baseline_sha256() -> str:
    source = inspect.getsource(deterministic_extract).replace("\r\n", "\n")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def evaluate_predictions(
    cases: Sequence[Mapping[str, Any]],
    predictions: Mapping[str, Mapping[str, Any] | None],
    *,
    fixture_hash: str,
    model: str,
    reasoning_effort: str,
    prompt_hash: str | None,
    repeat_count: int,
    stability_rate: float | None,
    full_contract_stability_rate: float | None,
    evaluation_instance_count: int,
    evaluation_structured_valid_rate: float,
    sol_counterfactual_nano_usd: int | None,
    **run_evidence: Any,
) -> dict[str, Any]:
    cache_write_tokens = run_evidence.pop("cache_write_tokens", 0)
    cache_write_rate_nano_usd = run_evidence.pop(
        "cache_write_rate_nano_usd",
        None,
    )
    attested_actual_models = run_evidence.pop("attested_actual_models", None)
    source_commit = str(run_evidence.pop("source_commit", ""))
    canonical_predictions: dict[str, Mapping[str, Any] | None] = {}
    unique_v2_errors: Counter[str] = Counter()
    for case in cases:
        case_id = str(case["id"])
        raw = predictions.get(case_id)
        if raw is None:
            canonical_predictions[case_id] = None
            continue
        normalized, errors = validate_prediction(case, raw)
        if errors:
            canonical_predictions[case_id] = None
            unique_v2_errors.update(errors)
        else:
            canonical_predictions[case_id] = {
                "candidates": [
                    {
                        key: candidate[key]
                        for key in (
                            "candidate_type",
                            "epistemic",
                            "durability",
                            "evidence_refs",
                            "evidence_span",
                            "supersedes_row_keys",
                            "conflicts_row_keys",
                            "valid_until",
                            "confidence",
                            "reason_codes",
                        )
                    }
                    for candidate in normalized["candidates"]
                ],
                "no_candidate_reason": normalized["no_candidate_reason"],
            }
    is_baseline = model == BASELINE_MODEL
    base = v1.evaluate_predictions(
        cases,
        canonical_predictions,
        fixture_hash=fixture_hash,
        model="deterministic-baseline-v1" if is_baseline else model,
        reasoning_effort=reasoning_effort,
        prompt_hash=prompt_hash,
        repeat_count=repeat_count,
        stability_rate=stability_rate,
        full_contract_stability_rate=full_contract_stability_rate,
        evaluation_instance_count=evaluation_instance_count,
        evaluation_structured_valid_rate=evaluation_structured_valid_rate,
        **run_evidence,
    )
    splits = {str(case.get("split", "")) for case in cases}
    split = next(iter(splits), "") if len(splits) == 1 else "mixed"
    case_hash = fixture_case_set_sha256(cases)
    expected_file_hash = {
        "development": FROZEN_DEVELOPMENT_FILE_SHA256,
        "holdout": FROZEN_HOLDOUT_FILE_SHA256,
    }.get(split, "")
    expected_case_hash = {
        "development": FROZEN_DEVELOPMENT_CASE_SHA256,
        "holdout": FROZEN_HOLDOUT_CASE_SHA256,
    }.get(split, "")
    quality = base["quality"]
    overall = quality["overall"]
    durable = quality["durable"]
    base_gates = base["gates"]
    worker_cost = base["usage"].get("estimated_cost_nano_usd")
    cost_gate = (
        isinstance(worker_cost, int)
        and worker_cost > 0
        and isinstance(sol_counterfactual_nano_usd, int)
        and sol_counterfactual_nano_usd > 0
        and worker_cost * 2 <= sol_counterfactual_nano_usd
    )
    expected_rates = PRICING_SNAPSHOT["models"].get(model)
    usage = base["usage"]
    usage["cache_write_tokens"] = cache_write_tokens
    usage["cache_write_rate_nano_usd"] = cache_write_rate_nano_usd
    input_count = usage.get("input_tokens")
    cached_count = usage.get("cached_input_tokens")
    output_count = usage.get("output_tokens")
    estimated_cost = usage.get("estimated_cost_nano_usd")
    count_values = (input_count, cached_count, cache_write_tokens, output_count)
    rate_values = (
        usage.get("input_rate_nano_usd"),
        usage.get("cached_input_rate_nano_usd"),
        cache_write_rate_nano_usd,
        usage.get("output_rate_nano_usd"),
    )
    counts_valid = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in count_values
    )
    rates_valid = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in rate_values
    )
    v2_recomputed_cost = None
    if counts_valid and rates_valid and cached_count + cache_write_tokens <= input_count:
        ordinary_input = input_count - cached_count - cache_write_tokens
        v2_recomputed_cost = (
            ordinary_input * rate_values[0]
            + cached_count * rate_values[1]
            + cache_write_tokens * rate_values[2]
            + output_count * rate_values[3]
        )
    usage["recomputed_cost_nano_usd"] = v2_recomputed_cost
    v2_pricing_complete = bool(base_gates["pricing_complete"] and rates_valid)
    v2_pricing_consistent = bool(
        v2_pricing_complete
        and isinstance(estimated_cost, int)
        and v2_recomputed_cost is not None
        and estimated_cost == v2_recomputed_cost
    )
    pricing_snapshot_matches = bool(
        not is_baseline
        and expected_rates
        and usage.get("pricing_snapshot_version") == PRICING_SNAPSHOT["version"]
        and usage.get("input_rate_nano_usd") == expected_rates["input_rate_nano_usd"]
        and usage.get("cached_input_rate_nano_usd")
        == expected_rates["cached_input_rate_nano_usd"]
        and cache_write_rate_nano_usd
        == expected_rates["cache_write_rate_nano_usd"]
        and usage.get("output_rate_nano_usd") == expected_rates["output_rate_nano_usd"]
    )
    artifact_gates = {
        "single_frozen_split": split in {"development", "holdout"},
        "frozen_fixture_file_hash_matches": fixture_hash == expected_file_hash,
        "prompt_hash_matches": prompt_hash == FROZEN_PROMPT_SHA256,
        "output_schema_hash_matches": output_schema_sha256() == FROZEN_OUTPUT_SCHEMA_SHA256,
        "deterministic_baseline_hash_matches": deterministic_baseline_sha256()
        == FROZEN_DETERMINISTIC_BASELINE_SHA256,
        "pricing_snapshot_hash_matches": pricing_snapshot_sha256()
        == FROZEN_PRICING_SNAPSHOT_SHA256,
        "evaluation_bundle_hash_matches": evaluation_bundle_sha256()
        == FROZEN_EVALUATION_BUNDLE_SHA256,
        "run_matrix_hash_matches": run_matrix_sha256() == FROZEN_RUN_MATRIX_SHA256,
        "manifest_hash_matches": manifest_sha256() == FROZEN_MANIFEST_SHA256,
        "source_commit_bound": bool(re.fullmatch(r"[0-9a-f]{40}", source_commit)),
    }
    provider_gates = {
        "provider_model_mismatches_zero": base_gates["provider_model_mismatches_zero"],
        "provider_effort_mismatches_zero": base_gates["provider_effort_mismatches_zero"],
        "provider_metadata_complete": base_gates["provider_metadata_complete"],
        "provider_failures_zero": base_gates["provider_failures_zero"],
        "single_actual_model_snapshot": len(base["provider"]["actual_models"]) == 1,
        "actual_model_matches_development_attestation": split != "holdout"
        or (
            isinstance(attested_actual_models, list)
            and base["provider"]["actual_models"] == attested_actual_models
        ),
        "pricing_complete": v2_pricing_complete,
        "pricing_cost_consistent": v2_pricing_consistent,
        "pricing_snapshot_matches": pricing_snapshot_matches,
        "usage_metadata_complete": base_gates["usage_metadata_complete"],
        "worker_cost_at_most_half_sol": cost_gate,
        "api_model_candidate": not is_baseline and (model, reasoning_effort) in API_ARMS,
    }
    run_configuration_gates = {
        "concurrency_exactly_6": base["run"].get("concurrency") == 6,
        "timeout_seconds_exactly_45": base["run"].get("timeout_seconds") == 45,
        "max_output_tokens_exactly_1200": base["run"].get("max_output_tokens") == 1200,
        "store_false": base["run"].get("store") is False,
    }
    full_gates = {
        **artifact_gates,
        "full_fixture": len(cases) == EXPECTED_CASES_PER_SPLIT,
        "frozen_fixture_case_hash_matches": case_hash == expected_case_hash,
        "repeat_count_exactly_3": repeat_count == 3,
        "evaluation_instance_count_matches": evaluation_instance_count == len(cases) * repeat_count,
        "structured_valid_rate_100": quality["structured_valid_rate"] == 1.0,
        "all_repeat_structured_valid_rate_100": evaluation_structured_valid_rate == 1.0,
        "overall_precision_wilson_lower_at_least_095": bool(overall["precision_wilson_95"] and overall["precision_wilson_95"][0] >= 0.95),
        "overall_recall_wilson_lower_at_least_095": bool(overall["recall_wilson_95"] and overall["recall_wilson_95"][0] >= 0.95),
        "durable_precision_wilson_lower_at_least_095": bool(durable["precision_wilson_95"] and durable["precision_wilson_95"][0] >= 0.95),
        "durable_recall_wilson_lower_at_least_095": bool(durable["recall_wilson_95"] and durable["recall_wilson_95"][0] >= 0.95),
        "exact_candidate_set_rate_at_least_099": quality["exact_candidate_set_rate"] >= 0.99,
        "negative_case_false_positives_zero": quality["negative_case_false_positive_count"] == 0,
        "hard_safety_violations_zero": base["safety"]["hard_violation_count"] == 0 and not unique_v2_errors,
        "candidate_behavior_stability_at_least_099": isinstance(stability_rate, (int, float)) and stability_rate >= 0.99,
        **run_configuration_gates,
        **provider_gates,
    }
    is_screen = (
        not is_baseline
        and split == "development"
        and len(cases) == SCREEN_CASE_COUNT
        and repeat_count == 1
    )
    screen_gates = {
        **artifact_gates,
        "fixed_screen_case_count": len(cases) == SCREEN_CASE_COUNT,
        "frozen_screen_case_hash_matches": case_hash == FROZEN_SCREEN_CASE_SHA256,
        "repeat_count_exactly_1": repeat_count == 1,
        "evaluation_instance_count_matches": evaluation_instance_count == len(cases),
        "structured_valid_rate_100": quality["structured_valid_rate"] == 1.0,
        "all_repeat_structured_valid_rate_100": evaluation_structured_valid_rate == 1.0,
        "negative_case_false_positives_zero": quality["negative_case_false_positive_count"] == 0,
        "hard_safety_violations_zero": base["safety"]["hard_violation_count"] == 0
        and not unique_v2_errors,
        **run_configuration_gates,
        **provider_gates,
    }
    if is_baseline:
        verdict = "INCONCLUSIVE"
        phase = "baseline"
        gates = full_gates
    elif is_screen:
        verdict = (
            "PASS_FOR_LOCKED_DEVELOPMENT"
            if all(screen_gates.values())
            else "SCREEN_FAIL"
        )
        phase = "screen"
        gates = screen_gates
    elif not full_gates["full_fixture"]:
        verdict = "INCONCLUSIVE"
        phase = "ad_hoc"
        gates = full_gates
    elif all(full_gates.values()) and split == "development":
        verdict = "GO_FOR_HOLDOUT_CANDIDATE"
        phase = "locked_development"
        gates = full_gates
    elif all(full_gates.values()) and split == "holdout":
        verdict = "GO_FOR_RUNTIME_SHADOW_PR"
        phase = "holdout"
        gates = full_gates
    else:
        verdict = "NO_GO"
        phase = "holdout" if split == "holdout" else "locked_development"
        gates = full_gates
    base["fixture"] = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "split": split,
        "sha256": fixture_hash,
        "case_set_sha256": case_hash,
        "unique_case_count": len(cases),
        "evaluation_instance_count": evaluation_instance_count,
    }
    base["versions"] = {
        "output_schema": OUTPUT_SCHEMA_VERSION,
        "output_schema_sha256": output_schema_sha256(),
        "deterministic_baseline_sha256": deterministic_baseline_sha256(),
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_hash,
        "evaluator": EVALUATOR_VERSION,
        "evaluation_bundle_sha256": evaluation_bundle_sha256(),
        "run_matrix_sha256": run_matrix_sha256(),
        "manifest_version": MANIFEST_VERSION,
        "manifest_sha256": manifest_sha256(),
        "pricing_snapshot_version": PRICING_SNAPSHOT["version"],
        "pricing_snapshot_sha256": pricing_snapshot_sha256(),
        "source_commit": source_commit,
        "legacy_semantic_validator_bundle": v1.FROZEN_EVALUATION_BUNDLE_SHA256,
    }
    base["phase"] = phase
    base["run"]["model"] = model
    base["usage"]["sol_counterfactual_nano_usd"] = sol_counterfactual_nano_usd
    base["safety"]["v2_unique_error_count"] = sum(unique_v2_errors.values())
    base["safety"]["v2_unique_error_buckets"] = dict(sorted(unique_v2_errors.items()))

    def namespaced_buckets(prefix: str, buckets: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in sorted(buckets.items()):
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_")
            result[f"{prefix}_{normalized or 'unknown'}"] = value
        return result

    base["quality"]["mismatch_buckets"] = namespaced_buckets(
        "mismatch",
        base["quality"]["mismatch_buckets"],
    )
    for bucket_field in (
        "error_buckets",
        "repeat_error_buckets",
        "v2_unique_error_buckets",
    ):
        base["safety"][bucket_field] = namespaced_buckets(
            "validation",
            base["safety"].get(bucket_field, {}),
        )
    base["gates"] = gates
    base["verdict"] = verdict
    base["runtime_authorized"] = False
    return base
