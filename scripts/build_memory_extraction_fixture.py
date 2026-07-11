from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory_extraction import (
    FIXTURE_SCHEMA_VERSION,
    FORBIDDEN_OUTCOMES,
    fixture_sha256,
    validate_fixture_case,
)


CATEGORY_COUNTS = (
    ("fact_claim", 14),
    ("preference", 10),
    ("decision", 10),
    ("relationship", 10),
    ("correction", 14),
    ("uncertainty", 10),
    ("validity_expiry", 10),
    ("opinion", 8),
    ("joke", 8),
    ("question_hypothetical", 6),
    ("transient_ack", 6),
    ("forwarded_quote", 6),
    ("prior_bot_echo", 4),
    ("cross_scope_bait", 4),
)

LANGUAGE_SEQUENCE = tuple(
    ["uk", "ru", "en", "mixed"] * 20
    + ["uk"] * 20
    + ["ru"] * 10
    + ["en"] * 10
)


def phrase(
    category: str,
    language: str,
    number: int,
    *,
    correction: bool = False,
    split: str = "development",
) -> str:
    values = {
        "fact_claim": {
            "uk": f"Я живу в місті Синтетик {number}.",
            "ru": f"Я живу в городе Синтетик {number}.",
            "en": f"I live in Synthetic City {number}.",
            "mixed": f"Моє місто is Synthetic City {number}.",
        },
        "preference": {
            "uk": f"Я віддаю перевагу темній темі {number}.",
            "ru": f"Я предпочитаю тёмную тему {number}.",
            "en": f"I prefer dark theme {number}.",
            "mixed": f"Я prefer dark theme {number}.",
        },
        "decision": {
            "uk": f"Ми вирішили використовувати план Синтетик {number}.",
            "ru": f"Мы решили использовать план Синтетик {number}.",
            "en": f"We decided to use Synthetic Plan {number}.",
            "mixed": f"Ми decided to use Synthetic Plan {number}.",
        },
        "relationship": {
            "uk": f"Синтетик {number} — мій друг.",
            "ru": f"Синтетик {number} — мой друг.",
            "en": f"Synthetic Person {number} is my friend.",
            "mixed": f"Synthetic Person {number} — мій friend.",
        },
        "uncertainty": {
            "uk": f"Можливо, я виберу синтетичний варіант {number}.",
            "ru": f"Возможно, я выберу синтетический вариант {number}.",
            "en": f"Maybe I will choose synthetic option {number}.",
            "mixed": f"Можливо, I choose synthetic option {number}.",
        },
        "validity_expiry": {
            "uk": f"Мій синтетичний пропуск {number} діє до 2030-02-15.",
            "ru": f"Мой синтетический пропуск {number} действует до 2030-02-15.",
            "en": f"My synthetic pass {number} is valid until 2030-02-15.",
            "mixed": f"Мій synthetic pass {number} valid until 2030-02-15.",
        },
        "opinion": {
            "uk": f"На мою думку, синтетичний колір {number} найкращий.",
            "ru": f"По-моему, синтетический цвет {number} лучший.",
            "en": f"In my opinion, synthetic color {number} is best.",
            "mixed": f"На мою думку, synthetic color {number} is best.",
        },
        "joke": {
            "uk": f"Я переїхав на Синтетик {number} — жартую.",
            "ru": f"Я переехал на Синтетик {number} — шучу.",
            "en": f"I moved to Synthetic Planet {number}, just kidding.",
            "mixed": f"Я moved to Synthetic Planet {number} — жартую.",
        },
        "question": {
            "uk": f"Ти пам'ятаєш синтетичний номер {number}?",
            "ru": f"Ты помнишь синтетический номер {number}?",
            "en": f"Do you remember synthetic number {number}?",
            "mixed": f"Ти remember synthetic number {number}?",
        },
        "hypothetical": {
            "uk": f"А що як я виберу синтетичний номер {number}?",
            "ru": f"А что если я выберу синтетический номер {number}?",
            "en": f"What if I choose synthetic number {number}?",
            "mixed": f"А що як I choose synthetic number {number}?",
        },
        "transient": {
            "uk": "Добре.",
            "ru": "Хорошо.",
            "en": "Okay.",
            "mixed": "Ок 👍",
        },
        "forwarded": {
            "uk": f"Переслане джерело стверджує синтетичний факт {number}.",
            "ru": f"Пересланный источник утверждает синтетический факт {number}.",
            "en": f"The forwarded source claims synthetic fact {number}.",
            "mixed": f"Forwarded джерело claims synthetic fact {number}.",
        },
        "prior_bot": {
            "uk": f"Попередня відповідь бота: синтетичний факт {number}.",
            "ru": f"Предыдущий ответ бота: синтетический факт {number}.",
            "en": f"Prior bot answer: synthetic fact {number}.",
            "mixed": f"Prior bot відповідь: synthetic fact {number}.",
        },
    }
    holdout_values = {
        "fact_claim": {
            "uk": f"Моє місто проживання — Синтетик {number}.",
            "ru": f"Мой город проживания — Синтетик {number}.",
            "en": f"My city is Synthetic Borough {number}.",
            "mixed": f"My city — Синтетик Borough {number}.",
        },
        "preference": {
            "uk": f"Мені подобається більше синтетичний режим {number}.",
            "ru": f"Мне больше нравится синтетический режим {number}.",
            "en": f"My preference is synthetic mode {number}.",
            "mixed": f"Мені подобається більше synthetic mode {number}.",
        },
        "decision": {
            "uk": f"Я вирішив обрати синтетичний маршрут {number}.",
            "ru": f"Я решил выбрать синтетический маршрут {number}.",
            "en": f"I decided to choose synthetic route {number}.",
            "mixed": f"Я вирішив choose synthetic route {number}.",
        },
        "relationship": {
            "uk": f"Синтетик {number} — моя сестра.",
            "ru": f"Синтетик {number} — моя сестра.",
            "en": f"Synthetic Person {number} is my sister.",
            "mixed": f"Synthetic Person {number} — моя sister.",
        },
        "uncertainty": {
            "uk": f"Ймовірно, я залишу синтетичний варіант {number}.",
            "ru": f"Вероятно, я оставлю синтетический вариант {number}.",
            "en": f"Probably I will keep synthetic option {number}.",
            "mixed": f"Ймовірно, I keep synthetic option {number}.",
        },
        "validity_expiry": {
            "uk": f"Синтетичний дозвіл {number} діє до 2030-03-20.",
            "ru": f"Синтетическое разрешение {number} действует до 2030-03-20.",
            "en": f"Synthetic permit {number} is valid until 2030-03-20.",
            "mixed": f"Synthetic permit {number} діє до 2030-03-20.",
        },
        "opinion": {
            "uk": f"На мою думку, синтетичний стиль {number} кращий.",
            "ru": f"Я думаю, синтетический стиль {number} лучше.",
            "en": f"In my opinion, synthetic style {number} is better.",
            "mixed": f"На мою думку, synthetic style {number} is better.",
        },
        "joke": {
            "uk": f"Я став синтетичним драконом {number} — жартую.",
            "ru": f"Я стал синтетическим драконом {number} — шучу.",
            "en": f"I became synthetic dragon {number}, just kidding.",
            "mixed": f"Я became synthetic dragon {number} — жартую.",
        },
        "question": {
            "uk": f"Чи був синтетичний ключ {number}?",
            "ru": f"Был ли синтетический ключ {number}?",
            "en": f"Was there a synthetic key {number}?",
            "mixed": f"Was there синтетичний key {number}?",
        },
        "hypothetical": {
            "uk": f"Якби я обрав синтетичний ключ {number}, що було б?",
            "ru": f"Если бы я выбрал синтетический ключ {number}, что было бы?",
            "en": f"What if I selected synthetic key {number}?",
            "mixed": f"Якби I selected synthetic key {number}?",
        },
        "transient": {
            "uk": "Гаразд.",
            "ru": "Понял.",
            "en": "Thanks.",
            "mixed": "Okay ✅",
        },
        "forwarded": {
            "uk": f"Цитоване джерело повідомляє синтетичну тезу {number}.",
            "ru": f"Цитируемый источник сообщает синтетический тезис {number}.",
            "en": f"The quoted source reports synthetic claim {number}.",
            "mixed": f"Quoted джерело reports synthetic claim {number}.",
        },
        "prior_bot": {
            "uk": f"Архівний текст бота: синтетична теза {number}.",
            "ru": f"Архивный текст бота: синтетический тезис {number}.",
            "en": f"Archived bot text: synthetic claim {number}.",
            "mixed": f"Archived bot текст: synthetic claim {number}.",
        },
    }
    selected_values = holdout_values if split == "holdout" else values
    if category == "correction":
        if correction:
            prefixes = {
                "uk": "Виправлення:",
                "ru": "Исправление:",
                "en": "Correction:",
                "mixed": "Correction:",
            }
            base = phrase("fact_claim", language, number, split=split)
            return f"{prefixes[language]} {base}"
        return phrase("fact_claim", language, number + 100, split=split)
    return selected_values[category][language]


def make_row(
    *,
    row_key: str,
    chat_key: str,
    speaker_key: str,
    authored_text: str = "",
    source_text: str = "",
    actor_role: str = "user",
    source_role: str = "user_authored",
    created_at: str = "2030-01-15T11:59:00Z",
    reply_to_row_key: str | None = None,
    tool_evidence: str = "",
) -> dict[str, Any]:
    return {
        "row_key": row_key,
        "chat_key": chat_key,
        "speaker_key": speaker_key,
        "actor_role": actor_role,
        "authored_text": authored_text,
        "source_text": source_text,
        "source_role": source_role,
        "reply_to_row_key": reply_to_row_key,
        "created_at": created_at,
        "tool_evidence_row_key": row_key if source_role == "verified_tool" else None,
        "tool_evidence": tool_evidence,
    }


def blank_case(
    category: str,
    language: str,
    ordinal: int,
    global_index: int,
    split: str,
) -> dict[str, Any]:
    case_id = f"{split}_{category}_{ordinal:03d}"
    chat_key = f"chat_{global_index % 5 + 1}"
    speaker_key = f"speaker_{global_index % 7 + 1}"
    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "id": case_id,
        "privacy_class": "public_synthetic",
        "split": split,
        "tags": [category, language],
        "language": language,
        "as_of": "2030-01-15T12:00:00Z",
        "target_chat_key": chat_key,
        "target_speaker_key": speaker_key,
        "inputs": [],
        "expected": {
            "eligible": True,
            "candidates": [],
            "no_candidate_reason": "unclassified",
            "forbidden_outcomes": list(FORBIDDEN_OUTCOMES),
        },
    }


def expected_candidate(
    row: dict[str, Any],
    candidate_type: str,
    *,
    epistemic: str = "asserted",
    durability: str = "durable",
    reason_code: str,
    supersedes_row_keys: list[str] | None = None,
    conflicts_row_keys: list[str] | None = None,
) -> dict[str, Any]:
    if row["source_role"] == "verified_tool":
        evidence_field = "tool_evidence"
    elif row["source_role"] == "forwarded_source":
        evidence_field = "source_text"
    else:
        evidence_field = "authored_text"
    evidence_span = str(row[evidence_field]).strip()
    expiry = re.search(r"2030-\d{2}-\d{2}", evidence_span)
    return {
        "candidate_type": candidate_type,
        "epistemic": epistemic,
        "durability": durability,
        "evidence_refs": [{"row_key": row["row_key"], "field": evidence_field}],
        "evidence_span": evidence_span,
        "supersedes_row_keys": sorted(supersedes_row_keys or []),
        "conflicts_row_keys": sorted(conflicts_row_keys or []),
        "valid_until": expiry.group(0) + "T00:00:00Z" if expiry else "none",
        "confidence": 1.0,
        "reason_codes": [reason_code],
    }


def expected_from_semantics(case: dict[str, Any], category: str) -> None:
    """Build labels from scenario intent, independently of the rule baseline."""
    rows = [
        row
        for row in case["inputs"]
        if row["chat_key"] == case["target_chat_key"]
    ]
    candidates: list[dict[str, Any]] = []
    no_candidate_reason = "none"
    positive_specs = {
        "fact_claim": ("fact_claim", "asserted", "durable", "explicit_fact"),
        "preference": ("preference", "asserted", "durable", "explicit_preference"),
        "decision": ("decision", "asserted", "durable", "explicit_decision"),
        "relationship": ("relationship", "asserted", "durable", "explicit_relationship"),
        "uncertainty": ("uncertainty", "uncertain", "transient", "uncertainty_marker"),
        "validity_expiry": ("validity_expiry", "asserted", "durable", "explicit_validity"),
        "cross_scope_bait": ("fact_claim", "asserted", "durable", "explicit_fact"),
    }
    if category in positive_specs:
        candidate_type, epistemic, durability, reason_code = positive_specs[category]
        row = rows[0]
        if row["source_role"] == "verified_tool":
            epistemic = "verified"
            reason_code = "verified_tool_anchor"
        candidates.append(
            expected_candidate(
                row,
                candidate_type,
                epistemic=epistemic,
                durability=durability,
                reason_code=reason_code,
            )
        )
    elif category == "correction":
        first, correction = rows
        candidates.append(
            expected_candidate(
                first,
                "fact_claim",
                reason_code="explicit_fact",
            )
        )
        same_speaker = first["speaker_key"] == correction["speaker_key"]
        candidates.append(
            expected_candidate(
                correction,
                "correction",
                reason_code="explicit_correction",
                supersedes_row_keys=[first["row_key"]] if same_speaker else [],
                conflicts_row_keys=[] if same_speaker else [first["row_key"]],
            )
        )
    else:
        negative_reasons = {
            "opinion": "opinion",
            "joke": "joke",
            "question_hypothetical": (
                "hypothetical" if "hypothetical" in case["tags"] else "question"
            ),
            "transient_ack": "transient",
            "forwarded_quote": "forwarded_without_endorsement",
            "prior_bot_echo": "prior_bot_output",
        }
        no_candidate_reason = negative_reasons[category]
    case["expected"]["candidates"] = sorted(
        candidates,
        key=lambda item: (item["evidence_refs"][0]["row_key"], item["candidate_type"]),
    )
    case["expected"]["no_candidate_reason"] = no_candidate_reason


def build_case(
    category: str,
    language: str,
    ordinal: int,
    global_index: int,
    split: str,
    value_number: int,
) -> dict[str, Any]:
    case = blank_case(category, language, ordinal, global_index, split)
    chat = case["target_chat_key"]
    speaker = case["target_speaker_key"]

    if category == "fact_claim" and ordinal <= 3:
        tool_text = {
            "uk": f"Синтетичний інструмент підтвердив значення {value_number}.",
            "ru": f"Синтетический инструмент подтвердил значение {value_number}.",
            "en": f"Synthetic tool verified value {value_number}.",
            "mixed": f"Synthetic tool підтвердив value {value_number}.",
        }[language]
        row = make_row(
            row_key="row_1",
            chat_key=chat,
            speaker_key=f"tool_{ordinal}",
            actor_role="tool",
            source_role="verified_tool",
            tool_evidence=tool_text,
        )
        case["target_speaker_key"] = row["speaker_key"]
        case["tags"].append("tool_fact")
        case["inputs"] = [row]
    elif category in {"fact_claim", "preference", "decision", "relationship", "uncertainty", "validity_expiry"}:
        case["inputs"] = [
            make_row(
                row_key="row_1",
                chat_key=chat,
                speaker_key=speaker,
                authored_text=phrase(category, language, value_number, split=split),
            )
        ]
    elif category == "correction":
        first_speaker = speaker
        second_speaker = speaker if ordinal > 6 else f"speaker_conflict_{ordinal}"
        first = make_row(
            row_key="row_1",
            chat_key=chat,
            speaker_key=first_speaker,
            authored_text=phrase("correction", language, value_number, correction=False, split=split),
            created_at="2030-01-15T11:57:00Z",
        )
        second = make_row(
            row_key="row_2",
            chat_key=chat,
            speaker_key=second_speaker,
            authored_text=phrase("correction", language, value_number, correction=True, split=split),
            created_at="2030-01-15T11:59:00Z",
            reply_to_row_key="row_1",
        )
        case["target_speaker_key"] = second_speaker
        case["inputs"] = [first, second]
        case["tags"].append("cross_speaker_conflict" if ordinal <= 6 else "same_speaker_supersession")
    elif category in {"opinion", "joke"}:
        case["inputs"] = [
            make_row(
                row_key="row_1",
                chat_key=chat,
                speaker_key=speaker,
                authored_text=phrase(category, language, value_number, split=split),
            )
        ]
    elif category == "question_hypothetical":
        kind = "hypothetical" if ordinal % 2 == 0 else "question"
        case["tags"].append(kind)
        case["inputs"] = [
            make_row(
                row_key="row_1",
                chat_key=chat,
                speaker_key=speaker,
                authored_text=phrase(kind, language, value_number, split=split),
            )
        ]
    elif category == "transient_ack":
        case["tags"].append("acknowledgement")
        case["inputs"] = [
            make_row(
                row_key="row_1",
                chat_key=chat,
                speaker_key=speaker,
                authored_text=phrase("transient", language, value_number, split=split),
            )
        ]
    elif category == "forwarded_quote":
        case["tags"].append("quote_only")
        case["inputs"] = [
            make_row(
                row_key="row_1",
                chat_key=chat,
                speaker_key=speaker,
                authored_text="FYI" if split == "development" else "For reference",
                source_text=phrase("forwarded", language, value_number, split=split),
                source_role="forwarded_source",
            )
        ]
    elif category == "prior_bot_echo":
        case["inputs"] = [
            make_row(
                row_key="row_1",
                chat_key=chat,
                speaker_key="aigan_bot",
                authored_text=phrase("prior_bot", language, value_number, split=split),
                actor_role="bot",
                source_role="prior_bot_output",
            )
        ]
        case["target_speaker_key"] = "aigan_bot"
    elif category == "cross_scope_bait":
        target = make_row(
            row_key="row_1",
            chat_key=chat,
            speaker_key=speaker,
            authored_text=phrase("fact_claim", language, value_number, split=split),
        )
        distractor = make_row(
            row_key="row_2",
            chat_key=f"other_chat_{ordinal}",
            speaker_key=f"other_speaker_{ordinal}",
            authored_text=phrase("fact_claim", language, value_number + 50, split=split),
        )
        case["inputs"] = [target, distractor]
        case["tags"].append("cross_chat")
    else:
        raise AssertionError(category)

    expected_from_semantics(case, category)
    return case


def add_identity_pairs(cases: list[dict[str, Any]], split: str) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    allowed = {
        "fact_claim",
        "preference",
        "decision",
        "relationship",
        "uncertainty",
        "validity_expiry",
    }
    for case in cases:
        category = case["tags"][0]
        if (
            case["split"] == split
            and
            category in allowed
            and len(case["inputs"]) == 1
            and case["inputs"][0]["source_role"] == "user_authored"
            and case["expected"]["candidates"]
        ):
            groups[(category, case["language"])].append(case)

    pair_number = 0
    for key in sorted(groups):
        group = groups[key]
        while len(group) >= 2 and pair_number < 12:
            pair_number += 1
            first = group.pop(0)
            second = group.pop(0)
            shared_text = first["inputs"][0]["authored_text"]
            for suffix, case in (("a", first), ("b", second)):
                chat = f"pair_chat_{pair_number}_{suffix}"
                speaker = f"pair_speaker_{pair_number}_{suffix}"
                case["target_chat_key"] = chat
                case["target_speaker_key"] = speaker
                case["inputs"][0]["chat_key"] = chat
                case["inputs"][0]["speaker_key"] = speaker
                case["inputs"][0]["authored_text"] = shared_text
                case["tags"].extend(["identity_pair", f"identity_pair_{pair_number}"])
                expected_from_semantics(case, case["tags"][0])
        if pair_number == 12:
            break
    if pair_number != 12:
        raise RuntimeError(f"expected 12 identity pairs, built {pair_number}")


def build_fixture() -> list[dict[str, Any]]:
    categories: list[str] = []
    for category, count in CATEGORY_COUNTS:
        categories.extend([category] * count)
    if len(categories) != 120 or len(LANGUAGE_SEQUENCE) != 120:
        raise RuntimeError("fixture distribution must contain 120 cases")

    cases: list[dict[str, Any]] = []
    for split, value_offset in (("development", 0), ("holdout", 500)):
        ordinals: Counter[str] = Counter()
        for global_index, (category, language) in enumerate(zip(categories, LANGUAGE_SEQUENCE), 1):
            ordinals[category] += 1
            cases.append(
                build_case(
                    category,
                    language,
                    ordinals[category],
                    global_index + value_offset,
                    split,
                    ordinals[category] + value_offset,
                )
            )
        add_identity_pairs(cases, split)

    if len(cases) != 240:
        raise RuntimeError("fixture must contain 120 development and 120 holdout cases")
    for case in cases:
        validate_fixture_case(case)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the frozen public-synthetic memory extraction fixture.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/memory_extraction_v1.jsonl"),
    )
    args = parser.parse_args()
    cases = build_fixture()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for case in cases
    )
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(f"case_count={len(cases)}")
    print(f"sha256={fixture_sha256(args.output)}")


if __name__ == "__main__":
    main()
