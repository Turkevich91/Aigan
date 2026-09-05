from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence


AUTHORED_TURN_NOTE_PREFIX = "[aigan.authored_turn.v1]"
AUTHORED_TURN_MAX_PARTS = 10
AUTHORED_TURN_MAX_CHARS = 12000


def _without_authored_turn_marker(note: str) -> str:
    if not any(line.startswith(AUTHORED_TURN_NOTE_PREFIX) for line in note.splitlines()):
        return note
    return "\n".join(line for line in note.splitlines() if not line.startswith(AUTHORED_TURN_NOTE_PREFIX))


def _merge_authored_turn_note(previous: str, replacement: str) -> str:
    """Keep host-only turn metadata when a later cache/import updates its note."""
    markers = [line for line in previous.splitlines() if line.startswith(AUTHORED_TURN_NOTE_PREFIX)]
    note = _without_authored_turn_marker(replacement)
    if not markers:
        return note
    if not note:
        note = "\n".join(line for line in previous.splitlines() if not line.startswith(AUTHORED_TURN_NOTE_PREFIX))
    return "\n".join(part for part in (note, *markers) if part)


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


@dataclass(frozen=True)
class MemorySearchBatch:
    """One filtered retriever invocation; query variants remain separate batches."""

    channel: str
    results: Sequence[SemanticMemoryResult]


@dataclass(frozen=True)
class MemoryFusionOutcome:
    results: list[SemanticMemoryResult]
    requested_policy: str
    applied_policy: str
    fallback_reason: str = ""


def legacy_memory_result_merge(
    results: Sequence[SemanticMemoryResult], limit: int,
) -> list[SemanticMemoryResult]:
    """Preserve the existing raw-score ordering and first-occurrence behavior."""
    merged: dict[int, SemanticMemoryResult] = {}
    for result in results:
        existing = merged.get(result.item.id)
        if existing is None:
            merged[result.item.id] = result
        else:
            source = existing.source if result.source in existing.source.split("+") else f"{existing.source}+{result.source}"
            merged[result.item.id] = SemanticMemoryResult(
                item=existing.item, search_text=existing.search_text,
                score=max(existing.score, result.score), source=source,
            )
    return sorted(merged.values(), key=lambda item: item.score, reverse=True)[:max(1, limit)]


def fuse_memory_search_batches(
    batches: Sequence[MemorySearchBatch], *, limit: int, policy: str = "legacy",
    protect_numeric: bool = False,
) -> MemoryFusionOutcome:
    """Fuse incomparable retriever scales without multiplying query-variant votes.

    Each row receives at most one contribution per channel. Invalid candidate
    inputs and protected numeric retrieval retain the exact legacy merge.
    """
    requested = str(policy or "legacy").strip().casefold()
    flat = [result for batch in batches for result in batch.results]

    def legacy(reason=""):
        return MemoryFusionOutcome(legacy_memory_result_merge(flat, limit), requested, "legacy", reason)

    if requested == "legacy":
        return legacy()
    if requested not in {"rrf", "normalized"}:
        return legacy("unknown_policy")
    if protect_numeric and any(batch.channel == "keyword" and batch.results for batch in batches):
        return legacy("numeric_protected")
    if any(batch.channel not in {"keyword", "semantic", "fts"} for batch in batches):
        return legacy("invalid_channel")
    try:
        if any(not math.isfinite(result.score) for result in flat):
            return legacy("nonfinite_score")
        canonical: dict[int, SemanticMemoryResult] = {}
        sources: dict[int, set[str]] = {}
        contributions: dict[tuple[int, str], float] = {}
        best_rank: dict[int, int] = {}
        for batch in batches:
            unique: dict[int, SemanticMemoryResult] = {}
            for result in batch.results:
                key = result.item.id
                canonical.setdefault(key, result)
                unique.setdefault(key, result)
                sources.setdefault(key, set()).update(result.source.split("+"))
            values = list(unique.values())
            low = min((result.score for result in values), default=0.)
            high = max((result.score for result in values), default=0.)
            for rank, result in enumerate(values, 1):
                key = result.item.id
                contribution = (1. / (60 + rank) if requested == "rrf" else
                                (result.score - low) / (high - low) if high != low else 1.)
                if not math.isfinite(contribution):
                    return legacy("invalid_candidate_input")
                channel_key = (key, batch.channel)
                contributions[channel_key] = max(contribution, contributions.get(channel_key, 0.))
                best_rank[key] = min(rank, best_rank.get(key, rank))
        totals = {key: sum(contributions.get((key, channel), 0.)
                           for channel in ("keyword", "semantic", "fts")) for key in canonical}
        ordered = sorted(canonical, key=lambda key: (-totals[key], best_rank[key], key))
        results = [SemanticMemoryResult(canonical[key].item, canonical[key].search_text,
                                        totals[key], "+".join(sorted(sources[key])))
                   for key in ordered[:max(1, limit)]]
        return MemoryFusionOutcome(results, requested, requested)
    except (TypeError, ValueError, OverflowError):
        return legacy("invalid_candidate_input")


@dataclass(frozen=True)
class ProvenanceToolRecord:
    ordinal: int
    tool_kind: str
    source_fingerprint: str
    result_status: str
    result_digest: str


@dataclass(frozen=True)
class RunProvenance:
    run_id: str
    route: str
    status: str
    trigger_message_id: int | None
    input_memory_id: int | None
    subject_kind: str
    subject_id: int | None
    subject_key: str
    started_at: str
    completed_at: str
    tools: tuple[ProvenanceToolRecord, ...]


@dataclass(frozen=True)
class DeliveryReceipt:
    run_id: str
    subject_kind: str
    subject_id: int
    subject_key: str
    message_id: int
    status: str


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
        self._conn.create_function("history_casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        self._conn.create_function(
            "history_evidence_digest", -1,
            lambda *values: hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
            deterministic=True,
        )
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
            CREATE TABLE IF NOT EXISTS provenance_runs (
                run_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                trigger_message_id INTEGER,
                input_memory_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                route TEXT NOT NULL DEFAULT '',
                subject_kind TEXT NOT NULL DEFAULT '',
                subject_id INTEGER,
                subject_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'partial_delivery',
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_provenance_runs_chat_trigger
                ON provenance_runs(chat_id, trigger_message_id);
            CREATE TABLE IF NOT EXISTS provenance_tools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES provenance_runs(run_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                tool_kind TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL DEFAULT '',
                result_status TEXT NOT NULL,
                result_digest TEXT NOT NULL DEFAULT '{}',
                input_memory_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                output_memory_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                UNIQUE(run_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS provenance_outputs (
                run_id TEXT NOT NULL REFERENCES provenance_runs(run_id) ON DELETE CASCADE,
                memory_id INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                part_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(run_id, memory_id),
                UNIQUE(run_id, ordinal)
            );
            """
        )
        self._ensure_column("messages", "source_text", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("provenance_runs", "subject_kind", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("provenance_runs", "subject_id", "INTEGER")
        self._ensure_column("provenance_runs", "subject_key", "TEXT NOT NULL DEFAULT ''")
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provenance_runs_subject
                ON provenance_runs(subject_kind, subject_id, subject_key)
            """
        )
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
            "raw_note": _without_authored_turn_marker(raw_note or ""),
        }

        with self._lock:
            existing_id = None
            if message_id is not None:
                row = self._conn.execute(
                    "SELECT id, raw_note FROM messages WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                ).fetchone()
                existing_id = int(row["id"]) if row else None
                if row is not None:
                    values["raw_note"] = _merge_authored_turn_note(str(row["raw_note"] or ""), values["raw_note"])

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
            previous = self._conn.execute("SELECT raw_note FROM messages WHERE id = ?", (item_id,)).fetchone()
            if previous is not None:
                raw_note = _merge_authored_turn_note(str(previous["raw_note"] or ""), raw_note)
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

    def remember_authored_turn(
        self, *, chat_id: int, trigger_message_id: int,
        authored_parts: Sequence[tuple[int, str]], turn_end_message_id: int,
    ) -> bool:
        """Persist host-admitted authored relations without replacing messages.

        The ingress owner supplies its already isolated cohort and original
        text snapshots. Canonical IDs and digests are resolved under one lock;
        source material never becomes an authored continuation request.
        """
        if not 1 <= len(authored_parts) <= AUTHORED_TURN_MAX_PARTS:
            return False
        message_ids = [part[0] for part in authored_parts]
        if message_ids != sorted(set(message_ids)) or message_ids[-1] > turn_end_message_id:
            return False
        if sum(len(part[1]) for part in authored_parts) + len(authored_parts) - 1 > AUTHORED_TURN_MAX_CHARS:
            return False
        with self._lock:
            trigger = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, trigger_message_id),
            ).fetchone()
            end = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, turn_end_message_id),
            ).fetchone()
            if trigger is None or end is None or trigger["is_bot"] or trigger["user_id"] is None:
                return False
            if trigger_message_id > turn_end_message_id or end["user_id"] != trigger["user_id"]:
                return False
            records = []
            valid = True
            for message_id, expected_text in authored_parts:
                row = self._conn.execute(
                    "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id),
                ).fetchone()
                if (
                    row is None or row["is_bot"] or row["forward_origin"] or row["source_text"]
                    or row["content_kind"] != "text" or str(row["text"]).casefold().startswith("[message has ")
                    or row["user_id"] != trigger["user_id"] or not str(row["text"]).strip()
                    or row["text"] != expected_text or row["created_at"] > end["created_at"]
                    or row["reply_to_message_id"] != trigger["reply_to_message_id"]
                ):
                    # A captured turn whose canonical rows changed must not
                    # silently become a legacy first-fragment continuation.
                    valid = False
                    records = []
                    break
                records.append([int(row["id"]), hashlib.sha256(expected_text.encode("utf-8")).hexdigest()])
            metadata = {
                "actor": int(trigger["user_id"]), "end": int(turn_end_message_id),
                "cutoff": str(end["created_at"]), "parts": records,
            }
            note = "\n".join(
                line for line in str(trigger["raw_note"] or "").splitlines()
                if not line.startswith(AUTHORED_TURN_NOTE_PREFIX)
            )
            note = "\n".join(part for part in (note, AUTHORED_TURN_NOTE_PREFIX + json.dumps(metadata, separators=(",", ":"))) if part)
            self._conn.execute("UPDATE messages SET raw_note = ? WHERE id = ?", (note, trigger["id"]))
            self._conn.commit()
            return valid

    def authored_turn_items(
        self, *, chat_id: int, trigger_message_id: int, before_message_id: int,
    ) -> list[MemoryItem] | None:
        """Return validated authored rows; None means absent, [] invalid evidence.

        Callers must not fall back to a fragment after invalidation. This
        metadata is host-owned provenance and must never be rendered to models.
        """
        with self._lock:
            trigger = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, trigger_message_id),
            ).fetchone()
            if trigger is None:
                return []
            markers = [line for line in str(trigger["raw_note"] or "").splitlines() if line.startswith(AUTHORED_TURN_NOTE_PREFIX)]
            if not markers:
                return None
            if len(markers) != 1 or len(markers[0]) > 4096:
                return []
            try:
                metadata = json.loads(markers[0][len(AUTHORED_TURN_NOTE_PREFIX):])
                actor, end, cutoff, parts = (metadata[key] for key in ("actor", "end", "cutoff", "parts"))
                if (
                    type(actor) is not int or type(end) is not int or not isinstance(cutoff, str)
                    or not isinstance(parts, list) or not 1 <= len(parts) <= AUTHORED_TURN_MAX_PARTS
                    or trigger["is_bot"] or trigger["user_id"] != actor
                    or not trigger_message_id <= end < before_message_id
                ):
                    return []
                end_row = self._conn.execute(
                    "SELECT user_id, created_at FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, end),
                ).fetchone()
                if end_row is None or end_row["user_id"] != actor or end_row["created_at"] != cutoff:
                    return []
                items = []
                previous_message_id = -1
                for record in parts:
                    if not isinstance(record, list) or len(record) != 2 or type(record[0]) is not int:
                        return []
                    row = self._conn.execute("SELECT * FROM messages WHERE id = ? AND chat_id = ?", (record[0], chat_id)).fetchone()
                    if (
                        row is None or row["is_bot"] or row["forward_origin"] or row["source_text"]
                        or row["content_kind"] != "text" or str(row["text"]).casefold().startswith("[message has ")
                        or row["user_id"] != actor or not str(row["text"]).strip()
                        or row["message_id"] is None or not previous_message_id < row["message_id"] <= end
                        or row["created_at"] > cutoff or row["reply_to_message_id"] != trigger["reply_to_message_id"]
                        or hashlib.sha256(str(row["text"]).encode("utf-8")).hexdigest() != record[1]
                    ):
                        return []
                    items.append(self._row_to_item(row))
                    previous_message_id = row["message_id"]
                if sum(len(item.text) for item in items) + len(items) - 1 > AUTHORED_TURN_MAX_CHARS:
                    return []
                return items
            except (KeyError, TypeError, ValueError):
                return []

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

    def chat_message_count(self, chat_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return int(row["count"] or 0) if row is not None else 0

    def bounded_history_rows(
        self, *, chat_id: int, cutoff_memory_id: int, cutoff_created_at: str,
        mode: str, limit: int, terms: tuple[str, ...] = (),
        anchor_id: int | None = None, participant_id: int | None = None,
        after: str = "", before: str = "", authored_only: bool = False,
        before_key: tuple[float, int] | None = None,
    ) -> tuple[list[dict[str, object]], bool]:
        """Read a bounded original-message page without embeddings or FTS exclusions.

        This low-level API receives a trusted scope from ChatHistorySession. SQL
        projects only safe, size-limited fields; no complete MemoryItem leaves it.
        The lexical path includes bot replies, which the ordinary FTS omits.
        """
        limit = max(1, min(20, int(limit)))
        conditions = ["m.chat_id = ?", "m.id < ?", "julianday(m.created_at) <= julianday(?)"]
        params: list[object] = [chat_id, cutoff_memory_id, cutoff_created_at]
        if participant_id is not None:
            conditions.append("m.user_id = ?")
            params.append(participant_id)
        if after:
            conditions.append("julianday(m.created_at) >= julianday(?)")
            params.append(after)
        if before:
            conditions.append("julianday(m.created_at) < julianday(?)")
            params.append(before)
        if authored_only:
            conditions.extend([
                "m.is_bot = 0", "m.text != ''", "m.text NOT LIKE '[message has %'",
                "m.forward_origin = ''", "m.content_kind = 'text'",
            ])
        if mode == "search":
            if not terms:
                return [], False
            text_expression = "m.text" if authored_only else "m.text || ' ' || m.vision_summary || ' ' || m.source_text"
            for term in terms[:8]:
                conditions.append(f"instr(history_casefold({text_expression}), ?) > 0")
                params.append(term.casefold())
        if before_key is not None:
            if mode not in {"recent", "search"}:
                raise ValueError("Only chronological pages have a continuation key")
            conditions.append("(julianday(m.created_at) < ? OR (julianday(m.created_at) = ? AND m.id < ?))")
            params.extend((before_key[0], before_key[0], before_key[1]))
        summary = "''" if authored_only else "substr(m.vision_summary, 1, 1001)"
        source = "''" if authored_only else "substr(m.source_text, 1, 1001)"
        projection = f"""m.id, m.message_id, m.user_id, m.created_at,
            substr(m.sender_label, 1, 97) AS sender_label,
            substr(m.text, 1, 1001) AS text,
            {source} AS source_text,
            substr(m.attachment_type, 1, 49) AS attachment_type,
            {summary} AS attachment_summary, m.reply_to_message_id,
            (m.forward_origin != '') AS is_forwarded,
            julianday(m.created_at) AS sort_time,
            history_evidence_digest(m.id, m.message_id, m.user_id, m.created_at,
                m.sender_label, m.text, m.source_text, m.attachment_type,
                m.vision_summary, m.reply_to_message_id, m.is_bot, m.content_kind,
                m.forward_origin) AS evidence_digest"""
        where = " AND ".join(conditions)
        with self._lock:
            if mode == "around":
                anchor = self._conn.execute(
                    f"SELECT {projection} FROM messages m WHERE {where} AND m.id = ? LIMIT 1",
                    (*params, anchor_id),
                ).fetchone()
                if anchor is None:
                    return [], False
                sides = []
                for comparison, direction in (("<", "DESC"), (">", "ASC")):
                    sides.append(self._conn.execute(
                        f"""SELECT {projection} FROM messages m WHERE {where}
                            AND (julianday(m.created_at) {comparison} ? OR
                                (julianday(m.created_at) = ? AND m.id {comparison} ?))
                            ORDER BY julianday(m.created_at) {direction}, m.id {direction} LIMIT ?""",
                        (*params, anchor["sort_time"], anchor["sort_time"], anchor_id, limit),
                    ).fetchall())
                earlier, later = sides
                earlier_count = min(len(earlier), (limit - 1) // 2)
                later_count = min(len(later), limit - 1 - earlier_count)
                earlier_count = min(len(earlier), limit - 1 - later_count)
                rows = list(reversed(earlier[:earlier_count])) + [anchor] + later[:later_count]
                more = len(earlier) > earlier_count or len(later) > later_count
            elif mode in {"recent", "search"}:
                rows = self._conn.execute(
                    f"""SELECT {projection} FROM messages m WHERE {where}
                        ORDER BY julianday(m.created_at) DESC, m.id DESC LIMIT ?""",
                    (*params, limit + 1),
                ).fetchall()
                more = len(rows) > limit
                rows = list(reversed(rows[:limit]))
            else:
                raise ValueError("Unknown history mode")
        return [dict(row) for row in rows], more

    def read_history_retrieval_candidates(
        self, *, chat_id: int, cutoff_memory_id: int, cutoff_created_at: str,
        participant_id: int | None = None, after: str = "", before: str = "",
        authored_only: bool = False, scan_limit: int = 8192, query: str = "",
    ) -> tuple[list[tuple[MemoryItem, dict[str, object]]], bool, dict[int, float], bool]:
        """Private bounded snapshot for history_retrieval, never a model payload.

        Scope filters precede both the cap probe and every retrieval channel.
        Probe IDs first: an oversized archive is refused without loading bodies
        or silently narrowing it to recent rows. The caller projects safe fields.
        """
        scan_limit = max(1, min(8192, int(scan_limit)))
        conditions = ["m.chat_id = ?", "m.id < ?", "julianday(m.created_at) <= julianday(?)"]
        params: list[object] = [chat_id, cutoff_memory_id, cutoff_created_at]
        if participant_id is not None:
            conditions.append("m.user_id = ?")
            params.append(participant_id)
        if after:
            conditions.append("julianday(m.created_at) >= julianday(?)")
            params.append(after)
        if before:
            conditions.append("julianday(m.created_at) < julianday(?)")
            params.append(before)
        if authored_only:
            conditions.extend([
                "m.is_bot = 0", "m.text != ''", "m.text NOT LIKE '[message has %'",
                "m.forward_origin = ''", "m.content_kind = 'text'",
            ])
        where = " AND ".join(conditions)
        with self._lock:
            ids = self._conn.execute(
                f"SELECT m.id FROM messages m WHERE {where} LIMIT ?", (*params, scan_limit + 1),
            ).fetchall()
            if len(ids) > scan_limit:
                return [], True, {}, False
            rows = self._conn.execute(
                f"""SELECT m.*, julianday(m.created_at) AS sort_time,
                    history_evidence_digest(m.id, m.message_id, m.user_id, m.created_at,
                        m.sender_label, m.text, m.source_text, m.attachment_type,
                        m.vision_summary, m.reply_to_message_id, m.is_bot, m.content_kind,
                        m.forward_origin) AS evidence_digest,
                    e.chat_id AS embedding_chat_id, e.model AS embedding_model,
                    e.dimensions AS embedding_dimensions, e.content_hash AS embedding_hash,
                    e.embedding_blob, e.embedded_at
                    FROM messages m LEFT JOIN message_embeddings e ON e.message_id = m.id
                    WHERE {where} ORDER BY m.id LIMIT ?""", (*params, scan_limit + 1),
            ).fetchall()
            if len(rows) > scan_limit:
                return [], True, {}, False
            scores: dict[int, float] = {}
            fts_available = True
            # The existing FTS index mixes authored/source fields. Do not use
            # that channel for an authored-only scope.
            fts_query = self._fts_query(query) if not authored_only else ""
            if fts_query:
                try:
                    matches = self._conn.execute(
                        f"""SELECT m.id, bm25(message_fts) AS rank FROM message_fts
                            JOIN messages m ON m.id = message_fts.rowid
                            WHERE message_fts MATCH ? AND {where}
                            ORDER BY rank ASC, m.id ASC LIMIT ?""",
                        (fts_query, *params, scan_limit),
                    ).fetchall()
                    scores = {row["id"]: -float(row["rank"] or 0.) for row in matches}
                except sqlite3.OperationalError:
                    fts_available = False
        fields = ("sort_time", "evidence_digest", "embedding_chat_id", "embedding_model", "embedding_dimensions",
                  "embedding_hash", "embedding_blob", "embedded_at")
        return [(self._row_to_item(row), {key: row[key] for key in fields}) for row in rows], False, scores, fts_available

    def context_window_around_item(
        self,
        chat_id: int,
        item_id: int,
        *,
        before: int = 2,
        after: int = 2,
    ) -> list[MemoryItem]:
        with self._lock:
            anchor = self._conn.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND id = ? LIMIT 1",
                (chat_id, int(item_id)),
            ).fetchone()
            if anchor is None:
                return []
            before_rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ?
                  AND (
                    created_at < ?
                    OR (created_at = ? AND id < ?)
                  )
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (
                    chat_id,
                    anchor["created_at"],
                    anchor["created_at"],
                    int(item_id),
                    max(0, int(before)),
                ),
            ).fetchall()
            after_rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE chat_id = ?
                  AND (
                    created_at > ?
                    OR (created_at = ? AND id > ?)
                  )
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (
                    chat_id,
                    anchor["created_at"],
                    anchor["created_at"],
                    int(item_id),
                    max(0, int(after)),
                ),
            ).fetchall()
        rows = list(reversed(before_rows)) + [anchor] + list(after_rows)
        return [self._row_to_item(row) for row in rows]

    def latest_user_message(self, chat_id: int) -> MemoryItem | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM messages
                WHERE chat_id = ?
                  AND is_bot = 0
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def latest_bot_message(self, chat_id: int, label_prefixes: tuple[str, ...] = ("Aigan",)) -> MemoryItem | None:
        prefixes = tuple(prefix for prefix in label_prefixes if prefix)
        if not prefixes:
            return None
        conditions = ["chat_id = ?", "is_bot = 1"]
        params: list[object] = [chat_id]
        label_conditions = []
        for prefix in prefixes:
            label_conditions.append("sender_label LIKE ?")
            params.append(f"{prefix}%")
        conditions.append("(" + " OR ".join(label_conditions) + ")")
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT *
                FROM messages
                WHERE {" AND ".join(conditions)}
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return self._row_to_item(row) if row is not None else None

    def recent_user_text_activity(self, chat_id: int, *, lookback_days: int, limit: int = 500) -> list[MemoryItem]:
        cutoff = self._format_datetime(datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days))))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM messages
                WHERE chat_id = ?
                  AND is_bot = 0
                  AND created_at >= ?
                  AND text != ''
                  AND text NOT LIKE '[message has %'
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (chat_id, cutoff, max(1, int(limit))),
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

    def record_provenance_output(
        self,
        *,
        run_id: str,
        chat_id: int,
        trigger_message_id: int | None,
        input_memory_id: int | None,
        route: str,
        started_at: datetime | str,
        output_memory_id: int,
        output_ordinal: int,
        output_part_count: int,
        tools: tuple[object, ...] | list[object] = (),
        subject_kind: str = "",
        subject_id: int | None = None,
        subject_key: str = "",
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
            raise ValueError("run_id must be 32 lowercase hex characters")
        route_value = re.sub(r"[^a-zA-Z0-9_-]", "", route or "")[:64]
        subject_kind_value = re.sub(r"[^a-zA-Z0-9_-]", "", subject_kind or "")[:64]
        subject_key_value = re.sub(r"[^0-9a-f]", "", subject_key or "")[:64]
        started = self._format_datetime(started_at)
        completed = self._format_datetime(None)
        part_count = max(1, int(output_part_count))
        ordinal = max(0, int(output_ordinal))
        if ordinal >= part_count:
            raise ValueError("provenance output ordinal must be smaller than part_count")
        chat_value = int(chat_id)
        trigger_value = int(trigger_message_id) if trigger_message_id is not None else None
        input_value = int(input_memory_id) if input_memory_id is not None else None
        subject_id_value = int(subject_id) if subject_id is not None else None
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                output_chat = self._conn.execute(
                    "SELECT chat_id, is_bot FROM messages WHERE id = ?",
                    (int(output_memory_id),),
                ).fetchone()
                if output_chat is None or int(output_chat["chat_id"]) != chat_value:
                    raise ValueError("provenance output chat mismatch")
                if not bool(output_chat["is_bot"]):
                    raise ValueError("provenance output must be bot-authored")
                if input_value is not None:
                    input_chat = self._conn.execute(
                        "SELECT chat_id FROM messages WHERE id = ?",
                        (input_value,),
                    ).fetchone()
                    if input_chat is None or int(input_chat["chat_id"]) != chat_value:
                        raise ValueError("provenance input chat mismatch")
                existing_run = self._conn.execute(
                    "SELECT * FROM provenance_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                expected_run_metadata = (
                    chat_value,
                    trigger_value,
                    input_value,
                    route_value,
                    subject_kind_value,
                    subject_id_value,
                    subject_key_value,
                    started,
                )
                if existing_run is None:
                    self._conn.execute(
                        """
                        INSERT INTO provenance_runs (
                            run_id, chat_id, trigger_message_id, input_memory_id,
                            route, subject_kind, subject_id, subject_key,
                            status, started_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'partial_delivery', ?, ?)
                        """,
                        (run_id, *expected_run_metadata, completed),
                    )
                else:
                    actual_run_metadata = (
                        int(existing_run["chat_id"]),
                        existing_run["trigger_message_id"],
                        existing_run["input_memory_id"],
                        str(existing_run["route"]),
                        str(existing_run["subject_kind"]),
                        existing_run["subject_id"],
                        str(existing_run["subject_key"]),
                        str(existing_run["started_at"]),
                    )
                    if actual_run_metadata != expected_run_metadata:
                        raise ValueError("provenance run metadata mismatch")
                for tool in tools:
                    tool_ordinal = max(0, int(getattr(tool, "ordinal", 0)))
                    tool_kind = re.sub(r"[^a-zA-Z0-9_-]", "", str(getattr(tool, "tool_kind", "unknown")))[:64]
                    source_fingerprint = re.sub(
                        r"[^0-9a-f]",
                        "",
                        str(getattr(tool, "source_fingerprint", "")),
                    )[:64]
                    result_status = re.sub(
                        r"[^a-z0-9_-]",
                        "",
                        str(getattr(tool, "result_status", "unknown")).casefold(),
                    )[:48]
                    result_digest = self._safe_provenance_result_digest(
                        getattr(tool, "result_digest", "{}"),
                        result_status or "unknown",
                    )
                    expected_tool_metadata = (
                        tool_kind or "unknown",
                        source_fingerprint,
                        result_status or "unknown",
                        result_digest,
                        input_value,
                    )
                    existing_tool = self._conn.execute(
                        """
                        SELECT tool_kind, source_fingerprint, result_status, result_digest, input_memory_id
                        FROM provenance_tools
                        WHERE run_id = ? AND ordinal = ?
                        """,
                        (run_id, tool_ordinal),
                    ).fetchone()
                    if existing_tool is None:
                        self._conn.execute(
                            """
                            INSERT INTO provenance_tools (
                                run_id, ordinal, tool_kind, source_fingerprint,
                                result_status, result_digest, input_memory_id, output_memory_id
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (run_id, tool_ordinal, *expected_tool_metadata, int(output_memory_id)),
                        )
                    else:
                        actual_tool_metadata = tuple(existing_tool)
                        if actual_tool_metadata != expected_tool_metadata:
                            raise ValueError("provenance tool metadata mismatch")

                output_id = int(output_memory_id)
                existing_slot = self._conn.execute(
                    """
                    SELECT memory_id, part_count
                    FROM provenance_outputs
                    WHERE run_id = ? AND ordinal = ?
                    """,
                    (run_id, ordinal),
                ).fetchone()
                existing_output = self._conn.execute(
                    """
                    SELECT run_id, ordinal, part_count
                    FROM provenance_outputs
                    WHERE memory_id = ?
                    """,
                    (output_id,),
                ).fetchone()
                expected_slot = (output_id, part_count)
                if existing_slot is not None and tuple(existing_slot) != expected_slot:
                    raise ValueError("provenance output slot conflict")
                if existing_output is not None and tuple(existing_output) != (run_id, ordinal, part_count):
                    raise ValueError("provenance output mapping conflict")
                existing_part_count = self._conn.execute(
                    "SELECT MIN(part_count), MAX(part_count) FROM provenance_outputs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if (
                    existing_part_count is not None
                    and existing_part_count[0] is not None
                    and (int(existing_part_count[0]) != part_count or int(existing_part_count[1]) != part_count)
                ):
                    raise ValueError("provenance output part_count mismatch")
                if existing_slot is None and existing_output is None:
                    self._conn.execute(
                        """
                        INSERT INTO provenance_outputs (run_id, memory_id, ordinal, part_count)
                        VALUES (?, ?, ?, ?)
                        """,
                        (run_id, output_id, ordinal, part_count),
                    )
                self._conn.execute(
                    """
                    UPDATE provenance_tools
                    SET output_memory_id = COALESCE(output_memory_id, ?)
                    WHERE run_id = ?
                    """,
                    (output_memory_id, run_id),
                )
                delivery = self._conn.execute(
                    """
                    SELECT COUNT(*) AS delivered_count,
                           MIN(ordinal) AS min_ordinal,
                           MAX(ordinal) AS max_ordinal,
                           MIN(part_count) AS min_part_count,
                           MAX(part_count) AS max_part_count
                    FROM provenance_outputs
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                delivered_count = int(delivery["delivered_count"] or 0)
                intended_count = max(1, int(delivery["max_part_count"] or part_count))
                complete = (
                    int(delivery["min_part_count"] or intended_count) == intended_count
                    and delivered_count == intended_count
                    and int(delivery["min_ordinal"] if delivery["min_ordinal"] is not None else -1) == 0
                    and int(delivery["max_ordinal"] if delivery["max_ordinal"] is not None else -1)
                    == intended_count - 1
                )
                status = "delivered" if complete else "partial_delivery"
                self._conn.execute(
                    "UPDATE provenance_runs SET status = ?, completed_at = ? WHERE run_id = ?",
                    (status, completed, run_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _safe_provenance_result_digest(value: object, status: str) -> str:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
        safe: dict[str, object] = {"status": status}
        if isinstance(parsed, dict):
            chars = parsed.get("chars")
            if chars in {"empty", "small", "medium", "large"}:
                safe["chars"] = chars
            source = parsed.get("source")
            if source in {"captions", "audio", "empty", "other"}:
                safe["source"] = source
            if isinstance(parsed.get("timestamps"), bool):
                safe["timestamps"] = parsed["timestamps"]
        return json.dumps(safe, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    def provenance_for_output(self, output_memory_id: int | None) -> RunProvenance | None:
        if output_memory_id is None:
            return None
        with self._lock:
            run = self._conn.execute(
                """
                SELECT r.*
                FROM provenance_runs AS r
                JOIN provenance_outputs AS o ON o.run_id = r.run_id
                JOIN messages AS m ON m.id = o.memory_id AND m.chat_id = r.chat_id
                WHERE o.memory_id = ?
                LIMIT 1
                """,
                (int(output_memory_id),),
            ).fetchone()
            if run is None:
                return None
            tool_rows = self._conn.execute(
                """
                SELECT ordinal, tool_kind, source_fingerprint, result_status, result_digest
                FROM provenance_tools
                WHERE run_id = ?
                ORDER BY ordinal, id
                """,
                (run["run_id"],),
            ).fetchall()
        return RunProvenance(
            run_id=str(run["run_id"]),
            route=str(run["route"]),
            status=str(run["status"]),
            trigger_message_id=run["trigger_message_id"],
            input_memory_id=run["input_memory_id"],
            subject_kind=str(run["subject_kind"]),
            subject_id=run["subject_id"],
            subject_key=str(run["subject_key"]),
            started_at=str(run["started_at"]),
            completed_at=str(run["completed_at"]),
            tools=tuple(
                ProvenanceToolRecord(
                    ordinal=int(row["ordinal"]),
                    tool_kind=str(row["tool_kind"]),
                    source_fingerprint=str(row["source_fingerprint"]),
                    result_status=str(row["result_status"]),
                    result_digest=str(row["result_digest"]),
                )
                for row in tool_rows
            ),
        )

    def delivery_receipt_for_subject(
        self,
        subject_kind: str,
        subject_id: int,
        subject_key: str,
    ) -> DeliveryReceipt | None:
        kind = re.sub(r"[^a-zA-Z0-9_-]", "", subject_kind or "")[:64]
        key = re.sub(r"[^0-9a-f]", "", subject_key or "")[:64]
        with self._lock:
            row = self._conn.execute(
                """
                SELECT r.run_id, r.subject_kind, r.subject_id, r.subject_key,
                       r.status, m.message_id
                FROM provenance_runs AS r
                JOIN provenance_outputs AS o ON o.run_id = r.run_id
                JOIN messages AS m ON m.id = o.memory_id AND m.chat_id = r.chat_id
                WHERE r.subject_kind = ? AND r.subject_id = ? AND r.subject_key = ?
                ORDER BY r.completed_at DESC, o.ordinal, m.id
                LIMIT 1
                """,
                (kind, int(subject_id), key),
            ).fetchone()
        if row is None or row["message_id"] is None:
            return None
        return DeliveryReceipt(
            run_id=str(row["run_id"]),
            subject_kind=str(row["subject_kind"]),
            subject_id=int(row["subject_id"]),
            subject_key=str(row["subject_key"]),
            message_id=int(row["message_id"]),
            status=str(row["status"]),
        )

    def delivery_siblings(self, output_memory_id: int | None) -> list[MemoryItem]:
        if output_memory_id is None:
            return []
        with self._lock:
            run = self._conn.execute(
                """
                SELECT o.run_id, r.chat_id
                FROM provenance_outputs AS o
                JOIN provenance_runs AS r ON r.run_id = o.run_id
                JOIN messages AS m ON m.id = o.memory_id AND m.chat_id = r.chat_id
                WHERE o.memory_id = ?
                LIMIT 1
                """,
                (int(output_memory_id),),
            ).fetchone()
            if run is None:
                return []
            rows = self._conn.execute(
                """
                SELECT m.*
                FROM provenance_outputs AS o
                JOIN messages AS m ON m.id = o.memory_id
                WHERE o.run_id = ? AND m.chat_id = ?
                ORDER BY o.ordinal, m.id
                """,
                (run["run_id"], run["chat_id"]),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def safe_provenance_summary(self, output_memory_id: int | None) -> str:
        provenance = self.provenance_for_output(output_memory_id)
        if provenance is None:
            return ""
        parts = [f"route={provenance.route or 'unknown'}", f"delivery={provenance.status}"]
        if provenance.tools:
            tools = ",".join(f"{tool.tool_kind}:{tool.result_status}" for tool in provenance.tools[:8])
            parts.append(f"tools={tools}")
        return "; ".join(parts)

    def reply_chain(self, chat_id: int, message_id: int | None, depth: int = 6) -> list[MemoryItem]:
        depth = max(0, int(depth))
        if message_id is None or depth == 0:
            return []

        groups: list[list[MemoryItem]] = []
        seen: set[int] = set()
        current_id: int | None = message_id
        for _ in range(depth):
            item = self.message_by_message_id(chat_id, current_id)
            if item is None or item.id in seen:
                break
            provenance = self.provenance_for_output(item.id)
            siblings = self.delivery_siblings(item.id) if provenance is not None else []
            group = siblings or [item]
            unseen_group = [member for member in group if member.id not in seen]
            if not unseen_group:
                break
            groups.append(unseen_group)
            seen.update(member.id for member in unseen_group)
            current_id = provenance.trigger_message_id if provenance is not None else item.reply_to_message_id
            if current_id is None:
                break

        return [item for group in reversed(groups) for item in group]

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
            rows = self._conn.execute(
                """
                SELECT DISTINCT m.id, m.local_media_path
                FROM messages AS m
                LEFT JOIN provenance_outputs AS own_output ON own_output.memory_id = m.id
                WHERE m.created_at < ?
                   OR own_output.run_id IN (
                       SELECT old_output.run_id
                       FROM provenance_outputs AS old_output
                       JOIN messages AS old_message ON old_message.id = old_output.memory_id
                       WHERE old_message.created_at < ?
                   )
                """,
                (cutoff_text, cutoff_text),
            ).fetchall()
            deleted_ids = [int(row["id"]) for row in rows]
            deleted_count = 0
            for start in range(0, len(deleted_ids), 500):
                batch = deleted_ids[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor = self._conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", batch)
                deleted_count += int(cursor.rowcount)
            self._conn.execute(
                "DELETE FROM provenance_runs WHERE run_id NOT IN (SELECT run_id FROM provenance_outputs)"
            )
            for item_id in deleted_ids:
                self._conn.execute("DELETE FROM message_fts WHERE rowid = ?", (item_id,))
            self._conn.commit()

        for row in rows:
            path = Path(row["local_media_path"])
            try:
                if row["local_media_path"] and path.is_file():
                    path.unlink()
            except OSError:
                pass
        return deleted_count

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM provenance_runs")
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
