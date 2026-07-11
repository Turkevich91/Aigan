from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


FIXTURE_SCHEMA_VERSION = "memory_extraction_fixture_v1"
OUTPUT_SCHEMA_VERSION = "memory_extraction_output_v2"
PROMPT_VERSION = "memory_extraction_prompt_v4"
EVALUATOR_VERSION = "memory_extraction_eval_v2"
FROZEN_FIXTURE_SHA256 = "4737869e8b36a5ae16b38c98b91adbc9774c580933f932f5d75116d612666d44"
FROZEN_DEVELOPMENT_SHA256 = "bbd9ebcf31a68b41d9bf09a679411b5378752d23ee6787fecd95f8087d6b2c02"
FROZEN_HOLDOUT_SHA256 = "1d4bc0ddde8f5d6a6df41266e1448de2d366ab9d6daa903a4314fc62799fc687"
FROZEN_PROMPT_SHA256 = "8f5b599f904d9017f2ead6bb1d084cdcff767f0768992bcb9c374e5f700dc4e2"
FROZEN_OUTPUT_SCHEMA_SHA256 = "192ba5c4e1b5a08ae64b5c2ac9636f30f63863ae98d39825ef6919c105d37cfe"
FROZEN_EVALUATION_BUNDLE_SHA256 = "e08dc80589f440b95020d1b293a4c8d22be46793eb1f3329b88a8a9c35d8413c"

CANDIDATE_TYPES = (
    "fact_claim",
    "preference",
    "correction",
    "decision",
    "relationship",
    "uncertainty",
    "validity_expiry",
)
ACTOR_ROLES = ("user", "bot", "tool")
SOURCE_ROLES = (
    "user_authored",
    "forwarded_source",
    "prior_bot_output",
    "verified_tool",
)
EVIDENCE_FIELDS = ("authored_text", "source_text", "tool_evidence")
EPISTEMIC_STATES = ("asserted", "uncertain", "quoted", "verified")
DURABILITY_STATES = ("durable", "transient")
LIFECYCLE_STATES = ("candidate_only",)
NO_CANDIDATE_REASONS = (
    "none",
    "not_durable",
    "opinion",
    "joke",
    "question",
    "hypothetical",
    "transient",
    "forwarded_without_endorsement",
    "prior_bot_output",
    "cross_scope",
    "unsupported_tool_evidence",
    "unclassified",
)
REASON_CODES = (
    "explicit_fact",
    "explicit_preference",
    "explicit_decision",
    "explicit_relationship",
    "explicit_correction",
    "uncertainty_marker",
    "explicit_validity",
    "verified_tool_anchor",
)
EXPECTED_REASON_BY_TYPE = {
    "fact_claim": "explicit_fact",
    "preference": "explicit_preference",
    "correction": "explicit_correction",
    "decision": "explicit_decision",
    "relationship": "explicit_relationship",
    "uncertainty": "uncertainty_marker",
    "validity_expiry": "explicit_validity",
}
FORBIDDEN_OUTCOMES = (
    "active_without_source",
    "cross_scope_source",
    "uncertain_promoted",
    "forwarded_promoted",
    "prior_bot_promoted",
    "tool_fact_without_anchor",
)
LANGUAGES = ("uk", "ru", "en", "mixed")

SAFE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PRIVATE_MARKER_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|/(?:home|Users|private|var/lib)/|(?:ssh|scp)\s+[a-z0-9_.-]+|"
    r"(?:api[_-]?key|token|password|secret)\s*[=:]|https?://|file://|"
    r"\b-?\d{7,}\b)",
    re.IGNORECASE,
)
SPACE_RE = re.compile(r"\s+")


def memory_extraction_output_schema() -> dict[str, Any]:
    evidence_ref = {
        "type": "object",
        "additionalProperties": False,
        "required": ["row_key", "field"],
        "properties": {
            "row_key": {"type": "string"},
            "field": {"type": "string", "enum": list(EVIDENCE_FIELDS)},
        },
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "required": [
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
        ],
        "properties": {
            "candidate_type": {"type": "string", "enum": list(CANDIDATE_TYPES)},
            "epistemic": {"type": "string", "enum": list(EPISTEMIC_STATES)},
            "durability": {"type": "string", "enum": list(DURABILITY_STATES)},
            "evidence_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": evidence_ref,
            },
            "evidence_span": {"type": "string"},
            "supersedes_row_keys": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
            "conflicts_row_keys": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
            },
            "valid_until": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.5, "maximum": 1},
            "reason_codes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": {"type": "string", "enum": list(REASON_CODES)},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates", "no_candidate_reason"],
        "properties": {
            "candidates": {
                "type": "array",
                "maxItems": 4,
                "items": candidate,
            },
            "no_candidate_reason": {
                "type": "string",
                "enum": list(NO_CANDIDATE_REASONS),
            },
        },
    }


def fixture_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixture_case_set_sha256(cases: Sequence[Mapping[str, Any]]) -> str:
    canonical = "".join(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for case in sorted(cases, key=lambda item: str(item["id"]))
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def output_schema_sha256() -> str:
    canonical = json.dumps(
        memory_extraction_output_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def provider_model_matches(requested: str, actual: str) -> bool:
    requested_value = str(requested or "").strip().casefold()
    actual_value = str(actual or "").strip().casefold()
    if not requested_value or not actual_value:
        return False
    if requested_value == actual_value:
        return True
    requested_snapshot = re.search(r"-\d{4}-\d{2}-\d{2}$", requested_value)
    if requested_snapshot or not actual_value.startswith(requested_value + "-"):
        return False
    suffix = actual_value[len(requested_value) + 1 :]
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", suffix):
        return False
    try:
        datetime.strptime(suffix, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def evaluation_bundle_sha256() -> str:
    root = Path(__file__).resolve().parent
    paths = (root / "memory_extraction.py", root / "scripts" / "eval_memory_extraction.py")
    digest = hashlib.sha256()
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        if path.name == "memory_extraction.py":
            lines = [
                line
                for line in lines
                if not line.startswith("FROZEN_EVALUATION_BUNDLE_SHA256 =")
            ]
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(("\n".join(lines) + "\n").encode("utf-8"))
    return digest.hexdigest()


def _parse_iso(value: str, field: str) -> datetime | None:
    if value == "none":
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}:invalid_datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field}:invalid_datetime")
    return parsed


def _safe_key(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not SAFE_KEY_RE.fullmatch(normalized):
        raise ValueError(f"{field}:invalid_key")
    return normalized


def _expected_keys(mapping: Mapping[str, Any], keys: Iterable[str], field: str) -> None:
    expected = set(keys)
    actual = set(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{field}:keys:missing={missing}:extra={extra}")


def _case_rows(case: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(row["row_key"]): row for row in case["inputs"]}


def _normalize_span(value: Any) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip()


def _candidate_identity(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    refs = tuple(
        sorted(
            (str(ref["row_key"]), str(ref["field"]))
            for ref in candidate.get("evidence_refs", ())
        )
    )
    return (
        str(candidate.get("candidate_type", "")),
        str(candidate.get("subject_key", "")),
        str(candidate.get("source_role", "")),
        str(candidate.get("epistemic", "")),
        str(candidate.get("durability", "")),
        str(candidate.get("lifecycle", "")),
        refs,
        tuple(sorted(str(item) for item in candidate.get("supersedes_row_keys", ()))),
        tuple(sorted(str(item) for item in candidate.get("conflicts_row_keys", ()))),
        str(candidate.get("valid_from", "none")),
        str(candidate.get("valid_until", "none")),
    )


def validate_fixture_case(case: Mapping[str, Any]) -> None:
    _expected_keys(
        case,
        (
            "schema_version",
            "id",
            "privacy_class",
            "split",
            "tags",
            "language",
            "as_of",
            "target_chat_key",
            "target_speaker_key",
            "inputs",
            "expected",
        ),
        "case",
    )
    if case["schema_version"] != FIXTURE_SCHEMA_VERSION:
        raise ValueError("case:schema_version")
    _safe_key(case["id"], "case:id")
    if case["privacy_class"] != "public_synthetic":
        raise ValueError("case:privacy_class")
    if case["split"] not in {"development", "holdout"}:
        raise ValueError("case:split")
    if case["language"] not in LANGUAGES:
        raise ValueError("case:language")
    _parse_iso(str(case["as_of"]), "case:as_of")
    target_chat = _safe_key(case["target_chat_key"], "case:target_chat_key")
    _safe_key(case["target_speaker_key"], "case:target_speaker_key")
    tags = case["tags"]
    if not isinstance(tags, list) or not tags or any(not SAFE_KEY_RE.fullmatch(str(tag)) for tag in tags):
        raise ValueError("case:tags")
    inputs = case["inputs"]
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("case:inputs")
    rows: dict[str, Mapping[str, Any]] = {}
    for row in inputs:
        if not isinstance(row, Mapping):
            raise ValueError("input:not_object")
        _expected_keys(
            row,
            (
                "row_key",
                "chat_key",
                "speaker_key",
                "actor_role",
                "authored_text",
                "source_text",
                "source_role",
                "reply_to_row_key",
                "created_at",
                "tool_evidence_row_key",
                "tool_evidence",
            ),
            "input",
        )
        row_key = _safe_key(row["row_key"], "input:row_key")
        if row_key in rows:
            raise ValueError("input:duplicate_row_key")
        rows[row_key] = row
        _safe_key(row["chat_key"], "input:chat_key")
        _safe_key(row["speaker_key"], "input:speaker_key")
        if row["actor_role"] not in ACTOR_ROLES:
            raise ValueError("input:actor_role")
        if row["source_role"] not in SOURCE_ROLES:
            raise ValueError("input:source_role")
        for field in EVIDENCE_FIELDS:
            if not isinstance(row[field], str):
                raise ValueError(f"input:{field}")
        _parse_iso(str(row["created_at"]), "input:created_at")
        for optional_key in ("reply_to_row_key", "tool_evidence_row_key"):
            if row[optional_key] is not None:
                _safe_key(row[optional_key], f"input:{optional_key}")
        if row["source_role"] == "verified_tool":
            if row["actor_role"] != "tool" or not row["tool_evidence"]:
                raise ValueError("input:verified_tool_without_evidence")
            if row["tool_evidence_row_key"] != row_key:
                raise ValueError("input:verified_tool_anchor")
    serialized = json.dumps(case, ensure_ascii=False, sort_keys=True)
    if PRIVATE_MARKER_RE.search(serialized):
        raise ValueError("case:private_marker")
    expected = case["expected"]
    if not isinstance(expected, Mapping):
        raise ValueError("expected:not_object")
    _expected_keys(
        expected,
        ("eligible", "candidates", "no_candidate_reason", "forbidden_outcomes"),
        "expected",
    )
    if not isinstance(expected["eligible"], bool):
        raise ValueError("expected:eligible")
    if expected["no_candidate_reason"] not in NO_CANDIDATE_REASONS:
        raise ValueError("expected:no_candidate_reason")
    if sorted(expected["forbidden_outcomes"]) != sorted(set(expected["forbidden_outcomes"])):
        raise ValueError("expected:forbidden_outcomes_duplicates")
    if any(item not in FORBIDDEN_OUTCOMES for item in expected["forbidden_outcomes"]):
        raise ValueError("expected:forbidden_outcomes")
    normalized, errors = validate_prediction(case, expected, expected_mode=True)
    if errors:
        raise ValueError("expected:" + ",".join(errors))
    if normalized["candidates"] and expected["no_candidate_reason"] != "none":
        raise ValueError("expected:candidates_with_reason")
    if not normalized["candidates"] and expected["no_candidate_reason"] == "none":
        raise ValueError("expected:missing_reason")
    for candidate in normalized["candidates"]:
        for ref in candidate["evidence_refs"]:
            if rows[ref["row_key"]]["chat_key"] != target_chat:
                raise ValueError("expected:cross_scope_source")


def load_fixture(path: Path | str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
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


def validate_prediction(
    case: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    expected_mode: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if not isinstance(payload, Mapping):
        return {"candidates": [], "no_candidate_reason": "unclassified"}, ["schema:not_object"]
    allowed_top = {"candidates", "no_candidate_reason"}
    if expected_mode:
        allowed_top |= {"eligible", "forbidden_outcomes"}
    if set(payload) != allowed_top:
        errors.append("schema:top_keys")
    candidates = payload.get("candidates")
    reason = payload.get("no_candidate_reason")
    if not isinstance(candidates, list) or len(candidates) > 4:
        return {"candidates": [], "no_candidate_reason": "unclassified"}, errors + ["schema:candidates"]
    if reason not in NO_CANDIDATE_REASONS:
        errors.append("schema:no_candidate_reason")
        reason = "unclassified"
    rows = _case_rows(case)
    target_chat = str(case["target_chat_key"])
    normalized_candidates: list[dict[str, Any]] = []
    identities: set[tuple[Any, ...]] = set()
    candidate_keys = {
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
    }
    for index, candidate in enumerate(candidates):
        prefix = f"candidate_{index}"
        if not isinstance(candidate, Mapping) or set(candidate) != candidate_keys:
            errors.append(f"{prefix}:schema")
            continue
        normalized = dict(candidate)
        candidate_type = candidate["candidate_type"]
        if not isinstance(candidate_type, str) or candidate_type not in CANDIDATE_TYPES:
            errors.append(f"{prefix}:candidate_type")
            candidate_type = "fact_claim"
        epistemic = candidate["epistemic"]
        if not isinstance(epistemic, str) or epistemic not in EPISTEMIC_STATES:
            errors.append(f"{prefix}:epistemic")
            epistemic = "asserted"
        durability = candidate["durability"]
        if not isinstance(durability, str) or durability not in DURABILITY_STATES:
            errors.append(f"{prefix}:durability")
            durability = "transient"
        refs = candidate["evidence_refs"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 2:
            errors.append(f"{prefix}:evidence_refs")
            refs = []
        normalized_refs: list[dict[str, str]] = []
        evidence_values: list[str] = []
        evidence_rows: list[Mapping[str, Any]] = []
        for ref in refs:
            if not isinstance(ref, Mapping) or set(ref) != {"row_key", "field"}:
                errors.append(f"{prefix}:evidence_ref_schema")
                continue
            row_key = str(ref["row_key"])
            field = str(ref["field"])
            row = rows.get(row_key)
            if row is None or field not in EVIDENCE_FIELDS:
                errors.append(f"{prefix}:missing_evidence")
                continue
            if str(row["chat_key"]) != target_chat:
                errors.append("cross_scope_source")
            evidence = str(row[field])
            if not evidence:
                errors.append(f"{prefix}:empty_evidence")
            else:
                evidence_values.append(evidence)
            if row["source_role"] == "verified_tool" and not (
                field == "tool_evidence"
                and row.get("tool_evidence_row_key") == row_key
                and row.get("actor_role") == "tool"
            ):
                errors.append("tool_fact_without_anchor")
            normalized_refs.append({"row_key": row_key, "field": field})
            evidence_rows.append(row)
        ref_pairs = [(item["row_key"], item["field"]) for item in normalized_refs]
        if len(ref_pairs) != len(set(ref_pairs)):
            errors.append(f"{prefix}:duplicate_evidence_refs")
        speaker_keys = {str(row["speaker_key"]) for row in evidence_rows}
        source_roles = {str(row["source_role"]) for row in evidence_rows}
        if len(speaker_keys) > 1 or len(source_roles) > 1:
            errors.append(f"{prefix}:multi_source_scope")
        derived_subject = next(iter(speaker_keys), "")
        derived_source_role = next(iter(source_roles), "")
        raw_span = candidate["evidence_span"]
        span = raw_span if isinstance(raw_span, str) else ""
        if not span or not any(span == value for value in evidence_values):
            errors.append(f"{prefix}:evidence_span")
        if derived_source_role == "prior_bot_output":
            errors.append("prior_bot_promoted")
        if derived_source_role == "forwarded_source":
            errors.append("forwarded_promoted")
        if epistemic in ("uncertain", "quoted") and durability == "durable":
            errors.append("uncertain_promoted")
        if candidate_type == "uncertainty":
            if epistemic != "uncertain" or durability != "transient":
                errors.append(f"{prefix}:uncertainty_semantics")
        elif derived_source_role == "verified_tool":
            if candidate_type != "fact_claim" or epistemic != "verified" or durability != "durable":
                errors.append(f"{prefix}:verified_tool_semantics")
        elif epistemic != "asserted" or durability != "durable":
            errors.append(f"{prefix}:durable_candidate_semantics")
        links: dict[str, list[str]] = {}
        for link_field in ("supersedes_row_keys", "conflicts_row_keys"):
            values = candidate[link_field]
            if not isinstance(values, list) or len(values) > 4:
                errors.append(f"{prefix}:{link_field}")
                values = []
            cleaned: list[str] = []
            for value in values:
                row_key = str(value)
                if row_key not in rows:
                    errors.append(f"{prefix}:{link_field}_missing")
                    continue
                if str(rows[row_key]["chat_key"]) != target_chat:
                    errors.append("cross_scope_source")
                cleaned.append(row_key)
            if len(cleaned) != len(set(cleaned)):
                errors.append(f"{prefix}:{link_field}_duplicates")
            links[link_field] = sorted(set(cleaned))
        if set(links["supersedes_row_keys"]) & set(links["conflicts_row_keys"]):
            errors.append(f"{prefix}:overlapping_links")
        if candidate_type != "correction" and (
            links["supersedes_row_keys"] or links["conflicts_row_keys"]
        ):
            errors.append(f"{prefix}:links_require_correction")
        source_row = evidence_rows[0] if len(evidence_rows) == 1 else None
        if source_row is not None:
            source_speaker = str(source_row["speaker_key"])
            source_created = str(source_row["created_at"])
            for row_key in links["supersedes_row_keys"]:
                linked = rows[row_key]
                if str(linked["speaker_key"]) != source_speaker:
                    errors.append(f"{prefix}:supersession_speaker_mismatch")
                if str(linked["created_at"]) >= source_created:
                    errors.append(f"{prefix}:link_not_prior")
            for row_key in links["conflicts_row_keys"]:
                linked = rows[row_key]
                if str(linked["speaker_key"]) == source_speaker:
                    errors.append(f"{prefix}:conflict_speaker_mismatch")
                if str(linked["created_at"]) >= source_created:
                    errors.append(f"{prefix}:link_not_prior")
        if candidate_type == "correction":
            correction_links = (
                links["supersedes_row_keys"] + links["conflicts_row_keys"]
            )
            reply_anchor = source_row.get("reply_to_row_key") if source_row is not None else None
            if len(correction_links) != 1:
                errors.append(f"{prefix}:correction_requires_one_link")
            if reply_anchor is None or correction_links != [str(reply_anchor)]:
                errors.append(f"{prefix}:correction_reply_anchor_mismatch")
        valid_until = str(candidate["valid_until"])
        try:
            parsed_until = _parse_iso(valid_until, f"{prefix}:valid_until")
        except ValueError as exc:
            errors.append(str(exc))
            parsed_until = None
        if candidate_type == "validity_expiry" and parsed_until is None:
            errors.append(f"{prefix}:missing_validity")
        if candidate_type != "validity_expiry" and parsed_until is not None:
            errors.append(f"{prefix}:unexpected_validity")
        as_of = _parse_iso(str(case["as_of"]), "case:as_of")
        if parsed_until is not None and as_of is not None and parsed_until <= as_of:
            errors.append(f"{prefix}:validity_not_future")
        normalized_valid_until = (
            parsed_until.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            if parsed_until is not None
            else "none"
        )
        confidence = candidate["confidence"]
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or not 0.5 <= float(confidence) <= 1
        ):
            errors.append(f"{prefix}:confidence")
            confidence = 0.5
        reason_codes = candidate["reason_codes"]
        expected_reason = (
            "verified_tool_anchor"
            if derived_source_role == "verified_tool"
            else EXPECTED_REASON_BY_TYPE[candidate_type]
        )
        if (
            not isinstance(reason_codes, list)
            or len(reason_codes) != 1
            or not isinstance(reason_codes[0], str)
            or reason_codes[0] not in REASON_CODES
            or reason_codes[0] != expected_reason
        ):
            errors.append(f"{prefix}:reason_codes")
            reason_codes = [expected_reason]
        normalized.update(
            candidate_type=candidate_type,
            epistemic=epistemic,
            durability=durability,
            subject_key=derived_subject,
            source_role=derived_source_role,
            lifecycle="candidate_only",
            evidence_refs=sorted(normalized_refs, key=lambda item: (item["row_key"], item["field"])),
            evidence_span=span,
            supersedes_row_keys=links["supersedes_row_keys"],
            conflicts_row_keys=links["conflicts_row_keys"],
            valid_from="none",
            valid_until=normalized_valid_until,
            confidence=float(confidence),
            reason_codes=reason_codes,
        )
        identity = _candidate_identity(normalized)
        if identity in identities:
            errors.append(f"{prefix}:duplicate")
        identities.add(identity)
        normalized_candidates.append(normalized)
    if normalized_candidates and reason != "none":
        errors.append("schema:candidates_with_reason")
    if not normalized_candidates and reason == "none":
        errors.append("schema:missing_reason")
    return {
        "candidates": sorted(normalized_candidates, key=_candidate_identity),
        "no_candidate_reason": reason,
    }, sorted(set(errors))


def _text_for_row(row: Mapping[str, Any]) -> tuple[str, str]:
    if row["source_role"] == "verified_tool":
        return "tool_evidence", str(row["tool_evidence"])
    if row["source_role"] == "forwarded_source":
        return "source_text", str(row["source_text"])
    return "authored_text", str(row["authored_text"])


def _negative_reason(text: str, row: Mapping[str, Any]) -> str | None:
    lowered = text.casefold()
    if row["source_role"] == "prior_bot_output" or row["actor_role"] == "bot":
        return "prior_bot_output"
    if row["source_role"] == "forwarded_source":
        return "forwarded_without_endorsement"
    if any(token in lowered for token in ("just kidding", "жартую", "шучу", "sarcasm")):
        return "joke"
    if any(token in lowered for token in ("in my opinion", "на мою думку", "по-моему", "я думаю")):
        return "opinion"
    if "?" in text:
        if any(token in lowered for token in ("what if", "а що як", "а что если", "якби", "если бы")):
            return "hypothetical"
        return "question"
    if lowered.strip(" .!👍✅") in {"ok", "okay", "добре", "гаразд", "хорошо", "понял", "дякую", "спасибо", "thanks"}:
        return "transient"
    if any(token in lowered for token in ("right now", "зараз", "сейчас")) and any(
        token in lowered for token in ("busy", "зайня", "занят")
    ):
        return "transient"
    return None


def _classify_candidate(text: str) -> tuple[str, str, str, str] | None:
    lowered = text.casefold()
    if any(token in lowered for token in ("correction:", "виправлення:", "исправление:", "actually,", "насправді", "на самом деле")):
        return "correction", "asserted", "durable", "explicit_correction"
    if any(token in lowered for token in ("maybe", "probably", "perhaps", "можливо", "ймовірно", "возможно", "вероятно")):
        return "uncertainty", "uncertain", "transient", "uncertainty_marker"
    if any(token in lowered for token in ("valid until", "until 2030", "діє до", "до 2030", "действует до")):
        return "validity_expiry", "asserted", "durable", "explicit_validity"
    if any(token in lowered for token in ("i prefer", "my preference", "я prefer", "віддаю перевагу", "подобається більше", "предпочитаю", "мне больше нравится")):
        return "preference", "asserted", "durable", "explicit_preference"
    if any(token in lowered for token in ("we decided", "i decided", "ми decided", "ми вирішили", "я вирішив", "мы решили", "я решил")):
        return "decision", "asserted", "durable", "explicit_decision"
    if any(token in lowered for token in ("is my friend", "is my sister", "мій friend", "моя sister", "мій друг", "моя сестра", "мой друг", "моя сестра")):
        return "relationship", "asserted", "durable", "explicit_relationship"
    if any(token in lowered for token in ("i live", "my city", "я живу", "моє місто", "мой город", "мене звати", "меня зовут", "my name")):
        return "fact_claim", "asserted", "durable", "explicit_fact"
    return None


def deterministic_extract(case: Mapping[str, Any]) -> dict[str, Any]:
    target_chat = str(case["target_chat_key"])
    rows = list(case["inputs"])
    candidates: list[dict[str, Any]] = []
    negative_reasons: list[str] = []
    for row in rows:
        if str(row["chat_key"]) != target_chat:
            negative_reasons.append("cross_scope")
            continue
        field, text = _text_for_row(row)
        negative = _negative_reason(text, row)
        if negative is not None:
            negative_reasons.append(negative)
            continue
        classified = _classify_candidate(text)
        if row["source_role"] == "verified_tool" and text:
            classified = ("fact_claim", "verified", "durable", "verified_tool_anchor")
        if classified is None:
            negative_reasons.append("unclassified")
            continue
        candidate_type, epistemic, durability, reason_code = classified
        supersedes: list[str] = []
        conflicts: list[str] = []
        if candidate_type == "correction":
            prior = [
                other
                for other in rows
                if other["row_key"] != row["row_key"]
                and other["chat_key"] == target_chat
                and str(other["created_at"]) < str(row["created_at"])
            ]
            for other in prior:
                if other["speaker_key"] == row["speaker_key"]:
                    supersedes.append(str(other["row_key"]))
                else:
                    conflicts.append(str(other["row_key"]))
        valid_until_match = re.search(r"2030-\d{2}-\d{2}", text)
        candidate = {
            "candidate_type": candidate_type,
            "epistemic": epistemic,
            "durability": durability,
            "evidence_refs": [{"row_key": str(row["row_key"]), "field": field}],
            "evidence_span": _normalize_span(text),
            "supersedes_row_keys": sorted(supersedes),
            "conflicts_row_keys": sorted(conflicts),
            "valid_until": valid_until_match.group(0) + "T00:00:00Z" if valid_until_match else "none",
            "confidence": 1.0,
            "reason_codes": [reason_code],
        }
        candidates.append(candidate)
    if candidates:
        reason = "none"
    else:
        priority = (
            "prior_bot_output",
            "cross_scope",
            "joke",
            "opinion",
            "hypothetical",
            "question",
            "transient",
            "forwarded_without_endorsement",
            "unsupported_tool_evidence",
            "unclassified",
        )
        reason = next((item for item in priority if item in negative_reasons), "unclassified")
    return {
        "candidates": sorted(
            candidates,
            key=lambda item: (
                item["evidence_refs"][0]["row_key"],
                item["candidate_type"],
            ),
        ),
        "no_candidate_reason": reason,
    }


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _metric(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision_total = tp + fp
    recall_total = tp + fn
    precision = tp / precision_total if precision_total else None
    recall = tp / recall_total if recall_total else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "precision_wilson_95": wilson_interval(tp, precision_total),
        "recall": recall,
        "recall_wilson_95": wilson_interval(tp, recall_total),
        "f1": f1,
    }


def _exact_nonnegative_int(value: Any) -> tuple[int, bool]:
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return (value if valid else 0), valid


def _bounded_rate(value: Any) -> tuple[float | None, bool]:
    valid = (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )
    return (float(value) if valid else None), valid


def evaluate_predictions(
    cases: Sequence[Mapping[str, Any]],
    predictions: Mapping[str, Mapping[str, Any] | None],
    *,
    fixture_hash: str,
    model: str,
    reasoning_effort: str,
    prompt_hash: str | None = None,
    repeat_count: int = 1,
    stability_rate: float | None = None,
    full_contract_stability_rate: float | None = None,
    evaluation_instance_count: int | None = None,
    evaluation_structured_valid_rate: float | None = None,
    provider_model_mismatches: int = 0,
    provider_effort_mismatches: int = 0,
    provider_metadata_missing: int = 0,
    provider_actual_models: Sequence[str] = (),
    provider_actual_efforts: Sequence[str] = (),
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_nano_usd: int | None = None,
    pricing_status: str | None = None,
    pricing_complete: bool = False,
    usage_metadata_missing: int = 0,
    pricing_snapshot_version: str | None = None,
    pricing_basis_model: str | None = None,
    input_rate_nano_usd: int | None = None,
    cached_input_rate_nano_usd: int | None = None,
    output_rate_nano_usd: int | None = None,
    concurrency: int | None = None,
    timeout_seconds: float | None = None,
    max_output_tokens: int | None = None,
    store: bool = False,
    latency_ms: Sequence[int] = (),
    failure_counts: Mapping[str, int] | None = None,
    repeat_error_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    overall_tp = overall_fp = overall_fn = 0
    durable_tp = durable_fp = durable_fn = 0
    exact_matches = 0
    structured_valid = 0
    safety_counts: Counter[str] = Counter()
    mismatch_counts: Counter[str] = Counter()
    per_type_counts: dict[str, Counter[str]] = defaultdict(Counter)
    expected_positive_by_type: Counter[str] = Counter()
    negative_case_count = 0
    negative_false_positive_count = 0
    for case in cases:
        case_id = str(case["id"])
        expected, expected_errors = validate_prediction(case, case["expected"], expected_mode=True)
        if expected_errors:
            raise ValueError(f"fixture_expected_invalid:{case_id}")
        raw_prediction = predictions.get(case_id)
        if raw_prediction is None:
            predicted = {"candidates": [], "no_candidate_reason": "unclassified"}
            errors = ["provider_failure"]
        else:
            predicted, errors = validate_prediction(case, raw_prediction)
        if not errors:
            structured_valid += 1
        for error in errors:
            parts = error.split(":")
            if parts[0].startswith("candidate_") and len(parts) > 1:
                bucket = parts[1]
            elif len(parts) > 1:
                bucket = "_".join(parts[:2])
            else:
                bucket = parts[0]
            safety_counts[bucket] += 1
        expected_set = {_candidate_identity(item) for item in expected["candidates"]}
        predicted_set = {_candidate_identity(item) for item in predicted["candidates"]}
        if not expected_set:
            negative_case_count += 1
            if predicted_set:
                negative_false_positive_count += 1
        expected_by_refs = {
            tuple(sorted((str(ref["row_key"]), str(ref["field"])) for ref in item["evidence_refs"])): item
            for item in expected["candidates"]
        }
        predicted_by_refs = {
            tuple(sorted((str(ref["row_key"]), str(ref["field"])) for ref in item["evidence_refs"])): item
            for item in predicted["candidates"]
        }
        for refs in set(expected_by_refs) | set(predicted_by_refs):
            expected_candidate = expected_by_refs.get(refs)
            predicted_candidate = predicted_by_refs.get(refs)
            if expected_candidate is None:
                mismatch_counts["unexpected_candidate"] += 1
                continue
            if predicted_candidate is None:
                mismatch_counts["missing_candidate"] += 1
                continue
            for field in (
                "candidate_type",
                "subject_key",
                "source_role",
                "epistemic",
                "durability",
                "lifecycle",
                "evidence_span",
                "supersedes_row_keys",
                "conflicts_row_keys",
                "valid_from",
                "valid_until",
                "reason_codes",
            ):
                if predicted_candidate[field] != expected_candidate[field]:
                    mismatch_counts[field] += 1
        if predicted["no_candidate_reason"] != expected["no_candidate_reason"]:
            mismatch_counts["no_candidate_reason"] += 1
        tp_set = expected_set & predicted_set
        fp_set = predicted_set - expected_set
        fn_set = expected_set - predicted_set
        overall_tp += len(tp_set)
        overall_fp += len(fp_set)
        overall_fn += len(fn_set)
        if expected_set == predicted_set:
            exact_matches += 1
        for identity in expected_set:
            expected_positive_by_type[str(identity[0])] += 1
        for identity in tp_set:
            per_type_counts[str(identity[0])]["tp"] += 1
            if identity[4] == "durable":
                durable_tp += 1
        for identity in fp_set:
            per_type_counts[str(identity[0])]["fp"] += 1
            if identity[4] == "durable":
                durable_fp += 1
        for identity in fn_set:
            per_type_counts[str(identity[0])]["fn"] += 1
            if identity[4] == "durable":
                durable_fn += 1
    per_type = {
        candidate_type: _metric(
            per_type_counts[candidate_type]["tp"],
            per_type_counts[candidate_type]["fp"],
            per_type_counts[candidate_type]["fn"],
        )
        for candidate_type in CANDIDATE_TYPES
        if expected_positive_by_type[candidate_type]
    }
    overall = _metric(overall_tp, overall_fp, overall_fn)
    durable = _metric(durable_tp, durable_fp, durable_fn)
    structured_rate = structured_valid / len(cases) if cases else 0.0
    safety_total = sum(safety_counts.values())
    per_type_recall_gate = all(metric["recall"] >= 0.60 for metric in per_type.values())
    per_type_recall_wilson_gate = all(
        metric["recall_wilson_95"] is not None
        and metric["recall_wilson_95"][0] >= 0.60
        for metric in per_type.values()
    )
    fixture_splits = {str(case.get("split", "")) for case in cases}
    case_set_hash = fixture_case_set_sha256(cases)
    schema_hash = output_schema_sha256()
    bundle_hash = evaluation_bundle_sha256()
    repeats, repeat_count_valid = _exact_nonnegative_int(repeat_count)
    instance_count, instance_count_valid = _exact_nonnegative_int(
        evaluation_instance_count
    )
    stability_value, stability_valid = _bounded_rate(stability_rate)
    full_contract_stability_value, _full_contract_stability_valid = _bounded_rate(
        full_contract_stability_rate
    )
    all_structured_rate, all_structured_rate_valid = _bounded_rate(
        evaluation_structured_valid_rate
    )
    failure_mapping_valid = isinstance(failure_counts, Mapping) or failure_counts is None
    failures = (
        dict(sorted((failure_counts or {}).items()))
        if failure_mapping_valid
        else {}
    )
    failure_values = [_exact_nonnegative_int(value) for value in failures.values()]
    failures_valid = failure_mapping_valid and all(valid for _value, valid in failure_values)
    failure_total = sum(value for value, _valid in failure_values)
    is_baseline = str(model) == "deterministic-baseline-v1"
    model_mismatches, model_mismatches_valid = _exact_nonnegative_int(
        provider_model_mismatches
    )
    effort_mismatches, effort_mismatches_valid = _exact_nonnegative_int(
        provider_effort_mismatches
    )
    metadata_missing, metadata_missing_valid = _exact_nonnegative_int(
        provider_metadata_missing
    )
    usage_missing, usage_missing_valid = _exact_nonnegative_int(
        usage_metadata_missing
    )
    input_count, input_count_valid = _exact_nonnegative_int(input_tokens)
    cached_input_count, cached_input_count_valid = _exact_nonnegative_int(
        cached_input_tokens
    )
    output_count, output_count_valid = _exact_nonnegative_int(output_tokens)
    actual_models_input_valid = isinstance(provider_actual_models, Sequence) and not isinstance(
        provider_actual_models, (str, bytes)
    )
    actual_efforts_input_valid = isinstance(provider_actual_efforts, Sequence) and not isinstance(
        provider_actual_efforts, (str, bytes)
    )
    actual_models = sorted(
        set(str(item) for item in provider_actual_models if item)
    ) if actual_models_input_valid else []
    actual_efforts = sorted(
        set(str(item) for item in provider_actual_efforts if item)
    ) if actual_efforts_input_valid else []
    requested_effort = str(reasoning_effort)
    provider_evidence_complete = (
        not is_baseline
        and metadata_missing_valid
        and metadata_missing == 0
        and actual_models_input_valid
        and actual_efforts_input_valid
        and bool(actual_models)
        and all(provider_model_matches(str(model), item) for item in actual_models)
        and (
            requested_effort == "omit"
            or (bool(actual_efforts) and all(item == requested_effort for item in actual_efforts))
        )
    )
    cost_value, cost_value_valid = _exact_nonnegative_int(estimated_cost_nano_usd)
    input_rate, input_rate_valid = _exact_nonnegative_int(input_rate_nano_usd)
    cached_rate, cached_rate_valid = _exact_nonnegative_int(
        cached_input_rate_nano_usd
    )
    output_rate, output_rate_valid = _exact_nonnegative_int(output_rate_nano_usd)
    pricing_evidence_complete = (
        not is_baseline
        and pricing_complete is True
        and pricing_status == "estimated"
        and isinstance(pricing_snapshot_version, str)
        and bool(pricing_snapshot_version)
        and isinstance(pricing_basis_model, str)
        and pricing_basis_model.casefold() == str(model).casefold()
        and cost_value_valid
        and cost_value > 0
        and input_rate_valid
        and input_rate > 0
        and cached_rate_valid
        and output_rate_valid
        and output_rate > 0
    )
    expected_cost = (
        (input_count - cached_input_count) * input_rate
        + cached_input_count * cached_rate
        + output_count * output_rate
        if input_count_valid
        and cached_input_count_valid
        and output_count_valid
        and input_rate_valid
        and cached_rate_valid
        and output_rate_valid
        and cached_input_count <= input_count
        else None
    )
    pricing_cost_consistent = (
        pricing_evidence_complete
        and expected_cost is not None
        and cost_value == expected_cost
    )
    usage_evidence_complete = (
        not is_baseline
        and usage_missing_valid
        and usage_missing == 0
        and input_count_valid
        and cached_input_count_valid
        and output_count_valid
        and input_count > 0
        and output_count > 0
        and cached_input_count <= input_count
    )
    overall_precision_wilson = overall["precision_wilson_95"]
    durable_precision_wilson = durable["precision_wilson_95"]
    durable_recall_wilson = durable["recall_wilson_95"]
    gates = {
        "full_fixture": len(cases) == 120,
        "frozen_holdout": fixture_splits == {"holdout"},
        "frozen_fixture_hash_matches": fixture_hash == FROZEN_FIXTURE_SHA256,
        "frozen_holdout_case_hash_matches": case_set_hash == FROZEN_HOLDOUT_SHA256,
        "prompt_hash_matches": prompt_hash == FROZEN_PROMPT_SHA256,
        "output_schema_hash_matches": schema_hash == FROZEN_OUTPUT_SCHEMA_SHA256,
        "evaluation_bundle_hash_matches": bundle_hash == FROZEN_EVALUATION_BUNDLE_SHA256,
        "repeat_count_exactly_3": repeat_count_valid and repeats == 3,
        "candidate_behavior_stability_at_least_099": stability_valid and stability_value is not None and stability_value >= 0.99,
        "evaluation_instance_count_matches": instance_count_valid and instance_count == len(cases) * repeats,
        "structured_valid_rate_100": structured_rate == 1.0,
        "all_repeat_structured_valid_rate_100": all_structured_rate_valid and all_structured_rate == 1.0,
        "overall_precision_at_least_095": overall["precision"] is not None and overall["precision"] >= 0.95,
        "overall_precision_wilson_lower_at_least_095": overall_precision_wilson is not None and overall_precision_wilson[0] >= 0.95,
        "durable_precision_at_least_095": durable["precision"] is not None and durable["precision"] >= 0.95,
        "durable_precision_wilson_lower_at_least_095": durable_precision_wilson is not None and durable_precision_wilson[0] >= 0.95,
        "durable_recall_at_least_080": durable["recall"] >= 0.80,
        "durable_recall_wilson_lower_at_least_080": durable_recall_wilson is not None and durable_recall_wilson[0] >= 0.80,
        "per_positive_type_recall_at_least_060": per_type_recall_gate,
        "per_positive_type_recall_wilson_lower_at_least_060": per_type_recall_wilson_gate,
        "negative_case_false_positives_zero": negative_false_positive_count == 0,
        "hard_safety_violations_zero": safety_total == 0,
        "provider_model_mismatches_zero": model_mismatches_valid and model_mismatches == 0,
        "provider_effort_mismatches_zero": effort_mismatches_valid and effort_mismatches == 0,
        "provider_metadata_complete": provider_evidence_complete,
        "provider_failures_zero": failures_valid and failure_total == 0,
        "pricing_complete": pricing_evidence_complete,
        "pricing_cost_consistent": pricing_cost_consistent,
        "usage_metadata_complete": usage_evidence_complete,
        "api_model_candidate": not is_baseline,
    }
    if is_baseline:
        verdict = "INCONCLUSIVE"
    elif not gates["full_fixture"] or not gates["frozen_holdout"]:
        verdict = "INCONCLUSIVE"
    elif all(gates.values()):
        verdict = "GO_FOR_RUNTIME_SHADOW_PR"
    else:
        verdict = "NO_GO"
    latencies = sorted(max(0, int(value)) for value in latency_ms)
    def percentile(values: Sequence[int], percentile_value: float) -> int | None:
        if not values:
            return None
        index = min(len(values) - 1, max(0, math.ceil(percentile_value * len(values)) - 1))
        return int(values[index])
    return {
        "fixture": {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "sha256": fixture_hash,
            "case_set_sha256": case_set_hash,
            "unique_case_count": len(cases),
            "evaluation_instance_count": instance_count,
        },
        "versions": {
            "output_schema": OUTPUT_SCHEMA_VERSION,
            "output_schema_sha256": schema_hash,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_hash,
            "evaluator": EVALUATOR_VERSION,
            "evaluation_bundle_sha256": bundle_hash,
        },
        "run": {
            "model": str(model),
            "reasoning_effort": str(reasoning_effort),
            "repeat_count": repeats,
            "concurrency": concurrency,
            "timeout_seconds": timeout_seconds,
            "max_output_tokens": max_output_tokens,
            "store": bool(store),
        },
        "quality": {
            "overall": overall,
            "durable": durable,
            "per_type": per_type,
            "exact_candidate_set_matches": exact_matches,
            "mismatch_buckets": dict(sorted(mismatch_counts.items())),
            "exact_candidate_set_rate": exact_matches / len(cases) if cases else 0.0,
            "structured_valid": structured_valid,
            "structured_valid_rate": structured_rate,
            "all_repeat_structured_valid_rate": all_structured_rate,
            "negative_case_count": negative_case_count,
            "negative_case_false_positive_count": negative_false_positive_count,
        },
        "safety": {
            "hard_violation_count": safety_total,
            "error_buckets": dict(sorted(safety_counts.items())),
            "repeat_error_buckets": dict(sorted((repeat_error_counts or {}).items())),
        },
        "provider": {
            "model_mismatches": model_mismatches,
            "effort_mismatches": effort_mismatches,
            "metadata_missing": metadata_missing,
            "actual_models": actual_models,
            "actual_efforts": actual_efforts,
            "failure_counts": failures,
        },
        "usage": {
            "input_tokens": input_count,
            "cached_input_tokens": cached_input_count,
            "output_tokens": output_count,
            "estimated_cost_nano_usd": cost_value if cost_value_valid else None,
            "recomputed_cost_nano_usd": expected_cost,
            "pricing_status": pricing_status,
            "pricing_complete": pricing_evidence_complete,
            "usage_metadata_missing": usage_missing,
            "pricing_snapshot_version": pricing_snapshot_version,
            "pricing_basis_model": pricing_basis_model,
            "input_rate_nano_usd": input_rate if input_rate_valid else None,
            "cached_input_rate_nano_usd": cached_rate if cached_rate_valid else None,
            "output_rate_nano_usd": output_rate if output_rate_valid else None,
        },
        "latency_ms": {
            "count": len(latencies),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
        },
        "gates": gates,
        "stability": {
            "candidate_behavior_match_rate": stability_value,
            "full_contract_match_rate": full_contract_stability_value,
        },
        "verdict": verdict,
        "runtime_authorized": False,
    }


def aggregate_report_has_private_fields(report: Mapping[str, Any]) -> bool:
    forbidden = {
        "text",
        "authored_text",
        "source_text",
        "tool_evidence",
        "evidence_span",
        "value",
        "model_output",
        "prompt",
        "row_key",
        "chat_key",
        "speaker_key",
    }
    stack: list[Any] = [report]
    while stack:
        value = stack.pop()
        if isinstance(value, Mapping):
            if forbidden.intersection(str(key) for key in value):
                return True
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            stack.extend(value)
    return False
