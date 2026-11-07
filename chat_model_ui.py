"""Telegram controls for current-chat primary answer model settings.

Owner: chat model controls. This module owns transport validation and bounded
panel sessions; chat_model_settings owns the persistent policy and model catalog.
"""
from __future__ import annotations

import html
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from chat_model_settings import (
    MODEL_CATALOG,
    SPARK_MODEL,
    ChatModelSettingsStore,
    InvalidModelSelection,
    StaleSettingsRevision,
    get_model_capability,
    member_duration_limit,
    parse_model_selection,
)

from chat_tool_policy import CHAT_TOOLS, ChatToolPolicy

PANEL_TITLE = "Налаштування моделі · цей чат"
CALLBACK_PREFIX = "cm:"
CONTROL_NAMES = ("model", "settings", "select_model")
_CONTROL_RE = re.compile(r"^/(model|settings|select_model)(?:@([A-Za-z0-9_]+))?(?:\s+|$)", re.IGNORECASE)
DURATIONS = ((900, "15 хв"), (3600, "1 год"), (14400, "4 год"), (None, "Постійно"))


@dataclass(frozen=True)
class SettingsControl:
    name: str
    arguments: str
    caption: bool
    eligible: bool


def settings_control_for_message(message: Any, bot_username: str = "") -> SettingsControl | None:
    """Recognize transport controls before any chat-memory or media ingestion.

    Forwarded, edited, anonymous and other-bot controls remain recognizable so
    ingestion can exclude them, but are never eligible to change settings.
    """
    text = str(getattr(message, "text", "") or "")
    caption = not bool(text)
    if caption:
        text = str(getattr(message, "caption", "") or "")
    text = text.lstrip()
    match = _CONTROL_RE.match(text)
    if match is None:
        return None
    target = match.group(2)
    forwarded = any(
        getattr(message, field, None)
        for field in ("forward_origin", "forward_from", "forward_from_chat",
                      "forward_sender_name", "forward_date", "is_automatic_forward", "via_bot")
    )
    user = getattr(message, "from_user", None)
    eligible = bool(
        user is not None
        and not getattr(user, "is_bot", False)
        and not getattr(message, "sender_chat", None)
        and not getattr(message, "edit_date", None)
        and not forwarded
        and (not target or (bot_username and target.casefold() == bot_username.casefold()))
    )
    return SettingsControl(match.group(1).lower(), text[match.end():].strip(), caption, eligible)


def is_settings_control_message(message: Any, bot_username: str = "") -> bool:
    return settings_control_for_message(message, bot_username) is not None


def is_settings_panel_message(message: Any) -> bool:
    """Identify bot settings source text, including after a process restart."""
    user = getattr(message, "from_user", None)
    if user is None or not getattr(user, "is_bot", False):
        return False
    if str(getattr(message, "text", "") or "").startswith(PANEL_TITLE):
        return True
    markup = getattr(message, "reply_markup", None)
    return any(
        str(getattr(button, "callback_data", "") or "").startswith(CALLBACK_PREFIX)
        for row in (getattr(markup, "inline_keyboard", None) or ())
        for button in row
    )


@dataclass
class _Panel:
    nonce: str
    owner_id: int
    chat_id: int
    message_id: int
    settings_revision: int
    panel_revision: int
    expires_at: float
    model: str
    effort: str
    role: str
    member_ceiling: tuple[str, str] | None
    duration_seconds: int | None = 900
    stage: str = "model"
    tool_policy: ChatToolPolicy | None = None


class ChatModelUI:
    """Transport controller; all provider and application dependencies are injected."""

    def __init__(
        self,
        store: ChatModelSettingsStore | None,
        *,
        defaults: Callable[[], tuple[str, str]],
        is_admin: Callable[[int], bool],
        chat_allowed: Callable[[int, str, int], bool],
        is_owner: Callable[[int], bool] = lambda _: False,
        spark_available: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
        delete_commands: bool = True,
        panel_ttl_seconds: int = 900,
        max_panels: int = 128,
    ) -> None:
        self.store = store
        self.defaults = defaults
        self.is_admin = is_admin
        self.is_owner = is_owner
        self.chat_allowed = chat_allowed
        self.spark_available = spark_available
        self.clock = clock
        self.delete_commands = delete_commands
        self.panel_ttl_seconds = max(1, min(int(panel_ttl_seconds), 3600))
        self.max_panels = max(1, min(int(max_panels), 1024))
        self._panels: dict[str, _Panel] = {}

    def _profile(self, chat_id: int):
        model, effort = self.defaults()
        return self.store.resolve(chat_id, model, effort)

    def _role(self, actor_id: int) -> str:
        if self.is_owner(actor_id):
            return "owner"
        return "admin" if self.is_admin(actor_id) else "member"

    @staticmethod
    async def _is_current_member(bot: Any, chat_id: int, actor_id: int) -> bool:
        try:
            member = await bot.get_chat_member(chat_id, actor_id)
        except (TelegramError, OSError):
            return False
        return member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and member.is_member
        )

    def _actor(self, message: Any, user: Any) -> tuple[int, int, str] | None:
        if user is None or getattr(user, "is_bot", False) or getattr(message, "sender_chat", None):
            return None
        user_id = getattr(user, "id", None)
        chat_id = getattr(message, "chat_id", None)
        chat = getattr(message, "chat", None)
        if chat_id is None:
            chat_id = getattr(chat, "id", None)
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            return None
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            return None
        chat_type = str(getattr(chat, "type", "") or "")
        if chat_type not in {"private", "group", "supergroup"} or (chat_type == "private" and chat_id != user_id):
            return None
        return user_id, chat_id, chat_type

    def _prune(self) -> None:
        now = self.clock()
        self._panels = {key: panel for key, panel in self._panels.items() if panel.expires_at > now}
        while len(self._panels) >= self.max_panels:
            oldest = min(self._panels, key=lambda key: self._panels[key].expires_at)
            self._panels.pop(oldest, None)

    @staticmethod
    def _draft(profile: Any, ceiling: tuple[str, str] | None) -> tuple[str, str]:
        try:
            capability = get_model_capability(profile.model)
        except InvalidModelSelection:
            capability = MODEL_CATALOG[0]
        if capability is None:
            capability = MODEL_CATALOG[0]
        effort = profile.reasoning_effort
        if effort not in capability.reasoning_efforts:
            effort = "low" if "low" in capability.reasoning_efforts else capability.reasoning_efforts[0]
        if ceiling is not None and member_duration_limit(capability.model, effort, ceiling) == 0:
            return ceiling
        return capability.model, effort

    @staticmethod
    def _duration_label(seconds: int | None) -> str:
        if seconds is None:
            return "постійно"
        return f"{seconds // 60} хв" if seconds % 60 == 0 else f"{seconds} с"

    def _text(self, panel: _Panel, profile: Any, *, notice: str = "") -> str:
        if panel.stage == "tools":
            lines = [
                f"<b>{PANEL_TITLE}</b>", "<b>Інструменти · дозволи для цього чату</b>",
                "✅ Дозволено · 🚫 Заборонено. Натисни, щоб змінити дозвіл для всіх тем чату.",
                "Дозволи зберігаються після зміни моделі та завершення її таймера. "
                "Доступність також залежить від сервера й обраної моделі.",
            ]
            if notice:
                lines.extend(["", html.escape(notice)])
            return "\n".join(lines)
        lines = [
            f"<b>{PANEL_TITLE}</b>",
            "Діє для всього поточного чату, включно з усіма темами.",
            f"Зараз: <code>{html.escape(profile.model)}</code> · <code>{html.escape(profile.reasoning_effort)}</code>",
        ]
        if profile.overridden:
            if profile.expires_at is None:
                lines.append("Постійний вибір чату.")
            else:
                deadline = datetime.fromtimestamp(profile.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                lines.append(f"Чинне до {deadline}. Потім повернеться постійний профіль чату.")
        else:
            lines.append("Зараз діють типові налаштування.")
        updated = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"Стан на {updated}.")
        if panel.member_ceiling is not None:
            model, effort = panel.member_ceiling
            alias = get_model_capability(model).aliases[0]
            lines.append(f"Без таймера можна обрати {alias}/{effort} або нижче.")
            if member_duration_limit("astra", "low", panel.member_ceiling) == 900:
                lines.append("Вище цієї межі можна обрати профіль до Astra/low включно на строк до 15 хв.")
        lines.extend([
            "",
            f"Вибір: <code>{html.escape(panel.model)}</code> · <code>{html.escape(panel.effort)}</code> · {self._duration_label(panel.duration_seconds)}.",
            {"model": "1/3 · Обери модель.",
             "effort": "2/3 · Обери рівень міркування.",
             "duration": "3/3 · Обери термін.",
             "confirm": "Перевір вибір і натисни «Застосувати»."}[panel.stage],
        ])
        if SPARK_MODEL in {panel.model, profile.model}:
            lines.append("Spark / Qwen генерує текст локально без оплати за токени відповіді. "
                         "Допоміжні запити, зокрема для пам’яті, можуть тарифікуватися окремо.")
        if notice:
            lines.extend(["", html.escape(notice)])
        return "\n".join(lines)

    @staticmethod
    def _data(panel: _Panel, action: str, index: int | None = None) -> str:
        return f"cm:{panel.nonce}:{panel.panel_revision}:{action}:{index if index is not None else '-'}"

    @staticmethod
    def _duration_allowed(panel: _Panel, model: str, effort: str, seconds: int | None) -> bool:
        if panel.member_ceiling is None:
            return True
        limit = member_duration_limit(model, effort, panel.member_ceiling)
        return limit is None or (seconds is not None and seconds <= limit)

    def _markup(self, panel: _Panel) -> InlineKeyboardMarkup:
        def button(label: str, action: str, index: int | None = None):
            return InlineKeyboardButton(label, callback_data=self._data(panel, action, index))
        rows = []
        if panel.stage == "tools":
            policy = panel.tool_policy
            rows = [[button(("🚫 " if tool.key in policy.disabled else "✅ ") + tool.label, "v", index)]
                    for index, tool in enumerate(CHAT_TOOLS)]
            rows.extend([[button("Відновити дозволи інструментів", "z")],
                         [button("Назад", "b"), button("Оновити", "f"), button("Закрити", "x")]])
            return InlineKeyboardMarkup(rows)
        if panel.stage == "model":
            for index, capability in enumerate(MODEL_CATALOG):
                if not any(self._duration_allowed(panel, capability.model, effort, 900)
                           for effort in capability.reasoning_efforts):
                    continue
                label = capability.aliases[0] if capability.aliases else capability.model
                if capability.model == SPARK_MODEL:
                    label = "Spark / Qwen · локально, безплатно" if self.spark_available() else "Spark / Qwen · недоступний"
                rows.append([button(("✓ " if capability.model == panel.model else "") + label, "m", index)])
            if panel.member_ceiling is not None and member_duration_limit("astra", "low", panel.member_ceiling) == 900:
                rows.append([button("Astra/low · 15 хв", "q")])
        elif panel.stage == "effort":
            capability = get_model_capability(panel.model)
            rows = [[button(("✓ " if effort == panel.effort else "") + effort, "e", index)]
                    for index, effort in enumerate(capability.reasoning_efforts)
                    if self._duration_allowed(panel, panel.model, effort, 900)]
        elif panel.stage == "duration":
            rows = [[button(label, "d", index)] for index, (seconds, label) in enumerate(DURATIONS)
                    if self._duration_allowed(panel, panel.model, panel.effort, seconds)]
        else:
            rows = [[button("Застосувати", "a")]]
        if panel.stage == "model" and panel.role != "member":
            rows.append([button("Інструменти", "t")])
        if panel.stage != "model":
            rows.append([button("Назад", "b")])
        rows.extend([
            [button("Скинути до типових" if panel.role == "owner" else "Повернути базовий профіль", "r")],
            [button("Оновити", "f"), button("Закрити", "x")],
        ])
        return InlineKeyboardMarkup(rows)

    async def _delete_command(self, message: Any) -> None:
        if not self.delete_commands:
            return
        delete = getattr(message, "delete", None)
        if delete is None:
            return
        try:
            await delete()
        except (TelegramError, OSError):
            # Deletion needs extra Telegram rights in groups. It never rolls
            # back settings or turns a successful operation into a false failure.
            pass

    async def _notice(self, message: Any, text: str) -> None:
        await message.reply_text(
            f"<b>{PANEL_TITLE}</b>\n{html.escape(text)}",
            parse_mode="HTML", do_quote=False,
        )

    async def _open(self, message: Any, owner_id: int, chat_id: int, role: str) -> None:
        profile = self._profile(chat_id)
        ceiling = self.store.member_ceiling(chat_id) if role == "member" else None
        model, effort = self._draft(profile, ceiling)
        panel = _Panel(secrets.token_urlsafe(12), owner_id, chat_id, 0, profile.revision,
                       0, self.clock() + self.panel_ttl_seconds, model, effort, role, ceiling)
        sent = await message.reply_text(
            self._text(panel, profile), reply_markup=self._markup(panel), parse_mode="HTML", do_quote=False,
        )
        panel.message_id = int(sent.message_id)
        self._prune()
        self._panels[panel.nonce] = panel

    async def handle_command(self, update: Any, context: Any) -> bool:
        message = getattr(update, "effective_message", None)
        if message is None:
            return False
        bot_username = str(getattr(getattr(context, "bot", None), "username", "") or "")
        control = settings_control_for_message(message, bot_username)
        if control is None:
            return False
        if (not control.eligible or getattr(update, "edited_message", None) is not None
                or getattr(update, "edited_channel_post", None) is not None):
            return True
        actor = self._actor(message, getattr(message, "from_user", None))
        if actor is None:
            return True
        user_id, chat_id, chat_type = actor
        if not self.chat_allowed(chat_id, chat_type, user_id):
            return True
        role = self._role(user_id)
        if self.store is None:
            await self._notice(message, "Зміна моделі зараз недоступна. Типові налаштування залишаються чинними.")
            return True
        if role == "member" and chat_type != "private" and control.name == "select_model" and control.arguments:
            current_member = await self._is_current_member(context.bot, chat_id, user_id)
            if not self.chat_allowed(chat_id, chat_type, user_id) or self._role(user_id) != role:
                return True
            if not current_member:
                await self._notice(message, "Не вдалося підтвердити участь у цьому чаті.")
                return True
        try:
            if control.name in {"model", "settings"} and not control.arguments:
                await self._open(message, user_id, chat_id, role)
            elif control.name == "select_model" and control.arguments.lower() in {"reset", "default"}:
                profile = self._profile(chat_id)
                self.store.reset(chat_id, role=role, default_profile=self.defaults(), expected_revision=profile.revision)
                await self._notice(message, "Відновлено базовий профіль для всього поточного чату.")
            elif control.name == "select_model" and control.arguments:
                selection = parse_model_selection(control.arguments)
                if selection.model == SPARK_MODEL and not self.spark_available():
                    await self._notice(message, "Spark / Qwen не налаштований. Обери іншу модель у /model.")
                    return True
                previous = self._profile(chat_id)
                profile = self.store.apply(chat_id, selection.model, selection.reasoning_effort,
                                           selection.duration_seconds, role=role, expected_revision=previous.revision)
                term = "постійно"
                if profile.expires_at is not None:
                    deadline = datetime.fromtimestamp(profile.expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    term = f"до {deadline}"
                await self._notice(message, f"Застосовано {profile.model} · {profile.reasoning_effort} "
                                            f"для всього поточного чату ({term}).")
            else:
                await self._notice(message, "Відкрий /model або використай /select_model astra-low 15m. "
                                  "Без терміну профіль діє 15 хв. persistent зберігає його постійно. "
                                  "reset повертає базовий профіль чату.")
                return True
        except InvalidModelSelection:
            await self._notice(message, "Ця модель, рівень міркування або термін недоступні. Обери дозволений варіант у /model.")
            return True
        except StaleSettingsRevision:
            await self._notice(message, "Налаштування вже змінилися. Відкрий /model і перевір поточний стан.")
            return True
        except (OSError, sqlite3.Error):
            await self._notice(message, "Не вдалося зберегти налаштування. Перевір поточний стан через /model.")
            return True
        await self._delete_command(message)
        return True

    @staticmethod
    async def _answer(query: Any, text: str = "", *, alert: bool = False) -> None:
        try:
            await query.answer(text=text, show_alert=alert)
        except (TelegramError, OSError):
            pass

    async def handle_callback(self, update: Any, context: Any) -> None:
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        message = getattr(query, "message", None)
        actor = self._actor(message, getattr(query, "from_user", None))
        if actor is None:
            await self._answer(query, "Ця панель недоступна.", alert=True)
            return
        user_id, chat_id, chat_type = actor
        if not self.chat_allowed(chat_id, chat_type, user_id):
            await self._answer(query, "Ця панель недоступна.", alert=True)
            return
        if self.store is None:
            await self._answer(query, "Зміна моделі зараз недоступна.", alert=True)
            return
        data = getattr(query, "data", None)
        parts = data.split(":") if isinstance(data, str) and len(data) <= 64 else []
        if len(parts) != 5 or parts[0] != "cm" or not parts[2].isascii() or not parts[2].isdigit():
            await self._answer(query, "Невідома дія.", alert=True)
            return
        _, nonce, revision_text, action, option = parts
        role = self._role(user_id)
        panel = self._panels.get(nonce)
        if (
            panel is None or panel.owner_id != user_id or panel.chat_id != chat_id
            or panel.message_id != getattr(message, "message_id", None)
            or panel.role != role or panel.expires_at <= self.clock()
        ):
            await self._answer(query, "Панель застаріла або належить іншому учаснику. Відкрий /model.", alert=True)
            return
        if role == "member" and chat_type != "private":
            if not await self._is_current_member(context.bot, chat_id, user_id):
                await self._answer(query, "Не вдалося підтвердити участь у цьому чаті.", alert=True)
                return
        # The membership lookup yields control. Recheck the session and policy before writing.
        if (self._panels.get(nonce) is not panel or panel.panel_revision != int(revision_text)
                or panel.expires_at <= self.clock() or self._role(user_id) != role
                or not self.chat_allowed(chat_id, chat_type, user_id)):
            await self._answer(query, "Панель застаріла. Відкрий /model.", alert=True)
            return
        indexed = action in {"m", "e", "d", "v"}
        if ((indexed and (not option.isascii() or not option.isdigit() or len(option) > 3))
                or (not indexed and (option != "-" or action not in {"a", "r", "f", "b", "x", "t", "z", "q"}))):
            await self._answer(query, "Невідома дія.", alert=True)
            return
        if role == "member" and (panel.stage == "tools" or action in {"t", "v", "z"}):
            await self._answer(query, "Дозволи інструментів можуть змінювати тільки адміністратори.", alert=True)
            return
        if action == "x":
            # An in-flight deletion must exclude duplicate clicks and concurrent apply.
            panel.panel_revision = -1
            try:
                await message.delete()
            except (TelegramError, OSError):
                panel.panel_revision = int(revision_text)
                await self._answer(query, "Не вдалося закрити панель. Спробуй ще раз.", alert=True)
            else:
                self._panels.pop(nonce, None)
                await self._answer(query)
            return
        try:
            profile = self._profile(chat_id)
            if role == "member":
                panel.member_ceiling = self.store.member_ceiling(chat_id)
        except (OSError, sqlite3.Error, InvalidModelSelection):
            await self._answer(query, "Налаштування зараз недоступні. Відкрий /model пізніше.", alert=True)
            return
        if panel.stage == "tools" and action not in {"v", "z", "b", "f"}:
            await self._answer(query, "Недоступна дія.", alert=True)
            return
        if panel.stage != "tools" and action not in {"f", "t"} and profile.revision != panel.settings_revision:
            await self._answer(query, "Налаштування вже змінилися. Натисни «Оновити» або відкрий /model.", alert=True)
            return
        notice = ""
        try:
            if (action == "t" and panel.stage == "model") or (action == "f" and panel.stage == "tools"):
                panel.tool_policy = self.store.tools.resolve(chat_id)
                panel.stage = "tools"
            elif action == "v" and panel.stage == "tools" and int(option) < len(CHAT_TOOLS):
                tool = CHAT_TOOLS[int(option)]
                policy = panel.tool_policy
                panel.tool_policy = self.store.tools.set_enabled(
                    chat_id, tool.key, tool.key in policy.disabled, expected_revision=policy.revision,
                )
            elif action == "z" and panel.stage == "tools":
                panel.tool_policy = self.store.tools.reset(chat_id, expected_revision=panel.tool_policy.revision)
                notice = "Відновлено типові дозволи інструментів для цього чату."
            elif action == "b" and panel.stage == "tools":
                panel.stage = "model"
                panel.settings_revision = profile.revision
                panel.model, panel.effort = self._draft(profile, panel.member_ceiling)
            elif action == "q" and panel.stage == "model" and panel.member_ceiling is not None:
                if member_duration_limit("astra", "low", panel.member_ceiling) != 900:
                    raise InvalidModelSelection("invalid_shortcut")
                panel.model, panel.effort = get_model_capability("astra").model, "low"
                panel.duration_seconds = 900
                panel.stage = "confirm"
            elif action == "m" and panel.stage == "model" and int(option) < len(MODEL_CATALOG):
                capability = MODEL_CATALOG[int(option)]
                if capability.model == SPARK_MODEL and not self.spark_available():
                    await self._answer(query, "Spark / Qwen не налаштований.", alert=True)
                    return
                efforts = [e for e in capability.reasoning_efforts if self._duration_allowed(panel, capability.model, e, 900)]
                if not efforts:
                    raise InvalidModelSelection("invalid_model_option")
                panel.model = capability.model
                if panel.effort not in efforts:
                    panel.effort = "low" if "low" in efforts else efforts[0]
                if not self._duration_allowed(panel, panel.model, panel.effort, panel.duration_seconds):
                    panel.duration_seconds = 900
                panel.stage = "duration" if len(capability.reasoning_efforts) == 1 else "effort"
            elif action == "e" and panel.stage == "effort":
                efforts = get_model_capability(panel.model).reasoning_efforts
                if int(option) >= len(efforts) or not self._duration_allowed(panel, panel.model, efforts[int(option)], 900):
                    raise InvalidModelSelection("invalid_effort_option")
                panel.effort = efforts[int(option)]
                if not self._duration_allowed(panel, panel.model, panel.effort, panel.duration_seconds):
                    panel.duration_seconds = 900
                panel.stage = "duration"
            elif action == "d" and panel.stage == "duration" and int(option) < len(DURATIONS):
                seconds = DURATIONS[int(option)][0]
                if not self._duration_allowed(panel, panel.model, panel.effort, seconds):
                    raise InvalidModelSelection("invalid_duration_option")
                panel.duration_seconds = seconds
                panel.stage = "confirm"
            elif action == "a" and panel.stage == "confirm":
                if panel.model == SPARK_MODEL and not self.spark_available():
                    await self._answer(query, "Spark / Qwen не налаштований.", alert=True)
                    return
                profile = self.store.apply(chat_id, panel.model, panel.effort, panel.duration_seconds,
                                           role=role, expected_revision=panel.settings_revision)
                panel.settings_revision = profile.revision
                panel.stage = "model"
                notice = "Збережено. Нові відповіді використовуватимуть цей вибір."
            elif action == "r":
                self.store.reset(chat_id, role=role, default_profile=self.defaults(), expected_revision=panel.settings_revision)
                profile = self._profile(chat_id)
                panel.settings_revision = profile.revision
                panel.model, panel.effort = self._draft(profile, panel.member_ceiling)
                panel.duration_seconds = 900
                panel.stage = "model"
                notice = "Відновлено базовий профіль чату."
            elif action == "f":
                panel.settings_revision = profile.revision
                panel.model, panel.effort = self._draft(profile, panel.member_ceiling)
                panel.duration_seconds = 900
                panel.stage = "model"
            elif action == "b" and panel.stage != "model":
                previous_to_duration = "model" if panel.model == SPARK_MODEL else "effort"
                panel.stage = {"effort": "model", "duration": previous_to_duration, "confirm": "duration"}[panel.stage]
            else:
                raise InvalidModelSelection("invalid_panel_action")
        except InvalidModelSelection:
            await self._answer(query, "Недоступний варіант. Відкрий /model.", alert=True)
            return
        except StaleSettingsRevision:
            await self._answer(query, "Налаштування вже змінилися. Натисни «Оновити».", alert=True)
            return
        except (OSError, sqlite3.Error):
            await self._answer(query, "Не вдалося зберегти налаштування. Відкрий /model.", alert=True)
            return
        # No await occurs between validation, mutation and revision increment.
        # Concurrent/repeated clicks therefore cannot reuse the same panel step.
        panel.panel_revision += 1
        await self._answer(query)
        try:
            await query.edit_message_text(self._text(panel, profile, notice=notice),
                                          reply_markup=self._markup(panel), parse_mode="HTML")
        except (TelegramError, OSError):
            # A transport failure after persistence must not leave a usable old
            # confirmation button. A fresh /model reveals the committed state.
            self._panels.pop(nonce, None)
