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


BOT_WORDS = {
    "aigan",
    "thrd_ua_bot",
    "bot",
    "бот",
    "бота",
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

    mentions_bot = reply_to_bot or any(marker in lowered for marker in bot_markers)
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
