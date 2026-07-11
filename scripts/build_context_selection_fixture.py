from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from context_selection_eval import CASE_SCHEMA_VERSION, PRIMARY_CLASSES, candidate_snapshot_sha256


DEFAULT_FIXTURE = Path("tests/fixtures/context_selection_contract_v1.jsonl")
DEFAULT_MANIFEST = Path("tests/fixtures/context_selection_contract_v1_manifest.json")
LANGUAGES = ("uk", "ru", "en", "mixed")
TOPICS = (
    "cobalt-lantern",
    "maple-orbit",
    "quiet-harbor",
    "amber-comet",
    "silver-cabin",
    "violet-bridge",
    "polar-garden",
    "linen-rocket",
    "mossy-signal",
    "paper-saturn",
)
SOURCE_COMMIT = "ad2c69079181311cb416d13f7445f05c393aa45f"
SYNTHETIC_CONFIG_SHA256 = hashlib.sha256(b"context-selection-public-contract-v1").hexdigest()


def _timestamp(base: datetime, day_offset: int, hour: int = 12) -> str:
    value = base + timedelta(days=day_offset)
    value = value.replace(hour=hour, minute=0, second=0, microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def _language_text(language: str, role: str, topic: str) -> str:
    texts = {
        "uk": {
            "old": f"Синтетична передумова теми {topic}: початковий варіант був круглим.",
            "anchor": f"Актуальне синтетичне рішення для {topic}: тепер варіант квадратний.",
            "optional": f"Додаткова синтетична деталь для {topic}: перевірка запланована на п'ятницю.",
            "recent_distractor": "Нещодавня, але нерелевантна розмова була про зелений велосипед.",
            "keyword_distractor": f"Слово {topic} випадково згадане у сторонньому жарті.",
            "stale": f"Застаріле твердження про {topic}: варіант досі круглий.",
            "wrong_speaker": f"Інший учасник обрав для своєї теми {topic} трикутний варіант.",
            "noise": "Фонове повідомлення про чай і погоду.",
        },
        "ru": {
            "old": f"Синтетическая предпосылка темы {topic}: исходный вариант был круглым.",
            "anchor": f"Актуальное синтетическое решение для {topic}: теперь вариант квадратный.",
            "optional": f"Дополнительная синтетическая деталь для {topic}: проверка назначена на пятницу.",
            "recent_distractor": "Недавний, но нерелевантный разговор был о зелёном велосипеде.",
            "keyword_distractor": f"Слово {topic} случайно упомянуто в посторонней шутке.",
            "stale": f"Устаревшее утверждение о {topic}: вариант всё ещё круглый.",
            "wrong_speaker": f"Другой участник выбрал для своей темы {topic} треугольный вариант.",
            "noise": "Фоновое сообщение о чае и погоде.",
        },
        "en": {
            "old": f"Synthetic premise for {topic}: the original option was round.",
            "anchor": f"Current synthetic decision for {topic}: the option is now square.",
            "optional": f"Additional synthetic detail for {topic}: validation is planned for Friday.",
            "recent_distractor": "A recent but irrelevant discussion concerned a green bicycle.",
            "keyword_distractor": f"The word {topic} appeared incidentally in an unrelated joke.",
            "stale": f"Stale claim about {topic}: the option is still round.",
            "wrong_speaker": f"A different participant chose a triangular option for their own {topic} topic.",
            "noise": "Background message about tea and weather.",
        },
        "mixed": {
            "old": f"Synthetic передумова для {topic}: original option був круглим.",
            "anchor": f"Current рішення для {topic}: тепер option квадратний.",
            "optional": f"Extra деталь для {topic}: validation у п'ятницю.",
            "recent_distractor": "Recent, але нерелевантна розмова про зелений bicycle.",
            "keyword_distractor": f"Token {topic} випадково з'явився в unrelated joke.",
            "stale": f"Stale твердження про {topic}: option досі круглий.",
            "wrong_speaker": f"Інший speaker обрав triangular option для власної теми {topic}.",
            "noise": "Background повідомлення про tea і weather.",
        },
    }
    return texts[language][role]


def _query(language: str, eligibility_class: str, topic: str) -> str:
    if language == "uk":
        queries = {
            "explicit_reply": "Чому саме так?",
            "short_followup": "А чому?",
            "same_question_transform": "Дай те саме рішення коротко.",
            "topic_shift_distractors": f"Яке актуальне рішення для {topic}?",
            "knowledge_update": f"Що зараз правильно для {topic}?",
            "correction_stale_guardrail": f"Який виправлений варіант для {topic}?",
        }
    elif language == "ru":
        queries = {
            "explicit_reply": "Почему именно так?",
            "short_followup": "А почему?",
            "same_question_transform": "Дай то же решение кратко.",
            "topic_shift_distractors": f"Какое актуальное решение для {topic}?",
            "knowledge_update": f"Что сейчас правильно для {topic}?",
            "correction_stale_guardrail": f"Какой исправленный вариант для {topic}?",
        }
    elif language == "en":
        queries = {
            "explicit_reply": "Why exactly?",
            "short_followup": "Why?",
            "same_question_transform": "Give the same decision briefly.",
            "topic_shift_distractors": f"What is the current decision for {topic}?",
            "knowledge_update": f"What is correct now for {topic}?",
            "correction_stale_guardrail": f"What is the corrected option for {topic}?",
        }
    else:
        queries = {
            "explicit_reply": "Why саме так?",
            "short_followup": "А why?",
            "same_question_transform": "Give те саме рішення briefly.",
            "topic_shift_distractors": f"Яке current рішення для {topic}?",
            "knowledge_update": f"Що correct зараз для {topic}?",
            "correction_stale_guardrail": f"Який corrected option для {topic}?",
        }
    return queries[eligibility_class]


def _source(
    *,
    source_id: str,
    speaker_key: str,
    created_at: str,
    text: str,
    reply_to_source_id: str | None = None,
    is_current_payload: bool = False,
    is_structured_reply: bool = False,
    reply_depth: int | None = None,
    recent_rank: int | None = None,
    semantic_rank: int | None = None,
    keyword_rank: int | None = None,
    fts_rank: int | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "speaker_key": speaker_key,
        "source_kind": "user_text",
        "created_at": created_at,
        "text": text,
        "reply_to_source_id": reply_to_source_id,
        "is_current_payload": is_current_payload,
        "is_structured_reply": is_structured_reply,
        "reply_depth": reply_depth,
        "recent_rank": recent_rank,
        "semantic_rank": semantic_rank,
        "keyword_rank": keyword_rank,
        "fts_rank": fts_rank,
    }


def _b0_context_chars(sources: list[dict[str, Any]], selected: list[str]) -> int:
    by_id = {source["source_id"]: source for source in sources}
    return sum(
        len(by_id[source_id]["text"])
        + len(by_id[source_id]["speaker_key"])
        + len(by_id[source_id]["source_kind"])
        + 48
        for source_id in selected
    )


def _build_case(eligibility_class: str, variant: int) -> dict[str, Any]:
    language = LANGUAGES[variant % len(LANGUAGES)]
    topic = TOPICS[variant]
    prefix = f"pub-{eligibility_class}-{variant:02d}"
    source_ids = {
        role: f"{prefix}-{role.replace('_', '-')}"
        for role in (
            "old",
            "anchor",
            "optional",
            "recent_distractor",
            "keyword_distractor",
            "stale",
            "wrong_speaker",
            "noise",
        )
    }
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    explicit_reply = eligibility_class == "explicit_reply"
    current_payload = eligibility_class == "same_question_transform"
    sources = [
        _source(
            source_id=source_ids["old"],
            speaker_key="speaker-a",
            created_at=_timestamp(base, 0),
            text=_language_text(language, "old", topic),
            recent_rank=6,
            semantic_rank=4,
        ),
        _source(
            source_id=source_ids["stale"],
            speaker_key="speaker-a",
            created_at=_timestamp(base, 1),
            text=_language_text(language, "stale", topic),
            recent_rank=5,
            semantic_rank=2,
            keyword_rank=2,
        ),
        _source(
            source_id=source_ids["optional"],
            speaker_key="speaker-a",
            created_at=_timestamp(base, 2),
            text=_language_text(language, "optional", topic),
            recent_rank=4,
            semantic_rank=3,
            fts_rank=3,
        ),
        _source(
            source_id=source_ids["wrong_speaker"],
            speaker_key="speaker-b",
            created_at=_timestamp(base, 3),
            text=_language_text(language, "wrong_speaker", topic),
            recent_rank=3,
            semantic_rank=7,
        ),
        _source(
            source_id=source_ids["noise"],
            speaker_key="speaker-c",
            created_at=_timestamp(base, 4),
            text=_language_text(language, "noise", topic),
            recent_rank=7,
        ),
        _source(
            source_id=source_ids["keyword_distractor"],
            speaker_key="speaker-c",
            created_at=_timestamp(base, 5),
            text=_language_text(language, "keyword_distractor", topic),
            recent_rank=8,
            keyword_rank=1,
            fts_rank=1,
        ),
        _source(
            source_id=source_ids["recent_distractor"],
            speaker_key="speaker-c",
            created_at=_timestamp(base, 6),
            text=_language_text(language, "recent_distractor", topic),
            recent_rank=2,
            semantic_rank=5,
        ),
        _source(
            source_id=source_ids["anchor"],
            speaker_key="speaker-a",
            created_at=_timestamp(base, 7),
            text=_language_text(language, "anchor", topic),
            reply_to_source_id=source_ids["old"],
            is_current_payload=current_payload,
            is_structured_reply=explicit_reply,
            reply_depth=0 if explicit_reply else None,
            recent_rank=1,
            semantic_rank=1,
            fts_rank=2,
        ),
    ]

    if eligibility_class in {"explicit_reply", "same_question_transform"}:
        b0_selected = [source_ids["anchor"], source_ids["recent_distractor"], source_ids["stale"]]
    elif variant % 3 == 0:
        b0_selected = [source_ids["anchor"], source_ids["stale"], source_ids["keyword_distractor"]]
    elif eligibility_class in {"knowledge_update", "correction_stale_guardrail"}:
        b0_selected = [source_ids["stale"], source_ids["keyword_distractor"], source_ids["anchor"]]
    elif eligibility_class == "topic_shift_distractors":
        b0_selected = [source_ids["keyword_distractor"], source_ids["recent_distractor"], source_ids["anchor"]]
    else:
        b0_selected = [source_ids["old"], source_ids["recent_distractor"], source_ids["anchor"]]

    gold_event_ranks = {"semantic_rank": 1, "keyword_rank": None, "fts_rank": 1}
    distractor_event_ranks = {"semantic_rank": None, "keyword_rank": 1, "fts_rank": None}
    if variant == 9:
        gold_event_ranks = {"semantic_rank": 2, "keyword_rank": None, "fts_rank": 2}
        distractor_event_ranks = {"semantic_rank": 1, "keyword_rank": 1, "fts_rank": 1}
    events = [
        {
            "event_id": f"{prefix}-event-current",
            "summary": f"Synthetic event anchor for the current state of {topic}.",
            "created_at": _timestamp(base, 7, hour=18),
            "anchor_source_id": source_ids["anchor"],
            "source_ids": [source_ids["old"], source_ids["optional"], source_ids["anchor"]],
            **gold_event_ranks,
            "amortized_calls": 0.25,
            "amortized_input_tokens": 180.0,
            "amortized_output_tokens": 45.0,
            "amortized_latency_ms": 120.0,
        },
        {
            "event_id": f"{prefix}-event-distractor",
            "summary": "Synthetic unrelated event anchor about a bicycle discussion.",
            "created_at": _timestamp(base, 6, hour=18),
            "anchor_source_id": source_ids["keyword_distractor"],
            "source_ids": [source_ids["recent_distractor"], source_ids["keyword_distractor"]],
            **distractor_event_ranks,
            "amortized_calls": 0.25,
            "amortized_input_tokens": 160.0,
            "amortized_output_tokens": 40.0,
            "amortized_latency_ms": 110.0,
        },
    ]
    required = [source_ids["anchor"]]
    if eligibility_class in {"knowledge_update", "correction_stale_guardrail"}:
        required.insert(0, source_ids["old"])
    case = {
        "schema_version": CASE_SCHEMA_VERSION,
        "corpus_kind": "public_synthetic",
        "split": "contract",
        "case_id": prefix,
        "case_family": f"pub-family-{eligibility_class}-{variant // 2:02d}",
        "eligibility_class": eligibility_class,
        "language": language,
        "target_time": _timestamp(base, 8),
        "query": _query(language, eligibility_class, topic),
        "budget_chars": 560,
        "b0": {
            "selected_source_ids": b0_selected,
            "context_chars": _b0_context_chars(sources, b0_selected),
            "compile_ms": 1.0 + variant / 10,
            "recommended_action": "answer",
        },
        "sources": sources,
        "events": events,
        "expected": {
            "acceptable_anchor_source_ids": [source_ids["anchor"]],
            "required_source_ids": required,
            "optional_source_ids": [source_ids["optional"]],
            "forbidden_source_ids": [
                source_ids["recent_distractor"],
                source_ids["keyword_distractor"],
                source_ids["stale"],
                source_ids["wrong_speaker"],
                source_ids["noise"],
            ],
            "stale_source_ids": [source_ids["stale"]],
            "wrong_speaker_source_ids": [source_ids["wrong_speaker"]],
            "expected_action": "answer",
        },
    }
    case["snapshot"] = {
        "retrieval_snapshot_kind": "synthetic_ranks",
        "source_commit": SOURCE_COMMIT,
        "effective_config_sha256": SYNTHETIC_CONFIG_SHA256,
        "candidate_snapshot_sha256": candidate_snapshot_sha256(case),
        "query_embedding_sha256": None,
        "embedding_model": "text-embedding-3-small",
        "answer_model": "gpt-5.6-sol",
        "reasoning_effort": "low",
        "b0_compiler_version": f"main@{SOURCE_COMMIT}",
    }
    return case


def build_cases() -> list[dict[str, Any]]:
    return [
        _build_case(eligibility_class, variant)
        for eligibility_class in PRIMARY_CLASSES
        for variant in range(10)
    ]


def write_fixture(output: Path, manifest_path: Path) -> dict[str, Any]:
    cases = build_cases()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases)
    output.write_text(payload, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "context-selection-contract-manifest-v1",
        "corpus_kind": "public_synthetic",
        "split": "contract",
        "fixture_sha256": digest,
        "case_count": len(cases),
        "case_family_count": len({case["case_family"] for case in cases}),
        "eligibility_class_counts": dict(sorted(Counter(case["eligibility_class"] for case in cases).items())),
        "languages": dict(sorted(Counter(case["language"] for case in cases).items())),
        "efficacy_evidence": False,
        "source_commit": SOURCE_COMMIT,
        "effective_config_sha256": SYNTHETIC_CONFIG_SHA256,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the public-synthetic #119 evaluator contract fixture.")
    parser.add_argument("--output", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    manifest = write_fixture(args.output, args.manifest)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
