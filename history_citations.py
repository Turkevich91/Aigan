"""Host-owned references to retained evidence actually exposed in one run."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import threading


ACTIVE_HISTORY_CITATIONS: ContextVar[HistoryCitationSession | None] = ContextVar(
    "active_history_citations", default=None,
)
_REF = re.compile(r"\[\[history:([a-f0-9]{12}):([1-9][0-9]{0,18})\]\]")
_ANY_REF = re.compile(r"\[\[history:[^\]\r\n]{0,256}\]\]")
# Private-message URLs in generated prose must not bypass reference validation.
_PRIVATE_MESSAGE_URL = re.compile(
    r"(?:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/c/[^\s<>\]\)]+"
    r"|tg://privatepost\?[^\s<>\]\)]+)", re.I,
)
_PUBLIC_MESSAGE_URL = re.compile(
    r"(?:(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/[a-z0-9_]+/[0-9]+[^\s<>\]\)]*"
    r"|(?:https?://)?[a-z0-9_]+\.t\.me/[0-9]+[^\s<>\]\)]*"
    r"|tg://resolve\?(?=[^\s<>\]\)]*\bpost=)[^\s<>\]\)]+)", re.I,
)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _fingerprint(item) -> str:
    # Hash complete evidence and attribution, including text beyond the preview.
    # Never retain operational paths, tokens or raw_note in this registry.
    names = ("id", "chat_id", "message_id", "created_at", "user_id", "sender_label", "username",
             "is_bot", "text", "source_text", "content_kind", "attachment_type", "vision_summary",
             "source_title", "source_url", "forward_origin", "reply_to_message_id")
    values = {name: getattr(item, name, None) for name in names}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class _ContextEvidence:
    row_id: int
    fingerprint: str
    rendered_line: str
    citation_ref: str


class HistoryCitationSession:
    """A separate registry for preloaded context; history tools keep their own budget."""

    def __init__(self, store, *, chat_id: int, chat_type: str, cutoff_memory_id: int,
                 cutoff_created_at: str, history=None):
        self._store = store
        self._chat_id = chat_id
        self._chat_type = chat_type
        self._cutoff_id = cutoff_memory_id
        self._cutoff_at = _instant(cutoff_created_at)
        self._history = history
        self._nonce = secrets.token_hex(6)
        self._candidates: dict[int, _ContextEvidence] = {}
        self._exposed: dict[int, _ContextEvidence] = {}
        self._lock = threading.RLock()

    def _eligible(self, item) -> bool:
        try:
            return (item is not None and item.chat_id == self._chat_id
                    and 0 < item.id < self._cutoff_id and _instant(item.created_at) <= self._cutoff_at)
        except (AttributeError, TypeError, ValueError):
            return False

    def decorate_context(self, item, line: str) -> str:
        """Prepare a candidate reference; this does not yet expose or authorize it."""
        if not self._eligible(item):
            return line
        with self._lock:
            if len(self._candidates) >= 128 and item.id not in self._candidates:
                return line
            fingerprint = _fingerprint(item)
            version = hashlib.sha256((self._nonce + fingerprint).encode()).hexdigest()[:12]
            ref = f"[[history:{version}:{item.id}]]"
            rendered = line + " " + ref
            self._candidates[item.id] = _ContextEvidence(item.id, fingerprint, rendered, ref)
            return rendered

    def expose_contexts(self, blocks) -> None:
        """Commit only exact formatter output present in the supplied memory blocks."""
        blocks = tuple(block for block in blocks if isinstance(block, str))
        with self._lock:
            self._exposed = {key: evidence for key, evidence in self._candidates.items()
                             if any(evidence.rendered_line in block for block in blocks)}
            self._candidates.clear()

    @property
    def exposed_ids(self) -> frozenset[int]:
        with self._lock:
            own = set(self._exposed)
        if self._history is not None:
            own.update(self._history.exposed_ids)
        return frozenset(own)

    def _resolve(self, ref: str) -> dict | None:
        match = _REF.fullmatch(ref)
        if match is None:
            return None
        row_id = int(match.group(2))
        with self._lock:
            evidence = self._exposed.get(row_id)
        if evidence is not None and evidence.citation_ref == ref:
            item = self._store.item_by_id(row_id)
            if not self._eligible(item) or _fingerprint(item) != evidence.fingerprint:
                return None
            return {"id": item.id, "message_id": item.message_id, "created_at": item.created_at,
                    "source_text": item.source_text, "forward_origin": item.forward_origin}
        if self._history is not None:
            evidence = self._history.resolve_citation_ref(ref)
            return evidence.to_dict() if evidence is not None else None
        return None

    def _source_line(self, number: int, row: dict) -> str:
        date = _instant(row["created_at"]).strftime("%d.%m.%Y")
        source = " · пересланий/цитований матеріал у чаті" if row.get("source_text") or row.get("forward_origin") or row.get("is_forwarded") else ""
        message_id = row.get("message_id")
        chat = str(self._chat_id)
        if (self._chat_type in {"supergroup", "channel"} and chat.startswith("-100")
                and chat[4:].isdigit() and int(chat[4:]) > 0 and isinstance(message_id, int)
                and not isinstance(message_id, bool) and 0 < message_id < 2**31):
            return f"[{number}] {date}{source}: https://t.me/c/{int(chat[4:])}/{message_id}"
        return f"[{number}] {date}{source} — оригінал зі збереженої історії; пряме посилання недоступне."

    def _coverage_line(self) -> str:
        count = len(self.exposed_ids)
        line = f"Фрагментів історії в контексті: {count}. Це вибірка зі збережених повідомлень."
        coverage = self._history.last_coverage if self._history is not None else None
        if not coverage:
            return line
        start, end = coverage.get("returned_start"), coverage.get("returned_end")
        if start and end:
            line += (f" Остання вибірка: {coverage.get('returned_count', 0)} фрагментів, "
                     f"{_instant(start).strftime('%d.%m.%Y %H:%M')}–{_instant(end).strftime('%d.%m.%Y %H:%M')} UTC.")
        after, before = coverage.get("scope_after"), coverage.get("scope_before")
        if after or before:
            lower = _instant(after).strftime("%d.%m.%Y %H:%M UTC") if after else "початок збереженої історії"
            upper = _instant(before).strftime("%d.%m.%Y %H:%M UTC") if before else "межа поточного запиту"
            exclusive = " (верхня межа не включена)" if before else ""
            line += f" Останній фільтр дат: від {lower} до {upper}{exclusive}."
        if coverage.get("has_more_results"):
            line += " Є ще результати за останнім запитом."
        if coverage.get("text_truncated") or coverage.get("omitted_due_to_response_budget"):
            line += " Частину матеріалу скорочено через ліміт обсягу."
        return line

    def render(self, output: str, *, max_chars: int = 12000, fits=None) -> str:
        """Resolve references after generation; no provider requests or Telegram I/O."""
        refs: dict[int, int] = {}
        sources: list[str] = []
        invalid = False

        def replace(match: re.Match) -> str:
            nonlocal invalid
            try:
                row = self._resolve(match.group(0))
            except Exception:
                row = None
            if row is None:
                invalid = True
                return "[джерело змінилося або недоступне]"
            row_id = row["id"]
            if row_id in refs:
                return f"[{refs[row_id]}]"
            if len(sources) >= 3:
                return ""
            number = len(sources) + 1
            try:
                line = self._source_line(number, row)
            except (ValueError, TypeError, KeyError):
                invalid = True
                return "[джерело недоступне]"
            refs[row_id] = number
            sources.append(line)
            return f"[{number}]"

        # Full URLs are emitted only after all model-generated references were checked.
        text = _PRIVATE_MESSAGE_URL.sub("[неперевірене посилання на повідомлення]", output)
        if _ANY_REF.search(output) or (self._history is not None and self._history.calls_used):
            text = _PUBLIC_MESSAGE_URL.sub("[неперевірене посилання на повідомлення]", text)
        text = _ANY_REF.sub(replace, text).strip()
        footer = ""
        if sources:
            footer += "\n\nДжерела:\n" + "\n".join(sources)
        if sources or invalid or (self._history is not None and self._history.calls_used):
            footer += "\n\n" + self._coverage_line()
        if not footer:
            return text
        limit = max(1, int(max_chars))
        if len(footer) + 32 > limit:
            return "Не вдалося вмістити відповідь із перевіреними джерелами."[:limit]
        def compose(length):
            marker = " […] скорочено" if length < len(text) else ""
            return text[:length].rstrip() + marker + footer

        def accepted(candidate):
            return len(candidate) <= limit and (fits is None or fits(candidate, footer))

        if accepted(text + footer):
            return text + footer
        # The transport splits paragraphs, so a character product alone cannot
        # guarantee that a footer survives the maximum number of chunks.
        best = compose(0)
        if not accepted(best):
            return "Не вдалося вмістити відповідь із перевіреними джерелами."[:limit]
        low, high = 0, min(len(text), limit - len(footer))
        while low <= high:
            middle = (low + high) // 2
            candidate = compose(middle)
            if accepted(candidate):
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best
