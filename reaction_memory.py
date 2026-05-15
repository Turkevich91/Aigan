from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from memory import MemoryItem, MemoryStore


@dataclass(frozen=True)
class ReactionSpec:
    reaction_type: str
    reaction_key: str
    custom_emoji_id: str = ""
    base_emoji: str = ""


@dataclass(frozen=True)
class ReactionAsset:
    id: int
    reaction_key: str
    reaction_type: str
    custom_emoji_id: str
    base_emoji: str
    file_id: str
    file_unique_id: str
    set_name: str
    sticker_type: str
    is_animated: bool
    is_video: bool
    local_media_path: str
    thumbnail_path: str
    mime_type: str
    raw_metadata_json: str
    visual_summary_uk: str
    inferred_meaning_uk: str
    tone_tags_json: str
    confidence: float
    analysis_model: str
    analysis_prompt_version: str
    analysis_input_hash: str
    analysis_status: str
    analysis_updated_at: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReactionPreference:
    reaction_key: str
    reaction_type: str
    base_emoji: str
    visual_summary_uk: str
    inferred_meaning_uk: str
    usage_summary_uk: str
    tone_tags_json: str
    count: int
    topics: tuple[str, ...]
    confidence: float
    last_seen_at: str


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def clip_text(text: str, limit: int = 120) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "..."


def item_topic_hint(item: MemoryItem | None, limit: int = 90) -> str:
    if item is None:
        return ""
    for value in (item.text, item.source_title, item.vision_summary, item.source_text, item.source_url):
        if value:
            return clip_text(value, limit)
    if item.attachment_type:
        return f"attachment:{item.attachment_type}"
    return ""


class ReactionMemoryStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.media_dir = self.db_path.parent / "reaction_assets"
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
            CREATE TABLE IF NOT EXISTS reaction_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reaction_key TEXT NOT NULL UNIQUE,
                reaction_type TEXT NOT NULL,
                custom_emoji_id TEXT NOT NULL DEFAULT '',
                base_emoji TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL DEFAULT '',
                file_unique_id TEXT NOT NULL DEFAULT '',
                set_name TEXT NOT NULL DEFAULT '',
                sticker_type TEXT NOT NULL DEFAULT '',
                is_animated INTEGER NOT NULL DEFAULT 0,
                is_video INTEGER NOT NULL DEFAULT 0,
                local_media_path TEXT NOT NULL DEFAULT '',
                thumbnail_path TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                raw_metadata_json TEXT NOT NULL DEFAULT '',
                visual_summary_uk TEXT NOT NULL DEFAULT '',
                inferred_meaning_uk TEXT NOT NULL DEFAULT '',
                tone_tags_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0,
                analysis_model TEXT NOT NULL DEFAULT '',
                analysis_prompt_version TEXT NOT NULL DEFAULT '',
                analysis_input_hash TEXT NOT NULL DEFAULT '',
                analysis_status TEXT NOT NULL DEFAULT '',
                analysis_updated_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reaction_assets_custom
                ON reaction_assets(custom_emoji_id);

            CREATE TABLE IF NOT EXISTS message_reaction_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER UNIQUE,
                event_kind TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                target_message_id INTEGER NOT NULL,
                target_memory_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                actor_key TEXT NOT NULL DEFAULT '',
                actor_kind TEXT NOT NULL DEFAULT '',
                actor_user_id INTEGER,
                actor_username TEXT NOT NULL DEFAULT '',
                actor_chat_id INTEGER,
                old_reactions_json TEXT NOT NULL DEFAULT '[]',
                new_reactions_json TEXT NOT NULL DEFAULT '[]',
                received_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_message_reaction_events_target
                ON message_reaction_events(chat_id, target_message_id, received_at);

            CREATE TABLE IF NOT EXISTS message_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                target_message_id INTEGER NOT NULL,
                target_memory_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                actor_key TEXT NOT NULL,
                actor_kind TEXT NOT NULL DEFAULT '',
                actor_user_id INTEGER,
                actor_username TEXT NOT NULL DEFAULT '',
                actor_chat_id INTEGER,
                reaction_asset_id INTEGER NOT NULL REFERENCES reaction_assets(id) ON DELETE CASCADE,
                reacted_at TEXT NOT NULL,
                UNIQUE(chat_id, target_message_id, actor_key, reaction_asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_message_reactions_actor
                ON message_reactions(chat_id, actor_user_id, actor_username, actor_key);
            CREATE INDEX IF NOT EXISTS idx_message_reactions_target
                ON message_reactions(chat_id, target_message_id);

            CREATE TABLE IF NOT EXISTS message_reaction_counts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                target_message_id INTEGER NOT NULL,
                target_memory_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
                reaction_asset_id INTEGER NOT NULL REFERENCES reaction_assets(id) ON DELETE CASCADE,
                total_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(chat_id, target_message_id, reaction_asset_id)
            );

            CREATE TABLE IF NOT EXISTS chat_reaction_semantics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                reaction_asset_id INTEGER NOT NULL REFERENCES reaction_assets(id) ON DELETE CASCADE,
                usage_summary_uk TEXT NOT NULL DEFAULT '',
                polarity TEXT NOT NULL DEFAULT 'unknown',
                tone_tags_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0,
                use_count INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NOT NULL,
                UNIQUE(chat_id, reaction_asset_id)
            );
            CREATE INDEX IF NOT EXISTS idx_chat_reaction_semantics_chat
                ON chat_reaction_semantics(chat_id, use_count, last_seen_at);
            """
        )
        self._conn.commit()

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM chat_reaction_semantics")
            self._conn.execute("DELETE FROM message_reaction_counts")
            self._conn.execute("DELETE FROM message_reactions")
            self._conn.execute("DELETE FROM message_reaction_events")
            self._conn.execute("DELETE FROM reaction_assets")
            self._conn.commit()

    def get_or_create_asset(self, spec: ReactionSpec) -> int:
        now = utc_now_text()
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM reaction_assets WHERE reaction_key = ?",
                (spec.reaction_key,),
            ).fetchone()
            if row is not None:
                self._conn.execute(
                    """
                    UPDATE reaction_assets
                    SET reaction_type = ?, custom_emoji_id = COALESCE(NULLIF(?, ''), custom_emoji_id),
                        base_emoji = COALESCE(NULLIF(?, ''), base_emoji), updated_at = ?
                    WHERE id = ?
                    """,
                    (spec.reaction_type, spec.custom_emoji_id, spec.base_emoji, now, int(row["id"])),
                )
                self._conn.commit()
                return int(row["id"])
            cursor = self._conn.execute(
                """
                INSERT INTO reaction_assets (
                    reaction_key, reaction_type, custom_emoji_id, base_emoji,
                    visual_summary_uk, inferred_meaning_uk, confidence,
                    analysis_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.reaction_key,
                    spec.reaction_type,
                    spec.custom_emoji_id,
                    spec.base_emoji,
                    self.default_visual_summary(spec),
                    self.default_inferred_meaning(spec),
                    0.25 if spec.reaction_type == "emoji" else 0.0,
                    "metadata_only" if spec.reaction_type != "custom_emoji" else "",
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def asset_by_key(self, reaction_key: str) -> ReactionAsset | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reaction_assets WHERE reaction_key = ? LIMIT 1",
                (reaction_key,),
            ).fetchone()
        return self._row_to_asset(row) if row is not None else None

    def asset_by_id(self, asset_id: int | None) -> ReactionAsset | None:
        if asset_id is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reaction_assets WHERE id = ? LIMIT 1",
                (int(asset_id),),
            ).fetchone()
        return self._row_to_asset(row) if row is not None else None

    def update_asset_metadata(
        self,
        reaction_key: str,
        *,
        file_id: str = "",
        file_unique_id: str = "",
        set_name: str = "",
        sticker_type: str = "",
        is_animated: bool = False,
        is_video: bool = False,
        base_emoji: str = "",
        raw_metadata: dict | None = None,
    ) -> None:
        now = utc_now_text()
        with self._lock:
            self._conn.execute(
                """
                UPDATE reaction_assets
                SET file_id = COALESCE(NULLIF(?, ''), file_id),
                    file_unique_id = COALESCE(NULLIF(?, ''), file_unique_id),
                    set_name = COALESCE(NULLIF(?, ''), set_name),
                    sticker_type = COALESCE(NULLIF(?, ''), sticker_type),
                    is_animated = ?,
                    is_video = ?,
                    base_emoji = COALESCE(NULLIF(?, ''), base_emoji),
                    raw_metadata_json = COALESCE(NULLIF(?, ''), raw_metadata_json),
                    updated_at = ?
                WHERE reaction_key = ?
                """,
                (
                    file_id,
                    file_unique_id,
                    set_name,
                    sticker_type,
                    1 if is_animated else 0,
                    1 if is_video else 0,
                    base_emoji,
                    json.dumps(raw_metadata or {}, ensure_ascii=False, default=str) if raw_metadata is not None else "",
                    now,
                    reaction_key,
                ),
            )
            self._conn.commit()

    def update_asset_media(
        self,
        reaction_key: str,
        *,
        local_media_path: str = "",
        thumbnail_path: str = "",
        mime_type: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE reaction_assets
                SET local_media_path = COALESCE(NULLIF(?, ''), local_media_path),
                    thumbnail_path = COALESCE(NULLIF(?, ''), thumbnail_path),
                    mime_type = COALESCE(NULLIF(?, ''), mime_type),
                    updated_at = ?
                WHERE reaction_key = ?
                """,
                (local_media_path, thumbnail_path, mime_type, utc_now_text(), reaction_key),
            )
            self._conn.commit()

    def update_asset_analysis(
        self,
        reaction_key: str,
        *,
        visual_summary_uk: str,
        inferred_meaning_uk: str,
        tone_tags: list[str] | tuple[str, ...] = (),
        confidence: float,
        model: str,
        prompt_version: str,
        input_hash: str,
        status: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE reaction_assets
                SET visual_summary_uk = ?,
                    inferred_meaning_uk = ?,
                    tone_tags_json = ?,
                    confidence = ?,
                    analysis_model = ?,
                    analysis_prompt_version = ?,
                    analysis_input_hash = ?,
                    analysis_status = ?,
                    analysis_updated_at = ?,
                    updated_at = ?
                WHERE reaction_key = ?
                """,
                (
                    visual_summary_uk or "",
                    inferred_meaning_uk or "",
                    json.dumps(list(tone_tags), ensure_ascii=False),
                    max(0.0, min(float(confidence), 1.0)),
                    model or "",
                    str(prompt_version or ""),
                    input_hash or "",
                    status or "",
                    utc_now_text(),
                    utc_now_text(),
                    reaction_key,
                ),
            )
            self._conn.commit()

    def mark_asset_stale(self, reaction_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reaction_assets SET analysis_status = 'stale', updated_at = ? WHERE reaction_key = ?",
                (utc_now_text(), reaction_key),
            )
            self._conn.commit()

    def asset_analysis_input_hash(self, asset: ReactionAsset) -> str:
        raw = "|".join(
            [
                asset.reaction_key,
                asset.custom_emoji_id,
                asset.file_unique_id,
                asset.base_emoji,
                asset.set_name,
                asset.local_media_path,
                asset.thumbnail_path,
            ]
        )
        return content_hash(raw)

    def asset_needs_analysis(self, asset: ReactionAsset, *, model: str, prompt_version: str) -> bool:
        if asset.reaction_type != "custom_emoji":
            return False
        input_hash = self.asset_analysis_input_hash(asset)
        stale = (
            asset.analysis_status not in {"analyzed", "metadata_only"}
            or asset.analysis_model != model
            or asset.analysis_prompt_version != str(prompt_version)
            or asset.analysis_input_hash != input_hash
        )
        if stale and asset.analysis_status:
            self.mark_asset_stale(asset.reaction_key)
        return stale

    def record_message_reaction_update(
        self,
        *,
        update_id: int | None,
        chat_id: int,
        target_message_id: int,
        target_memory_id: int | None,
        actor_key: str,
        actor_kind: str,
        actor_user_id: int | None,
        actor_username: str = "",
        actor_chat_id: int | None = None,
        old_specs: list[ReactionSpec],
        new_specs: list[ReactionSpec],
        received_at: datetime | str | None = None,
        raw_json: str = "",
    ) -> bool:
        old_asset_ids = [self.get_or_create_asset(spec) for spec in old_specs]
        new_asset_ids = [self.get_or_create_asset(spec) for spec in new_specs]
        timestamp = self._format_datetime(received_at)
        with self._lock:
            if update_id is not None:
                exists = self._conn.execute(
                    "SELECT 1 FROM message_reaction_events WHERE update_id = ?",
                    (int(update_id),),
                ).fetchone()
                if exists is not None:
                    return False
            self._conn.execute(
                """
                INSERT INTO message_reaction_events (
                    update_id, event_kind, chat_id, target_message_id, target_memory_id,
                    actor_key, actor_kind, actor_user_id, actor_username, actor_chat_id,
                    old_reactions_json, new_reactions_json, received_at, raw_json
                ) VALUES (?, 'message_reaction', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    update_id,
                    chat_id,
                    target_message_id,
                    target_memory_id,
                    actor_key,
                    actor_kind,
                    actor_user_id,
                    actor_username or "",
                    actor_chat_id,
                    self._specs_json(old_specs),
                    self._specs_json(new_specs),
                    timestamp,
                    raw_json or "",
                ),
            )
            self._conn.execute(
                """
                DELETE FROM message_reactions
                WHERE chat_id = ? AND target_message_id = ? AND actor_key = ?
                """,
                (chat_id, target_message_id, actor_key),
            )
            for asset_id in new_asset_ids:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO message_reactions (
                        chat_id, target_message_id, target_memory_id, actor_key,
                        actor_kind, actor_user_id, actor_username, actor_chat_id,
                        reaction_asset_id, reacted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        target_message_id,
                        target_memory_id,
                        actor_key,
                        actor_kind,
                        actor_user_id,
                        actor_username or "",
                        actor_chat_id,
                        asset_id,
                        timestamp,
                    ),
                )
            self._conn.commit()
        return True

    def record_reaction_count_update(
        self,
        *,
        update_id: int | None,
        chat_id: int,
        target_message_id: int,
        target_memory_id: int | None,
        counts: list[tuple[ReactionSpec, int]],
        received_at: datetime | str | None = None,
        raw_json: str = "",
    ) -> bool:
        timestamp = self._format_datetime(received_at)
        specs = [spec for spec, _count in counts]
        asset_ids = [self.get_or_create_asset(spec) for spec in specs]
        with self._lock:
            if update_id is not None:
                exists = self._conn.execute(
                    "SELECT 1 FROM message_reaction_events WHERE update_id = ?",
                    (int(update_id),),
                ).fetchone()
                if exists is not None:
                    return False
            self._conn.execute(
                """
                INSERT INTO message_reaction_events (
                    update_id, event_kind, chat_id, target_message_id, target_memory_id,
                    old_reactions_json, new_reactions_json, received_at, raw_json
                ) VALUES (?, 'message_reaction_count', ?, ?, ?, '[]', ?, ?, ?)
                """,
                (update_id, chat_id, target_message_id, target_memory_id, self._specs_json(specs), timestamp, raw_json),
            )
            for (spec, total_count), asset_id in zip(counts, asset_ids):
                self._conn.execute(
                    """
                    INSERT INTO message_reaction_counts (
                        chat_id, target_message_id, target_memory_id, reaction_asset_id, total_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, target_message_id, reaction_asset_id)
                    DO UPDATE SET
                        target_memory_id = excluded.target_memory_id,
                        total_count = excluded.total_count,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, target_message_id, target_memory_id, asset_id, max(0, int(total_count)), timestamp),
                )
            self._conn.commit()
        return True

    def upsert_chat_semantics(
        self,
        *,
        chat_id: int,
        reaction_key: str,
        target_item: MemoryItem | None,
        count_increment: int = 1,
    ) -> int:
        asset = self.asset_by_key(reaction_key)
        if asset is None:
            return 0
        topic = item_topic_hint(target_item)
        usage_summary = f"used near: {topic}" if topic else "local meaning is still being learned from chat usage"
        confidence = 0.3 + min(0.35, max(0, int(count_increment)) * 0.03)
        now = utc_now_text()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT use_count
                FROM chat_reaction_semantics
                WHERE chat_id = ? AND reaction_asset_id = ?
                """,
                (chat_id, asset.id),
            ).fetchone()
            if row is None:
                use_count = max(0, int(count_increment))
                self._conn.execute(
                    """
                    INSERT INTO chat_reaction_semantics (
                        chat_id, reaction_asset_id, usage_summary_uk, confidence, use_count, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, asset.id, usage_summary, confidence, use_count, now),
                )
            else:
                use_count = int(row["use_count"]) + max(0, int(count_increment))
                self._conn.execute(
                    """
                    UPDATE chat_reaction_semantics
                    SET usage_summary_uk = ?,
                        confidence = max(confidence, ?),
                        use_count = ?,
                        last_seen_at = ?
                    WHERE chat_id = ? AND reaction_asset_id = ?
                    """,
                    (usage_summary, confidence, use_count, now, chat_id, asset.id),
                )
            self._conn.commit()
        return use_count

    def reaction_use_count(self, chat_id: int, reaction_key: str) -> int:
        asset = self.asset_by_key(reaction_key)
        if asset is None:
            return 0
        with self._lock:
            row = self._conn.execute(
                """
                SELECT use_count
                FROM chat_reaction_semantics
                WHERE chat_id = ? AND reaction_asset_id = ?
                """,
                (chat_id, asset.id),
            ).fetchone()
        return int(row["use_count"]) if row is not None else 0

    def user_preferences(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
        limit: int = 6,
    ) -> list[ReactionPreference]:
        conditions = ["r.chat_id = ?"]
        params: list[object] = [chat_id]
        username = username.strip().lstrip("@")
        identity_conditions: list[str] = []
        if user_id is not None:
            identity_conditions.append("r.actor_user_id = ?")
            params.append(user_id)
        if username:
            identity_conditions.append("(r.actor_username != '' AND lower(r.actor_username) = lower(?))")
            params.append(username)
        if not identity_conditions:
            return []
        conditions.append("(" + " OR ".join(identity_conditions) + ")")
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT a.*, r.reaction_asset_id, r.reacted_at,
                       s.usage_summary_uk, s.confidence AS semantic_confidence, m.text, m.source_text,
                       m.source_title, m.vision_summary, m.source_url
                FROM message_reactions r
                JOIN reaction_assets a ON a.id = r.reaction_asset_id
                LEFT JOIN chat_reaction_semantics s
                  ON s.chat_id = r.chat_id AND s.reaction_asset_id = r.reaction_asset_id
                LEFT JOIN messages m ON m.id = r.target_memory_id
                WHERE {" AND ".join(conditions)}
                ORDER BY datetime(r.reacted_at) DESC, r.id DESC
                LIMIT 1000
                """,
                tuple(params),
            ).fetchall()
        return self._preferences_from_rows(rows, limit)

    def group_preferences(self, chat_id: int, *, limit: int = 8) -> list[ReactionPreference]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.*, s.usage_summary_uk, s.confidence AS semantic_confidence,
                       s.use_count AS preference_count, s.last_seen_at
                FROM chat_reaction_semantics s
                JOIN reaction_assets a ON a.id = s.reaction_asset_id
                WHERE s.chat_id = ?
                ORDER BY s.use_count DESC, datetime(s.last_seen_at) DESC
                LIMIT ?
                """,
                (chat_id, max(1, int(limit))),
            ).fetchall()
        preferences: list[ReactionPreference] = []
        for row in rows:
            asset = self._row_to_asset(row)
            preferences.append(
                ReactionPreference(
                    reaction_key=asset.reaction_key,
                    reaction_type=asset.reaction_type,
                    base_emoji=asset.base_emoji,
                    visual_summary_uk=asset.visual_summary_uk,
                    inferred_meaning_uk=asset.inferred_meaning_uk,
                    usage_summary_uk=str(row["usage_summary_uk"] or ""),
                    tone_tags_json=asset.tone_tags_json,
                    count=int(row["preference_count"] or 0),
                    topics=(),
                    confidence=float(row["semantic_confidence"] or asset.confidence or 0),
                    last_seen_at=str(row["last_seen_at"] or ""),
                )
            )
        return preferences

    def link_pending_targets(self, memory: MemoryStore, chat_id: int) -> int:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT target_message_id
                FROM message_reaction_events
                WHERE chat_id = ? AND target_memory_id IS NULL
                """,
                (chat_id,),
            ).fetchall()
            updated = 0
            for row in rows:
                item = memory.message_by_message_id(chat_id, int(row["target_message_id"]))
                if item is None:
                    continue
                self._conn.execute(
                    "UPDATE message_reaction_events SET target_memory_id = ? WHERE chat_id = ? AND target_message_id = ?",
                    (item.id, chat_id, item.message_id),
                )
                self._conn.execute(
                    "UPDATE message_reactions SET target_memory_id = ? WHERE chat_id = ? AND target_message_id = ?",
                    (item.id, chat_id, item.message_id),
                )
                self._conn.execute(
                    "UPDATE message_reaction_counts SET target_memory_id = ? WHERE chat_id = ? AND target_message_id = ?",
                    (item.id, chat_id, item.message_id),
                )
                updated += 1
            self._conn.commit()
        return updated

    def _preferences_from_rows(self, rows: list[sqlite3.Row], limit: int) -> list[ReactionPreference]:
        grouped: dict[int, dict[str, object]] = {}
        for row in rows:
            asset_id = int(row["reaction_asset_id"])
            group = grouped.setdefault(
                asset_id,
                {
                    "asset": self._row_to_asset(row),
                    "count": 0,
                    "topics": [],
                    "last_seen_at": "",
                    "usage_summary": str(row["usage_summary_uk"] or ""),
                    "semantic_confidence": float(row["semantic_confidence"] or 0),
                },
            )
            group["count"] = int(group["count"]) + 1
            group["last_seen_at"] = max(str(group["last_seen_at"] or ""), str(row["reacted_at"] or ""))
            topic = clip_text(
                str(row["text"] or row["source_title"] or row["vision_summary"] or row["source_text"] or row["source_url"] or ""),
                90,
            )
            if topic and topic not in group["topics"]:
                cast_topics = group["topics"]
                assert isinstance(cast_topics, list)
                cast_topics.append(topic)
        sorted_groups = sorted(grouped.values(), key=lambda value: (-int(value["count"]), str(value["last_seen_at"])), reverse=False)
        preferences: list[ReactionPreference] = []
        for group in sorted_groups[: max(1, int(limit))]:
            asset = group["asset"]
            assert isinstance(asset, ReactionAsset)
            topics = tuple(str(topic) for topic in list(group["topics"])[:3])
            preferences.append(
                ReactionPreference(
                    reaction_key=asset.reaction_key,
                    reaction_type=asset.reaction_type,
                    base_emoji=asset.base_emoji,
                    visual_summary_uk=asset.visual_summary_uk,
                    inferred_meaning_uk=asset.inferred_meaning_uk,
                    usage_summary_uk=str(group["usage_summary"] or ""),
                    tone_tags_json=asset.tone_tags_json,
                    count=int(group["count"]),
                    topics=topics,
                    confidence=max(float(group["semantic_confidence"] or 0), asset.confidence),
                    last_seen_at=str(group["last_seen_at"] or ""),
                )
            )
        return preferences

    @staticmethod
    def default_visual_summary(spec: ReactionSpec) -> str:
        if spec.reaction_type == "emoji" and spec.base_emoji:
            return f"standard Telegram emoji {spec.base_emoji}"
        if spec.reaction_type == "custom_emoji":
            return "custom Telegram emoji; visual meaning not analyzed yet"
        return spec.reaction_type

    @staticmethod
    def default_inferred_meaning(spec: ReactionSpec) -> str:
        if spec.reaction_type == "custom_emoji":
            return "local chat meaning is learned from usage and optional vision analysis"
        return "local chat meaning is learned from usage"

    @staticmethod
    def _format_datetime(value: datetime | str | None) -> str:
        if value is None:
            return utc_now_text()
        if isinstance(value, str):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _specs_json(specs: list[ReactionSpec]) -> str:
        return json.dumps([spec.__dict__ for spec in specs], ensure_ascii=False)

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> ReactionAsset:
        return ReactionAsset(
            id=int(row["id"]),
            reaction_key=str(row["reaction_key"] or ""),
            reaction_type=str(row["reaction_type"] or ""),
            custom_emoji_id=str(row["custom_emoji_id"] or ""),
            base_emoji=str(row["base_emoji"] or ""),
            file_id=str(row["file_id"] or ""),
            file_unique_id=str(row["file_unique_id"] or ""),
            set_name=str(row["set_name"] or ""),
            sticker_type=str(row["sticker_type"] or ""),
            is_animated=bool(row["is_animated"]),
            is_video=bool(row["is_video"]),
            local_media_path=str(row["local_media_path"] or ""),
            thumbnail_path=str(row["thumbnail_path"] or ""),
            mime_type=str(row["mime_type"] or ""),
            raw_metadata_json=str(row["raw_metadata_json"] or ""),
            visual_summary_uk=str(row["visual_summary_uk"] or ""),
            inferred_meaning_uk=str(row["inferred_meaning_uk"] or ""),
            tone_tags_json=str(row["tone_tags_json"] or "[]"),
            confidence=float(row["confidence"] or 0),
            analysis_model=str(row["analysis_model"] or ""),
            analysis_prompt_version=str(row["analysis_prompt_version"] or ""),
            analysis_input_hash=str(row["analysis_input_hash"] or ""),
            analysis_status=str(row["analysis_status"] or ""),
            analysis_updated_at=str(row["analysis_updated_at"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )
