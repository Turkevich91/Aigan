from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import Any


TELEGRAM_ACTION_TYPING = "typing"
TELEGRAM_ACTION_UPLOAD_PHOTO = "upload_photo"
TELEGRAM_ACTION_UPLOAD_VIDEO = "upload_video"
TELEGRAM_ACTION_UPLOAD_DOCUMENT = "upload_document"
TELEGRAM_ACTION_UPLOAD_VOICE = "upload_voice"

_DRAFT_IDS = itertools.count(1)


@dataclass(frozen=True)
class ActivityPresenceSettings:
    enabled: bool = True
    refresh_seconds: float = 4.0
    drafts_enabled: bool = False
    draft_delay_seconds: float = 2.5


def activity_action_for_route(_route: str) -> str:
    # Telegram has no native "searching", "remembering", or "analyzing media" status.
    # Current long-running text/tool routes therefore share typing; senders switch
    # to upload_* only at the point they are actually about to upload media.
    return TELEGRAM_ACTION_TYPING


def draft_supported_for_chat(chat_type: str | None, settings: ActivityPresenceSettings) -> bool:
    return bool(settings.drafts_enabled and (chat_type or "").casefold() == "private")


class ActivityPresence:
    def __init__(
        self,
        *,
        bot: Any,
        chat_id: int,
        action: str = TELEGRAM_ACTION_TYPING,
        settings: ActivityPresenceSettings | None = None,
        message_thread_id: int | None = None,
        chat_type: str | None = None,
        draft_text: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.action = action or TELEGRAM_ACTION_TYPING
        self.settings = settings or ActivityPresenceSettings()
        self.message_thread_id = message_thread_id
        self.chat_type = chat_type
        self.draft_text = draft_text
        self.logger = logger or logging.getLogger(__name__)
        self._refresh_task: asyncio.Task[None] | None = None
        self._draft_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "ActivityPresence":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if not self.settings.enabled:
            return
        await self.send_once(self.action)
        refresh_seconds = max(0.0, float(self.settings.refresh_seconds or 0.0))
        if refresh_seconds > 0 and self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop(refresh_seconds))
        if draft_supported_for_chat(self.chat_type, self.settings) and self._draft_task is None:
            self._draft_task = asyncio.create_task(self._send_draft_after_delay())

    async def stop(self) -> None:
        tasks = [task for task in (self._refresh_task, self._draft_task) if task is not None]
        self._refresh_task = None
        self._draft_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                self.logger.debug("Telegram presence background task failed", exc_info=True)

    async def send_once(self, action: str | None = None) -> bool:
        if not self.settings.enabled:
            return False
        send_chat_action = getattr(self.bot, "send_chat_action", None)
        if not callable(send_chat_action):
            return False
        kwargs: dict[str, Any] = {"chat_id": self.chat_id, "action": action or self.action}
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        try:
            await send_chat_action(**kwargs)
            return True
        except Exception:
            self.logger.debug("Telegram send_chat_action failed", exc_info=True)
            return False

    async def _refresh_loop(self, refresh_seconds: float) -> None:
        while True:
            await asyncio.sleep(refresh_seconds)
            await self.send_once(self.action)

    async def _send_draft_after_delay(self) -> None:
        delay = max(0.0, float(self.settings.draft_delay_seconds or 0.0))
        if delay:
            await asyncio.sleep(delay)
        send_message_draft = getattr(self.bot, "send_message_draft", None)
        if not callable(send_message_draft):
            return
        kwargs: dict[str, Any] = {
            "chat_id": self.chat_id,
            "draft_id": next(_DRAFT_IDS),
            "text": self.draft_text,
        }
        if self.message_thread_id is not None:
            kwargs["message_thread_id"] = self.message_thread_id
        try:
            await send_message_draft(**kwargs)
        except Exception:
            self.logger.debug("Telegram send_message_draft failed", exc_info=True)
