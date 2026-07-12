from __future__ import annotations

import asyncio
import hashlib
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping


OPERATOR_ALERT_POLICY_VERSION = "operator_alert_v1"
ALERT_LEVELS = {"info": 0, "warning": 1, "error": 2, "critical": 3}


@dataclass(frozen=True)
class AlertDefinition:
    level: str
    title_uk: str
    issue_numbers: tuple[int, ...]
    fact_labels: Mapping[str, str]


ALERT_DEFINITIONS: dict[str, AlertDefinition] = {
    "image_search_failed": AlertDefinition(
        "error",
        "Пошук зображень завершився помилкою.",
        (153, 158),
        {"search_passes": "Пошукових проходів", "requested_candidates": "Запитано кандидатів"},
    ),
    "image_candidate_review_failed": AlertDefinition(
        "error",
        "Передвідправний visual-review не дав надійного результату.",
        (153, 158),
        {"candidate_count": "Кандидатів", "attempts": "Спроб review"},
    ),
    "image_pipeline_timeout": AlertDefinition(
        "error",
        "Пошук і перевірка зображень перевищили загальний дедлайн.",
        (153, 158),
        {"target_count": "Ціль альбому", "timeout_seconds": "Дедлайн, с"},
    ),
    "image_delivery_partial": AlertDefinition(
        "warning",
        "Альбом доставлено лише частково.",
        (153, 158),
        {"confirmed_parts": "Підтверджено", "intended_parts": "Планувалося"},
    ),
    "image_delivery_ambiguous": AlertDefinition(
        "warning",
        "Результат доставки зображень невизначений; повтор заблоковано.",
        (153, 158),
        {"confirmed_parts": "Підтверджено", "intended_parts": "Планувалося"},
    ),
    "image_delivery_failed": AlertDefinition(
        "error",
        "Telegram не прийняв жодного відібраного зображення.",
        (153, 158),
        {"intended_parts": "Планувалося"},
    ),
    "image_delivery_persistence_failed": AlertDefinition(
        "error",
        "Доставлене зображення не вдалося записати в локальну пам'ять.",
        (153, 158),
        {"failed_parts": "Незаписаних частин"},
    ),
    "telegram_command_sync_failed": AlertDefinition(
        "warning",
        "Telegram не прийняв оновлення меню команд.",
        (158, 159),
        {},
    ),
}


@dataclass(frozen=True)
class OperatorAlertSettings:
    enabled: bool
    chat_id: int | None
    min_level: str = "warning"
    cooldown_seconds: int = 3600


@dataclass(frozen=True)
class OperatorAlert:
    code: str
    level: str
    title_uk: str
    issue_numbers: tuple[int, ...]
    facts: tuple[tuple[str, int | bool], ...]
    fingerprint: str


@dataclass(frozen=True)
class OperatorAlertResult:
    status: str
    fingerprint: str = ""


AlertEventCallback = Callable[[str, OperatorAlert, Mapping[str, int | bool]], None]
RecentClaimChecker = Callable[[str, int], bool]


def normalize_alert_level(value: str) -> str:
    level = str(value or "").strip().casefold()
    if level not in ALERT_LEVELS:
        raise ValueError("invalid operator alert level")
    return level


def _clean_facts(
    definition: AlertDefinition,
    facts: Mapping[str, Any] | None,
) -> tuple[tuple[str, int | bool], ...]:
    clean: list[tuple[str, int | bool]] = []
    for key in sorted(definition.fact_labels):
        value = (facts or {}).get(key)
        if isinstance(value, bool):
            clean.append((key, value))
        elif isinstance(value, int) and not isinstance(value, bool):
            clean.append((key, max(0, min(value, 1_000_000))))
    return tuple(clean)


def build_operator_alert(
    code: str,
    facts: Mapping[str, Any] | None = None,
) -> OperatorAlert:
    normalized_code = str(code or "").strip().casefold()
    definition = ALERT_DEFINITIONS.get(normalized_code)
    if definition is None:
        raise ValueError("unknown operator alert code")
    clean_facts = _clean_facts(definition, facts)
    fact_fingerprint = ",".join(
        f"{key}={'b' if isinstance(value, bool) else 'i'}:{int(value)}"
        for key, value in clean_facts
    )
    fingerprint = hashlib.sha256(
        f"{OPERATOR_ALERT_POLICY_VERSION}:{normalized_code}:{fact_fingerprint}".encode("utf-8")
    ).hexdigest()[:20]
    return OperatorAlert(
        code=normalized_code,
        level=definition.level,
        title_uk=definition.title_uk,
        issue_numbers=definition.issue_numbers,
        facts=clean_facts,
        fingerprint=fingerprint,
    )


def render_operator_alert(alert: OperatorAlert) -> str:
    icon = {"info": "ℹ️", "warning": "⚠️", "error": "🛑", "critical": "🚨"}[alert.level]
    definition = ALERT_DEFINITIONS[alert.code]
    lines = [f"{icon} Aigan: {alert.title_uk}"]
    for key, value in alert.facts:
        rendered = "так" if value is True else "ні" if value is False else str(value)
        lines.append(f"{definition.fact_labels[key]}: {rendered}")
    if alert.issue_numbers:
        lines.append("GitHub: " + ", ".join(f"#{number}" for number in alert.issue_numbers))
    lines.append("У спільний чат технічний статус не надсилав.")
    return "\n".join(lines)[:900]


class OperatorAlertService:
    def __init__(
        self,
        settings: OperatorAlertSettings,
        *,
        event_callback: AlertEventCallback | None = None,
        recent_claim_checker: RecentClaimChecker | None = None,
    ) -> None:
        min_level = normalize_alert_level(settings.min_level)
        self.settings = OperatorAlertSettings(
            enabled=bool(settings.enabled),
            chat_id=int(settings.chat_id) if settings.chat_id is not None else None,
            min_level=min_level,
            cooldown_seconds=max(1, int(settings.cooldown_seconds)),
        )
        self._event_callback = event_callback
        self._recent_claim_checker = recent_claim_checker
        self._lock = asyncio.Lock()
        self._claimed_at: dict[str, float] = {}
        self._counts: Counter[str] = Counter()

    def _emit(self, event_type: str, alert: OperatorAlert) -> None:
        if self._event_callback is None:
            return
        try:
            self._event_callback(event_type, alert, dict(alert.facts))
        except Exception:
            return

    def _recently_claimed(self, alert: OperatorAlert, now: float) -> bool:
        claimed_at = self._claimed_at.get(alert.fingerprint)
        if claimed_at is not None and now - claimed_at < self.settings.cooldown_seconds:
            return True
        if self._recent_claim_checker is None:
            return False
        try:
            return bool(
                self._recent_claim_checker(
                    alert.fingerprint,
                    self.settings.cooldown_seconds,
                )
            )
        except Exception:
            return False

    async def notify(
        self,
        bot: Any,
        code: str,
        facts: Mapping[str, Any] | None = None,
    ) -> OperatorAlertResult:
        alert = build_operator_alert(code, facts)
        if not self.settings.enabled:
            self._counts["disabled"] += 1
            return OperatorAlertResult("disabled", alert.fingerprint)
        if self.settings.chat_id is None:
            self._counts["unconfigured"] += 1
            self._emit("operator_alert_unconfigured", alert)
            return OperatorAlertResult("unconfigured", alert.fingerprint)
        if self.settings.chat_id <= 0:
            self._counts["invalid_destination"] += 1
            self._emit("operator_alert_invalid_destination", alert)
            return OperatorAlertResult("invalid_destination", alert.fingerprint)
        if ALERT_LEVELS[alert.level] < ALERT_LEVELS[self.settings.min_level]:
            self._counts["below_threshold"] += 1
            return OperatorAlertResult("below_threshold", alert.fingerprint)

        async with self._lock:
            now = time.monotonic()
            if self._recently_claimed(alert, now):
                self._counts["deduplicated"] += 1
                return OperatorAlertResult("deduplicated", alert.fingerprint)
            self._claimed_at[alert.fingerprint] = now
            self._counts["claimed"] += 1
            self._emit("operator_alert_claimed", alert)

        try:
            await bot.send_message(
                chat_id=self.settings.chat_id,
                text=render_operator_alert(alert),
                disable_web_page_preview=True,
            )
        except Exception:
            self._counts["failed"] += 1
            self._emit("operator_alert_delivery_failed", alert)
            return OperatorAlertResult("failed", alert.fingerprint)

        self._counts["sent"] += 1
        self._emit("operator_alert_sent", alert)
        return OperatorAlertResult("sent", alert.fingerprint)

    def health_summary(self) -> dict[str, Any]:
        if not self.settings.enabled:
            status = "disabled"
        elif self.settings.chat_id is None:
            status = "unconfigured"
        elif self.settings.chat_id <= 0 or self._counts["invalid_destination"]:
            status = "degraded"
        elif self._counts["failed"]:
            status = "degraded"
        else:
            status = "ok"
        return {
            "status": status,
            "enabled": self.settings.enabled,
            "configured": self.settings.chat_id is not None and self.settings.chat_id > 0,
            "min_level": self.settings.min_level,
            "cooldown_seconds": self.settings.cooldown_seconds,
            "sent": self._counts["sent"],
            "failed": self._counts["failed"],
            "deduplicated": self._counts["deduplicated"],
        }
