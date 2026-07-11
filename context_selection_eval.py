from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import random
import re
import statistics
import time
from typing import Any, Iterable, Mapping, Sequence


CASE_SCHEMA_VERSION = "context-selection-case-v1"
REPORT_SCHEMA_VERSION = "context-selection-report-v1"
ARMS = ("B0", "B1", "C1")
CORPUS_KINDS = {"public_synthetic", "private_replay"}
ACTIONS = {"answer", "clarify", "abstain"}
PRIMARY_CLASSES = (
    "explicit_reply",
    "short_followup",
    "same_question_transform",
    "topic_shift_distractors",
    "knowledge_update",
    "correction_stale_guardrail",
)
MIN_FAMILIES_PER_CLASS = 5
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


class ContextSelectionFixtureError(ValueError):
    """Raised for a bounded fixture-contract failure without echoing payloads."""


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    speaker_key: str
    source_kind: str
    created_at: datetime
    text: str
    reply_to_source_id: str | None
    is_current_payload: bool
    is_structured_reply: bool
    reply_depth: int | None
    recent_rank: int | None
    semantic_rank: int | None
    keyword_rank: int | None
    fts_rank: int | None


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    summary: str
    created_at: datetime
    anchor_source_id: str
    source_ids: tuple[str, ...]
    semantic_rank: int | None
    keyword_rank: int | None
    fts_rank: int | None
    amortized_calls: float
    amortized_input_tokens: float
    amortized_output_tokens: float
    amortized_latency_ms: float


@dataclass(frozen=True)
class DeployedSnapshot:
    selected_source_ids: tuple[str, ...]
    context_chars: int
    compile_ms: float
    recommended_action: str


@dataclass(frozen=True)
class SnapshotMetadata:
    retrieval_snapshot_kind: str
    source_commit: str
    effective_config_sha256: str
    candidate_snapshot_sha256: str
    query_embedding_sha256: str | None
    embedding_model: str
    answer_model: str
    reasoning_effort: str
    b0_compiler_version: str


@dataclass(frozen=True)
class ExpectedLabels:
    acceptable_anchor_source_ids: frozenset[str]
    required_source_ids: frozenset[str]
    optional_source_ids: frozenset[str]
    forbidden_source_ids: frozenset[str]
    stale_source_ids: frozenset[str]
    wrong_speaker_source_ids: frozenset[str]
    expected_action: str


@dataclass(frozen=True)
class SelectionInput:
    target_time: datetime
    query: str
    budget_chars: int
    snapshot: SnapshotMetadata
    sources: tuple[SourceRecord, ...]
    events: tuple[EventRecord, ...]


@dataclass(frozen=True)
class ContextCase:
    corpus_kind: str
    split: str
    language: str
    case_id: str
    case_family: str
    eligibility_class: str
    deployed: DeployedSnapshot
    selection_input: SelectionInput
    expected: ExpectedLabels


@dataclass(frozen=True)
class SelectorConfig:
    version: str = "context-selector-v1"
    rrf_k: int = 60
    semantic_weight: float = 1.0
    keyword_weight: float = 0.8
    fts_weight: float = 0.8
    recent_weight: float = 0.35
    max_sources: int = 12
    max_events: int = 1
    minimum_useful_effect: float = 0.10
    bootstrap_seed: int = 119

    def __post_init__(self) -> None:
        if self.rrf_k <= 0 or self.max_sources <= 0:
            raise ValueError("selector limits must be positive")
        if self.max_events != 1:
            raise ValueError("C1 is frozen to exactly one event anchor")
        if not 0 < self.minimum_useful_effect < 1:
            raise ValueError("minimum useful effect must be between zero and one")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rrf_k": self.rrf_k,
            "semantic_weight": self.semantic_weight,
            "keyword_weight": self.keyword_weight,
            "fts_weight": self.fts_weight,
            "recent_weight": self.recent_weight,
            "max_sources": self.max_sources,
            "max_events": self.max_events,
            "minimum_useful_effect": self.minimum_useful_effect,
            "bootstrap_seed": self.bootstrap_seed,
        }

    def sha256(self) -> str:
        payload = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SelectionResult:
    arm: str
    selected_source_ids: tuple[str, ...]
    selected_event_ids: tuple[str, ...]
    context_chars: int
    navigation_chars: int
    compile_ms: float
    recommended_action: str
    dropped_by_reason: Mapping[str, int]
    candidate_construction_calls: float = 0.0
    candidate_construction_input_tokens: float = 0.0
    candidate_construction_output_tokens: float = 0.0
    candidate_construction_latency_ms: float = 0.0


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    case_family: str
    eligibility_class: str
    arm: str
    top1_eligible: bool
    top1_correct: bool
    relevant_selected: int
    selected_total: int
    required_selected: int
    required_total: int
    reciprocal_rank: float
    forbidden_selected: int
    stale_selected: int
    wrong_speaker_selected: int
    action_correct: bool
    context_chars: int
    navigation_chars: int
    compile_ms: float
    candidate_construction_calls: float
    candidate_construction_input_tokens: float
    candidate_construction_output_tokens: float
    candidate_construction_latency_ms: float
    dropped_by_reason: Mapping[str, int]


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ContextSelectionFixtureError(
            f"{location}: invalid fields missing={missing} extra={extra}"
        )


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextSelectionFixtureError(f"{location}: expected object")
    return value


def _require_string(value: Any, location: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ContextSelectionFixtureError(f"{location}: expected non-empty string")
    return value


def _require_identifier(value: Any, location: str) -> str:
    text = _require_string(value, location)
    if not IDENTIFIER_RE.fullmatch(text):
        raise ContextSelectionFixtureError(f"{location}: invalid opaque identifier")
    return text


def _require_sha256(value: Any, location: str) -> str:
    text = _require_string(value, location)
    if not SHA256_RE.fullmatch(text):
        raise ContextSelectionFixtureError(f"{location}: invalid SHA-256")
    return text


def candidate_snapshot_sha256(case_payload: Mapping[str, Any]) -> str:
    payload = {
        key: case_payload[key]
        for key in ("target_time", "query", "budget_chars", "b0", "sources", "events")
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_bool(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise ContextSelectionFixtureError(f"{location}: expected boolean")
    return value


def _require_nonnegative_int(value: Any, location: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContextSelectionFixtureError(f"{location}: expected non-negative integer")
    return value


def _require_nonnegative_number(value: Any, location: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ContextSelectionFixtureError(f"{location}: expected non-negative finite number")
    return float(value)


def _optional_rank(value: Any, location: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContextSelectionFixtureError(f"{location}: expected null or positive integer")
    return value


def _optional_identifier(value: Any, location: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, location)


def _parse_timestamp(value: Any, location: str) -> datetime:
    text = _require_string(value, location)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextSelectionFixtureError(f"{location}: invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContextSelectionFixtureError(f"{location}: timezone is required")
    return parsed


def _identifier_tuple(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContextSelectionFixtureError(f"{location}: expected array")
    identifiers = tuple(_require_identifier(item, f"{location}[]") for item in value)
    if len(identifiers) != len(set(identifiers)):
        raise ContextSelectionFixtureError(f"{location}: duplicate identifiers")
    return identifiers


def _parse_source(raw: Any, location: str) -> SourceRecord:
    value = _require_mapping(raw, location)
    _require_exact_keys(
        value,
        {
            "source_id",
            "speaker_key",
            "source_kind",
            "created_at",
            "text",
            "reply_to_source_id",
            "is_current_payload",
            "is_structured_reply",
            "reply_depth",
            "recent_rank",
            "semantic_rank",
            "keyword_rank",
            "fts_rank",
        },
        location,
    )
    reply_depth = value["reply_depth"]
    if reply_depth is not None:
        reply_depth = _require_nonnegative_int(reply_depth, f"{location}.reply_depth")
    return SourceRecord(
        source_id=_require_identifier(value["source_id"], f"{location}.source_id"),
        speaker_key=_require_identifier(value["speaker_key"], f"{location}.speaker_key"),
        source_kind=_require_identifier(value["source_kind"], f"{location}.source_kind"),
        created_at=_parse_timestamp(value["created_at"], f"{location}.created_at"),
        text=_require_string(value["text"], f"{location}.text"),
        reply_to_source_id=_optional_identifier(value["reply_to_source_id"], f"{location}.reply_to_source_id"),
        is_current_payload=_require_bool(value["is_current_payload"], f"{location}.is_current_payload"),
        is_structured_reply=_require_bool(value["is_structured_reply"], f"{location}.is_structured_reply"),
        reply_depth=reply_depth,
        recent_rank=_optional_rank(value["recent_rank"], f"{location}.recent_rank"),
        semantic_rank=_optional_rank(value["semantic_rank"], f"{location}.semantic_rank"),
        keyword_rank=_optional_rank(value["keyword_rank"], f"{location}.keyword_rank"),
        fts_rank=_optional_rank(value["fts_rank"], f"{location}.fts_rank"),
    )


def _parse_event(raw: Any, location: str) -> EventRecord:
    value = _require_mapping(raw, location)
    _require_exact_keys(
        value,
        {
            "event_id",
            "summary",
            "created_at",
            "anchor_source_id",
            "source_ids",
            "semantic_rank",
            "keyword_rank",
            "fts_rank",
            "amortized_calls",
            "amortized_input_tokens",
            "amortized_output_tokens",
            "amortized_latency_ms",
        },
        location,
    )
    return EventRecord(
        event_id=_require_identifier(value["event_id"], f"{location}.event_id"),
        summary=_require_string(value["summary"], f"{location}.summary"),
        created_at=_parse_timestamp(value["created_at"], f"{location}.created_at"),
        anchor_source_id=_require_identifier(value["anchor_source_id"], f"{location}.anchor_source_id"),
        source_ids=_identifier_tuple(value["source_ids"], f"{location}.source_ids"),
        semantic_rank=_optional_rank(value["semantic_rank"], f"{location}.semantic_rank"),
        keyword_rank=_optional_rank(value["keyword_rank"], f"{location}.keyword_rank"),
        fts_rank=_optional_rank(value["fts_rank"], f"{location}.fts_rank"),
        amortized_calls=_require_nonnegative_number(value["amortized_calls"], f"{location}.amortized_calls"),
        amortized_input_tokens=_require_nonnegative_number(
            value["amortized_input_tokens"], f"{location}.amortized_input_tokens"
        ),
        amortized_output_tokens=_require_nonnegative_number(
            value["amortized_output_tokens"], f"{location}.amortized_output_tokens"
        ),
        amortized_latency_ms=_require_nonnegative_number(
            value["amortized_latency_ms"], f"{location}.amortized_latency_ms"
        ),
    )


def _parse_case(raw: Any, line_number: int) -> ContextCase:
    location = f"line {line_number}"
    value = _require_mapping(raw, location)
    _require_exact_keys(
        value,
        {
            "schema_version",
            "corpus_kind",
            "split",
            "case_id",
            "case_family",
            "eligibility_class",
            "language",
            "target_time",
            "query",
            "budget_chars",
            "snapshot",
            "b0",
            "sources",
            "events",
            "expected",
        },
        location,
    )
    if value["schema_version"] != CASE_SCHEMA_VERSION:
        raise ContextSelectionFixtureError(f"{location}: unsupported schema version")
    corpus_kind = _require_string(value["corpus_kind"], f"{location}.corpus_kind")
    if corpus_kind not in CORPUS_KINDS:
        raise ContextSelectionFixtureError(f"{location}.corpus_kind: unsupported value")
    target_time = _parse_timestamp(value["target_time"], f"{location}.target_time")

    raw_snapshot = _require_mapping(value["snapshot"], f"{location}.snapshot")
    _require_exact_keys(
        raw_snapshot,
        {
            "retrieval_snapshot_kind",
            "source_commit",
            "effective_config_sha256",
            "candidate_snapshot_sha256",
            "query_embedding_sha256",
            "embedding_model",
            "answer_model",
            "reasoning_effort",
            "b0_compiler_version",
        },
        f"{location}.snapshot",
    )
    retrieval_snapshot_kind = _require_identifier(
        raw_snapshot["retrieval_snapshot_kind"], f"{location}.snapshot.retrieval_snapshot_kind"
    )
    if retrieval_snapshot_kind not in {"synthetic_ranks", "frozen_live_scores"}:
        raise ContextSelectionFixtureError(f"{location}.snapshot: unsupported retrieval snapshot")
    source_commit = _require_string(raw_snapshot["source_commit"], f"{location}.snapshot.source_commit")
    if not COMMIT_RE.fullmatch(source_commit):
        raise ContextSelectionFixtureError(f"{location}.snapshot.source_commit: invalid commit")
    query_embedding_sha256 = raw_snapshot["query_embedding_sha256"]
    if query_embedding_sha256 is not None:
        query_embedding_sha256 = _require_sha256(
            query_embedding_sha256, f"{location}.snapshot.query_embedding_sha256"
        )
    if corpus_kind == "private_replay" and (
        retrieval_snapshot_kind != "frozen_live_scores" or query_embedding_sha256 is None
    ):
        raise ContextSelectionFixtureError(f"{location}.snapshot: private replay requires frozen embedding")
    if corpus_kind == "public_synthetic" and (
        retrieval_snapshot_kind != "synthetic_ranks" or query_embedding_sha256 is not None
    ):
        raise ContextSelectionFixtureError(f"{location}.snapshot: synthetic fixture cannot claim live embedding")
    snapshot = SnapshotMetadata(
        retrieval_snapshot_kind=retrieval_snapshot_kind,
        source_commit=source_commit,
        effective_config_sha256=_require_sha256(
            raw_snapshot["effective_config_sha256"], f"{location}.snapshot.effective_config_sha256"
        ),
        candidate_snapshot_sha256=_require_sha256(
            raw_snapshot["candidate_snapshot_sha256"], f"{location}.snapshot.candidate_snapshot_sha256"
        ),
        query_embedding_sha256=query_embedding_sha256,
        embedding_model=_require_identifier(
            raw_snapshot["embedding_model"], f"{location}.snapshot.embedding_model"
        ),
        answer_model=_require_identifier(raw_snapshot["answer_model"], f"{location}.snapshot.answer_model"),
        reasoning_effort=_require_identifier(
            raw_snapshot["reasoning_effort"], f"{location}.snapshot.reasoning_effort"
        ),
        b0_compiler_version=_require_string(
            raw_snapshot["b0_compiler_version"], f"{location}.snapshot.b0_compiler_version"
        ),
    )
    if snapshot.reasoning_effort not in {"none", "low", "medium", "high"}:
        raise ContextSelectionFixtureError(f"{location}.snapshot.reasoning_effort: unsupported value")
    if snapshot.candidate_snapshot_sha256 != candidate_snapshot_sha256(value):
        raise ContextSelectionFixtureError(f"{location}.snapshot: candidate snapshot hash mismatch")

    raw_sources = value["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ContextSelectionFixtureError(f"{location}.sources: expected non-empty array")
    sources = tuple(_parse_source(item, f"{location}.sources[{index}]") for index, item in enumerate(raw_sources))
    source_ids = {item.source_id for item in sources}
    if len(source_ids) != len(sources):
        raise ContextSelectionFixtureError(f"{location}.sources: duplicate source_id")
    for source in sources:
        if source.created_at >= target_time:
            raise ContextSelectionFixtureError(f"{location}.sources: future source")
        if source.reply_to_source_id is not None and source.reply_to_source_id not in source_ids:
            raise ContextSelectionFixtureError(f"{location}.sources: unknown reply target")

    raw_events = value["events"]
    if not isinstance(raw_events, list):
        raise ContextSelectionFixtureError(f"{location}.events: expected array")
    events = tuple(_parse_event(item, f"{location}.events[{index}]") for index, item in enumerate(raw_events))
    if len({item.event_id for item in events}) != len(events):
        raise ContextSelectionFixtureError(f"{location}.events: duplicate event_id")
    source_by_id = {source.source_id: source for source in sources}
    for event in events:
        if event.created_at >= target_time:
            raise ContextSelectionFixtureError(f"{location}.events: future event")
        if event.anchor_source_id not in event.source_ids or not set(event.source_ids).issubset(source_ids):
            raise ContextSelectionFixtureError(f"{location}.events: invalid provenance links")
        if any(source_by_id[source_id].created_at > event.created_at for source_id in event.source_ids):
            raise ContextSelectionFixtureError(f"{location}.events: event precedes linked source")

    raw_b0 = _require_mapping(value["b0"], f"{location}.b0")
    _require_exact_keys(
        raw_b0,
        {"selected_source_ids", "context_chars", "compile_ms", "recommended_action"},
        f"{location}.b0",
    )
    b0_ids = _identifier_tuple(raw_b0["selected_source_ids"], f"{location}.b0.selected_source_ids")
    if not set(b0_ids).issubset(source_ids):
        raise ContextSelectionFixtureError(f"{location}.b0: unknown selected source")
    b0_action = _require_string(raw_b0["recommended_action"], f"{location}.b0.recommended_action")
    if b0_action not in ACTIONS:
        raise ContextSelectionFixtureError(f"{location}.b0.recommended_action: unsupported value")
    deployed = DeployedSnapshot(
        selected_source_ids=b0_ids,
        context_chars=_require_nonnegative_int(raw_b0["context_chars"], f"{location}.b0.context_chars"),
        compile_ms=_require_nonnegative_number(raw_b0["compile_ms"], f"{location}.b0.compile_ms"),
        recommended_action=b0_action,
    )

    raw_expected = _require_mapping(value["expected"], f"{location}.expected")
    _require_exact_keys(
        raw_expected,
        {
            "acceptable_anchor_source_ids",
            "required_source_ids",
            "optional_source_ids",
            "forbidden_source_ids",
            "stale_source_ids",
            "wrong_speaker_source_ids",
            "expected_action",
        },
        f"{location}.expected",
    )
    label_sets = {
        key: frozenset(_identifier_tuple(raw_expected[key], f"{location}.expected.{key}"))
        for key in (
            "acceptable_anchor_source_ids",
            "required_source_ids",
            "optional_source_ids",
            "forbidden_source_ids",
            "stale_source_ids",
            "wrong_speaker_source_ids",
        )
    }
    if not all(items.issubset(source_ids) for items in label_sets.values()):
        raise ContextSelectionFixtureError(f"{location}.expected: unknown source label")
    relevant = (
        label_sets["acceptable_anchor_source_ids"]
        | label_sets["required_source_ids"]
        | label_sets["optional_source_ids"]
    )
    if relevant & label_sets["forbidden_source_ids"]:
        raise ContextSelectionFixtureError(f"{location}.expected: relevant and forbidden overlap")
    if not label_sets["stale_source_ids"].issubset(label_sets["forbidden_source_ids"]):
        raise ContextSelectionFixtureError(f"{location}.expected: stale source must be forbidden")
    if not label_sets["wrong_speaker_source_ids"].issubset(label_sets["forbidden_source_ids"]):
        raise ContextSelectionFixtureError(f"{location}.expected: wrong-speaker source must be forbidden")
    expected_action = _require_string(raw_expected["expected_action"], f"{location}.expected.expected_action")
    if expected_action not in ACTIONS:
        raise ContextSelectionFixtureError(f"{location}.expected.expected_action: unsupported value")
    if expected_action == "answer" and (
        not label_sets["acceptable_anchor_source_ids"] or not label_sets["required_source_ids"]
    ):
        raise ContextSelectionFixtureError(f"{location}.expected: answer case needs anchor and evidence")

    eligibility_class = _require_identifier(
        value["eligibility_class"], f"{location}.eligibility_class"
    )
    if eligibility_class not in PRIMARY_CLASSES:
        raise ContextSelectionFixtureError(f"{location}.eligibility_class: not preregistered")
    return ContextCase(
        corpus_kind=corpus_kind,
        split=_require_identifier(value["split"], f"{location}.split"),
        language=_require_identifier(value["language"], f"{location}.language"),
        case_id=_require_identifier(value["case_id"], f"{location}.case_id"),
        case_family=_require_identifier(value["case_family"], f"{location}.case_family"),
        eligibility_class=eligibility_class,
        deployed=deployed,
        selection_input=SelectionInput(
            target_time=target_time,
            query=_require_string(value["query"], f"{location}.query"),
            budget_chars=_require_nonnegative_int(value["budget_chars"], f"{location}.budget_chars"),
            snapshot=snapshot,
            sources=sources,
            events=events,
        ),
        expected=ExpectedLabels(
            acceptable_anchor_source_ids=label_sets["acceptable_anchor_source_ids"],
            required_source_ids=label_sets["required_source_ids"],
            optional_source_ids=label_sets["optional_source_ids"],
            forbidden_source_ids=label_sets["forbidden_source_ids"],
            stale_source_ids=label_sets["stale_source_ids"],
            wrong_speaker_source_ids=label_sets["wrong_speaker_source_ids"],
            expected_action=expected_action,
        ),
    )


def load_context_selection_fixture(path: Path | str) -> list[ContextCase]:
    cases: list[ContextCase] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContextSelectionFixtureError(f"line {line_number}: invalid JSON") from exc
            cases.append(_parse_case(raw, line_number))
    if not cases:
        raise ContextSelectionFixtureError("fixture: no cases")
    if len({case.case_id for case in cases}) != len(cases):
        raise ContextSelectionFixtureError("fixture: duplicate case_id")
    if len({case.corpus_kind for case in cases}) != 1:
        raise ContextSelectionFixtureError("fixture: mixed corpus kinds")
    if len({case.split for case in cases}) != 1:
        raise ContextSelectionFixtureError("fixture: mixed splits")
    if cases[0].corpus_kind == "private_replay" and cases[0].split not in {"development", "holdout"}:
        raise ContextSelectionFixtureError("fixture: invalid private split")
    snapshot_contracts = {
        (
            case.selection_input.snapshot.source_commit,
            case.selection_input.snapshot.effective_config_sha256,
            case.selection_input.snapshot.embedding_model,
            case.selection_input.snapshot.answer_model,
            case.selection_input.snapshot.reasoning_effort,
            case.selection_input.snapshot.b0_compiler_version,
        )
        for case in cases
    }
    if len(snapshot_contracts) != 1:
        raise ContextSelectionFixtureError("fixture: mixed source/config/model snapshots")
    return cases


def _source_char_cost(source: SourceRecord) -> int:
    return len(source.text) + len(source.speaker_key) + len(source.source_kind) + 48


def _event_summary_char_cost(event: EventRecord) -> int:
    return len(event.summary) + len(event.event_id) + 32


def _payload_key(source: SourceRecord) -> tuple[str, str, str, str]:
    normalized = " ".join(TOKEN_RE.findall(source.text.casefold()))
    return (
        source.speaker_key,
        source.source_kind,
        normalized,
        source.reply_to_source_id or "",
    )


def _rrf_score(
    *,
    semantic_rank: int | None,
    keyword_rank: int | None,
    fts_rank: int | None,
    recent_rank: int | None,
    config: SelectorConfig,
) -> float:
    score = 0.0
    for rank, weight in (
        (semantic_rank, config.semantic_weight),
        (keyword_rank, config.keyword_weight),
        (fts_rank, config.fts_weight),
        (recent_rank, config.recent_weight),
    ):
        if rank is not None:
            score += weight / (config.rrf_k + rank)
    return score


def _source_tier(source: SourceRecord) -> int:
    if source.is_current_payload or source.is_structured_reply:
        return 0
    if source.reply_depth is not None:
        return 1
    if any(rank is not None for rank in (source.semantic_rank, source.keyword_rank, source.fts_rank)):
        return 2
    if source.recent_rank is not None:
        return 3
    return 4


def rank_b1_sources(selection_input: SelectionInput, config: SelectorConfig) -> list[SourceRecord]:
    def key(source: SourceRecord) -> tuple[Any, ...]:
        tier = _source_tier(source)
        explicit_order = 0 if source.is_current_payload else 1
        reply_depth = source.reply_depth if source.reply_depth is not None else 1_000_000
        score = _rrf_score(
            semantic_rank=source.semantic_rank,
            keyword_rank=source.keyword_rank,
            fts_rank=source.fts_rank,
            recent_rank=source.recent_rank,
            config=config,
        )
        recent_rank = source.recent_rank if source.recent_rank is not None else 1_000_000
        return (
            tier,
            explicit_order if tier == 0 else 0,
            reply_depth if tier == 1 else 0,
            -score,
            recent_rank,
            -source.created_at.timestamp(),
            source.source_id,
        )

    return sorted(selection_input.sources, key=key)


class _PackState:
    def __init__(self, *, budget_chars: int, max_sources: int) -> None:
        self.budget_chars = budget_chars
        self.max_sources = max_sources
        self.context_chars = 0
        self.selected: list[str] = []
        self.seen_ids: set[str] = set()
        self.seen_payloads: set[tuple[str, str, str, str]] = set()
        self.dropped: Counter[str] = Counter()

    def add_source(self, source: SourceRecord, *, extra_chars: int = 0) -> bool:
        if source.source_id in self.seen_ids:
            self.dropped["duplicate_id"] += 1
            return False
        payload_key = _payload_key(source)
        if payload_key in self.seen_payloads:
            self.dropped["duplicate_payload"] += 1
            return False
        if len(self.selected) >= self.max_sources:
            self.dropped["source_limit"] += 1
            return False
        cost = _source_char_cost(source) + extra_chars
        if self.context_chars + cost > self.budget_chars:
            self.dropped["budget"] += 1
            return False
        self.selected.append(source.source_id)
        self.seen_ids.add(source.source_id)
        self.seen_payloads.add(payload_key)
        self.context_chars += cost
        return True


def select_b0(deployed: DeployedSnapshot) -> SelectionResult:
    return SelectionResult(
        arm="B0",
        selected_source_ids=deployed.selected_source_ids,
        selected_event_ids=(),
        context_chars=deployed.context_chars,
        navigation_chars=0,
        compile_ms=deployed.compile_ms,
        recommended_action=deployed.recommended_action,
        dropped_by_reason={},
    )


def select_b1(selection_input: SelectionInput, config: SelectorConfig) -> SelectionResult:
    started = time.perf_counter()
    state = _PackState(budget_chars=selection_input.budget_chars, max_sources=config.max_sources)
    for source in rank_b1_sources(selection_input, config):
        state.add_source(source)
    action = "answer" if state.selected else "clarify"
    # Persistent event construction happens before query-time selection. This
    # provisional, explicitly non-deduplicated ledger therefore includes every
    # candidate event in the frozen snapshot, not only the selected anchor.
    return SelectionResult(
        arm="B1",
        selected_source_ids=tuple(state.selected),
        selected_event_ids=(),
        context_chars=state.context_chars,
        navigation_chars=0,
        compile_ms=(time.perf_counter() - started) * 1000,
        recommended_action=action,
        dropped_by_reason=dict(sorted(state.dropped.items())),
    )


def _rank_events(events: Sequence[EventRecord], config: SelectorConfig) -> list[EventRecord]:
    return sorted(
        (
            event
            for event in events
            if any(rank is not None for rank in (event.semantic_rank, event.keyword_rank, event.fts_rank))
        ),
        key=lambda event: (
            -_rrf_score(
                semantic_rank=event.semantic_rank,
                keyword_rank=event.keyword_rank,
                fts_rank=event.fts_rank,
                recent_rank=None,
                config=config,
            ),
            -event.created_at.timestamp(),
            event.event_id,
        ),
    )


def select_c1(selection_input: SelectionInput, config: SelectorConfig) -> SelectionResult:
    started = time.perf_counter()
    state = _PackState(budget_chars=selection_input.budget_chars, max_sources=config.max_sources)
    source_by_id = {source.source_id: source for source in selection_input.sources}
    b1_ranked = rank_b1_sources(selection_input, config)

    for source in b1_ranked:
        if _source_tier(source) <= 1:
            state.add_source(source)

    selected_events: list[str] = []
    for event in _rank_events(selection_input.events, config)[: config.max_events]:
        event_sources = [source_by_id[event.anchor_source_id]]
        event_sources.extend(
            sorted(
                (source_by_id[source_id] for source_id in event.source_ids if source_id != event.anchor_source_id),
                key=lambda source: (source.created_at, source.source_id),
            )
        )
        new_sources = [
            source
            for source in event_sources
            if source.source_id not in state.seen_ids and _payload_key(source) not in state.seen_payloads
        ]
        if not new_sources:
            state.dropped["event_duplicate"] += 1
            continue
        first_added = state.add_source(new_sources[0])
        if not first_added:
            state.dropped["event_budget_or_limit"] += 1
            continue
        selected_events.append(event.event_id)
        for source in new_sources[1:]:
            state.add_source(source)

    for source in b1_ranked:
        state.add_source(source)

    action = "answer" if state.selected else "clarify"
    return SelectionResult(
        arm="C1",
        selected_source_ids=tuple(state.selected),
        selected_event_ids=tuple(selected_events),
        context_chars=state.context_chars,
        navigation_chars=sum(
            _event_summary_char_cost(event)
            for event in selection_input.events
            if event.event_id in selected_events
        ),
        compile_ms=(time.perf_counter() - started) * 1000,
        recommended_action=action,
        dropped_by_reason=dict(sorted(state.dropped.items())),
        candidate_construction_calls=sum(
            event.amortized_calls for event in selection_input.events
        ),
        candidate_construction_input_tokens=sum(
            event.amortized_input_tokens for event in selection_input.events
        ),
        candidate_construction_output_tokens=sum(
            event.amortized_output_tokens for event in selection_input.events
        ),
        candidate_construction_latency_ms=sum(
            event.amortized_latency_ms for event in selection_input.events
        ),
    )


def select_arm(selection_input: SelectionInput, arm: str, config: SelectorConfig) -> SelectionResult:
    if arm == "B1":
        return select_b1(selection_input, config)
    if arm == "C1":
        return select_c1(selection_input, config)
    raise ValueError(f"unsupported arm: {arm}")


def score_case(case: ContextCase, result: SelectionResult) -> CaseScore:
    expected = case.expected
    selected = result.selected_source_ids
    selected_set = set(selected)
    relevant = (
        expected.acceptable_anchor_source_ids
        | expected.required_source_ids
        | expected.optional_source_ids
    )
    first_relevant_rank = next(
        (index for index, source_id in enumerate(selected, 1) if source_id in expected.acceptable_anchor_source_ids),
        None,
    )
    top1_eligible = expected.expected_action == "answer"
    return CaseScore(
        case_id=case.case_id,
        case_family=case.case_family,
        eligibility_class=case.eligibility_class,
        arm=result.arm,
        top1_eligible=top1_eligible,
        top1_correct=bool(top1_eligible and selected and selected[0] in expected.acceptable_anchor_source_ids),
        relevant_selected=len(selected_set & relevant),
        selected_total=len(selected),
        required_selected=len(selected_set & expected.required_source_ids),
        required_total=len(expected.required_source_ids),
        reciprocal_rank=(1.0 / first_relevant_rank) if first_relevant_rank is not None else 0.0,
        forbidden_selected=len(selected_set & expected.forbidden_source_ids),
        stale_selected=len(selected_set & expected.stale_source_ids),
        wrong_speaker_selected=len(selected_set & expected.wrong_speaker_source_ids),
        action_correct=result.recommended_action == expected.expected_action,
        context_chars=result.context_chars,
        navigation_chars=result.navigation_chars,
        compile_ms=result.compile_ms,
        candidate_construction_calls=result.candidate_construction_calls,
        candidate_construction_input_tokens=result.candidate_construction_input_tokens,
        candidate_construction_output_tokens=result.candidate_construction_output_tokens,
        candidate_construction_latency_ms=result.candidate_construction_latency_ms,
        dropped_by_reason=result.dropped_by_reason,
    )


def _rate(successes: int, total: int) -> dict[str, Any]:
    return {
        "successes": successes,
        "total": total,
        "rate": (successes / total) if total else None,
    }


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "median": statistics.median(values) if values else None,
        "p95": _percentile(values, 0.95),
        "sum": sum(values),
    }


def aggregate_arm(scores: Sequence[CaseScore]) -> dict[str, Any]:
    top1_scores = [score for score in scores if score.top1_eligible]
    precision_numerator = sum(score.relevant_selected for score in scores)
    precision_denominator = sum(score.selected_total for score in scores)
    recall_numerator = sum(score.required_selected for score in scores)
    recall_denominator = sum(score.required_total for score in scores)
    dropped: Counter[str] = Counter()
    for score in scores:
        dropped.update(score.dropped_by_reason)
    by_class: dict[str, Any] = {}
    for eligibility_class in sorted({score.eligibility_class for score in scores}):
        class_scores = [score for score in scores if score.eligibility_class == eligibility_class]
        class_top1 = [score for score in class_scores if score.top1_eligible]
        by_class[eligibility_class] = {
            "cases": len(class_scores),
            "top1": _rate(sum(score.top1_correct for score in class_top1), len(class_top1)),
            "forbidden_cases": sum(score.forbidden_selected > 0 for score in class_scores),
            "stale_cases": sum(score.stale_selected > 0 for score in class_scores),
            "wrong_speaker_cases": sum(score.wrong_speaker_selected > 0 for score in class_scores),
        }
    return {
        "cases": len(scores),
        "top1": _rate(sum(score.top1_correct for score in top1_scores), len(top1_scores)),
        "source_precision": _rate(precision_numerator, precision_denominator),
        "required_recall": _rate(recall_numerator, recall_denominator),
        "mean_reciprocal_rank": statistics.fmean(score.reciprocal_rank for score in top1_scores)
        if top1_scores
        else None,
        "action_accuracy": _rate(sum(score.action_correct for score in scores), len(scores)),
        "forbidden": {
            "selected_sources": sum(score.forbidden_selected for score in scores),
            "affected_cases": sum(score.forbidden_selected > 0 for score in scores),
        },
        "stale": {
            "selected_sources": sum(score.stale_selected for score in scores),
            "affected_cases": sum(score.stale_selected > 0 for score in scores),
        },
        "wrong_speaker": {
            "selected_sources": sum(score.wrong_speaker_selected for score in scores),
            "affected_cases": sum(score.wrong_speaker_selected > 0 for score in scores),
        },
        "selected_sources": _distribution([float(score.selected_total) for score in scores]),
        "context_chars": _distribution([float(score.context_chars) for score in scores]),
        "navigation_chars": _distribution([float(score.navigation_chars) for score in scores]),
        "compile_ms": _distribution([score.compile_ms for score in scores]),
        "candidate_snapshot_construction_not_deduplicated": {
            "calls": sum(score.candidate_construction_calls for score in scores),
            "input_tokens": sum(
                score.candidate_construction_input_tokens for score in scores
            ),
            "output_tokens": sum(
                score.candidate_construction_output_tokens for score in scores
            ),
            "latency_ms": sum(
                score.candidate_construction_latency_ms for score in scores
            ),
            "decision_usable": False,
        },
        "dropped_by_reason": dict(sorted(dropped.items())),
        "by_class": by_class,
    }


def _exact_two_sided_binomial(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    tail = sum(math.comb(total, index) for index in range(0, min(successes, total - successes) + 1))
    return min(1.0, 2.0 * tail / (2**total))


def paired_top1_comparison(
    left_scores: Sequence[CaseScore],
    right_scores: Sequence[CaseScore],
    *,
    bootstrap_samples: int,
    seed: int,
) -> dict[str, Any]:
    left = {score.case_id: score for score in left_scores if score.top1_eligible}
    right = {score.case_id: score for score in right_scores if score.top1_eligible}
    if set(left) != set(right):
        raise ValueError("paired comparison requires identical eligible cases")
    rows = [
        (
            left[case_id].eligibility_class,
            left[case_id].case_family,
            int(right[case_id].top1_correct) - int(left[case_id].top1_correct),
            left[case_id].top1_correct,
            right[case_id].top1_correct,
        )
        for case_id in sorted(left)
    ]
    by_class_family: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for eligibility_class, family, delta, _left_correct, _right_correct in rows:
        by_class_family[eligibility_class][family].append(delta)
    point_delta = (
        statistics.fmean(
            statistics.fmean(delta for family_values in families.values() for delta in family_values)
            for families in by_class_family.values()
        )
        if by_class_family
        else None
    )

    bootstrap: list[float] = []
    if rows and bootstrap_samples > 0 and all(
        len(families) >= MIN_FAMILIES_PER_CLASS for families in by_class_family.values()
    ):
        rng = random.Random(seed)
        class_weight = 1.0 / len(by_class_family)
        for _ in range(bootstrap_samples):
            sample_delta = 0.0
            for eligibility_class, families in sorted(by_class_family.items()):
                family_names = sorted(families)
                sampled_values: list[int] = []
                for _family_index in range(len(family_names)):
                    sampled_values.extend(families[rng.choice(family_names)])
                sample_delta += class_weight * statistics.fmean(sampled_values)
            bootstrap.append(sample_delta)

    family_deltas = [
        statistics.fmean(values)
        for families in by_class_family.values()
        for values in families.values()
    ]
    left_family_only = sum(delta < 0 for delta in family_deltas)
    right_family_only = sum(delta > 0 for delta in family_deltas)
    return {
        "eligible_cases": len(rows),
        "estimand": "macro_equal_class",
        "family_counts_by_class": {
            eligibility_class: len(families)
            for eligibility_class, families in sorted(by_class_family.items())
        },
        "minimum_families_per_class": MIN_FAMILIES_PER_CLASS,
        "delta_right_minus_left": point_delta,
        "cluster_bootstrap_95": (
            [_percentile(bootstrap, 0.025), _percentile(bootstrap, 0.975)] if bootstrap else None
        ),
        "bootstrap_samples": len(bootstrap),
        "cluster_sign_test": {
            "left_only_families": left_family_only,
            "right_only_families": right_family_only,
            "exact_p_two_sided": _exact_two_sided_binomial(
                right_family_only,
                left_family_only + right_family_only,
            ),
        },
    }


def _fixture_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate_context_selection_fixture(
    path: Path | str,
    *,
    config: SelectorConfig | None = None,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    config = config or SelectorConfig()
    cases = load_context_selection_fixture(path)
    fixture_sha256 = _fixture_sha256(path)
    class_counts = Counter(case.eligibility_class for case in cases)
    fixture_metadata = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "stage": "offline_source_selection",
        "corpus_kind": cases[0].corpus_kind,
        "split": cases[0].split,
        "fixture_sha256": fixture_sha256,
        "selector_config": config.as_dict(),
        "selector_config_sha256": config.sha256(),
        "case_count": len(cases),
        "case_family_count": len({case.case_family for case in cases}),
        "eligibility_class_counts": dict(sorted(class_counts.items())),
        "holdout_authorization_present": False,
        "conditional_route_analysis_complete": False,
        "answer_evaluation_complete": False,
        "timing_comparable": False,
        "cost_evaluation_complete": False,
        "construction_cost_deduplicated": False,
        "research_decision": "INCONCLUSIVE",
        "runtime_authorized": False,
    }
    if cases[0].corpus_kind == "private_replay" and cases[0].split == "holdout":
        return {
            **fixture_metadata,
            "selection_signal": "HOLDOUT_NOT_AUTHORIZED",
            "evaluation_suppressed": True,
        }

    scores_by_arm: dict[str, list[CaseScore]] = {arm: [] for arm in ARMS}
    for case in cases:
        for arm in ARMS:
            result = select_b0(case.deployed) if arm == "B0" else select_arm(case.selection_input, arm, config)
            scores_by_arm[arm].append(score_case(case, result))

    comparisons = {
        "B1_minus_B0": paired_top1_comparison(
            scores_by_arm["B0"],
            scores_by_arm["B1"],
            bootstrap_samples=bootstrap_samples,
            seed=config.bootstrap_seed,
        ),
        "C1_minus_B1": paired_top1_comparison(
            scores_by_arm["B1"],
            scores_by_arm["C1"],
            bootstrap_samples=bootstrap_samples,
            seed=config.bootstrap_seed + 1,
        ),
        "C1_minus_B0": paired_top1_comparison(
            scores_by_arm["B0"],
            scores_by_arm["C1"],
            bootstrap_samples=bootstrap_samples,
            seed=config.bootstrap_seed + 2,
        ),
    }
    top1_eligible_counts = Counter(
        score.eligibility_class for score in scores_by_arm["B1"] if score.top1_eligible
    )
    top1_family_counts = {
        eligibility_class: len(
            {
                score.case_family
                for score in scores_by_arm["B1"]
                if score.top1_eligible and score.eligibility_class == eligibility_class
            }
        )
        for eligibility_class in PRIMARY_CLASSES
    }
    decision_signal = "CONTRACT_ONLY"
    if cases[0].corpus_kind == "private_replay":
        decision_signal = "DEVELOPMENT_ONLY"

    return {
        **fixture_metadata,
        "top1_eligible_class_counts": dict(sorted(top1_eligible_counts.items())),
        "top1_family_counts": dict(sorted(top1_family_counts.items())),
        "arms": {arm: aggregate_arm(scores_by_arm[arm]) for arm in ARMS},
        "comparisons": comparisons,
        "selection_signal": decision_signal,
        "evaluation_suppressed": False,
    }


def canonical_report_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
