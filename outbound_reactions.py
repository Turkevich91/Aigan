from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from telegram import ReactionTypeCustomEmoji, ReactionTypeEmoji
from telegram.constants import ChatType
from telegram.error import BadRequest

from memory import MemoryItem
from reaction_memory import ReactionMemoryStore, ReactionSpec


EventCallback = Callable[..., None]
ValueProvider = Callable[[], int | str | None]


@dataclass(frozen=True)
class OutboundReactionConfig:
    enabled: bool = False
    every_n_messages: int = 10
    cooldown_seconds: int = 1800
    min_score: float = 0.72
    allowed_emoji: tuple[str, ...] = ("🔥", "👀", "👍", "🤔", "😂")
    use_custom_emoji: bool = True
    is_big: bool = False
    bot_trigger: str = "!m"


class ReactionAdapter(Protocol):
    async def on_message_ingested(self, message: Any, memory_item: MemoryItem | None, phase: str) -> None:
        ...

    async def on_reaction_update(self, update: Any, context: Any) -> None:
        ...

    def health_summary(self) -> dict[str, Any]:
        ...


class NullReactionAdapter:
    async def on_message_ingested(self, message: Any, memory_item: MemoryItem | None, phase: str) -> None:
        return None

    async def on_reaction_update(self, update: Any, context: Any) -> None:
        return None

    def health_summary(self) -> dict[str, Any]:
        return {"enabled": False, "adapter": "null"}


class OutboundReactionAdapter:
    def __init__(
        self,
        *,
        config: OutboundReactionConfig,
        reaction_memory: ReactionMemoryStore | None = None,
        event_callback: EventCallback | None = None,
        bot_id_provider: ValueProvider | None = None,
        bot_username_provider: ValueProvider | None = None,
    ) -> None:
        self.config = config
        self.reaction_memory = reaction_memory
        self.event_callback = event_callback or (lambda **_kwargs: None)
        self.bot_id_provider = bot_id_provider or (lambda: None)
        self.bot_username_provider = bot_username_provider or (lambda: None)
        self._eligible_since_sent: dict[int, int] = {}
        self._last_sent_at: dict[int, float] = {}
        self._sent_count = 0
        self._skip_count = 0
        self._error_count = 0

    async def on_message_ingested(self, message: Any, memory_item: MemoryItem | None, phase: str) -> None:
        if not self.config.enabled or phase != "pre_embedding":
            return
        try:
            await self._maybe_react(message, memory_item)
        except Exception as exc:
            self._error_count += 1
            self._emit(
                level="warning",
                event_type="outbound_reaction_adapter_error",
                message_obj=message,
                message=f"{type(exc).__name__}: {exc}",
            )

    async def on_reaction_update(self, update: Any, context: Any) -> None:
        return None

    def health_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "adapter": "outbound",
            "every_n_messages": self.config.every_n_messages,
            "cooldown_seconds": self.config.cooldown_seconds,
            "min_score": self.config.min_score,
            "sent_count": self._sent_count,
            "skip_count": self._skip_count,
            "error_count": self._error_count,
            "tracked_chats": len(self._eligible_since_sent),
        }

    async def _maybe_react(self, message: Any, item: MemoryItem | None) -> None:
        if item is None or not self._is_eligible_message(message, item):
            self._skip_count += 1
            return

        chat_id = int(item.chat_id)
        self._eligible_since_sent[chat_id] = self._eligible_since_sent.get(chat_id, 0) + 1
        if self._eligible_since_sent[chat_id] < max(1, self.config.every_n_messages):
            self._emit(
                event_type="outbound_reaction_skipped_rate",
                message_obj=message,
                details={"eligible_since_sent": self._eligible_since_sent[chat_id]},
            )
            return

        now = time.time()
        last_sent_at = self._last_sent_at.get(chat_id, 0.0)
        if now - last_sent_at < max(0, self.config.cooldown_seconds):
            self._emit(
                event_type="outbound_reaction_skipped_cooldown",
                message_obj=message,
                details={"remaining_seconds": int(self.config.cooldown_seconds - (now - last_sent_at))},
            )
            return

        score = self.relevance_score(message, item)
        if score < self.config.min_score:
            self._emit(
                event_type="outbound_reaction_skipped_score",
                message_obj=message,
                details={"score": round(score, 3), "min_score": self.config.min_score},
            )
            return

        primary = self._select_reaction(chat_id, item)
        sent_spec = await self._send_reaction(message, primary, fallback=True)
        if sent_spec is None:
            return

        self._last_sent_at[chat_id] = now
        self._eligible_since_sent[chat_id] = 0
        self._sent_count += 1
        self._record_outbound_reaction(message, item, sent_spec)
        self._emit(
            event_type="outbound_reaction_sent",
            message_obj=message,
            details={"reaction_key": sent_spec.reaction_key, "score": round(score, 3)},
        )

    def _is_eligible_message(self, message: Any, item: MemoryItem) -> bool:
        if item.is_bot or item.message_id is None:
            return False
        chat_type = str(getattr(getattr(message, "chat", None), "type", "") or item.chat_type or "")
        if chat_type not in {ChatType.GROUP, ChatType.SUPERGROUP, "group", "supergroup"}:
            return False
        user = getattr(message, "from_user", None)
        if user is None or bool(getattr(user, "is_bot", False)):
            return False
        text = self._message_text(message)
        stripped = text.strip()
        if stripped.startswith("/"):
            return False
        trigger = (self.config.bot_trigger or "").strip()
        if trigger and stripped.startswith(trigger):
            return False
        username = str(self.bot_username_provider() or "").strip().lstrip("@")
        if username and re.search(rf"@{re.escape(username)}\b", text, flags=re.IGNORECASE):
            return False
        bot_id = self.bot_id_provider()
        reply = getattr(message, "reply_to_message", None)
        reply_user = getattr(reply, "from_user", None)
        if bot_id is not None and reply_user is not None and getattr(reply_user, "id", None) == bot_id:
            return False
        if item.attachment_type in {"sticker", "dice", "poll", "location", "contact"} and not (item.text or item.source_text):
            return False
        content = self._content_for_scoring(item).strip()
        if len(content) < 8 and not (item.source_text or item.vision_summary or item.source_url):
            return False
        return bool(content)

    def relevance_score(self, message: Any, item: MemoryItem) -> float:
        content = self._content_for_scoring(item)
        text = self._message_text(message)
        score = 0.0
        length = len(content)
        if length >= 50:
            score += 0.2
        if length >= 120:
            score += 0.16
        if length >= 260:
            score += 0.12
        if item.source_text or item.source_title or item.source_url:
            score += 0.18
        if item.vision_summary or item.content_kind == "image":
            score += 0.14
        if "?" in text or "?" in content:
            score += 0.08
        if re.search(r"\b\d{2,}\b|[$€₴%]", content):
            score += 0.08
        if re.search(r"[!?]{2,}|[“”\"']|:", content):
            score += 0.06
        if re.search(r"\b(новина|реліз|ціна|скандал|проблема|питання|анонс|release|price|issue)\b", content, re.I):
            score += 0.08
        if len((item.text or "").strip()) < 12 and not (item.source_text or item.vision_summary):
            score -= 0.12
        return max(0.0, min(1.0, score))

    async def _send_reaction(self, message: Any, spec: ReactionSpec, *, fallback: bool) -> ReactionSpec | None:
        bot = self._message_bot(message)
        if bot is None or not hasattr(bot, "set_message_reaction"):
            self._emit(event_type="outbound_reaction_unavailable", message_obj=message)
            return None

        reaction = self._telegram_reaction(spec)
        try:
            await bot.set_message_reaction(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reaction=[reaction],
                is_big=bool(self.config.is_big),
            )
            return spec
        except BadRequest as exc:
            self._emit(
                level="warning",
                event_type="outbound_reaction_rejected",
                message_obj=message,
                message=str(exc),
                details={"reaction_key": spec.reaction_key},
            )
            if fallback and spec.reaction_type == "custom_emoji":
                fallback_spec = self._standard_fallback_spec(message.chat_id, None)
                return await self._send_reaction(message, fallback_spec, fallback=False)
            return None

    def _telegram_reaction(self, spec: ReactionSpec) -> Any:
        if spec.reaction_type == "custom_emoji" and spec.custom_emoji_id:
            return ReactionTypeCustomEmoji(custom_emoji_id=spec.custom_emoji_id)
        return ReactionTypeEmoji(emoji=spec.base_emoji or self.config.allowed_emoji[0])

    def _select_reaction(self, chat_id: int, item: MemoryItem) -> ReactionSpec:
        if self.config.use_custom_emoji and self.reaction_memory is not None:
            for preference in self.reaction_memory.group_preferences(chat_id, limit=10):
                if preference.reaction_type == "custom_emoji" and preference.reaction_key.startswith("custom:"):
                    custom_id = preference.reaction_key.split(":", 1)[1]
                    if custom_id:
                        return ReactionSpec(
                            reaction_type="custom_emoji",
                            reaction_key=preference.reaction_key,
                            custom_emoji_id=custom_id,
                        )
        return self._standard_fallback_spec(chat_id, item)

    def _standard_fallback_spec(self, chat_id: int, item: MemoryItem | None) -> ReactionSpec:
        allowed = tuple(emoji for emoji in self.config.allowed_emoji if emoji)
        if not allowed:
            allowed = ("👍",)
        basis = f"{chat_id}:{getattr(item, 'id', '')}:{getattr(item, 'text', '')[:80]}"
        digest = hashlib.sha256(basis.encode("utf-8")).digest()
        emoji = allowed[digest[0] % len(allowed)]
        return ReactionSpec(reaction_type="emoji", reaction_key=f"emoji:{emoji}", base_emoji=emoji)

    def _record_outbound_reaction(self, message: Any, item: MemoryItem, spec: ReactionSpec) -> None:
        if self.reaction_memory is None:
            return
        bot_id_raw = self.bot_id_provider()
        bot_id = int(bot_id_raw) if isinstance(bot_id_raw, int) or str(bot_id_raw or "").isdigit() else None
        bot_username = str(self.bot_username_provider() or "").strip().lstrip("@")
        raw_json = json.dumps({"outbound": True, "phase": "pre_embedding"}, ensure_ascii=False)
        self.reaction_memory.record_message_reaction_update(
            update_id=None,
            chat_id=item.chat_id,
            target_message_id=int(item.message_id or message.message_id),
            target_memory_id=item.id,
            actor_key=f"bot:{bot_id or bot_username or 'self'}",
            actor_kind="bot",
            actor_user_id=bot_id,
            actor_username=bot_username,
            old_specs=[],
            new_specs=[spec],
            received_at=datetime.now(timezone.utc),
            raw_json=raw_json,
        )

    def _content_for_scoring(self, item: MemoryItem) -> str:
        values = [
            item.text,
            item.source_title,
            item.source_text,
            item.vision_summary,
            item.source_url,
        ]
        return " ".join(value for value in values if value)

    def _message_text(self, message: Any) -> str:
        return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")

    def _message_bot(self, message: Any) -> Any:
        get_bot = getattr(message, "get_bot", None)
        if callable(get_bot):
            try:
                return get_bot()
            except Exception:
                return None
        return getattr(message, "bot", None)

    def _emit(
        self,
        *,
        event_type: str,
        message_obj: Any,
        level: str = "info",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.event_callback(
                level=level,
                event_type=event_type,
                chat_id=int(getattr(message_obj, "chat_id", 0) or 0),
                user_id=getattr(getattr(message_obj, "from_user", None), "id", None),
                message=message,
                details=details or {},
            )
        except Exception:
            return
