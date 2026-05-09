from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class MemoryItem:
    id: int
    chat_id: int
    message_id: int | None
    chat_type: str
    created_at: str
    sender_label: str
    user_id: int | None
    username: str
    is_bot: bool
    text: str
    content_kind: str
    attachment_type: str
    telegram_file_id: str
    telegram_unique_id: str
    local_media_path: str
    mime_type: str
    vision_summary: str
    source_url: str
    source_title: str
    reply_to_message_id: int | None
    forward_origin: str
    raw_note: str


class MemoryStore:
    def __init__(self, db_path: Path | str, retention_days: int = 30) -> None:
        self.db_path = Path(db_path)
        self.retention_days = max(1, int(retention_days))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.db_path.parent / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                chat_type TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                sender_label TEXT NOT NULL DEFAULT '',
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                is_bot INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL DEFAULT '',
                content_kind TEXT NOT NULL DEFAULT 'text',
                attachment_type TEXT NOT NULL DEFAULT '',
                telegram_file_id TEXT NOT NULL DEFAULT '',
                telegram_unique_id TEXT NOT NULL DEFAULT '',
                local_media_path TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                vision_summary TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                reply_to_message_id INTEGER,
                forward_origin TEXT NOT NULL DEFAULT '',
                raw_note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat_order
                ON messages(chat_id, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_user_order
                ON messages(chat_id, user_id, created_at, id);
            CREATE INDEX IF NOT EXISTS idx_messages_chat_username_order
                ON messages(chat_id, username COLLATE NOCASE, created_at, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_message
                ON messages(chat_id, message_id)
                WHERE message_id IS NOT NULL;
            """
        )
        self._conn.commit()

    def save_message(
        self,
        *,
        chat_id: int,
        message_id: int | None,
        chat_type: str = "",
        created_at: datetime | str | None = None,
        sender_label: str = "",
        user_id: int | None = None,
        username: str = "",
        is_bot: bool = False,
        text: str = "",
        content_kind: str = "text",
        attachment_type: str = "",
        telegram_file_id: str = "",
        telegram_unique_id: str = "",
        local_media_path: str = "",
        mime_type: str = "",
        vision_summary: str = "",
        source_url: str = "",
        source_title: str = "",
        reply_to_message_id: int | None = None,
        forward_origin: str = "",
        raw_note: str = "",
    ) -> int:
        created = self._format_datetime(created_at)
        values = {
            "chat_id": int(chat_id),
            "message_id": message_id,
            "chat_type": chat_type or "",
            "created_at": created,
            "sender_label": sender_label or "",
            "user_id": user_id,
            "username": username or "",
            "is_bot": 1 if is_bot else 0,
            "text": text or "",
            "content_kind": content_kind or "text",
            "attachment_type": attachment_type or "",
            "telegram_file_id": telegram_file_id or "",
            "telegram_unique_id": telegram_unique_id or "",
            "local_media_path": local_media_path or "",
            "mime_type": mime_type or "",
            "vision_summary": vision_summary or "",
            "source_url": source_url or "",
            "source_title": source_title or "",
            "reply_to_message_id": reply_to_message_id,
            "forward_origin": forward_origin or "",
            "raw_note": raw_note or "",
        }

        with self._lock:
            existing_id = None
            if message_id is not None:
                row = self._conn.execute(
                    "SELECT id FROM messages WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                ).fetchone()
                existing_id = int(row["id"]) if row else None

            if existing_id is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO messages (
                        chat_id, message_id, chat_type, created_at, sender_label,
                        user_id, username, is_bot, text, content_kind, attachment_type,
                        telegram_file_id, telegram_unique_id, local_media_path,
                        mime_type, vision_summary, source_url, source_title,
                        reply_to_message_id, forward_origin, raw_note
                    ) VALUES (
                        :chat_id, :message_id, :chat_type, :created_at, :sender_label,
                        :user_id, :username, :is_bot, :text, :content_kind, :attachment_type,
                        :telegram_file_id, :telegram_unique_id, :local_media_path,
                        :mime_type, :vision_summary, :source_url, :source_title,
                        :reply_to_message_id, :forward_origin, :raw_note
                    )
                    """,
                    values,
                )
                self._conn.commit()
                return int(cursor.lastrowid)

            values["id"] = existing_id
            self._conn.execute(
                """
                UPDATE messages SET
                    chat_type = :chat_type,
                    created_at = :created_at,
                    sender_label = :sender_label,
                    user_id = :user_id,
                    username = :username,
                    is_bot = :is_bot,
                    text = :text,
                    content_kind = :content_kind,
                    attachment_type = :attachment_type,
                    telegram_file_id = COALESCE(NULLIF(:telegram_file_id, ''), telegram_file_id),
                    telegram_unique_id = COALESCE(NULLIF(:telegram_unique_id, ''), telegram_unique_id),
                    local_media_path = COALESCE(NULLIF(:local_media_path, ''), local_media_path),
                    mime_type = COALESCE(NULLIF(:mime_type, ''), mime_type),
                    vision_summary = COALESCE(NULLIF(:vision_summary, ''), vision_summary),
                    source_url = COALESCE(NULLIF(:source_url, ''), source_url),
                    source_title = COALESCE(NULLIF(:source_title, ''), source_title),
                    reply_to_message_id = :reply_to_message_id,
                    forward_origin = COALESCE(NULLIF(:forward_origin, ''), forward_origin),
                    raw_note = COALESCE(NULLIF(:raw_note, ''), raw_note)
                WHERE id = :id
                """,
                values,
            )
            self._conn.commit()
            return existing_id

    def update_media(
        self,
        item_id: int,
        *,
        attachment_type: str,
        telegram_file_id: str,
        telegram_unique_id: str = "",
        local_media_path: str,
        mime_type: str,
        raw_note: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE messages SET
                    content_kind = 'image',
                    attachment_type = ?,
                    telegram_file_id = ?,
                    telegram_unique_id = ?,
                    local_media_path = ?,
                    mime_type = ?,
                    raw_note = COALESCE(NULLIF(?, ''), raw_note)
                WHERE id = ?
                """,
                (
                    attachment_type,
                    telegram_file_id,
                    telegram_unique_id,
                    local_media_path,
                    mime_type,
                    raw_note,
                    item_id,
                ),
            )
            self._conn.commit()

    def update_vision_summary(self, item_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET vision_summary = ? WHERE id = ?",
                (summary or "", item_id),
            )
            self._conn.commit()

    def latest(self, chat_id: int, limit: int = 10) -> list[MemoryItem]:
        limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE chat_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
                """,
                (chat_id, limit),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def user_messages(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
        limit: int = 100,
    ) -> list[MemoryItem]:
        return self._user_text_messages(chat_id, user_id=user_id, username=username, limit=max(1, int(limit)))

    def user_stats(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
    ) -> list[MemoryItem]:
        return self._user_text_messages(chat_id, user_id=user_id, username=username, limit=None)

    def _user_text_messages(
        self,
        chat_id: int,
        *,
        user_id: int | None,
        username: str = "",
        limit: int | None,
    ) -> list[MemoryItem]:
        username = username.strip().lstrip("@")
        if user_id is None and not username:
            return []

        conditions = [
            "chat_id = ?",
            "is_bot = 0",
            "text != ''",
            "text NOT LIKE '[message has %'",
        ]
        params: list[object] = [chat_id]
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        else:
            conditions.append("username != ''")
            conditions.append("lower(username) = lower(?)")
            params.append(username)

        where = " AND ".join(conditions)
        if limit is None:
            sql = f"""
                SELECT * FROM messages
                WHERE {where}
                ORDER BY created_at ASC, id ASC
                """
        else:
            sql = f"""
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE {where}
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
                """
            params.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_item(row) for row in rows]

    def unsummarized_recent_images(self, chat_id: int, limit: int = 3) -> list[MemoryItem]:
        limit = max(0, int(limit))
        if limit == 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ?
                  AND content_kind = 'image'
                  AND local_media_path != ''
                  AND vision_summary = ''
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def cleanup(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_text = self._format_datetime(cutoff)
        with self._lock:
            rows = self._conn.execute(
                "SELECT local_media_path FROM messages WHERE created_at < ? AND local_media_path != ''",
                (cutoff_text,),
            ).fetchall()
            cursor = self._conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff_text,))
            self._conn.commit()

        for row in rows:
            path = Path(row["local_media_path"])
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                pass
        return int(cursor.rowcount)

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _format_datetime(value: datetime | str | None) -> str:
        if isinstance(value, str):
            return value
        if value is None:
            value = datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            message_id=row["message_id"],
            chat_type=row["chat_type"],
            created_at=row["created_at"],
            sender_label=row["sender_label"],
            user_id=row["user_id"],
            username=row["username"],
            is_bot=bool(row["is_bot"]),
            text=row["text"],
            content_kind=row["content_kind"],
            attachment_type=row["attachment_type"],
            telegram_file_id=row["telegram_file_id"],
            telegram_unique_id=row["telegram_unique_id"],
            local_media_path=row["local_media_path"],
            mime_type=row["mime_type"],
            vision_summary=row["vision_summary"],
            source_url=row["source_url"],
            source_title=row["source_title"],
            reply_to_message_id=row["reply_to_message_id"],
            forward_origin=row["forward_origin"],
            raw_note=row["raw_note"],
        )
