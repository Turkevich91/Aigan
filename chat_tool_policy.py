"""Request-local availability and revocation of optional conversational tools."""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ChatTool:
    key: str
    label: str


CHAT_TOOLS = (
    ChatTool("web", "Вебпошук і читання сторінок"),
    ChatTool("youtube", "Транскрипти YouTube"),
    ChatTool("images", "Пошук і надсилання зображень"),
    ChatTool("randomizer", "Випадковий вибір"),
)
TOOL_KEYS = frozenset(tool.key for tool in CHAT_TOOLS)


@dataclass(frozen=True)
class ChatToolPolicy:
    disabled: frozenset[str]
    revision: int
    last_disabled: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class ChatToolRun:
    chat_id: int
    initial: ChatToolPolicy
    resolve: Callable[[int], ChatToolPolicy]

    def allows(self, key: str, *, before_call: bool) -> bool:
        if key in self.initial.disabled:
            return False
        if not before_call:
            return True
        try:
            current = self.resolve(self.chat_id)
        except Exception as exc:
            logging.getLogger(__name__).warning("Tool policy unavailable error=%s", type(exc).__name__)
            return False
        # A disable followed by a re-enable still revokes this run. Availability
        # is granted only to requests admitted after the later enable.
        return (key not in current.disabled
                and dict(current.last_disabled).get(key, 0) <= self.initial.revision)


_ACTIVE: contextvars.ContextVar[ChatToolRun | None] = contextvars.ContextVar(
    "active_chat_tool_run", default=None)


@contextmanager
def chat_tool_scope(chat_id: int, resolve: Callable[[int], ChatToolPolicy]):
    run = ChatToolRun(chat_id, resolve(chat_id), resolve)
    token = _ACTIVE.set(run)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def tool_allowed(key: str, *, before_call: bool = False) -> bool:
    if key not in TOOL_KEYS:
        raise ValueError("unknown_chat_tool")
    run = _ACTIVE.get()
    return run is None or run.allows(key, before_call=before_call)
