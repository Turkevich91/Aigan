"""Durable neutral sticker recognition, independent of message-history retention.

The caller owns source authorization and recognition. This store never fetches
media or invokes a model. Its short SQLite claims span those external operations
without holding a database transaction open. A crash after a provider response
but before completion can require another paid attempt; billing is not exactly-once.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Callable, Iterator


REPRESENTATIONS = frozenset({"static_original", "thumbnail"})
TEXT_STATUSES = frozenset({"none", "readable", "partial", "unreadable"})
MAX_RESULT_BYTES = 40_000
_VERSION = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
_KEY = re.compile(r"sticker:(static_original|thumbnail):[A-Za-z0-9_.-]{1,80}:[a-f0-9]{64}\Z")
_FIELDS = frozenset({"summary", "visible_text", "text_status", "representation",
                     "model", "prompt_version", "content_sha256"})


def make_cache_key(file_unique_id: str, representation: str, version: str = "1") -> str:
    """Stable asset identity; download handles and producer models are not keys."""
    if (not isinstance(file_unique_id, str) or not file_unique_id.strip()
            or len(file_unique_id) > 1024 or representation not in REPRESENTATIONS
            or not isinstance(version, str) or _VERSION.fullmatch(version) is None):
        raise ValueError("invalid_sticker_cache_identity")
    material = json.dumps([file_unique_id, representation, version], ensure_ascii=True,
                          separators=(",", ":")).encode("utf-8")
    return f"sticker:{representation}:{version}:{hashlib.sha256(material).hexdigest()}"


def _representation(key: str) -> str:
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        raise ValueError("invalid_sticker_cache_key")
    return match.group(1)


def _result_json(result: dict, representation: str) -> str:
    """Validate the bounded schema, not the semantic accuracy of model observations."""
    if not isinstance(result, dict) or set(result) != _FIELDS:
        raise ValueError("invalid_sticker_recognition")
    if any(not isinstance(value, str) for value in result.values()):
        raise ValueError("invalid_sticker_recognition")
    if (not result["summary"].strip() or len(result["summary"]) > 3000
            or len(result["visible_text"]) > 6000
            or result["text_status"] not in TEXT_STATUSES
            or result["representation"] != representation
            or not result["model"].strip() or len(result["model"]) > 200
            or not result["prompt_version"].strip() or len(result["prompt_version"]) > 80
            or re.fullmatch(r"[a-fA-F0-9]{64}", result["content_sha256"]) is None):
        raise ValueError("invalid_sticker_recognition")
    if ((result["text_status"] in {"readable", "partial"})
            != bool(result["visible_text"].strip())):
        raise ValueError("invalid_sticker_text_status")
    normalized = dict(result, content_sha256=result["content_sha256"].lower())
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError("oversized_sticker_recognition")
    except UnicodeError as exc:
        raise ValueError("invalid_sticker_recognition") from exc
    return encoded


class StickerCache:
    """One durable asset table, with expiring ownership and bounded capacity."""

    def __init__(self, db_path: Path | str, max_entries: int = 10_000,
                 clock: Callable[[], float] = time.time):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("invalid_sticker_cache_capacity")
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.RLock()
        self._counters: Counter = Counter()
        self._conn = sqlite3.connect(db_path, timeout=5, isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""CREATE TABLE IF NOT EXISTS sticker_recognition_cache (
            cache_key TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('pending', 'ready', 'failed')),
            result_json TEXT NOT NULL DEFAULT '',
            owner TEXT NOT NULL DEFAULT '',
            lease_until REAL NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            last_access REAL NOT NULL
        )""")
        self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_sticker_recognition_lru
            ON sticker_recognition_cache(last_access, updated_at)""")

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise ValueError("invalid_sticker_cache_clock")
        return value

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def get(self, key: str) -> dict | None:
        representation = _representation(key)
        with self._transaction():
            row = self._conn.execute("SELECT state, result_json FROM sticker_recognition_cache WHERE cache_key=?",
                                     (key,)).fetchone()
            if row is None or row["state"] != "ready":
                self._counters["misses"] += 1
                return None
            try:
                if (not isinstance(row["result_json"], str)
                        or len(row["result_json"].encode("utf-8")) > MAX_RESULT_BYTES):
                    raise ValueError("oversized_sticker_recognition")
                result = json.loads(row["result_json"])
                _result_json(result, representation)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                self._conn.execute("DELETE FROM sticker_recognition_cache WHERE cache_key=?", (key,))
                self._counters["invalid_results"] += 1
                self._counters["misses"] += 1
                return None
            self._conn.execute("UPDATE sticker_recognition_cache SET last_access=? WHERE cache_key=?",
                               (self._now(), key))
            self._counters["hits"] += 1
            return result

    def state(self, key: str) -> str:
        """Content-free state for bounded waiting; expired claims are claimable."""
        _representation(key)
        with self._lock:
            row = self._conn.execute("SELECT state, lease_until, retry_at FROM sticker_recognition_cache WHERE cache_key=?",
                                     (key,)).fetchone()
            now = self._now()
            if row is None:
                return "missing"
            if row["state"] == "ready":
                return "ready"
            if row["state"] == "pending" and row["lease_until"] > now:
                return "busy"
            if row["state"] == "failed" and row["retry_at"] > now:
                return "retry"
            return "missing"

    def _make_room(self, now: float) -> bool:
        count = self._conn.execute("SELECT count(*) FROM sticker_recognition_cache").fetchone()[0]
        while count >= self._max_entries:
            row = self._conn.execute("""SELECT cache_key FROM sticker_recognition_cache
                WHERE state='ready' OR (state='failed' AND retry_at<=?)
                   OR (state='pending' AND lease_until<=?)
                ORDER BY last_access, updated_at, cache_key LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return False
            self._conn.execute("DELETE FROM sticker_recognition_cache WHERE cache_key=?", (row["cache_key"],))
            self._counters["evictions"] += 1
            count -= 1
        return True

    def claim(self, key: str, lease_seconds: float = 180) -> str | None:
        _representation(key)
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("invalid_sticker_cache_lease")
        with self._transaction():
            now = self._now()
            row = self._conn.execute("SELECT state, lease_until, retry_at FROM sticker_recognition_cache WHERE cache_key=?",
                                     (key,)).fetchone()
            if row is not None:
                if row["state"] == "ready":
                    return None
                if row["state"] == "pending" and row["lease_until"] > now:
                    self._counters["coalesced"] += 1
                    return None
                if row["state"] == "failed" and row["retry_at"] > now:
                    self._counters["backoff"] += 1
                    return None
            elif not self._make_room(now):
                self._counters["capacity_denied"] += 1
                return None
            owner = secrets.token_hex(16)
            self._conn.execute("""INSERT INTO sticker_recognition_cache
                (cache_key, state, owner, lease_until, created_at, updated_at, last_access)
                VALUES (?, 'pending', ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET state='pending', owner=excluded.owner,
                    lease_until=excluded.lease_until, retry_at=0, result_json='',
                    updated_at=excluded.updated_at, last_access=excluded.last_access""",
                (key, owner, now + lease_seconds, now, now, now))
            self._counters["claims"] += 1
            return owner

    def complete(self, key: str, owner: str, result: dict) -> bool:
        representation = _representation(key)
        try:
            encoded = _result_json(result, representation)
        except (ValueError, TypeError):
            with self._lock:
                self._counters["invalid_results"] += 1
            return False
        with self._transaction():
            now = self._now()
            changed = self._conn.execute("""UPDATE sticker_recognition_cache
                SET state='ready', result_json=?, owner='', lease_until=0, retry_at=0,
                    updated_at=?, last_access=?
                WHERE cache_key=? AND state='pending' AND owner=? AND lease_until>?""",
                (encoded, now, now, key, owner, now)).rowcount
            self._counters["completed" if changed else "stale_writes"] += 1
            return bool(changed)

    def fail(self, key: str, owner: str, retry_seconds: float = 30) -> bool:
        _representation(key)
        if not math.isfinite(retry_seconds) or retry_seconds < 0:
            raise ValueError("invalid_sticker_cache_retry")
        with self._transaction():
            now = self._now()
            changed = self._conn.execute("""UPDATE sticker_recognition_cache
                SET state='failed', result_json='', owner='', lease_until=0,
                    retry_at=?, updated_at=?, last_access=?
                WHERE cache_key=? AND state='pending' AND owner=? AND lease_until>?""",
                (now + retry_seconds, now, now, key, owner, now)).rowcount
            self._counters["failed" if changed else "stale_writes"] += 1
            return bool(changed)

    def invalidate(self, key: str) -> None:
        _representation(key)
        with self._transaction():
            changed = self._conn.execute("DELETE FROM sticker_recognition_cache WHERE cache_key=?", (key,)).rowcount
            self._counters["invalidated"] += changed

    def request_refresh(self, key: str) -> bool:
        """Forget a completed result without interrupting recognition or backoff."""
        _representation(key)
        with self._transaction():
            changed = self._conn.execute("""DELETE FROM sticker_recognition_cache
                WHERE cache_key=? AND state='ready'""", (key,)).rowcount
            self._counters["invalidated"] += changed
            return bool(changed)

    def record_recognition_call(self) -> None:
        """Count an actual provider attempt after validated pixels are available."""
        with self._lock:
            self._counters["recognition_calls"] += 1

    def stats(self) -> dict[str, int]:
        """Process counters plus durable row totals; never content or identifiers."""
        with self._lock:
            result = {name: self._counters[name] for name in (
                "hits", "misses", "claims", "coalesced", "backoff", "completed", "recognition_calls",
                "failed", "stale_writes", "invalid_results", "invalidated", "evictions", "capacity_denied")}
            counts = self._conn.execute("SELECT state, count(*) AS n FROM sticker_recognition_cache GROUP BY state").fetchall()
            for state in ("ready", "pending", "failed"):
                result[f"{state}_entries"] = next((row["n"] for row in counts if row["state"] == state), 0)
            result["entries"] = sum(row["n"] for row in counts)
            return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()
