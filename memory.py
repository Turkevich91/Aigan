from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
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
    source_text: str
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


@dataclass(frozen=True)
class EmbeddingCandidate:
    item: MemoryItem
    search_text: str
    content_hash: str


@dataclass(frozen=True)
class SemanticMemoryResult:
    item: MemoryItem
    search_text: str
    score: float
    source: str


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
                source_text TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS message_embeddings (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                chat_id INTEGER NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                embedding_blob BLOB NOT NULL,
                embedded_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_message_embeddings_chat
                ON message_embeddings(chat_id, model, dimensions);
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts
                USING fts5(search_text, tokenize = 'unicode61');
            """
        )
        self._ensure_column("messages", "source_text", "TEXT NOT NULL DEFAULT ''")
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        existing = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
        source_text: str = "",
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
            "source_text": source_text or "",
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
                        user_id, username, is_bot, text, source_text, content_kind, attachment_type,
                        telegram_file_id, telegram_unique_id, local_media_path,
                        mime_type, vision_summary, source_url, source_title,
                        reply_to_message_id, forward_origin, raw_note
                    ) VALUES (
                        :chat_id, :message_id, :chat_type, :created_at, :sender_label,
                        :user_id, :username, :is_bot, :text, :source_text, :content_kind, :attachment_type,
                        :telegram_file_id, :telegram_unique_id, :local_media_path,
                        :mime_type, :vision_summary, :source_url, :source_title,
                        :reply_to_message_id, :forward_origin, :raw_note
                    )
                    """,
                    values,
                )
                self._conn.commit()
                item_id = int(cursor.lastrowid)
                self._sync_search_index_from_values(item_id, values)
                return item_id

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
                    source_text = :source_text,
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
            self._sync_search_index_from_values(existing_id, values)
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
            self._sync_search_index_for_id(item_id)

    def update_vision_summary(self, item_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET vision_summary = ? WHERE id = ?",
                (summary or "", item_id),
            )
            self._conn.commit()
            self._sync_search_index_for_id(item_id)

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

    def item_by_id(self, item_id: int | None) -> MemoryItem | None:
        if item_id is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ? LIMIT 1",
                (item_id,),
            ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def message_by_message_id(self, chat_id: int, message_id: int | None) -> MemoryItem | None:
        if message_id is None:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ? AND message_id = ?
                LIMIT 1
                """,
                (chat_id, message_id),
            ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def reply_chain(self, chat_id: int, message_id: int | None, depth: int = 6) -> list[MemoryItem]:
        depth = max(0, int(depth))
        if message_id is None or depth == 0:
            return []

        chain: list[MemoryItem] = []
        seen: set[int] = set()
        current_id: int | None = message_id
        for _ in range(depth):
            item = self.message_by_message_id(chat_id, current_id)
            if item is None or item.id in seen:
                break
            chain.append(item)
            seen.add(item.id)
            current_id = item.reply_to_message_id
            if current_id is None:
                break

        return list(reversed(chain))

    def user_messages(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
        label_aliases: tuple[str, ...] | list[str] = (),
        limit: int = 100,
    ) -> list[MemoryItem]:
        return self._user_text_messages(
            chat_id,
            user_id=user_id,
            username=username,
            label_aliases=label_aliases,
            limit=max(1, int(limit)),
        )

    def user_stats(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
        label_aliases: tuple[str, ...] | list[str] = (),
    ) -> list[MemoryItem]:
        return self._user_text_messages(chat_id, user_id=user_id, username=username, label_aliases=label_aliases, limit=None)

    def user_source_count(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
        label_aliases: tuple[str, ...] | list[str] = (),
    ) -> int:
        username = username.strip().lstrip("@")
        aliases = tuple(dict.fromkeys(alias.strip() for alias in label_aliases if alias and alias.strip()))
        if user_id is None and not username and not aliases:
            return 0

        conditions = ["chat_id = ?", "is_bot = 0", "source_text != ''"]
        params: list[object] = [chat_id]
        identity_conditions: list[str] = []
        if user_id is not None:
            identity_conditions.append("user_id = ?")
            params.append(user_id)
        if username:
            identity_conditions.append("(username != '' AND lower(username) = lower(?))")
            params.append(username)
        if aliases:
            placeholders = ",".join("?" for _alias in aliases)
            identity_conditions.append(f"sender_label IN ({placeholders})")
            params.extend(aliases)
        conditions.append("(" + " OR ".join(identity_conditions) + ")")
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS count FROM messages WHERE {' AND '.join(conditions)}",
                tuple(params),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def user_id_for_username(self, chat_id: int, username: str) -> int | None:
        username = username.strip().lstrip("@")
        if not username:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT user_id, COUNT(*) AS count
                FROM messages
                WHERE chat_id = ?
                  AND is_bot = 0
                  AND user_id IS NOT NULL
                  AND (
                    lower(username) = lower(?)
                    OR lower(sender_label) LIKE lower(?)
                  )
                GROUP BY user_id
                ORDER BY count DESC
                LIMIT 1
                """,
                (chat_id, username, f"%@{username}%"),
            ).fetchone()
        return int(row["user_id"]) if row is not None else None

    @staticmethod
    def base_sender_label(label: str) -> str:
        value = (label or "").strip()
        value = re.sub(r"\s*\(@[^)]*\)\s*$", "", value)
        value = re.sub(r"\s*\((?:current request|translation request|image request)\)\s*$", "", value)
        return value.strip()

    def identity_label_aliases(self, chat_id: int, *, user_id: int | None = None, username: str = "") -> tuple[str, ...]:
        username = username.strip().lstrip("@")
        if user_id is None and not username:
            return ()
        conditions = ["chat_id = ?", "is_bot = 0", "sender_label != ''", "sender_label != 'Telegram export'"]
        params: list[object] = [chat_id]
        identity_conditions: list[str] = []
        if user_id is not None:
            identity_conditions.append("user_id = ?")
            params.append(user_id)
        if username:
            identity_conditions.append("lower(username) = lower(?)")
            params.append(username)
            identity_conditions.append("lower(sender_label) LIKE lower(?)")
            params.append(f"%@{username}%")
        if not identity_conditions:
            return ()
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT DISTINCT sender_label
                FROM messages
                WHERE {" AND ".join(conditions)}
                  AND ({" OR ".join(identity_conditions)})
                """,
                tuple(params),
            ).fetchall()

        aliases: set[str] = set()
        for row in rows:
            label = str(row["sender_label"] or "").strip()
            for alias in (label, self.base_sender_label(label)):
                if alias and alias != "Telegram export":
                    aliases.add(alias)
        return tuple(sorted(aliases, key=str.casefold))

    def _user_text_messages(
        self,
        chat_id: int,
        *,
        user_id: int | None,
        username: str = "",
        label_aliases: tuple[str, ...] | list[str] = (),
        limit: int | None = None,
    ) -> list[MemoryItem]:
        username = username.strip().lstrip("@")
        aliases = tuple(dict.fromkeys(alias.strip() for alias in label_aliases if alias and alias.strip()))
        if user_id is None and not username and not aliases:
            return []

        conditions = [
            "chat_id = ?",
            "is_bot = 0",
            "text != ''",
            "text NOT LIKE '[message has %'",
        ]
        params: list[object] = [chat_id]
        identity_conditions: list[str] = []
        if user_id is not None:
            identity_conditions.append("user_id = ?")
            params.append(user_id)
        if username:
            identity_conditions.append("(username != '' AND lower(username) = lower(?))")
            params.append(username)
        if aliases:
            placeholders = ",".join("?" for _alias in aliases)
            identity_conditions.append(f"sender_label IN ({placeholders})")
            params.extend(aliases)
        conditions.append("(" + " OR ".join(identity_conditions) + ")")

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

    def embeddings_for_items(self, item_ids: list[int], *, model: str, dimensions: int) -> dict[int, list[float]]:
        ids = [int(item_id) for item_id in item_ids if item_id is not None]
        if not ids:
            return {}
        placeholders = ",".join("?" for _item_id in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT message_id, embedding_blob
                FROM message_embeddings
                WHERE model = ?
                  AND dimensions = ?
                  AND message_id IN ({placeholders})
                """,
                (model, int(dimensions), *ids),
            ).fetchall()
        return {int(row["message_id"]): self._embedding_from_blob(row["embedding_blob"]) for row in rows}

    def embedding_candidates_by_ids(
        self,
        item_ids: list[int],
        *,
        model: str,
        dimensions: int,
        limit: int,
    ) -> list[EmbeddingCandidate]:
        unique_ids = list(dict.fromkeys(int(item_id) for item_id in item_ids if item_id))
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        params: list[object] = [model, int(dimensions), *unique_ids]
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT m.*, e.content_hash AS embedded_hash
                FROM messages m
                LEFT JOIN message_embeddings e
                  ON e.message_id = m.id AND e.model = ? AND e.dimensions = ?
                WHERE m.id IN ({placeholders})
                ORDER BY m.created_at ASC, m.id ASC
                """,
                params,
            ).fetchall()
        return self._embedding_candidates_from_rows(rows, limit)

    def pending_embedding_candidates(
        self,
        *,
        model: str,
        dimensions: int,
        limit: int,
        lookback_days: int,
    ) -> list[EmbeddingCandidate]:
        limit = max(1, int(limit))
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, e.content_hash AS embedded_hash
                FROM messages m
                LEFT JOIN message_embeddings e
                  ON e.message_id = m.id AND e.model = ? AND e.dimensions = ?
                WHERE m.created_at >= ?
                  AND m.is_bot = 0
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (model, int(dimensions), cutoff),
            ).fetchall()
        return self._embedding_candidates_from_rows(rows, limit)

    def embedding_backlog_count(self, *, model: str, dimensions: int, lookback_days: int) -> int:
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, e.content_hash AS embedded_hash
                FROM messages m
                LEFT JOIN message_embeddings e
                  ON e.message_id = m.id AND e.model = ? AND e.dimensions = ?
                WHERE m.created_at >= ?
                  AND m.is_bot = 0
                ORDER BY m.created_at ASC, m.id ASC
                """,
                (model, int(dimensions), cutoff),
            ).fetchall()
        return len(self._embedding_candidates_from_rows(rows, 1000000))

    def embedding_index_count(self, *, chat_id: int, model: str, dimensions: int, lookback_days: int) -> int:
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM message_embeddings e
                JOIN messages m ON m.id = e.message_id
                WHERE e.chat_id = ?
                  AND e.model = ?
                  AND e.dimensions = ?
                  AND m.created_at >= ?
                  AND m.is_bot = 0
                """,
                (chat_id, model, int(dimensions), cutoff),
            ).fetchone()
        return int(row["count"] or 0)

    def upsert_embedding(
        self,
        *,
        message_id: int,
        chat_id: int,
        model: str,
        dimensions: int,
        content_hash: str,
        embedding: list[float],
    ) -> None:
        blob = self._embedding_to_blob(embedding)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO message_embeddings (
                    message_id, chat_id, model, dimensions, content_hash, embedding_blob, embedded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    model = excluded.model,
                    dimensions = excluded.dimensions,
                    content_hash = excluded.content_hash,
                    embedding_blob = excluded.embedding_blob,
                    embedded_at = excluded.embedded_at
                """,
                (
                    int(message_id),
                    int(chat_id),
                    model,
                    int(dimensions),
                    content_hash,
                    blob,
                    self._format_datetime(None),
                ),
            )
            self._conn.commit()

    def semantic_search(
        self,
        *,
        chat_id: int,
        query_embedding: list[float],
        model: str,
        dimensions: int,
        lookback_days: int,
        limit: int,
    ) -> list[SemanticMemoryResult]:
        limit = max(1, int(limit))
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, e.embedding_blob
                FROM message_embeddings e
                JOIN messages m ON m.id = e.message_id
                WHERE e.chat_id = ?
                  AND e.model = ?
                  AND e.dimensions = ?
                  AND m.created_at >= ?
                  AND m.is_bot = 0
                """,
                (chat_id, model, int(dimensions), cutoff),
            ).fetchall()

        results: list[SemanticMemoryResult] = []
        for row in rows:
            item = self._row_to_item(row)
            search_text = self.searchable_text_for_item(item)
            if not search_text:
                continue
            score = self._dot(query_embedding, self._embedding_from_blob(row["embedding_blob"]))
            results.append(SemanticMemoryResult(item=item, search_text=search_text, score=score, source="semantic"))
        return sorted(results, key=lambda result: result.score, reverse=True)[:limit]

    def fts_search(self, *, chat_id: int, query: str, lookback_days: int, limit: int) -> list[SemanticMemoryResult]:
        fts_query = self._fts_query(query)
        if not fts_query:
            return []
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT m.*, bm25(message_fts) AS rank
                    FROM message_fts
                    JOIN messages m ON m.id = message_fts.rowid
                    WHERE message_fts MATCH ?
                      AND m.chat_id = ?
                      AND m.created_at >= ?
                      AND m.is_bot = 0
                    ORDER BY rank ASC
                    LIMIT ?
                    """,
                    (fts_query, chat_id, cutoff, max(1, int(limit))),
                ).fetchall()
        except sqlite3.OperationalError:
            return []

        results: list[SemanticMemoryResult] = []
        for row in rows:
            item = self._row_to_item(row)
            results.append(
                SemanticMemoryResult(
                    item=item,
                    search_text=self.searchable_text_for_item(item),
                    score=-float(row["rank"] or 0.0),
                    source="fts",
                )
            )
        return results

    def keyword_search(self, *, chat_id: int, query: str, lookback_days: int, limit: int) -> list[SemanticMemoryResult]:
        terms = [term.casefold() for term in re.findall(r"[^\W_]{2,}", query, flags=re.UNICODE)]
        terms = [term for term in terms[:6] if term]
        if not terms:
            return []
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        text_expr = """
            lower(
                coalesce(text, '') || ' ' ||
                coalesce(source_text, '') || ' ' ||
                coalesce(source_title, '') || ' ' ||
                coalesce(source_url, '') || ' ' ||
                coalesce(vision_summary, '')
            )
        """
        conditions = [
            "chat_id = ?",
            "created_at >= ?",
            "is_bot = 0",
        ]
        params: list[object] = [chat_id, cutoff]
        for term in terms:
            conditions.append(f"{text_expr} LIKE ?")
            params.append(f"%{term}%")
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT *
                FROM messages
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        results: list[SemanticMemoryResult] = []
        for index, row in enumerate(rows):
            item = self._row_to_item(row)
            results.append(
                SemanticMemoryResult(
                    item=item,
                    search_text=self.searchable_text_for_item(item),
                    score=100.0 - (index * 0.001),
                    source="keyword",
                )
            )
        return results

    def rebuild_search_index(self) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM messages ORDER BY created_at ASC, id ASC").fetchall()
            self._conn.execute("DELETE FROM message_fts")
            indexed = 0
            for row in rows:
                item_id = int(row["id"])
                values = dict(row)
                search_text = self.searchable_text_from_values(values)
                if not search_text:
                    self._conn.execute("DELETE FROM message_embeddings WHERE message_id = ?", (item_id,))
                    continue
                content_hash = self.content_hash(search_text)
                self._conn.execute("INSERT INTO message_fts(rowid, search_text) VALUES (?, ?)", (item_id, search_text))
                self._conn.execute(
                    "DELETE FROM message_embeddings WHERE message_id = ? AND content_hash != ?",
                    (item_id, content_hash),
                )
                indexed += 1
            self._conn.commit()
        return indexed

    def cleanup(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        cutoff_text = self._format_datetime(cutoff)
        with self._lock:
            deleted_ids = [
                int(row["id"])
                for row in self._conn.execute("SELECT id FROM messages WHERE created_at < ?", (cutoff_text,)).fetchall()
            ]
            rows = self._conn.execute(
                "SELECT local_media_path FROM messages WHERE created_at < ? AND local_media_path != ''",
                (cutoff_text,),
            ).fetchall()
            cursor = self._conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff_text,))
            for item_id in deleted_ids:
                self._conn.execute("DELETE FROM message_fts WHERE rowid = ?", (item_id,))
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
            self._conn.execute("DELETE FROM message_embeddings")
            self._conn.execute("DELETE FROM message_fts")
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

    def _sync_search_index_for_id(self, item_id: int) -> None:
        row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return
        values = dict(row)
        self._sync_search_index_from_values(int(item_id), values)

    def _sync_search_index_from_values(self, item_id: int, values: dict[str, object]) -> None:
        search_text = self.searchable_text_from_values(values)
        if not search_text:
            self._conn.execute("DELETE FROM message_fts WHERE rowid = ?", (item_id,))
            self._conn.execute("DELETE FROM message_embeddings WHERE message_id = ?", (item_id,))
            self._conn.commit()
            return

        content_hash = self.content_hash(search_text)
        self._conn.execute("DELETE FROM message_fts WHERE rowid = ?", (item_id,))
        self._conn.execute("INSERT INTO message_fts(rowid, search_text) VALUES (?, ?)", (item_id, search_text))
        self._conn.execute(
            "DELETE FROM message_embeddings WHERE message_id = ? AND content_hash != ?",
            (item_id, content_hash),
        )
        self._conn.commit()

    def _embedding_candidates_from_rows(self, rows: list[sqlite3.Row], limit: int) -> list[EmbeddingCandidate]:
        candidates: list[EmbeddingCandidate] = []
        for row in rows:
            item = self._row_to_item(row)
            search_text = self.searchable_text_for_item(item)
            if not search_text:
                continue
            content_hash = self.content_hash(search_text)
            if row["embedded_hash"] == content_hash:
                continue
            candidates.append(EmbeddingCandidate(item=item, search_text=search_text, content_hash=content_hash))
            if len(candidates) >= limit:
                break
        return candidates

    @staticmethod
    def content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def searchable_text_from_values(values: dict[str, object]) -> str:
        if bool(values.get("is_bot")):
            return ""
        parts = [
            str(values.get("text") or ""),
            str(values.get("source_text") or ""),
            str(values.get("source_title") or ""),
            str(values.get("source_url") or ""),
            str(values.get("vision_summary") or ""),
        ]
        text = " ".join(part for part in parts if part)
        if not text or text.startswith("[message has "):
            text = str(values.get("vision_summary") or "")
        return " ".join(text.split())

    @staticmethod
    def searchable_text_for_item(item: MemoryItem) -> str:
        return MemoryStore.searchable_text_from_values(
            {
                "is_bot": item.is_bot,
                "text": item.text,
                "source_text": item.source_text,
                "source_title": item.source_title,
                "source_url": item.source_url,
                "vision_summary": item.vision_summary,
            }
        )

    @staticmethod
    def _embedding_to_blob(embedding: list[float]) -> bytes:
        return struct.pack(f"<{len(embedding)}f", *[float(value) for value in embedding])

    @staticmethod
    def _embedding_from_blob(blob: bytes) -> list[float]:
        if not blob:
            return []
        count = len(blob) // 4
        return list(struct.unpack(f"<{count}f", blob))

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        if len(left) != len(right) or not left:
            return -1.0
        return float(sum(a * b for a, b in zip(left, right)))

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[^\W_]{2,}", query.casefold(), flags=re.UNICODE)
        tokens = [token.replace('"', '""') for token in tokens[:12]]
        return " OR ".join(f'"{token}"' for token in tokens)

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
            source_text=row["source_text"],
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
