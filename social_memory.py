from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memory import MemoryItem, MemoryStore


@dataclass(frozen=True)
class SocialObservation:
    id: int
    chat_id: int
    scope: str
    user_id: int | None
    username: str
    sender_label: str
    kind: str
    topic: str
    normalized_topic: str
    polarity: str
    intensity: float
    confidence: float
    evidence_summary: str
    source_message_id: int | None
    source_memory_id: int | None
    created_at: str
    last_seen_at: str
    occurrences: int
    active: bool


@dataclass(frozen=True)
class SocialObservationDraft:
    scope: str
    kind: str
    topic: str
    polarity: str
    intensity: float
    confidence: float
    evidence_summary: str


URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
MENTION_RE = re.compile(r"@[A-Za-z0-9_]{2,64}")
COMMAND_RE = re.compile(r"(?<!\S)/[\w\u0400-\u04ff\u02bc]+(?:@[A-Za-z0-9_]+)?", re.IGNORECASE)
WORD_RE = re.compile(r"[A-Za-z\u0400-\u04ff][A-Za-z\u0400-\u04ff0-9_+-]{1,40}")
NOISE_RE = re.compile(r"\s+")
SENSITIVE_RE = re.compile(
    r"\b("
    r"депрес|псих|діагноз|здоров|хвор|травм|суїцид|самогуб|"
    r"religion|sexual|gender|orientation|health|diagnosis|trauma|suicide|"
    r"релігі|сексуаль|гендер|орієнтац|національн|етніч"
    r")\b",
    re.IGNORECASE,
)
LAUGH_RE = re.compile(r"\b(ахах+|хах+|lol|lmao|rofl|кек|ор|угар|смішн|ржач)\b", re.IGNORECASE)
QUESTION_RE = re.compile(r"\?")

LIKE_PATTERNS = (
    re.compile(
        r"\b(?:мені|мне|я)\s+(?:подобається|цікаво|люблю|обожнюю|нравится|интересно|like|love)\s+(?P<topic>[^.!?\n]{2,120})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<topic>[^.!?\n]{2,80})\s+(?:топ|клас|імба|кайф|годно|цікаво|прикольно)\b",
        re.IGNORECASE,
    ),
)
DISLIKE_PATTERNS = (
    re.compile(
        r"\b(?:мене|мне|я)\s+(?:бісить|дратує|задовбало|раздражает|ненавиджу|не люблю|hate|dislike)\s+(?P<topic>[^.!?\n]{2,120})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:не подобається|не нравиться|не нравится)\s+(?P<topic>[^.!?\n]{2,120})",
        re.IGNORECASE,
    ),
)
IRRITATION_PATTERNS = (
    re.compile(
        r"\b(?:бісить|дратує|задовбало|бомбить|палає|тригерить|раздражает|горит)\b(?:\s+(?P<topic>[^.!?\n]{2,120}))?",
        re.IGNORECASE,
    ),
)
AVOID_PATTERNS = (
    re.compile(
        r"\b(?:не хочу|не треба|не лізь|обійдемось без|skip|краще без)\s+(?P<topic>[^.!?\n]{2,120})",
        re.IGNORECASE,
    ),
)

STOP_TOPICS = {
    "це",
    "это",
    "тут",
    "там",
    "мені",
    "мне",
    "дуже",
    "очень",
    "просто",
    "really",
    "this",
    "that",
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_topic(topic: str) -> str:
    value = URL_RE.sub(" ", topic or "")
    value = MENTION_RE.sub(" ", value)
    value = COMMAND_RE.sub(" ", value)
    value = re.sub(r"[<>{}\[\]()`*_#|]", " ", value)
    value = NOISE_RE.sub(" ", value).strip(" -:;,.\t\r\n").casefold()
    words = [word for word in WORD_RE.findall(value) if word.casefold() not in STOP_TOPICS]
    if not words:
        return ""
    return " ".join(words[:8])


def display_topic(topic: str) -> str:
    normalized = normalize_topic(topic)
    if not normalized:
        return ""
    return normalized[:80].strip()


def sanitize_evidence(text: str, limit: int = 180) -> str:
    value = URL_RE.sub("[link]", text or "")
    value = MENTION_RE.sub("@user", value)
    value = COMMAND_RE.sub("[command]", value)
    value = NOISE_RE.sub(" ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 12].rstrip() + " [trimmed]"


def is_sensitive_topic(text: str) -> bool:
    return bool(SENSITIVE_RE.search(text or ""))


def topic_from_question(text: str) -> str:
    words = [word.casefold() for word in WORD_RE.findall(text) if word.casefold() not in STOP_TOPICS]
    return " ".join(words[:6])


def extract_observation_drafts(text: str, *, confidence_threshold: float = 0.65) -> list[SocialObservationDraft]:
    cleaned = sanitize_evidence(text, 500)
    if not cleaned or is_sensitive_topic(cleaned):
        return []

    drafts: list[SocialObservationDraft] = []

    def add(kind: str, raw_topic: str, polarity: str, intensity: float, confidence: float, evidence: str) -> None:
        topic = display_topic(raw_topic)
        if not topic or confidence < confidence_threshold:
            return
        drafts.append(
            SocialObservationDraft(
                scope="user",
                kind=kind,
                topic=topic,
                polarity=polarity,
                intensity=max(0.0, min(float(intensity), 1.0)),
                confidence=max(0.0, min(float(confidence), 1.0)),
                evidence_summary=sanitize_evidence(evidence),
            )
        )

    for pattern in LIKE_PATTERNS:
        for match in pattern.finditer(cleaned):
            add("interest_like", match.group("topic"), "positive", 0.65, 0.82, "participant signaled interest")

    for pattern in DISLIKE_PATTERNS:
        for match in pattern.finditer(cleaned):
            add("dislike", match.group("topic"), "negative", 0.7, 0.82, "participant signaled dislike")

    for pattern in IRRITATION_PATTERNS:
        for match in pattern.finditer(cleaned):
            topic = match.groupdict().get("topic") or topic_from_question(cleaned) or cleaned
            add("irritation", topic, "negative", 0.8, 0.78, "participant sounded irritated by this topic")

    for pattern in AVOID_PATTERNS:
        for match in pattern.finditer(cleaned):
            add("avoided_topic", match.group("topic"), "negative", 0.75, 0.76, "participant pushed this topic away")

    if LAUGH_RE.search(cleaned):
        topic = topic_from_question(cleaned) or cleaned
        add("amusement", topic, "positive", 0.55, 0.7, "participant reacted with amusement")

    if QUESTION_RE.search(cleaned):
        topic = topic_from_question(cleaned)
        if topic:
            add("recurring_question", topic, "neutral", 0.45, 0.66, "participant asked about this")

    unique: dict[tuple[str, str], SocialObservationDraft] = {}
    for draft in drafts:
        key = (draft.kind, normalize_topic(draft.topic))
        current = unique.get(key)
        if current is None or draft.confidence > current.confidence:
            unique[key] = draft
    return list(unique.values())


class SocialMemoryStore:
    def __init__(self, db_path: Path | str, retention_days: int = 180) -> None:
        self.db_path = Path(db_path)
        self.retention_days = max(1, int(retention_days))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS social_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                scope TEXT NOT NULL,
                user_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                sender_label TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                topic TEXT NOT NULL,
                normalized_topic TEXT NOT NULL,
                polarity TEXT NOT NULL,
                intensity REAL NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                evidence_summary TEXT NOT NULL DEFAULT '',
                source_message_id INTEGER,
                source_memory_id INTEGER,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_social_observations_chat_scope
                ON social_observations(chat_id, scope, active, last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_social_observations_user
                ON social_observations(chat_id, user_id, username, active);

            CREATE TABLE IF NOT EXISTS proactive_seeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                text_preview TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_proactive_seeds_chat
                ON proactive_seeds(chat_id, created_at);
            """
        )
        self._conn.commit()

    def record_from_item(self, item: MemoryItem, *, confidence_threshold: float = 0.65) -> int:
        if item.is_bot or not item.text.strip():
            return 0
        drafts = extract_observation_drafts(item.text, confidence_threshold=confidence_threshold)
        count = 0
        for draft in drafts:
            count += int(self.upsert_observation(item, draft))
            group_draft = SocialObservationDraft(
                scope="group",
                kind=draft.kind,
                topic=draft.topic,
                polarity=draft.polarity,
                intensity=draft.intensity,
                confidence=draft.confidence,
                evidence_summary=f"group signal: {draft.kind}",
            )
            count += int(self.upsert_observation(item, group_draft))
        return count

    def upsert_observation(self, item: MemoryItem, draft: SocialObservationDraft) -> bool:
        normalized = normalize_topic(draft.topic)
        if not normalized:
            return False
        now = utc_now_text()
        username = (item.username or "").strip().lstrip("@")
        sender_label = MemoryStore.base_sender_label(item.sender_label)
        user_id = item.user_id if draft.scope == "user" else None
        username = username if draft.scope == "user" else ""
        sender_label = sender_label if draft.scope == "user" else ""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, occurrences, confidence, intensity
                FROM social_observations
                WHERE chat_id = ?
                  AND scope = ?
                  AND kind = ?
                  AND normalized_topic = ?
                  AND active = 1
                  AND (
                    (? IS NOT NULL AND user_id = ?)
                    OR (? = '' AND username = '' AND sender_label = ?)
                    OR (? != '' AND lower(username) = lower(?))
                    OR (? != '' AND sender_label = ?)
                  )
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (
                    item.chat_id,
                    draft.scope,
                    draft.kind,
                    normalized,
                    user_id,
                    user_id,
                    username,
                    sender_label,
                    username,
                    username,
                    sender_label,
                    sender_label,
                ),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO social_observations (
                        chat_id, scope, user_id, username, sender_label, kind, topic, normalized_topic,
                        polarity, intensity, confidence, evidence_summary, source_message_id,
                        source_memory_id, created_at, last_seen_at, occurrences, active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
                    """,
                    (
                        item.chat_id,
                        draft.scope,
                        user_id,
                        username,
                        sender_label,
                        draft.kind,
                        draft.topic,
                        normalized,
                        draft.polarity,
                        draft.intensity,
                        draft.confidence,
                        draft.evidence_summary,
                        item.message_id,
                        item.id,
                        now,
                        item.created_at or now,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE social_observations
                    SET occurrences = occurrences + 1,
                        confidence = max(confidence, ?),
                        intensity = max(intensity, ?),
                        evidence_summary = ?,
                        source_message_id = ?,
                        source_memory_id = ?,
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        draft.confidence,
                        draft.intensity,
                        draft.evidence_summary,
                        item.message_id,
                        item.id,
                        item.created_at or now,
                        int(row["id"]),
                    ),
                )
            self._conn.commit()
        return True

    def group_observations(self, chat_id: int, limit: int = 12) -> list[SocialObservation]:
        return self._select_observations(
            """
            SELECT *
            FROM social_observations
            WHERE chat_id = ? AND scope = 'group' AND active = 1
            ORDER BY (occurrences * confidence * (1 + intensity)) DESC, last_seen_at DESC
            LIMIT ?
            """,
            (chat_id, max(1, int(limit))),
        )

    def user_observations(
        self,
        chat_id: int,
        *,
        user_id: int | None = None,
        username: str = "",
        label_aliases: tuple[str, ...] | list[str] = (),
        limit: int = 12,
    ) -> list[SocialObservation]:
        username = username.strip().lstrip("@")
        aliases = tuple(dict.fromkeys(MemoryStore.base_sender_label(alias) for alias in label_aliases if alias))
        conditions = ["chat_id = ?", "scope = 'user'", "active = 1"]
        params: list[object] = [chat_id]
        identity: list[str] = []
        if user_id is not None:
            identity.append("user_id = ?")
            params.append(user_id)
        if username:
            identity.append("(username != '' AND lower(username) = lower(?))")
            params.append(username)
        if aliases:
            placeholders = ",".join("?" for _ in aliases)
            identity.append(f"sender_label IN ({placeholders})")
            params.extend(aliases)
        if not identity:
            return []
        conditions.append("(" + " OR ".join(identity) + ")")
        params.append(max(1, int(limit)))
        return self._select_observations(
            f"""
            SELECT *
            FROM social_observations
            WHERE {' AND '.join(conditions)}
            ORDER BY (occurrences * confidence * (1 + intensity)) DESC, last_seen_at DESC
            LIMIT ?
            """,
            tuple(params),
        )

    def forget(
        self,
        chat_id: int,
        topic: str,
        *,
        user_id: int | None = None,
        username: str = "",
        label_aliases: tuple[str, ...] | list[str] = (),
    ) -> int:
        normalized = normalize_topic(topic)
        if not normalized:
            return 0
        conditions = ["chat_id = ?", "active = 1", "normalized_topic LIKE ?"]
        params: list[object] = [chat_id, f"%{normalized}%"]
        identity: list[str] = []
        if user_id is not None:
            identity.append("user_id = ?")
            params.append(user_id)
        username = username.strip().lstrip("@")
        if username:
            identity.append("(username != '' AND lower(username) = lower(?))")
            params.append(username)
        aliases = tuple(dict.fromkeys(MemoryStore.base_sender_label(alias) for alias in label_aliases if alias))
        if aliases:
            placeholders = ",".join("?" for _ in aliases)
            identity.append(f"sender_label IN ({placeholders})")
            params.extend(aliases)
        if identity:
            conditions.append("(" + " OR ".join(identity) + ")")
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE social_observations SET active = 0 WHERE {' AND '.join(conditions)}",
                tuple(params),
            )
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def clear_chat(self, chat_id: int) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM social_observations WHERE chat_id = ?", (chat_id,))
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM social_observations")
            self._conn.execute("DELETE FROM proactive_seeds")
            self._conn.commit()

    def cleanup(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute("DELETE FROM social_observations WHERE last_seen_at < ?", (cutoff,))
            self._conn.execute("DELETE FROM proactive_seeds WHERE created_at < ?", (cutoff,))
            self._conn.commit()
        return int(cursor.rowcount or 0)

    def save_seed(self, chat_id: int, *, direction: str, topic: str, text: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO proactive_seeds (chat_id, direction, topic, text_preview, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, direction, normalize_topic(topic), sanitize_evidence(text, 240), utc_now_text()),
            )
            self._conn.commit()

    def recent_seed_exists(self, chat_id: int, topic: str, *, cooldown_days: int) -> bool:
        normalized = normalize_topic(topic)
        if not normalized:
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(cooldown_days)))).isoformat(timespec="seconds")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM proactive_seeds
                WHERE chat_id = ?
                  AND created_at >= ?
                  AND topic = ?
                LIMIT 1
                """,
                (chat_id, cutoff, normalized),
            ).fetchone()
        return row is not None

    def _select_observations(self, sql: str, params: tuple[object, ...]) -> list[SocialObservation]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_observation(row) for row in rows]

    @staticmethod
    def _row_to_observation(row: sqlite3.Row) -> SocialObservation:
        return SocialObservation(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            scope=str(row["scope"]),
            user_id=int(row["user_id"]) if row["user_id"] is not None else None,
            username=str(row["username"] or ""),
            sender_label=str(row["sender_label"] or ""),
            kind=str(row["kind"]),
            topic=str(row["topic"]),
            normalized_topic=str(row["normalized_topic"]),
            polarity=str(row["polarity"]),
            intensity=float(row["intensity"]),
            confidence=float(row["confidence"]),
            evidence_summary=str(row["evidence_summary"] or ""),
            source_message_id=int(row["source_message_id"]) if row["source_message_id"] is not None else None,
            source_memory_id=int(row["source_memory_id"]) if row["source_memory_id"] is not None else None,
            created_at=str(row["created_at"]),
            last_seen_at=str(row["last_seen_at"]),
            occurrences=int(row["occurrences"]),
            active=bool(row["active"]),
        )
