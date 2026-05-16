from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol

from system_log import ComplaintCluster, SystemLogStore, redact_secrets, sanitize_text


class Reporter(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def create_self_report_issue(self, *, title: str, body: str, labels: list[str] | None = None) -> Any: ...


@dataclass(frozen=True)
class ComplaintSignal:
    category: str
    fingerprint: str
    sample: str
    reason: str


REACTION_HEALTH_CATEGORIES = {
    "insensitive_reaction",
    "reaction_reasoning_gap",
    "tone_boundary",
    "fake_empathy",
    "sycophancy",
}
BOT_WORDS = {
    "aigan",
    "thrd_ua_bot",
    "bot",
    "бот",
    "бота",
    "боте",
    "боті",
    "ботом",
    "боту",
    "агент",
    "аіган",
    "айган",
}
COMPLAINT_WORDS = {
    "не працю",
    "не работает",
    "не відпов",
    "не ответ",
    "не бач",
    "не вид",
    "не може",
    "не смог",
    "не зміг",
    "не вміє",
    "не умеет",
    "злам",
    "слом",
    "лага",
    "тупить",
    "тупит",
    "баг",
    "bug",
    "проблем",
    "помил",
    "ошиб",
    "галюцин",
    "hallucin",
    "забув",
    "забыл",
    "бреше",
    "врет",
    "не той",
    "не то",
    "не так",
}
CATEGORY_PATTERNS = [
    ("web_search", ("web", "веб", "search", "пошук", "поиск", "джерел", "источник", "source")),
    ("image", ("image", "vision", "картин", "фото", "зображ", "мем", "альбом")),
    ("memory_context", ("memory", "пам", "контекст", "context", "забув", "забыл")),
    ("formatting", ("format", "markdown", "html", "зіроч", "звезд", "**", "жирн")),
    ("telegram_delivery", ("telegram", "телеграм", "відпов", "ответ", "повідом", "сообщ")),
    ("agent_quality", ("галюцин", "hallucin", "бреше", "врет", "туп", "клоун")),
]
REACTION_TERMS = (
    "reaction",
    "react",
    "emoji",
    "emote",
    "smile",
    "smiley",
    "fire emoji",
    "fire reaction",
    "thumbs up",
    "put that",
    "put this",
    "set that",
    "set this",
    "posted reaction",
    "posted emoji",
    "reacted",
    "реакц*",
    "эмод",
    "емод",
    "смайл",
    "огон",
    "вогон",
    "лайк",
    "постав*",
)
REACTION_REASON_TERMS = (
    "why",
    "reason",
    "rationale",
    "explain",
    "what logic",
    "почему",
    "зачем",
    "чому",
    "навіщо",
    "поясн*",
    "объясн*",
)
REACTION_REASON_CHALLENGE_TERMS = (
    "explain that reaction",
    "explain this reaction",
    "explain your reaction",
    "what logic did you use",
    "what was the logic",
    "why did you do that",
    "why did you put",
    "why did you put that reaction",
    "why did you react",
    "why did you react that way",
    "why did you send",
    "why that reaction",
    "why this reaction",
    "why'd you do that",
    "зачем ты это сделал",
    "навіщо ти це зробив",
    "почему ты это сделал",
    "чому ти це зробив",
)
REACTION_APPROVAL_TERMS = (
    "approval of harm",
    "looks like approval",
    "looks like you approve",
    "looks like support",
    "looks like endorsement",
    "looks approving",
    "approving reaction",
    "endorsing reaction",
    "supporting harm",
    "liked the idea",
    "виглядає як підтримка",
    "виглядає як схвалення",
    "похоже на одобрение",
    "схоже на підтримку",
    "схоже на схвалення",
)
REACTION_INSENSITIVE_TERMS = (
    "inappropriate",
    "insensitive",
    "wrong reaction",
    "bad emoji",
    "bad emoji reaction",
    "tone deaf",
    "not ok",
    "неумест",
    "недореч",
    "не так",
)
REACTION_PASSIVE_COMPLAINT_TERMS = (
    "inappropriate reaction",
    "explain that reaction",
    "explain this reaction",
    "explain your reaction",
    "wrong emoji",
    "wrong reaction",
    "why did you put",
    "why did you put that reaction",
    "why did you react",
    "why did you react that way",
    "why did you send",
    "why that reaction",
    "why this reaction",
)
REACTION_FAKE_EMPATHY_TERMS = (
    "fake empathy",
    "performative empathy",
    "pretend empathy",
    "фальшив",
)
REACTION_TONE_TERMS = (
    "tone boundary",
    "crossed a tone boundary",
    "wrong tone",
    "bad tone",
    "too cheerful",
    "too casual",
    "tone deaf",
    "не тот тон",
    "плохой тон",
    "перейшов межу",
    "перешел границу",
)
REACTION_SYCOPHANCY_TERMS = (
    "sycophancy",
    "sycophantic",
    "sycophant",
    "yes-man",
    "yes man",
    "agree with everything",
    "поддаки",
)
NON_REACTION_BOT_OUTPUT_TERMS = (
    "answer",
    "message",
    "post",
    "posted",
    "reply",
    "response",
    "said",
)


def has_marker(lowered: str, marker: str) -> bool:
    clean_marker = marker.casefold().strip()
    if not clean_marker:
        return False
    if clean_marker.endswith("*"):
        stem = clean_marker[:-1].strip()
        if not stem:
            return False
        return re.search(rf"(?<![\w-]){re.escape(stem)}", lowered, re.UNICODE) is not None
    if re.match(r"[\w-]", clean_marker, re.UNICODE) or re.search(r"[\w-]$", clean_marker, re.UNICODE):
        return re.search(rf"(?<![\w-]){re.escape(clean_marker)}(?![\w-])", lowered, re.UNICODE) is not None
    return clean_marker in lowered


def has_any_marker(lowered: str, markers: tuple[str, ...] | set[str]) -> bool:
    return any(has_marker(lowered, marker) for marker in markers)


def has_reaction_complaint_hint(
    text: str,
    *,
    bot_username: str | None = None,
    reply_to_bot: bool = False,
) -> bool:
    clean = sanitize_text(text or "", 500)
    if not clean:
        return False
    lowered = clean.casefold()
    bot_markers = set(BOT_WORDS)
    if bot_username:
        bot_markers.add(bot_username.casefold().lstrip("@"))
        bot_markers.add("@" + bot_username.casefold().lstrip("@"))
    mentions_bot = reply_to_bot or has_any_marker(lowered, bot_markers)
    has_reaction_marker = has_any_marker(lowered, REACTION_TERMS)
    has_approval_marker = has_any_marker(lowered, REACTION_APPROVAL_TERMS)
    has_passive_complaint_marker = has_any_marker(lowered, REACTION_PASSIVE_COMPLAINT_TERMS)
    if not mentions_bot:
        return has_approval_marker or has_passive_complaint_marker
    return (
        has_reaction_marker
        or has_approval_marker
        or has_any_marker(lowered, REACTION_FAKE_EMPATHY_TERMS)
        or has_any_marker(lowered, REACTION_TONE_TERMS)
        or has_any_marker(lowered, REACTION_SYCOPHANCY_TERMS)
        or has_any_marker(lowered, REACTION_REASON_CHALLENGE_TERMS)
    )


def classify_complaint(
    text: str,
    *,
    bot_username: str | None = None,
    reply_to_bot: bool = False,
) -> ComplaintSignal | None:
    clean = sanitize_text(text or "", 500)
    if not clean:
        return None
    lowered = clean.casefold()
    bot_markers = set(BOT_WORDS)
    if bot_username:
        bot_markers.add(bot_username.casefold().lstrip("@"))
        bot_markers.add("@" + bot_username.casefold().lstrip("@"))

    mentions_bot = reply_to_bot or has_any_marker(lowered, bot_markers)
    has_complaint = any(marker in lowered for marker in COMPLAINT_WORDS)
    if not (mentions_bot and has_complaint):
        return None

    category = categorize_complaint(lowered)
    normalized = normalize_for_fingerprint(lowered)
    fingerprint = hashlib.sha256(f"{category}:{normalized}".encode("utf-8")).hexdigest()[:16]
    return ComplaintSignal(
        category=category,
        fingerprint=fingerprint,
        sample=clean,
        reason="bot-related complaint signal",
    )


def classify_reaction_complaint(
    text: str,
    *,
    bot_username: str | None = None,
    reply_to_bot: bool = False,
    has_recent_reaction: bool = False,
    rationale_state: str = "",
    decision_action: str = "",
    decision_reason: str = "",
    emotion_class: str = "",
    target_fingerprint: str = "",
) -> ComplaintSignal | None:
    clean = sanitize_text(text or "", 500)
    if not clean:
        return None
    lowered = clean.casefold()
    bot_markers = set(BOT_WORDS)
    if bot_username:
        bot_markers.add(bot_username.casefold().lstrip("@"))
        bot_markers.add("@" + bot_username.casefold().lstrip("@"))

    mentions_bot = reply_to_bot or has_any_marker(lowered, bot_markers)
    has_reaction_marker = has_any_marker(lowered, REACTION_TERMS)
    has_reason_marker = has_any_marker(lowered, REACTION_REASON_TERMS)
    has_reason_challenge_marker = has_any_marker(lowered, REACTION_REASON_CHALLENGE_TERMS)
    has_approval_marker = has_any_marker(lowered, REACTION_APPROVAL_TERMS)
    has_insensitive_marker = has_any_marker(lowered, REACTION_INSENSITIVE_TERMS)
    has_passive_complaint_marker = has_any_marker(lowered, REACTION_PASSIVE_COMPLAINT_TERMS)
    has_fake_empathy_marker = has_any_marker(lowered, REACTION_FAKE_EMPATHY_TERMS)
    has_tone_marker = has_any_marker(lowered, REACTION_TONE_TERMS)
    has_sycophancy_marker = has_any_marker(lowered, REACTION_SYCOPHANCY_TERMS)
    has_non_reaction_output_marker = has_any_marker(lowered, NON_REACTION_BOT_OUTPUT_TERMS)
    has_reaction_context = bool(has_recent_reaction or has_reaction_marker)
    has_temporal_reaction_context = bool(has_recent_reaction and mentions_bot)
    has_explicit_or_temporal_context = bool(has_reaction_marker or has_temporal_reaction_context)
    temporal_context_points_to_non_reaction_output = bool(
        has_temporal_reaction_context and not has_reaction_marker and has_non_reaction_output_marker
    )
    has_reaction_category_context = bool(
        has_explicit_or_temporal_context and not temporal_context_points_to_non_reaction_output
    )
    has_reason_context = bool(
        has_reaction_marker
        or (
            has_temporal_reaction_context
            and has_reason_challenge_marker
            and not temporal_context_points_to_non_reaction_output
        )
    )

    if not (mentions_bot or has_recent_reaction):
        return None
    if not mentions_bot and not (
        has_approval_marker or (has_recent_reaction and has_passive_complaint_marker)
    ):
        return None
    if not has_reaction_context and not has_approval_marker:
        return None

    if has_sycophancy_marker and has_reaction_category_context:
        category = "sycophancy"
    elif has_fake_empathy_marker and has_reaction_category_context:
        category = "fake_empathy"
    elif has_tone_marker and has_reaction_category_context:
        category = "tone_boundary"
    elif has_approval_marker and (has_reaction_marker or has_recent_reaction):
        category = "insensitive_reaction"
    elif (
        has_insensitive_marker
        and has_explicit_or_temporal_context
        and not temporal_context_points_to_non_reaction_output
    ):
        category = "insensitive_reaction"
    elif (
        has_reason_marker
        and has_reason_context
        and (has_recent_reaction or rationale_state in {"missing_decision", "insufficient_rationale"})
    ):
        category = "reaction_reasoning_gap"
    else:
        return None
    if category not in REACTION_HEALTH_CATEGORIES:
        return None

    context_bits = [
        f"category={category}",
        f"target={safe_detail_code(target_fingerprint or 'unlinked')}",
        f"decision={safe_detail_code(decision_action or 'unknown')}",
        f"reason={safe_detail_code(decision_reason or 'unknown')}",
        f"emotion={safe_detail_code(emotion_class or 'unknown')}",
        f"rationale={safe_detail_code(rationale_state or 'unknown')}",
    ]
    sample = "Reaction complaint signal: " + "; ".join(context_bits)
    fingerprint_basis = (
        f"{category}:{target_fingerprint or normalize_for_fingerprint(lowered)}:"
        f"{rationale_state}:{decision_reason}:{emotion_class}"
    )
    fingerprint = hashlib.sha256(fingerprint_basis.encode("utf-8")).hexdigest()[:16]
    return ComplaintSignal(
        category=category,
        fingerprint=fingerprint,
        sample=sanitize_text(sample, 300),
        reason="reaction-specific complaint signal",
    )


def categorize_complaint(lowered: str) -> str:
    for category, markers in CATEGORY_PATTERNS:
        if any(marker in lowered for marker in markers):
            return category
    return "general"


def normalize_for_fingerprint(lowered: str) -> str:
    text = redact_secrets(lowered)
    text = re.sub(r"https?://\S+|www\.\S+", " url ", text)
    text = re.sub(r"@[a-z0-9_]{1,64}", " mention ", text)
    words = re.findall(r"[a-zа-яіїєґ0-9*]+", text, flags=re.IGNORECASE)
    meaningful = [word for word in words if len(word) >= 3 and word not in BOT_WORDS]
    return " ".join(meaningful[:8]) or "general"


def safe_detail_code(value: str) -> str:
    redacted = redact_secrets(str(value or ""))
    if "[redacted]" in redacted.casefold():
        return "redacted"
    text = re.sub(r"[^a-z0-9_.:-]+", "_", redacted.casefold()).strip("_")
    return text[:80] if text else "unknown"


class SelfAnalysisService:
    def __init__(
        self,
        *,
        store: SystemLogStore | None,
        reporter: Reporter | None = None,
        complaint_lookback_seconds: int = 86400,
        complaint_report_temperature: int = 3,
    ) -> None:
        self.store = store
        self.reporter = reporter
        self.complaint_lookback_seconds = max(1, int(complaint_lookback_seconds))
        self.complaint_report_temperature = max(1, int(complaint_report_temperature))

    def enabled(self) -> bool:
        return self.store is not None

    def record_complaint_signal(
        self,
        *,
        text: str,
        bot_username: str | None = None,
        reply_to_bot: bool = False,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> ComplaintCluster | None:
        if self.store is None:
            return None
        signal = classify_complaint(text, bot_username=bot_username, reply_to_bot=reply_to_bot)
        if signal is None:
            return None

        cluster = self.store.upsert_complaint(
            fingerprint=signal.fingerprint,
            category=signal.category,
            sample=signal.sample,
            window_seconds=self.complaint_lookback_seconds,
        )
        self.store.record_event(
            level="warning",
            component="self_analysis",
            event_type="tech_issue_signal",
            chat_id=chat_id,
            user_id=user_id,
            message=f"{cluster.category} temperature={cluster.temperature}",
            details={
                "category": cluster.category,
                "temperature": cluster.temperature,
                "fingerprint": cluster.fingerprint,
                "reason": signal.reason,
                "sample_preview": signal.sample,
            },
        )
        self._maybe_report_complaint(cluster)
        return cluster

    def record_reaction_complaint_signal(
        self,
        *,
        text: str,
        bot_username: str | None = None,
        reply_to_bot: bool = False,
        chat_id: int | None = None,
        user_id: int | None = None,
        has_recent_reaction: bool = False,
        rationale_state: str = "",
        decision_action: str = "",
        decision_reason: str = "",
        emotion_class: str = "",
        target_fingerprint: str = "",
    ) -> ComplaintCluster | None:
        if self.store is None:
            return None
        signal = classify_reaction_complaint(
            text,
            bot_username=bot_username,
            reply_to_bot=reply_to_bot,
            has_recent_reaction=has_recent_reaction,
            rationale_state=rationale_state,
            decision_action=decision_action,
            decision_reason=decision_reason,
            emotion_class=emotion_class,
            target_fingerprint=target_fingerprint,
        )
        if signal is None:
            return None

        cluster = self.store.upsert_complaint(
            fingerprint=signal.fingerprint,
            category=signal.category,
            sample=signal.sample,
            window_seconds=self.complaint_lookback_seconds,
        )
        self.store.record_event(
            level="warning",
            component="self_analysis",
            event_type="reaction_complaint_signal",
            chat_id=chat_id,
            user_id=user_id,
            message=f"{cluster.category} temperature={cluster.temperature}",
            details={
                "category": cluster.category,
                "temperature": cluster.temperature,
                "fingerprint": cluster.fingerprint,
                "reason": signal.reason,
                "target": safe_detail_code(target_fingerprint or "unlinked"),
                "decision": safe_detail_code(decision_action or "unknown"),
                "decision_reason": safe_detail_code(decision_reason or "unknown"),
                "emotion": safe_detail_code(emotion_class or "unknown"),
                "rationale": safe_detail_code(rationale_state or "unknown"),
            },
        )
        self._maybe_report_complaint(cluster)
        return cluster

    def _maybe_report_complaint(self, cluster: ComplaintCluster) -> None:
        if self.store is None:
            return
        if cluster.temperature < self.complaint_report_temperature:
            return
        if cluster.github_issue_url and cluster.last_reported_temperature >= cluster.temperature:
            return
        if self.reporter is None or not getattr(self.reporter, "is_configured", False):
            self.store.record_event(
                level="info",
                component="github_reporting",
                event_type="self_report_skipped",
                message="GitHub reporting disabled or not configured",
                details={"fingerprint": cluster.fingerprint, "temperature": cluster.temperature},
            )
            return

        title = f"[Aigan] self-report: {cluster.category} temperature {cluster.temperature}"
        body = build_self_report_issue_body(cluster)
        try:
            issue = self.reporter.create_self_report_issue(title=title, body=body, labels=None)
        except Exception as exc:
            self.store.record_event(
                level="error",
                component="github_reporting",
                event_type="self_report_failed",
                message=str(exc),
                details={"fingerprint": cluster.fingerprint, "temperature": cluster.temperature},
            )
            return
        if issue is None:
            self.store.record_event(
                level="info",
                component="github_reporting",
                event_type="self_report_skipped",
                message="GitHub reporter returned no issue",
                details={"fingerprint": cluster.fingerprint, "temperature": cluster.temperature},
            )
            return
        self.store.mark_complaint_reported(cluster.fingerprint, issue.url, cluster.temperature)
        self.store.record_event(
            level="warning",
            component="github_reporting",
            event_type="self_report_created",
            message=issue.url,
            details={"fingerprint": cluster.fingerprint, "temperature": cluster.temperature},
        )

    def health_text(self, lookback_seconds: int = 21600) -> str:
        if self.store is None:
            return "System health logs are disabled."
        summary = self.store.health_summary(lookback_seconds)
        counts = summary["counts"]
        lines = [
            f"Status: {summary['status']}",
            f"Lookback: {int(lookback_seconds // 3600) or 1}h",
            f"Events: info={counts.get('info', 0)}, warning={counts.get('warning', 0)}, error={counts.get('error', 0)}, critical={counts.get('critical', 0)}",
        ]
        if summary["top_components"]:
            lines.append("Top problem components:")
            lines.extend(f"- {name}: {count}" for name, count in summary["top_components"])
        complaints = summary.get("active_complaints") or []
        if complaints:
            lines.append("Active complaint temperatures:")
            for item in complaints:
                issue = f" {item['github_issue_url']}" if item.get("github_issue_url") else ""
                lines.append(f"- {item['category']}: {item['temperature']}{issue}")
        if summary["recent_problems"]:
            lines.append("Recent problems:")
            lines.extend(f"- {item}" for item in summary["recent_problems"])
        return "\n".join(lines)

    def logs_text(self, limit: int = 20) -> str:
        if self.store is None:
            return "System health logs are disabled."
        events = self.store.latest_events(limit)
        if not events:
            return "No system events yet."
        lines = []
        for event in events:
            route = f" route={event.route}" if event.route else ""
            duration = f" {event.duration_ms}ms" if event.duration_ms is not None else ""
            lines.append(f"{event.created_at} {event.level} {event.component}/{event.event_type}{route}{duration}: {event.message}")
        return "\n".join(lines)

    def complaints_text(self, limit: int = 10) -> str:
        if self.store is None:
            return "System health logs are disabled."
        clusters = self.store.active_complaints(limit)
        if not clusters:
            return "No active complaint temperatures."
        lines = ["Active complaint temperatures:"]
        for cluster in clusters:
            issue = f"\n  issue: {cluster.github_issue_url}" if cluster.github_issue_url else ""
            lines.append(
                f"- {cluster.category}: temperature={cluster.temperature}, last_seen={cluster.last_seen}\n"
                f"  sample: {cluster.sample}{issue}"
            )
        return "\n".join(lines)

    def selfcheck_context(self, lookback_seconds: int = 21600) -> str:
        if self.store is None:
            return "System health logs are disabled."
        summary = self.store.health_summary(lookback_seconds)
        events = self.store.latest_events(20)
        return "\n".join(
            [
                "Sanitized system health summary:",
                sanitize_text(str(summary), 2500),
                "",
                "Recent sanitized events:",
                "\n".join(
                    f"- {event.created_at} {event.level} {event.component}/{event.event_type}: {event.message}"
                    for event in events
                )
                or "(none)",
            ]
        )


def build_self_report_issue_body(cluster: ComplaintCluster) -> str:
    return "\n".join(
        [
            "## Aigan self-report",
            "",
            "This issue was created from repeated user complaint signals. It is not a confirmed bug yet.",
            "",
            f"- category: `{cluster.category}`",
            f"- temperature: `{cluster.temperature}`",
            f"- fingerprint: `{cluster.fingerprint}`",
            f"- first seen: `{cluster.first_seen}`",
            f"- last seen: `{cluster.last_seen}`",
            "",
            "## Sanitized sample",
            "",
            cluster.sample or "(empty)",
            "",
            "## Triage checklist",
            "",
            "- Check container logs around `last_seen`.",
            "- Reproduce the route with a minimal Telegram update or unit test.",
            "- Confirm whether this is product behavior, Telegram delivery, web/image tooling, or model quality.",
            "- Add a regression test before fixing if the behavior is a bug.",
        ]
    )
