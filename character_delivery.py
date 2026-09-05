"""Private, bounded prepared delivery for the grounded character command only."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Awaitable, Callable

TTL_SECONDS = 24 * 60 * 60
LEASE_SECONDS = 480
MAX_RESPONSES = 128
MAX_COMMANDS = 2048
MAX_CHUNKS = 8
MAX_TEXT_CHARS = 16000
SEND_TIMEOUT_SECONDS = 30
RECOVERY_NOTICE = "Відновлюю готову відповідь; попередня спроба могла дійти."
CONFIRMED_FAILURE_RECOVERY_NOTICE = "Відновлюю готову відповідь."


class CharacterDeliveryError(RuntimeError):
    """A sanitized failure category; never include response text."""


def _integer(value: int, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -(2**63) < value < 2**63:
        raise ValueError("invalid_numeric_scope")
    if positive and value <= 0:
        raise ValueError("invalid_numeric_scope")
    return value


@dataclass(frozen=True)
class CharacterDeliveryScope:
    chat_id: int
    topic_id: int
    requester_id: int
    target_id: int

    def __post_init__(self):
        _integer(self.chat_id)
        _integer(self.topic_id)
        _integer(self.requester_id, positive=True)
        _integer(self.target_id, positive=True)
        if self.topic_id < 0:
            raise ValueError("invalid_topic")

    @property
    def key(self) -> str:
        return json.dumps([self.chat_id, self.topic_id, self.requester_id, self.target_id], separators=(",", ":"))


@dataclass(frozen=True)
class PreparedCharacterResponse:
    id: str
    chunks: tuple[str, ...]
    attempted: tuple[int, ...]
    confirmed: tuple[int | None, ...]
    rejected: tuple[int, ...]
    expires_at: float


@dataclass(frozen=True)
class CharacterDeliveryClaim:
    status: str
    scope: CharacterDeliveryScope
    command_id: int
    token: str = ""
    prepared: PreparedCharacterResponse | None = None


class CharacterDeliveryStore:
    """Own tables in the actual memory database; never touch chat/profile tables.

    Transactions are synchronous and short: each durable marker commits before
    the caller can await Telegram. A lease prevents another process or coroutine
    from taking over an in-flight request. Hard crashes require lease expiry.
    """
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._conn = sqlite3.connect(self.path, timeout=1)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS character_delivery_responses (
                id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                claim_token TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                chunks_json TEXT NOT NULL,
                attempted_json TEXT NOT NULL DEFAULT '[]',
                rejected_json TEXT NOT NULL DEFAULT '[]',
                uncertain_json TEXT NOT NULL DEFAULT '{}',
                confirmed_json TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_character_delivery_scope
                ON character_delivery_responses(scope_key, complete, created_at);
            CREATE TABLE IF NOT EXISTS character_delivery_commands (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                scope_key TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS character_delivery_leases (
                scope_key TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
        """)

    def close(self) -> None:
        self._conn.close()

    def _cleanup(self, now: float) -> None:
        for table in ("character_delivery_responses", "character_delivery_commands", "character_delivery_leases"):
            self._conn.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (now,))

    def _begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _owned(self, claim: CharacterDeliveryClaim, now: float) -> None:
        row = self._conn.execute(
            "SELECT token, expires_at FROM character_delivery_leases WHERE scope_key=?", (claim.scope.key,)
        ).fetchone()
        if row is None or row["token"] != claim.token or row["expires_at"] <= now:
            raise CharacterDeliveryError("delivery_lease_lost")

    @staticmethod
    def _response(row) -> PreparedCharacterResponse:
        return PreparedCharacterResponse(row["id"], tuple(json.loads(row["chunks_json"])),
            tuple(json.loads(row["attempted_json"])), tuple(json.loads(row["confirmed_json"])),
            tuple(json.loads(row["rejected_json"])), row["expires_at"])

    def admit(self, scope: CharacterDeliveryScope, command_id: int, *, command_time: float) -> CharacterDeliveryClaim:
        _integer(command_id, positive=True)
        now = self.clock()
        # An old Telegram update cannot regenerate after its dedupe record expires.
        if not now - TTL_SECONDS < command_time <= now + 300:
            return CharacterDeliveryClaim("expired_command", scope, command_id)
        try:
            self._begin()
            self._cleanup(now)
            if self._conn.execute("SELECT 1 FROM character_delivery_commands WHERE chat_id=? AND message_id=?",
                                  (scope.chat_id, command_id)).fetchone():
                self._conn.commit()
                return CharacterDeliveryClaim("duplicate", scope, command_id)
            if self._conn.execute("SELECT count(*) FROM character_delivery_commands").fetchone()[0] >= MAX_COMMANDS:
                raise CharacterDeliveryError("command_capacity")
            self._conn.execute("INSERT INTO character_delivery_commands VALUES (?, ?, ?, ?)",
                               (scope.chat_id, command_id, scope.key, now + TTL_SECONDS))
            if self._conn.execute("SELECT 1 FROM character_delivery_leases WHERE scope_key=?", (scope.key,)).fetchone():
                self._conn.commit()
                return CharacterDeliveryClaim("busy", scope, command_id)
            row = self._conn.execute("""SELECT * FROM character_delivery_responses
                WHERE scope_key=? AND complete=0 ORDER BY created_at DESC, id DESC LIMIT 1""", (scope.key,)).fetchone()
            # Reserve capacity before the model can run. Other scopes' leases
            # count as reservations, even before their prepared response exists.
            if row is None:
                prepared = self._conn.execute("SELECT count(*) FROM character_delivery_responses").fetchone()[0]
                reserved = self._conn.execute("SELECT count(*) FROM character_delivery_leases").fetchone()[0]
                if prepared + reserved >= MAX_RESPONSES:
                    self._conn.commit()
                    return CharacterDeliveryClaim("capacity", scope, command_id)
            token = secrets.token_hex(16)
            self._conn.execute("INSERT INTO character_delivery_leases VALUES (?, ?, ?)",
                               (scope.key, token, now + LEASE_SECONDS))
            self._conn.commit()
            return CharacterDeliveryClaim("recovery" if row else "new", scope, command_id, token,
                                          self._response(row) if row else None)
        except BaseException:
            self._conn.rollback()
            raise

    def prepare(self, claim: CharacterDeliveryClaim, chunks: tuple[str, ...]) -> PreparedCharacterResponse:
        if not isinstance(chunks, tuple) or not 1 <= len(chunks) <= MAX_CHUNKS:
            raise CharacterDeliveryError("prepared_chunk_limit")
        if any(not isinstance(c, str) or not c or len(c) > 4096 or len(c.encode("utf-16-le")) // 2 > 4096 for c in chunks):
            raise CharacterDeliveryError("prepared_chunk_invalid")
        if sum(map(len, chunks)) > MAX_TEXT_CHARS:
            raise CharacterDeliveryError("prepared_text_limit")
        now = self.clock()
        identity = secrets.token_hex(16)
        try:
            self._begin()
            self._owned(claim, now)
            if self._conn.execute("SELECT 1 FROM character_delivery_responses WHERE claim_token=?", (claim.token,)).fetchone():
                raise CharacterDeliveryError("prepared_response_exists")
            if claim.status != "new" or self._conn.execute("SELECT 1 FROM character_delivery_responses WHERE scope_key=? AND complete=0 AND expires_at>?", (claim.scope.key, now)).fetchone():
                raise CharacterDeliveryError("prepared_response_exists")
            if self._conn.execute("SELECT count(*) FROM character_delivery_responses").fetchone()[0] >= MAX_RESPONSES:
                raise CharacterDeliveryError("response_capacity")
            self._conn.execute("INSERT INTO character_delivery_responses (id,scope_key,claim_token,created_at,expires_at,chunks_json,confirmed_json) VALUES (?,?,?,?,?,?,?)",
                (identity, claim.scope.key, claim.token, now, now + TTL_SECONDS, json.dumps(chunks, ensure_ascii=False), json.dumps([None] * len(chunks))))
            self._conn.commit()
            return PreparedCharacterResponse(identity, chunks, (), (None,) * len(chunks), (), now + TTL_SECONDS)
        except BaseException:
            self._conn.rollback()
            raise

    def _get(self, claim: CharacterDeliveryClaim, response_id: str, now: float):
        self._owned(claim, now)
        row = self._conn.execute("SELECT * FROM character_delivery_responses WHERE id=? AND scope_key=? AND expires_at>?",
                                 (response_id, claim.scope.key, now)).fetchone()
        if row is None:
            raise CharacterDeliveryError("prepared_response_expired")
        return row

    def fence(self, claim: CharacterDeliveryClaim) -> None:
        """Renew only a still-owned lease before a bounded external operation."""
        try:
            self._begin()
            now = self.clock()
            self._owned(claim, now)
            self._conn.execute("UPDATE character_delivery_leases SET expires_at=? WHERE scope_key=? AND token=?",
                               (now + LEASE_SECONDS, claim.scope.key, claim.token))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def attempted(self, claim: CharacterDeliveryClaim, response_id: str, index: int) -> None:
        try:
            self._begin()
            now = self.clock()
            row = self._get(claim, response_id, now)
            self._conn.execute("UPDATE character_delivery_leases SET expires_at=? WHERE scope_key=? AND token=?",
                               (now + LEASE_SECONDS, claim.scope.key, claim.token))
            confirmed = json.loads(row["confirmed_json"])
            if not 0 <= index < len(confirmed) or confirmed[index] is not None:
                raise CharacterDeliveryError("chunk_already_confirmed")
            attempted = set(json.loads(row["attempted_json"]))
            attempted.add(index)
            rejected = set(json.loads(row["rejected_json"])) - {index}
            uncertain = json.loads(row["uncertain_json"])
            uncertain[str(index)] = uncertain.get(str(index), 0) + 1
            self._conn.execute("UPDATE character_delivery_responses SET attempted_json=?,rejected_json=?,uncertain_json=? WHERE id=?",
                               (json.dumps(sorted(attempted)), json.dumps(sorted(rejected)), json.dumps(uncertain), response_id))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def rejected(self, claim: CharacterDeliveryClaim, response_id: str, index: int) -> None:
        """Only the host's definite Telegram rejection may clear ambiguity."""
        try:
            self._begin()
            row = self._get(claim, response_id, self.clock())
            if index not in json.loads(row["attempted_json"]):
                raise CharacterDeliveryError("chunk_not_attempted")
            uncertain = json.loads(row["uncertain_json"])
            count = uncertain.get(str(index), 0)
            if count <= 0:
                raise CharacterDeliveryError("chunk_not_awaiting_rejection")
            uncertain[str(index)] = count - 1
            rejected = set(json.loads(row["rejected_json"]))
            if count == 1:
                rejected.add(index)
            self._conn.execute("UPDATE character_delivery_responses SET rejected_json=?,uncertain_json=? WHERE id=?",
                               (json.dumps(sorted(rejected)), json.dumps(uncertain), response_id))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def confirm(self, claim: CharacterDeliveryClaim, response_id: str, index: int, message_id: int) -> None:
        _integer(message_id, positive=True)
        try:
            self._begin()
            row = self._get(claim, response_id, self.clock())
            confirmed = json.loads(row["confirmed_json"])
            if index not in json.loads(row["attempted_json"]) or confirmed[index] is not None:
                raise CharacterDeliveryError("chunk_not_awaiting_confirmation")
            confirmed[index] = message_id
            self._conn.execute("UPDATE character_delivery_responses SET confirmed_json=?,complete=? WHERE id=?",
                               (json.dumps(confirmed), int(all(v is not None for v in confirmed)), response_id))
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def release(self, claim: CharacterDeliveryClaim) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM character_delivery_leases WHERE scope_key=? AND token=?",
                               (claim.scope.key, claim.token))


async def deliver_prepared_character(store: CharacterDeliveryStore, claim: CharacterDeliveryClaim,
    prepared: PreparedCharacterResponse, send: Callable[[str], Awaitable[object]],
    permitted: Callable[[], bool], *, definite_rejections: tuple[type[Exception], ...] = ()) -> None:
    """One attempt per unconfirmed chunk per explicit command; never auto-retry."""
    if claim.status == "recovery":
        if not permitted():
            raise CharacterDeliveryError("permission_changed")
        store.fence(claim)
        ambiguous = any(i not in prepared.rejected and prepared.confirmed[i] is None for i in prepared.attempted)
        notice = RECOVERY_NOTICE if ambiguous else CONFIRMED_FAILURE_RECOVERY_NOTICE
        await asyncio.wait_for(send(notice), timeout=SEND_TIMEOUT_SECONDS)
    for index, chunk in enumerate(prepared.chunks):
        if prepared.confirmed[index] is not None:
            continue
        if not permitted():
            raise CharacterDeliveryError("permission_changed")
        store.attempted(claim, prepared.id, index)
        try:
            delivered = await asyncio.wait_for(send(chunk), timeout=SEND_TIMEOUT_SECONDS)
        except definite_rejections:
            store.rejected(claim, prepared.id, index)
            raise
        # Missing/invalid ACKs and ACK->DB failure leave the attempted marker
        # ambiguous. Only another new, authorized command can send this again.
        message_id = getattr(delivered, "message_id", None)
        store.confirm(claim, prepared.id, index, message_id)
