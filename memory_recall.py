"""Pure recall admission and rollout policy, independent of Telegram and providers.

The adapter owns the unchanged legacy detector and supplies its observed cosine.
This module never fetches embeddings, reads memory, or grants invocation scope.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal


RecallGuardVerdict = Literal["allow", "veto", "defer"]
RecallPolicyMode = Literal["off", "shadow", "enforce"]


# Only the user's request outside quotations supplies intent. A quoted topic
# remains in the adapter-owned search query, but cannot activate this guard.
_QUOTED_TEXT = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|«[^»]*»|“[^”]*”|\"[^\"\n]*\"|"
    r"(?<!\w)'[^'\n]+'(?!\w)|‘[^’\n]*’|(?m:^[ \t]*>[^\n]*)"
)
_CONVERSATION = re.compile(
    r"\b(?:переписк\w*|переписц\w*|листуван\w*|чат\w*|розмов\w*|разговор\w*|"
    r"обговор\w*|обсужден\w*|повідомлен\w*|сообщен\w*|messages?|chats?|"
    r"conversations?|discussions?)\b"
)
_PAST = re.compile(
    r"\b(?:раніше|раньше|ранее|минул\w*|прошл\w*|поперед\w*|предыдущ\w*|"
    r"колись|тоді|тодіш\w*|тогда|тогдаш\w*|стар(?:ий|ого|ому|им|і|их|ими|а|ої|ій|у|е|ый|ое|ая|ую|ой|ые|ых|ым|ыми)|earlier|previous(?:ly)?|"
    r"before|then|old|last\s+(?:time|week|month|year|meeting))\b"
)
_SHARED = re.compile(
    r"\b(?:ми|мы|ти|ты|я|нам|нас|мені|мне|наш\w*|тут|здесь|сюди|сюда|"
    r"we|our|you|i|me|here)\b"
)
_PAST_ACTION = re.compile(
    r"\b(?:говорил\w*|говорили|казал\w*|казав\w*|казали|писал\w*|писав\w*|писали|"
    r"обговорювал\w*|обсуждал\w*|вирішил\w*|решил\w*|домовлял\w*|домовил\w*|договаривал\w*|"
    r"договорил\w*|погодил\w*|согласовал\w*|обрал\w*|выбрал\w*|радив\w*|радил\w*|порадив\w*|советовал\w*|"
    r"рекомендувал\w*|рекомендовал\w*|надсилав\w*|надсилал\w*|присылал\w*|надіслав\w*|відправил\w*|"
    r"отправил\w*|вказувал\w*|указывал\w*|називал\w*|называл\w*|пропонував\w*|"
    r"предлагал\w*|порівнювал\w*|сравнивал\w*|виправил\w*|исправил\w*|"
    r"відклал\w*|отложил\w*|зупинил\w*|остановил\w*|затвердил\w*|утвердил\w*|"
    r"said|told|wrote|discussed|agreed|decided|chose|chosen|recommended|suggested|"
    r"mentioned|specified|shared|sent|compared|corrected)\b|\b(?:did|had)\s+(?:we|you|i)\b"
)
_REQUEST = re.compile(
    r"\?|\b(?:знайд\w*|найд\w*|пошук\w*|поищ\w*|нагадай\w*|напомни\w*|"
    r"пригадай\w*|вспомни\w*|підніми|подними|перевір\w*|проверь\w*|віднов\w*|восстанов\w*|"
    r"find|search|look\s+up|pull\s+up|check|recover|remind|recall)\b|"
    r"^(?:(?:а|and)\s+)?(?:що|хто|де|коли|чому|скільки|як\w*|что|кто|где|когда|"
    r"почему|сколько|как\w*|what|which|who|where|when|why|how)\b"
)
_POLITE_START = r"^(?:(?:будь\s+ласка|пожалуйста|please|can\s+you|could\s+you)\W+)?"
_TEXT_TRANSFORM = re.compile(
    _POLITE_START
    + r"(?:перефраз\w*|переведи\w*|переклади\w*|відредагуй\w*|отредактируй\w*|"
    r"translate|rephrase|reword|rewrite|edit|make\s+(?:the|this)\s+(?:sentence|phrase))\b"
)
_NEW_MEMORY = re.compile(
    _POLITE_START
    + r"(?:запам['’]?ятай\w*\b|запомни\w*\b|remember\s+this\b|remember\s+that\s*:)"
)
_REMINDER_TIME = re.compile(
    _POLITE_START
    + r"(?:нагадай|напомни|remind)\s+(?:(?:мені|мне|me)\s+)?"
    r"(?:завтра|післязавтра|послезавтра|через|о\s+\d|в\s+\d|tomorrow|tonight|"
    r"next\b|in\s+\d|at\s+\d|on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))"
)
_NEGATED_LOOKUP = re.compile(
    r"\b(?:не\s+(?:шукай\w*|ищи\w*|згадуй\w*|вспоминай\w*|піднімай\w*|поднимай\w*|"
    r"використовуй\w*|используй\w*)|не\s+(?:треба|потрібно|надо|нужно|слід|стоит)\s+"
    r"(?:шукати|искать|згадувати|вспоминать|використовувати|использовать)|"
    r"(?:do\s+not|don't)\s+(?:search|look\s+up|retrieve|recall|use)|"
    r"without\s+(?:searching|using|looking\s+up)|без\s+(?:пошуку|поиска))\b"
)
_UNNEEDED_HISTORY = re.compile(
    r"\b(?:не\s+(?:потріб\w*|нуж\w*)|(?:not|aren't|isn't)\s+(?:needed|required))\b"
)
_HISTORY_TARGET = re.compile(r"\b(?:істор\w*|истори\w*|пам['’]?ят\w*|history|memory)\b")
_GENERAL_HELP = re.compile(
    _POLITE_START
    + r"(?:поясни\w*|объясни\w*|розкажи\w*|расскажи\w*|explain|describe|tell\s+me|"
    r"give\s+(?:a\s+)?(?:brief\s+)?history|як\s+|как\s+|how\s+(?:do|does|to)\s+)"
)
_REMIND_HOW = re.compile(
    _POLITE_START + r"(?:нагадай|напомни|remind(?:\s+me)?)\s*[, :]?\s*(?:як|как|how)\b"
)
_SHORT_FOLLOWUP = re.compile(
    r"^(?:(?:а|and)\s+)?(?:скільки|сколько|how\s+(?:many|much)|і\s+що|и\s+что|what\s+else)\W*$"
)
_PRIOR_DECISION = re.compile(
    r"\b(?:наш\w*|our)\b.{0,80}\b(?:рішен\w*|решен\w*|домовлен\w*|договорен\w*|"
    r"вибір|выбор|decisions?|agreements?|choices?|plans?)\b"
)


@dataclass(frozen=True)
class RecallSemanticGuard:
    """Explicit recall evidence, a conflicting intent, or no lexical decision."""

    verdict: RecallGuardVerdict
    reason: str

    def __post_init__(self) -> None:
        if self.verdict not in {"allow", "veto", "defer"}:
            raise ValueError("Unknown recall semantic guard verdict")


@dataclass(frozen=True)
class RecallAdmissionDecision:
    is_recall: bool
    confidence: float
    reason: str
    degraded: bool = False


@dataclass(frozen=True)
class RecallRolloutResolution:
    mode: RecallPolicyMode
    applied_is_recall: bool
    applied_source: Literal["legacy", "candidate"]
    candidate_is_recall: bool | None
    differs: bool | None
    fallback_reason: str = ""


def normalize_recall_policy_mode(value: str | None) -> RecallPolicyMode:
    mode = str(value or "off").strip().casefold()
    if mode not in {"off", "shadow", "enforce"}:
        raise ValueError("MEMORY_RECALL_POLICY_MODE must be one of: off, shadow, enforce")
    return mode


def recall_semantic_guard(prompt: str, *, has_reference: bool = False) -> RecallSemanticGuard:
    """Recognize prior conversation requests without treating topic words as intent.

    Rules use request grammar, shared/past evidence and explicit competing
    instructions. They contain no fixture entities and confer no permission to
    search another chat. References help distinguish interpretation of supplied
    material from an explicit request to recover an earlier decision.
    """
    unquoted, quoted_count = _QUOTED_TEXT.subn(" ", prompt or "")
    text = " ".join(unquoted.casefold().replace("’", "'").split())
    if not text:
        return RecallSemanticGuard("veto", "quoted_or_empty_request")
    transform = bool(_TEXT_TRANSFORM.search(text))
    if transform and (quoted_count or ":" in text):
        return RecallSemanticGuard("veto", "text_transformation")
    for clause in re.split(r"[.!?;\n]", text):
        if (_CONVERSATION.search(clause) or _HISTORY_TARGET.search(clause) or _PAST.search(clause)) and (
            _NEGATED_LOOKUP.search(clause) or _UNNEEDED_HISTORY.search(clause)
        ):
            return RecallSemanticGuard("veto", "explicit_no_history")
    if _NEW_MEMORY.search(text):
        return RecallSemanticGuard("veto", "new_memory_statement")
    if _REMINDER_TIME.search(text):
        return RecallSemanticGuard("veto", "future_reminder")

    has_past = bool(_PAST.search(text))
    has_shared = bool(_SHARED.search(text))
    has_conversation = bool(_CONVERSATION.search(text))
    shared_past = (has_shared and bool(_PAST_ACTION.search(text))) or (
        has_past and bool(_PRIOR_DECISION.search(text))
    )
    prior_source = has_conversation and (has_past or has_shared)
    if (transform or _REQUEST.search(text)) and (prior_source or shared_past):
        return RecallSemanticGuard("allow", "prior_shared_conversation")

    if transform:
        return RecallSemanticGuard("veto", "text_transformation")
    if quoted_count:
        return RecallSemanticGuard("veto", "quoted_context_without_history")
    if has_reference and not has_past:
        return RecallSemanticGuard("veto", "supplied_current_source")
    if _SHORT_FOLLOWUP.fullmatch(text):
        return RecallSemanticGuard("veto", "unanchored_followup")
    if not prior_source and not shared_past and (
        _GENERAL_HELP.search(text) or _REMIND_HOW.search(text)
    ):
        return RecallSemanticGuard("veto", "general_help_without_history")
    return RecallSemanticGuard("defer", "no_explicit_intent")


def evaluate_recall_admission(
    *,
    confidence: float,
    guard: RecallSemanticGuard,
    threshold: float,
    ambiguous_threshold: float,
    has_context_hint: bool,
    degraded: bool = False,
) -> RecallAdmissionDecision:
    """Apply a supplied guard and thresholds to one already observed score.

    Guard rules and thresholds are explicit inputs so development calibration
    can reuse frozen vectors. A veto cannot be overridden by a high cosine.
    A degraded observation supports lexical decisions only, never score-based
    admission. Confidence remains the observed cosine, not a probability or a
    synthetic score for lexical evidence.
    """
    if not math.isfinite(confidence):
        raise ValueError("Recall confidence must be finite")
    if not (
        math.isfinite(threshold)
        and math.isfinite(ambiguous_threshold)
        and 0.0 <= ambiguous_threshold <= threshold <= 1.0
    ):
        raise ValueError("Recall thresholds must satisfy 0 <= ambiguous <= strong <= 1")

    if guard.verdict == "allow":
        return RecallAdmissionDecision(True, confidence, guard.reason, degraded)
    if guard.verdict == "veto":
        return RecallAdmissionDecision(False, confidence, guard.reason, degraded)
    if degraded:
        return RecallAdmissionDecision(False, confidence, "degraded_without_explicit_recall", True)
    if confidence >= threshold:
        return RecallAdmissionDecision(True, confidence, "semantic_strong")
    if confidence >= ambiguous_threshold and has_context_hint:
        return RecallAdmissionDecision(True, confidence, "semantic_ambiguous_with_hint")
    return RecallAdmissionDecision(False, confidence, "semantic_below_threshold")


def resolve_recall_rollout(
    *,
    mode: str | None,
    legacy_is_recall: bool,
    candidate_is_recall: bool | None = None,
    candidate_failed: bool = False,
) -> RecallRolloutResolution:
    """Select only the applied decision; the adapter retains full legacy data.

    Off ignores any supplied candidate. Shadow records a comparison while
    applying legacy behavior. Missing/failed candidates use legacy behavior in
    both enabled modes, so a candidate fault cannot disable a legacy recall.
    """
    normalized_mode = normalize_recall_policy_mode(mode)
    if normalized_mode == "off":
        return RecallRolloutResolution("off", legacy_is_recall, "legacy", None, None)
    if candidate_failed or candidate_is_recall is None:
        return RecallRolloutResolution(
            normalized_mode,
            legacy_is_recall,
            "legacy",
            None,
            None,
            "candidate_failed" if candidate_failed else "candidate_missing",
        )
    if not isinstance(candidate_is_recall, bool):
        raise ValueError("Recall candidate decision must be a bool")
    apply_candidate = normalized_mode == "enforce"
    return RecallRolloutResolution(
        normalized_mode,
        candidate_is_recall if apply_candidate else legacy_is_recall,
        "candidate" if apply_candidate else "legacy",
        candidate_is_recall,
        candidate_is_recall != legacy_is_recall,
    )
