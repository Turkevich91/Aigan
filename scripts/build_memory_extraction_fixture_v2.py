#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory_extraction as v1
from memory_extraction_v2 import (
    FIXTURE_SCHEMA_VERSION,
    FROZEN_MANIFEST_SHA256,
    manifest_sha256,
    validate_fixture_case,
)


LANGUAGES = ("uk", "ru", "en", "mixed")
SPLITS = ("development", "holdout")
FORBIDDEN = list(v1.FORBIDDEN_OUTCOMES)


def _value(split: str, index: int) -> int:
    return index + (20 if split == "development" else 220)


def _texts(split: str, language: str, index: int) -> dict[str, str]:
    value = _value(split, index)
    day = 10 + index % 10
    if language == "uk":
        if split == "development":
            return {
                "fact_claim": f"Для майбутньої довідки: я живу у Кедровому кварталі {value}.",
                "preference": f"Віддаю перевагу матовому синьому варіанту {value} для макетів.",
                "decision": f"Ми вирішили зберігати синтетичний набір {value} окремо.",
                "relationship": f"Синтетична особа {value} — мій друг для тестового сценарію.",
                "correction": f"Виправлення: я живу у Кедровому кварталі {value}.",
                "uncertainty": f"Можливо, я оберу синтетичний маршрут {value} наступного місяця.",
                "validity_expiry": f"Синтетичний дозвіл {value} діє до 2032-04-{day:02d}.",
            }
        return {
            "fact_claim": f"Моє місто для цієї вправи — Гавань {value}.",
            "preference": f"Мені більше подобається бурштиновий прототип {value} у тестах.",
            "decision": f"Я вирішив архівувати умовну колекцію {value} окремим пакетом.",
            "relationship": f"У навчальному прикладі синтетична особа {value} — моя сестра.",
            "correction": f"Насправді моє місто для вправи — Гавань {value}.",
            "uncertainty": f"Ймовірно, я зміню умовний маршрут {value} пізніше.",
            "validity_expiry": f"Умовний сертифікат {value} діє до 2032-08-{day:02d}.",
        }
    if language == "ru":
        if split == "development":
            return {
                "fact_claim": f"Для будущей справки: я живу в Кедровом квартале {value}.",
                "preference": f"Предпочитаю матовый синий вариант {value} для макетов.",
                "decision": f"Мы решили хранить синтетический набор {value} отдельно.",
                "relationship": f"Синтетический человек {value} — мой друг в тестовом сценарии.",
                "correction": f"Исправление: я живу в Кедровом квартале {value}.",
                "uncertainty": f"Возможно, я выберу синтетический маршрут {value} в следующем месяце.",
                "validity_expiry": f"Синтетический пропуск {value} действует до 2032-04-{day:02d}.",
            }
        return {
            "fact_claim": f"Мой город для этого упражнения — Гавань {value}.",
            "preference": f"Мне больше нравится янтарный прототип {value} в тестах.",
            "decision": f"Я решил архивировать условную коллекцию {value} отдельным пакетом.",
            "relationship": f"В учебном примере синтетический человек {value} — моя сестра.",
            "correction": f"На самом деле мой город для упражнения — Гавань {value}.",
            "uncertainty": f"Вероятно, я изменю условный маршрут {value} позже.",
            "validity_expiry": f"Условный сертификат {value} действует до 2032-08-{day:02d}.",
        }
    if language == "en":
        if split == "development":
            return {
                "fact_claim": f"For future reference, I live in Cedar Quarter {value}.",
                "preference": f"I prefer matte cobalt option {value} for prototypes.",
                "decision": f"We decided to store synthetic set {value} separately.",
                "relationship": f"Synthetic Person {value} is my friend in this test scenario.",
                "correction": f"Correction: I live in Cedar Quarter {value}.",
                "uncertainty": f"Maybe I will choose synthetic route {value} next month.",
                "validity_expiry": f"Synthetic permit {value} is valid until 2032-04-{day:02d}.",
            }
        return {
            "fact_claim": f"My city for this exercise is Harbor District {value}.",
            "preference": f"My preference is amber prototype {value} during trials.",
            "decision": f"I decided to archive fictional collection {value} as a separate bundle.",
            "relationship": f"In the training example, Synthetic Person {value} is my sister.",
            "correction": f"Actually, my city for the exercise is Harbor District {value}.",
            "uncertainty": f"Probably I will change fictional route {value} later.",
            "validity_expiry": f"Fictional certificate {value} remains valid until 2032-08-{day:02d}.",
        }
    if split == "development":
        return {
            "fact_claim": f"For нотатки, my city is Cedar Mix {value}.",
            "preference": f"I prefer матовий cobalt option {value} для макетів.",
            "decision": f"We decided зберігати synthetic set {value} окремо.",
            "relationship": f"Synthetic Person {value} is my friend у тесті.",
            "correction": f"Correction: my city is Cedar Mix {value}.",
            "uncertainty": f"Maybe я оберу synthetic route {value} наступного місяця.",
            "validity_expiry": f"Synthetic permit {value} valid until 2032-04-{day:02d}.",
        }
    return {
        "fact_claim": f"Моє city for this exercise is Harbor Mix {value}.",
        "preference": f"My preference is бурштиновий prototype {value} у тестах.",
        "decision": f"I decided архівувати fictional collection {value} окремо.",
        "relationship": f"Synthetic Person {value} is my sister у навчальному прикладі.",
        "correction": f"Actually, моє city is Harbor Mix {value}.",
        "uncertainty": f"Probably я зміню fictional route {value} пізніше.",
        "validity_expiry": f"Fictional certificate {value} valid until 2032-08-{day:02d}.",
    }


def _negative_text(split: str, language: str, kind: str, index: int) -> str:
    value = _value(split, index)
    if split == "holdout":
        holdout_text = {
            "en": {
                "opinion": f"Harbor study {value} seems well proportioned to me.",
                "joke": f"Kidding: imaginary asteroid {value} belongs to me.",
                "question": f"Could fictional waypoint {value} be where I reside?",
                "hypothetical": f"Suppose amber choice {value} became my favorite?",
                "transient": f"Harbor review {value} noted; nothing lasting.",
                "forwarded": f"A relayed note reports invented metric {value}.",
                "prior_bot": f"The assistant earlier proposed imaginary metric {value}.",
                "unsupported_tool": f"Unverified automation alleges result {value}.",
                "cross_scope": f"Outside this room, a synthetic profile says location {value}.",
                "not_durable": f"For this minute, temporary task {value} occupies me.",
                "unclassified": f"Exercise token {value}; no lasting claim follows.",
            },
            "uk": {
                "opinion": f"Мені ескіз Гавані {value} здається добре врівноваженим.",
                "joke": f"Жарт: уявний астероїд {value} належить мені.",
                "question": f"Чи може умовний орієнтир {value} бути місцем мого проживання?",
                "hypothetical": f"Уявімо, що бурштиновий варіант {value} став моїм улюбленим?",
                "transient": f"Огляд Гавані {value} прийнято; нічого тривалого.",
                "forwarded": f"Переказана нотатка повідомляє вигаданий показник {value}.",
                "prior_bot": f"Асистент раніше запропонував уявний показник {value}.",
                "unsupported_tool": f"Неперевірена автоматизація заявляє результат {value}.",
                "cross_scope": f"Поза цією кімнатою синтетичний профіль називає місце {value}.",
                "not_durable": f"Цієї хвилини мене займає тимчасове завдання {value}.",
                "unclassified": f"Навчальний маркер {value}; тривалого твердження немає.",
            },
            "ru": {
                "opinion": f"Мне эскиз Гавани {value} кажется хорошо сбалансированным.",
                "joke": f"Шутка: воображаемый астероид {value} принадлежит мне.",
                "question": f"Может ли условный ориентир {value} быть местом моего проживания?",
                "hypothetical": f"Допустим, янтарный вариант {value} стал моим любимым?",
                "transient": f"Обзор Гавани {value} принят; ничего долговременного.",
                "forwarded": f"Пересказанная заметка сообщает выдуманный показатель {value}.",
                "prior_bot": f"Ассистент ранее предложил воображаемый показатель {value}.",
                "unsupported_tool": f"Непроверенная автоматизация заявляет результат {value}.",
                "cross_scope": f"Вне этой комнаты синтетический профиль называет место {value}.",
                "not_durable": f"Этой минутой меня занимает временная задача {value}.",
                "unclassified": f"Учебный маркер {value}; устойчивого утверждения нет.",
            },
            "mixed": {
                "opinion": f"Harbor ескіз {value} seems мені well proportioned.",
                "joke": f"Жарт: imaginary asteroid {value} належить мені.",
                "question": f"Could умовний waypoint {value} бути my residence?",
                "hypothetical": f"Suppose бурштиновий choice {value} став favorite?",
                "transient": f"Harbor огляд {value} noted; нічого lasting.",
                "forwarded": f"A relayed нотатка reports вигаданий metric {value}.",
                "prior_bot": f"The assistant раніше proposed imaginary metric {value}.",
                "unsupported_tool": f"Unverified автоматизація alleges result {value}.",
                "cross_scope": f"Outside цієї room, synthetic profile says location {value}.",
                "not_durable": f"For цю minute, temporary task {value} займає me.",
                "unclassified": f"Exercise маркер {value}; no lasting твердження follows.",
            },
        }
        return holdout_text[language][kind]
    marker = "Cedar"
    text = {
        "en": {
            "opinion": f"In my opinion, {marker} sketch {value} looks balanced.",
            "joke": f"I own imaginary moon {value}—just kidding.",
            "question": f"Do I live near fictional marker {value}?",
            "hypothetical": f"What if I preferred fictional option {value}?",
            "transient": f"Thanks, {marker} draft is understood for this moment.",
            "forwarded": f"Forwarded source claims fictional value {value}.",
            "prior_bot": f"Previous bot output guessed fictional value {value}.",
            "unsupported_tool": f"An unverified tool supposedly returned value {value}.",
            "cross_scope": f"I live in unrelated scope {value}.",
            "not_durable": f"Right now I am busy with temporary item {value}.",
            "unclassified": f"Synthetic fragment {value} without a durable assertion.",
        },
        "uk": {
            "opinion": f"На мою думку, ескіз {marker} {value} виглядає збалансовано.",
            "joke": f"Я володію уявним місяцем {value} — жартую.",
            "question": f"Чи живу я біля умовної позначки {value}?",
            "hypothetical": f"А що як я віддав би перевагу умовному варіанту {value}?",
            "transient": f"Дякую, чернетку {marker} зараз зрозуміло.",
            "forwarded": f"Переслане джерело стверджує умовне значення {value}.",
            "prior_bot": f"Попередня відповідь бота вгадала умовне значення {value}.",
            "unsupported_tool": f"Неперевірений інструмент нібито повернув значення {value}.",
            "cross_scope": f"Я живу в іншій області видимості {value}.",
            "not_durable": f"Зараз я зайнятий тимчасовим елементом {value}.",
            "unclassified": f"Синтетичний фрагмент {value} без тривалого твердження.",
        },
        "ru": {
            "opinion": f"По-моему, эскиз {marker} {value} выглядит сбалансированно.",
            "joke": f"Я владею воображаемой луной {value} — шучу.",
            "question": f"Живу ли я возле условной отметки {value}?",
            "hypothetical": f"А что если бы я предпочёл условный вариант {value}?",
            "transient": f"Спасибо, черновик {marker} сейчас понятен.",
            "forwarded": f"Пересланный источник утверждает условное значение {value}.",
            "prior_bot": f"Предыдущий ответ бота угадал условное значение {value}.",
            "unsupported_tool": f"Непроверенный инструмент якобы вернул значение {value}.",
            "cross_scope": f"Я живу в другой области видимости {value}.",
            "not_durable": f"Сейчас я занят временным элементом {value}.",
            "unclassified": f"Синтетический фрагмент {value} без устойчивого утверждения.",
        },
        "mixed": {
            "opinion": f"In my opinion, ескіз {marker} {value} виглядає balanced.",
            "joke": f"I own уявний moon {value} — just kidding.",
            "question": f"Do я live біля fictional marker {value}?",
            "hypothetical": f"What if я preferred умовний option {value}?",
            "transient": f"Thanks, чернетку {marker} зараз зрозуміло.",
            "forwarded": f"Forwarded джерело claims fictional value {value}.",
            "prior_bot": f"Previous bot output вгадав fictional value {value}.",
            "unsupported_tool": f"Unverified інструмент нібито returned value {value}.",
            "cross_scope": f"I live в іншій scope {value}.",
            "not_durable": f"Right now я зайнятий temporary item {value}.",
            "unclassified": f"Synthetic фрагмент {value} without durable assertion.",
        },
    }
    return text[language][kind]


def _row(
    row_key: str,
    *,
    chat_key: str,
    speaker_key: str,
    created_at: str,
    authored_text: str = "",
    source_text: str = "",
    source_role: str = "user_authored",
    actor_role: str = "user",
    reply_to_row_key: str | None = None,
    tool_evidence: str = "",
    tool_evidence_row_key: str | None = None,
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
        "tool_evidence_row_key": tool_evidence_row_key,
        "tool_evidence": tool_evidence,
    }


def _evidence_field(row: dict[str, Any]) -> str:
    if row["source_role"] == "verified_tool":
        return "tool_evidence"
    if row["source_role"] == "forwarded_source":
        return "source_text"
    return "authored_text"


def _candidate(
    candidate_type: str,
    row: dict[str, Any],
    *,
    supersedes: list[str] | None = None,
    conflicts: list[str] | None = None,
    valid_until: str = "none",
) -> dict[str, Any]:
    field = _evidence_field(row)
    verified = row["source_role"] == "verified_tool"
    uncertain = candidate_type == "uncertainty"
    return {
        "candidate_type": candidate_type,
        "epistemic": "verified" if verified else ("uncertain" if uncertain else "asserted"),
        "durability": "transient" if uncertain else "durable",
        "evidence_refs": [{"row_key": row["row_key"], "field": field}],
        "evidence_span": row[field],
        "supersedes_row_keys": supersedes or [],
        "conflicts_row_keys": conflicts or [],
        "valid_until": valid_until,
        "confidence": 1.0,
        "reason_codes": [
            "verified_tool_anchor" if verified else v1.EXPECTED_REASON_BY_TYPE[candidate_type]
        ],
    }


def _case(
    *,
    case_id: str,
    split: str,
    language: str,
    tags: list[str],
    target_chat: str,
    target_speaker: str,
    rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    no_candidate_reason: str,
) -> dict[str, Any]:
    identity_prefix = "dev" if split == "development" else "hold"
    row_key_map = {
        str(row["row_key"]): f"{identity_prefix}_{row['row_key']}"
        for row in rows
    }
    for row in rows:
        row["row_key"] = row_key_map[str(row["row_key"])]
        row["chat_key"] = f"{identity_prefix}_{row['chat_key']}"
        row["speaker_key"] = f"{identity_prefix}_{row['speaker_key']}"
        if row.get("reply_to_row_key") is not None:
            row["reply_to_row_key"] = row_key_map[str(row["reply_to_row_key"])]
        if row.get("tool_evidence_row_key") is not None:
            row["tool_evidence_row_key"] = row_key_map[
                str(row["tool_evidence_row_key"])
            ]
    for candidate in candidates:
        for ref in candidate["evidence_refs"]:
            ref["row_key"] = row_key_map[str(ref["row_key"])]
        candidate["supersedes_row_keys"] = [
            row_key_map[str(row_key)]
            for row_key in candidate["supersedes_row_keys"]
        ]
        candidate["conflicts_row_keys"] = [
            row_key_map[str(row_key)]
            for row_key in candidate["conflicts_row_keys"]
        ]
    case = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "id": case_id,
        "privacy_class": "public_synthetic",
        "split": split,
        "tags": tags,
        "language": language,
        "as_of": "2031-01-15T12:00:00Z",
        "target_chat_key": f"{identity_prefix}_{target_chat}",
        "target_speaker_key": f"{identity_prefix}_{target_speaker}",
        "inputs": rows,
        "expected": {
            "eligible": bool(candidates),
            "candidates": candidates,
            "no_candidate_reason": no_candidate_reason,
            "forbidden_outcomes": FORBIDDEN,
        },
    }
    validate_fixture_case(case)
    return case


def _single_positive(
    split: str,
    language: str,
    candidate_type: str,
    variant: int,
    ordinal: int,
) -> dict[str, Any]:
    texts = _texts(split, language, ordinal)
    text = texts[candidate_type]
    whitespace = variant == 0 and candidate_type in {
        "preference",
        "decision",
        "relationship",
        "uncertainty",
        "validity_expiry",
    }
    if candidate_type == "fact_claim" and variant == 3:
        whitespace = True
    if whitespace:
        text += "  \t\n"
    target_chat = f"chat_{ordinal % 7 + 1}"
    target_speaker = f"speaker_{ordinal % 9 + 1}"
    tags = [candidate_type, language]
    if whitespace:
        tags.append("exact_whitespace")
    if candidate_type == "fact_claim" and variant == 1:
        row = _row(
            "row_1",
            chat_key=target_chat,
            speaker_key=f"tool_{ordinal % 5 + 1}",
            actor_role="tool",
            source_role="verified_tool",
            created_at="2031-01-15T11:59:00Z",
            tool_evidence=f"Verified synthetic signal {_value(split, ordinal)} for evaluation only.",
            tool_evidence_row_key="row_1",
        )
        target_speaker = row["speaker_key"]
        tags.append("tool_fact")
    else:
        row = _row(
            "row_1",
            chat_key=target_chat,
            speaker_key=target_speaker,
            authored_text=text,
            created_at="2031-01-15T11:59:00Z",
        )
    rows = [row]
    if candidate_type == "fact_claim" and variant == 2:
        rows.append(
            _row(
                "row_2",
                chat_key=f"other_chat_{ordinal % 5 + 1}",
                speaker_key=f"other_speaker_{ordinal % 5 + 1}",
                authored_text=f"I live in unrelated synthetic district {_value(split, ordinal) + 50}.",
                created_at="2031-01-15T11:59:00Z",
            )
        )
        tags.extend(["cross_scope_bait", "cross_chat"])
    valid_until = "none"
    if candidate_type == "validity_expiry":
        day = 10 + ordinal % 10
        month = 4 if split == "development" else 8
        valid_until = f"2032-{month:02d}-{day:02d}T00:00:00Z"
    return _case(
        case_id=f"v2_{split}_{language}_{candidate_type}_{variant:02d}_{ordinal:03d}",
        split=split,
        language=language,
        tags=tags,
        target_chat=target_chat,
        target_speaker=target_speaker,
        rows=rows,
        candidates=[_candidate(candidate_type, row, valid_until=valid_until)],
        no_candidate_reason="none",
    )


def _correction(
    split: str,
    language: str,
    variant: int,
    ordinal: int,
) -> dict[str, Any]:
    target_chat = f"chat_{ordinal % 7 + 1}"
    target_speaker = f"speaker_{ordinal % 9 + 1}"
    same_speaker = variant % 2 == 0
    anchor_speaker = target_speaker if same_speaker else f"anchor_speaker_{ordinal % 5 + 1}"
    anchor_text = _texts(split, language, ordinal + 40)["fact_claim"]
    correction_text = _texts(split, language, ordinal)["correction"]
    row_1 = _row(
        "row_1",
        chat_key=target_chat,
        speaker_key=anchor_speaker,
        authored_text=anchor_text,
        created_at="2031-01-15T11:50:00Z",
    )
    row_2 = _row(
        "row_2",
        chat_key=target_chat,
        speaker_key=target_speaker,
        authored_text=(
            "Thanks, Cedar draft acknowledged for now."
            if split == "development"
            else "Acknowledged Harbor review for this moment."
        ),
        created_at="2031-01-15T11:52:00Z",
    )
    row_3 = _row(
        "row_3",
        chat_key=target_chat,
        speaker_key="bot_1",
        actor_role="bot",
        source_role="prior_bot_output",
        authored_text=(
            "Previous bot Cedar draft is not user evidence."
            if split == "development"
            else "Earlier bot Harbor review is not user evidence."
        ),
        created_at="2031-01-15T11:54:00Z",
    )
    row_4 = _row(
        "row_4",
        chat_key=target_chat,
        speaker_key=target_speaker,
        authored_text=correction_text,
        reply_to_row_key="row_1",
        created_at="2031-01-15T11:59:00Z",
    )
    correction = _candidate(
        "correction",
        row_4,
        supersedes=["row_1"] if same_speaker else [],
        conflicts=[] if same_speaker else ["row_1"],
    )
    return _case(
        case_id=f"v2_{split}_{language}_correction_{variant:02d}_{ordinal:03d}",
        split=split,
        language=language,
        tags=[
            "correction",
            language,
            "multi_prior_reply_anchor",
            "same_speaker_supersession" if same_speaker else "cross_speaker_conflict",
        ],
        target_chat=target_chat,
        target_speaker=target_speaker,
        rows=[row_1, row_2, row_3, row_4],
        candidates=[_candidate("fact_claim", row_1), correction],
        no_candidate_reason="none",
    )


def _negative(
    split: str,
    language: str,
    kind: str,
    variant: int,
    ordinal: int,
) -> dict[str, Any]:
    target_chat = f"chat_{ordinal % 7 + 1}"
    target_speaker = f"speaker_{ordinal % 9 + 1}"
    text = _negative_text(split, language, kind, ordinal)
    kwargs: dict[str, Any] = {
        "row_key": "row_1",
        "chat_key": target_chat,
        "speaker_key": target_speaker,
        "authored_text": text,
        "created_at": "2031-01-15T11:59:00Z",
    }
    reason = {
        "opinion": "opinion",
        "joke": "joke",
        "question": "question",
        "hypothetical": "hypothetical",
        "transient": "transient",
        "forwarded": "forwarded_without_endorsement",
        "prior_bot": "prior_bot_output",
        "unsupported_tool": "unsupported_tool_evidence",
        "cross_scope": "cross_scope",
        "not_durable": "not_durable",
        "unclassified": "unclassified",
    }[kind]
    tags = [kind, language, "expected_negative"]
    if kind == "forwarded":
        kwargs.update(
            authored_text="",
            source_text=text,
            source_role="forwarded_source",
        )
    elif kind == "prior_bot":
        kwargs.update(actor_role="bot", source_role="prior_bot_output")
    elif kind == "cross_scope":
        kwargs["chat_key"] = f"other_chat_{ordinal % 5 + 1}"
        tags.append("cross_chat")
    row = _row(**kwargs)
    return _case(
        case_id=f"v2_{split}_{language}_{kind}_{variant:02d}_{ordinal:03d}",
        split=split,
        language=language,
        tags=tags,
        target_chat=target_chat,
        target_speaker=target_speaker,
        rows=[row],
        candidates=[],
        no_candidate_reason=reason,
    )


def build_split(split: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    ordinal = 1
    for language in LANGUAGES:
        for candidate_type, count in (
            ("fact_claim", 4),
            ("preference", 4),
            ("decision", 4),
            ("relationship", 4),
        ):
            for variant in range(count):
                cases.append(
                    _single_positive(
                        split,
                        language,
                        candidate_type,
                        variant,
                        ordinal,
                    )
                )
                ordinal += 1
        for variant in range(6):
            cases.append(_correction(split, language, variant, ordinal))
            ordinal += 1
        for candidate_type in ("uncertainty", "validity_expiry"):
            for variant in range(3):
                cases.append(
                    _single_positive(
                        split,
                        language,
                        candidate_type,
                        variant,
                        ordinal,
                    )
                )
                ordinal += 1
        for kind in (
            "opinion",
            "opinion",
            "joke",
            "question",
            "hypothetical",
            "transient",
            "forwarded",
            "prior_bot",
            "unsupported_tool",
            "cross_scope",
            "not_durable",
            "unclassified",
        ):
            variant = sum(1 for case in cases if case["tags"][0] == kind and case["language"] == language)
            cases.append(_negative(split, language, kind, variant, ordinal))
            ordinal += 1
    if len(cases) != 160:
        raise RuntimeError(f"unexpected_{split}_case_count:{len(cases)}")
    return sorted(cases, key=lambda item: str(item["id"]))


def write_fixture(path: Path, cases: list[dict[str, Any]]) -> None:
    payload = "".join(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for case in cases
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build frozen public-synthetic v2 memory fixtures.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/fixtures"),
    )
    parser.add_argument("--split", choices=SPLITS, default="development")
    parser.add_argument("--acknowledge-holdout-manifest-sha256", default="")
    args = parser.parse_args()
    if args.split == "holdout":
        if args.acknowledge_holdout_manifest_sha256 != FROZEN_MANIFEST_SHA256:
            parser.error("holdout generation requires the exact frozen manifest hash")
        if manifest_sha256() != FROZEN_MANIFEST_SHA256:
            parser.error("frozen manifest bytes do not match")
    path = args.output_dir / f"memory_extraction_v2_{args.split}.jsonl"
    write_fixture(path, build_split(args.split))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
