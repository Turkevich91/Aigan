from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


REMINDER_KINDS = {"one_off", "birthday", "custom"}
REMINDER_RECURRENCES = {"none", "yearly"}
DEFAULT_CLAIM_TTL_SECONDS = 900
DEFAULT_MAX_FIRE_ATTEMPTS = 3


@dataclass(frozen=True)
class Reminder:
    id: int
    chat_id: int
    created_by_user_id: int | None
    created_from_message_id: int | None
    target_user_id: int | None
    target_username: str
    target_label: str
    kind: str
    trusted_instruction: str
    source_message_id: int | None
    due_at_utc: str
    timezone: str
    recurrence: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReminderFire:
    id: int
    reminder_id: int
    scheduled_for_utc: str
    idempotency_key: str
    status: str
    claimed_at: str
    completed_at: str
    attempt_count: int
    sent_message_id: int | None
    failure_category: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ClaimedReminderFire:
    reminder: Reminder
    fire: ReminderFire


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_datetime(value: datetime | str | None = None) -> str:
    if value is None:
        value = utc_now()
    if isinstance(value, str):
        value = parse_datetime(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reminder_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def next_yearly_time(
    scheduled_for: datetime | str,
    after: datetime | str | None = None,
    *,
    timezone_name: str = "UTC",
) -> datetime:
    zone = reminder_timezone(timezone_name)
    candidate = parse_datetime(scheduled_for).astimezone(zone)
    boundary = parse_datetime(after or utc_now())
    while candidate.astimezone(timezone.utc) <= boundary:
        try:
            candidate = candidate.replace(year=candidate.year + 1)
        except ValueError:
            candidate = candidate.replace(month=2, day=28, year=candidate.year + 1)
    return candidate.astimezone(timezone.utc)


def normalize_kind(value: str) -> str:
    kind = (value or "custom").strip().lower().replace("-", "_")
    return kind if kind in REMINDER_KINDS else "custom"


def normalize_recurrence(value: str) -> str:
    recurrence = (value or "none").strip().lower().replace("-", "_")
    return recurrence if recurrence in REMINDER_RECURRENCES else "none"


class ReminderStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by_user_id INTEGER,
                created_from_message_id INTEGER,
                target_user_id INTEGER,
                target_username TEXT NOT NULL DEFAULT '',
                target_label TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'custom',
                trusted_instruction TEXT NOT NULL DEFAULT '',
                source_message_id INTEGER,
                due_at_utc TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                recurrence TEXT NOT NULL DEFAULT 'none',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_chat_status_due
                ON reminders(chat_id, status, due_at_utc, id);
            CREATE INDEX IF NOT EXISTS idx_reminders_creator
                ON reminders(chat_id, created_by_user_id, status, id);

            CREATE TABLE IF NOT EXISTS reminder_fires (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
                scheduled_for_utc TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                sent_message_id INTEGER,
                failure_category TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_fires_due
                ON reminder_fires(status, scheduled_for_utc, id);
            CREATE INDEX IF NOT EXISTS idx_reminder_fires_reminder
                ON reminder_fires(reminder_id, status, scheduled_for_utc, id);
            """
        )
        self._conn.commit()

    def create_reminder(
        self,
        *,
        chat_id: int,
        created_by_user_id: int | None,
        created_from_message_id: int | None,
        target_user_id: int | None = None,
        target_username: str = "",
        target_label: str = "",
        kind: str = "custom",
        trusted_instruction: str,
        source_message_id: int | None = None,
        due_at_utc: datetime | str,
        timezone_name: str = "UTC",
        recurrence: str = "none",
    ) -> Reminder:
        due = format_datetime(due_at_utc)
        now = format_datetime()
        kind = normalize_kind(kind)
        recurrence = "yearly" if kind == "birthday" else normalize_recurrence(recurrence)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO reminders (
                    chat_id, created_by_user_id, created_from_message_id,
                    target_user_id, target_username, target_label, kind,
                    trusted_instruction, source_message_id, due_at_utc,
                    timezone, recurrence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    int(chat_id),
                    created_by_user_id,
                    created_from_message_id,
                    target_user_id,
                    target_username or "",
                    target_label or "",
                    kind,
                    trusted_instruction or "",
                    source_message_id,
                    due,
                    timezone_name or "UTC",
                    recurrence,
                    now,
                    now,
                ),
            )
            reminder_id = int(cursor.lastrowid)
            self._create_fire_locked(reminder_id, due)
            self._conn.commit()
            reminder = self.reminder_by_id(reminder_id)
        if reminder is None:  # pragma: no cover - defensive only
            raise RuntimeError("created reminder could not be loaded")
        return reminder

    def reminder_by_id(self, reminder_id: int) -> Reminder | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (int(reminder_id),)).fetchone()
        return row_to_reminder(row) if row else None

    def fire_by_id(self, fire_id: int) -> ReminderFire | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminder_fires WHERE id = ?", (int(fire_id),)).fetchone()
        return row_to_fire(row) if row else None

    def list_reminders(self, chat_id: int, *, user_id: int | None = None, include_all: bool = False) -> list[Reminder]:
        if not include_all and user_id is None:
            return []
        conditions = ["chat_id = ?", "status IN ('active', 'paused')"]
        params: list[Any] = [int(chat_id)]
        if not include_all:
            conditions.append("created_by_user_id = ?")
            params.append(int(user_id))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM reminders
                WHERE {' AND '.join(conditions)}
                ORDER BY due_at_utc ASC, id ASC
                LIMIT 50
                """,
                params,
            ).fetchall()
        return [row_to_reminder(row) for row in rows]

    def cancel_reminder(self, chat_id: int, reminder_id: int, *, user_id: int | None, is_admin: bool = False) -> bool:
        if not is_admin and user_id is None:
            return False
        now = format_datetime()
        conditions = ["id = ?", "chat_id = ?", "status IN ('active', 'paused')"]
        params: list[Any] = [int(reminder_id), int(chat_id)]
        if not is_admin:
            conditions.append("created_by_user_id = ?")
            params.append(int(user_id))
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE reminders SET status = 'canceled', updated_at = ? WHERE {' AND '.join(conditions)}",
                [now, *params],
            )
            if cursor.rowcount:
                self._conn.execute(
                    """
                    UPDATE reminder_fires
                    SET status = 'canceled', completed_at = ?, updated_at = ?, failure_category = 'canceled'
                    WHERE reminder_id = ? AND status IN ('pending', 'claimed', 'needs_context')
                    """,
                    (now, now, int(reminder_id)),
                )
            self._conn.commit()
            return bool(cursor.rowcount)

    def claim_due_fires(
        self,
        *,
        now: datetime | str | None = None,
        limit: int = 5,
        misfire_grace_seconds: int = 86400,
        claim_ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS,
    ) -> list[ClaimedReminderFire]:
        now_dt = parse_datetime(now or utc_now())
        now_text = format_datetime(now_dt)
        grace = max(0, int(misfire_grace_seconds))
        cutoff_text = format_datetime(now_dt - timedelta(seconds=grace))
        claim_cutoff_text = format_datetime(now_dt - timedelta(seconds=max(1, int(claim_ttl_seconds))))
        claimed: list[ClaimedReminderFire] = []
        with self._lock:
            self._reclaim_stale_claims_locked(claim_cutoff_text, now_text)
            self._expire_misfires_locked(now_dt, cutoff_text)
            rows = self._conn.execute(
                """
                SELECT
                    r.id AS r_id, r.chat_id AS r_chat_id, r.created_by_user_id AS r_created_by_user_id,
                    r.created_from_message_id AS r_created_from_message_id, r.target_user_id AS r_target_user_id,
                    r.target_username AS r_target_username, r.target_label AS r_target_label, r.kind AS r_kind,
                    r.trusted_instruction AS r_trusted_instruction, r.source_message_id AS r_source_message_id,
                    r.due_at_utc AS r_due_at_utc, r.timezone AS r_timezone, r.recurrence AS r_recurrence,
                    r.status AS r_status, r.created_at AS r_created_at, r.updated_at AS r_updated_at,
                    f.id AS f_id, f.reminder_id AS f_reminder_id, f.scheduled_for_utc AS f_scheduled_for_utc,
                    f.idempotency_key AS f_idempotency_key, f.status AS f_status, f.claimed_at AS f_claimed_at,
                    f.completed_at AS f_completed_at, f.attempt_count AS f_attempt_count,
                    f.sent_message_id AS f_sent_message_id, f.failure_category AS f_failure_category,
                    f.created_at AS f_created_at, f.updated_at AS f_updated_at
                FROM reminder_fires f
                JOIN reminders r ON r.id = f.reminder_id
                WHERE f.status = 'pending'
                  AND r.status = 'active'
                  AND f.scheduled_for_utc <= ?
                ORDER BY f.scheduled_for_utc ASC, f.id ASC
                LIMIT ?
                """,
                (now_text, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                cursor = self._conn.execute(
                    """
                    UPDATE reminder_fires
                    SET status = 'claimed', claimed_at = ?, updated_at = ?, attempt_count = attempt_count + 1
                    WHERE id = ? AND status = 'pending'
                    """,
                    (now_text, now_text, int(row["f_id"])),
                )
                if not cursor.rowcount:
                    continue
                claimed.append(row_to_claim(row, status="claimed", claimed_at=now_text))
            self._conn.commit()
        return claimed

    def expire_context_requests(self, *, now: datetime | str | None = None, ttl_seconds: int = 86400) -> int:
        now_dt = parse_datetime(now or utc_now())
        cutoff = format_datetime(now_dt - timedelta(seconds=max(1, int(ttl_seconds))))
        now_text = format_datetime(now_dt)
        expired = 0
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT f.id, f.reminder_id, f.scheduled_for_utc, r.recurrence, r.timezone
                FROM reminder_fires f
                JOIN reminders r ON r.id = f.reminder_id
                WHERE f.status = 'needs_context' AND f.updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                self._complete_fire_locked(
                    int(row["id"]),
                    status="failed",
                    failure_category="context_timeout",
                    now_text=now_text,
                    sent_message_id=None,
                )
                if str(row["recurrence"]) == "yearly":
                    self._schedule_next_yearly_locked(
                        int(row["reminder_id"]),
                        row["scheduled_for_utc"],
                        row["timezone"],
                        now_dt,
                    )
                else:
                    self._fail_one_off_locked(int(row["reminder_id"]), now_text)
                expired += 1
            self._conn.commit()
        return expired

    def mark_sent(
        self,
        fire_id: int,
        *,
        expected_claimed_at: str,
        sent_message_id: int | None = None,
        now: datetime | str | None = None,
    ) -> None:
        self._finish_fire(
            fire_id,
            status="sent",
            failure_category="",
            sent_message_id=sent_message_id,
            now=now,
            expected_claimed_at=expected_claimed_at,
        )

    def mark_needs_context(
        self,
        fire_id: int,
        *,
        expected_claimed_at: str,
        category: str = "insufficient_context",
        now: datetime | str | None = None,
    ) -> None:
        now_text = format_datetime(now or utc_now())
        with self._lock:
            self._conn.execute(
                """
                UPDATE reminder_fires
                SET status = 'needs_context', failure_category = ?, updated_at = ?
                WHERE id = ? AND status = 'claimed' AND claimed_at = ?
                """,
                (category, now_text, int(fire_id), str(expected_claimed_at)),
            )
            self._conn.commit()

    def is_claim_current(self, fire_id: int, *, expected_claimed_at: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM reminder_fires
                WHERE id = ? AND status = 'claimed' AND claimed_at = ?
                LIMIT 1
                """,
                (int(fire_id), str(expected_claimed_at)),
            ).fetchone()
        return row is not None

    def refresh_claim(
        self,
        fire_id: int,
        *,
        expected_claimed_at: str,
        now: datetime | str | None = None,
    ) -> str | None:
        now_text = format_datetime(now or utc_now())
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE reminder_fires
                SET claimed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'claimed' AND claimed_at = ?
                """,
                (now_text, now_text, int(fire_id), str(expected_claimed_at)),
            )
            self._conn.commit()
        return now_text if cursor.rowcount else None

    def resolve_context_request(
        self,
        chat_id: int,
        *,
        reminder_id: int,
        user_id: int | None,
        clarification: str,
        now: datetime | str | None = None,
    ) -> int | None:
        text = " ".join((clarification or "").split())
        if not text:
            return None
        if user_id is None:
            return None
        now_text = format_datetime(now or utc_now())
        conditions = ["r.chat_id = ?", "r.id = ?", "r.status = 'active'", "f.status = 'needs_context'"]
        params: list[Any] = [int(chat_id), int(reminder_id)]
        conditions.append("r.created_by_user_id = ?")
        params.append(int(user_id))
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT f.id AS fire_id, r.id AS reminder_id, r.trusted_instruction AS trusted_instruction
                FROM reminder_fires f
                JOIN reminders r ON r.id = f.reminder_id
                WHERE {' AND '.join(conditions)}
                ORDER BY f.updated_at DESC, f.id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            instruction = str(row["trusted_instruction"] or "").strip()
            updated_instruction = f"{instruction}\nClarification: {text}" if instruction else text
            self._conn.execute(
                """
                UPDATE reminders
                SET trusted_instruction = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (updated_instruction, now_text, int(row["reminder_id"])),
            )
            self._conn.execute(
                """
                UPDATE reminder_fires
                SET status = 'pending', claimed_at = '', updated_at = ?, failure_category = 'context_resolved'
                WHERE id = ? AND status = 'needs_context'
                """,
                (now_text, int(row["fire_id"])),
            )
            self._conn.commit()
            return int(row["reminder_id"])

    def mark_failed(
        self,
        fire_id: int,
        *,
        expected_claimed_at: str,
        category: str = "failed",
        now: datetime | str | None = None,
        max_attempts: int = DEFAULT_MAX_FIRE_ATTEMPTS,
    ) -> None:
        now_text = format_datetime(now or utc_now())
        with self._lock:
            row = self._conn.execute(
                """
                SELECT attempt_count FROM reminder_fires
                WHERE id = ? AND status = 'claimed' AND claimed_at = ?
                """,
                (int(fire_id), str(expected_claimed_at)),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return
            if int(row["attempt_count"]) < max(1, int(max_attempts)):
                cursor = self._conn.execute(
                    """
                    UPDATE reminder_fires
                    SET status = 'pending', claimed_at = '', updated_at = ?, failure_category = ?
                    WHERE id = ? AND status = 'claimed' AND claimed_at = ?
                    """,
                    (now_text, category, int(fire_id), str(expected_claimed_at)),
                )
                self._conn.commit()
                if not cursor.rowcount:
                    return
                return
        self._finish_fire(
            fire_id,
            status="failed",
            failure_category=category,
            sent_message_id=None,
            now=now_text,
            expected_claimed_at=expected_claimed_at,
        )

    def mark_skipped_unsafe(
        self,
        fire_id: int,
        *,
        expected_claimed_at: str,
        category: str = "model_skip",
        now: datetime | str | None = None,
    ) -> None:
        self._finish_fire(
            fire_id,
            status="skipped_unsafe",
            failure_category=category,
            sent_message_id=None,
            now=now,
            expected_claimed_at=expected_claimed_at,
        )

    def health_summary(self) -> dict[str, Any]:
        with self._lock:
            active = self._scalar("SELECT COUNT(*) FROM reminders WHERE status = 'active'")
            pending = self._scalar("SELECT COUNT(*) FROM reminder_fires WHERE status = 'pending'")
            claimed = self._scalar("SELECT COUNT(*) FROM reminder_fires WHERE status = 'claimed'")
            needs_context = self._scalar("SELECT COUNT(*) FROM reminder_fires WHERE status = 'needs_context'")
            failed = self._scalar("SELECT COUNT(*) FROM reminder_fires WHERE status = 'failed'")
            next_row = self._conn.execute(
                """
                SELECT scheduled_for_utc FROM reminder_fires
                WHERE status = 'pending'
                ORDER BY scheduled_for_utc ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
        return {
            "active": active,
            "pending": pending,
            "claimed": claimed,
            "needs_context": needs_context,
            "failed": failed,
            "next_due_set": bool(next_row),
        }

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM reminder_fires")
            self._conn.execute("DELETE FROM reminders")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _finish_fire(
        self,
        fire_id: int,
        *,
        status: str,
        failure_category: str,
        sent_message_id: int | None,
        now: datetime | str | None,
        expected_claimed_at: str,
    ) -> None:
        now_dt = parse_datetime(now or utc_now())
        now_text = format_datetime(now_dt)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT f.reminder_id, f.scheduled_for_utc, r.recurrence, r.timezone
                FROM reminder_fires f
                JOIN reminders r ON r.id = f.reminder_id
                WHERE f.id = ?
                """,
                (int(fire_id),),
            ).fetchone()
            if row is None:
                return
            completed = self._complete_fire_locked(
                int(fire_id),
                expected_status="claimed",
                status=status,
                failure_category=failure_category,
                now_text=now_text,
                sent_message_id=sent_message_id,
                expected_claimed_at=expected_claimed_at,
            )
            if not completed:
                self._conn.commit()
                return
            if str(row["recurrence"]) == "yearly":
                self._schedule_next_yearly_locked(
                    int(row["reminder_id"]),
                    row["scheduled_for_utc"],
                    row["timezone"],
                    now_dt,
                )
            elif status == "failed":
                self._fail_one_off_locked(int(row["reminder_id"]), now_text)
            else:
                self._complete_one_off_locked(int(row["reminder_id"]), now_text)
            self._conn.commit()

    def _reclaim_stale_claims_locked(self, claim_cutoff_text: str, now_text: str) -> None:
        self._conn.execute(
            """
            UPDATE reminder_fires
            SET status = 'pending',
                claimed_at = '',
                updated_at = ?,
                failure_category = 'claim_reclaimed'
            WHERE status = 'claimed'
              AND claimed_at != ''
              AND claimed_at < ?
            """,
            (now_text, claim_cutoff_text),
        )

    def _expire_misfires_locked(self, now_dt: datetime, cutoff_text: str) -> None:
        now_text = format_datetime(now_dt)
        rows = self._conn.execute(
            """
            SELECT f.id, f.reminder_id, f.scheduled_for_utc, r.recurrence, r.timezone
            FROM reminder_fires f
            JOIN reminders r ON r.id = f.reminder_id
            WHERE f.status = 'pending'
              AND r.status = 'active'
              AND f.scheduled_for_utc < ?
            """,
            (cutoff_text,),
        ).fetchall()
        for row in rows:
            completed = self._complete_fire_locked(
                int(row["id"]),
                expected_status="pending",
                status="failed",
                failure_category="misfire_expired",
                now_text=now_text,
                sent_message_id=None,
            )
            if not completed:
                continue
            if str(row["recurrence"]) == "yearly":
                self._schedule_next_yearly_locked(
                    int(row["reminder_id"]),
                    row["scheduled_for_utc"],
                    row["timezone"],
                    now_dt,
                )
            else:
                self._fail_one_off_locked(int(row["reminder_id"]), now_text)

    def _complete_fire_locked(
        self,
        fire_id: int,
        *,
        expected_status: str,
        status: str,
        failure_category: str,
        now_text: str,
        sent_message_id: int | None,
        expected_claimed_at: str | None = None,
    ) -> bool:
        conditions = ["id = ?", "status = ?"]
        params: list[Any] = [
            status,
            now_text,
            now_text,
            failure_category or "",
            sent_message_id,
            int(fire_id),
            str(expected_status),
        ]
        if expected_claimed_at is not None:
            conditions.append("claimed_at = ?")
            params.append(str(expected_claimed_at))
        cursor = self._conn.execute(
            f"""
            UPDATE reminder_fires
            SET status = ?, completed_at = ?, updated_at = ?, failure_category = ?, sent_message_id = ?
            WHERE {' AND '.join(conditions)}
            """,
            params,
        )
        return bool(cursor.rowcount)

    def _complete_one_off_locked(self, reminder_id: int, now_text: str) -> None:
        self._conn.execute(
            """
            UPDATE reminders
            SET status = 'completed', updated_at = ?
            WHERE id = ? AND recurrence = 'none'
            """,
            (now_text, int(reminder_id)),
        )

    def _fail_one_off_locked(self, reminder_id: int, now_text: str) -> None:
        self._conn.execute(
            """
            UPDATE reminders
            SET status = 'failed', updated_at = ?
            WHERE id = ? AND recurrence = 'none'
            """,
            (now_text, int(reminder_id)),
        )

    def _schedule_next_yearly_locked(
        self,
        reminder_id: int,
        scheduled_for_utc: str,
        timezone_name: str,
        now_dt: datetime,
    ) -> None:
        next_due = format_datetime(
            next_yearly_time(scheduled_for_utc, after=now_dt, timezone_name=timezone_name)
        )
        now_text = format_datetime(now_dt)
        self._conn.execute(
            """
            UPDATE reminders
            SET due_at_utc = ?, status = 'active', updated_at = ?
            WHERE id = ? AND recurrence = 'yearly'
            """,
            (next_due, now_text, int(reminder_id)),
        )
        self._create_fire_locked(reminder_id, next_due)

    def _create_fire_locked(self, reminder_id: int, scheduled_for_utc: str) -> None:
        scheduled = format_datetime(scheduled_for_utc)
        key = f"{int(reminder_id)}:{scheduled}"
        now = format_datetime()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO reminder_fires (
                reminder_id, scheduled_for_utc, idempotency_key, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (int(reminder_id), scheduled, key, now, now),
        )

    def _scalar(self, query: str) -> int:
        row = self._conn.execute(query).fetchone()
        return int(row[0]) if row else 0


def row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        created_by_user_id=row["created_by_user_id"],
        created_from_message_id=row["created_from_message_id"],
        target_user_id=row["target_user_id"],
        target_username=str(row["target_username"] or ""),
        target_label=str(row["target_label"] or ""),
        kind=str(row["kind"] or "custom"),
        trusted_instruction=str(row["trusted_instruction"] or ""),
        source_message_id=row["source_message_id"],
        due_at_utc=str(row["due_at_utc"]),
        timezone=str(row["timezone"] or "UTC"),
        recurrence=str(row["recurrence"] or "none"),
        status=str(row["status"] or "active"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def row_to_fire(row: sqlite3.Row) -> ReminderFire:
    return ReminderFire(
        id=int(row["id"]),
        reminder_id=int(row["reminder_id"]),
        scheduled_for_utc=str(row["scheduled_for_utc"]),
        idempotency_key=str(row["idempotency_key"]),
        status=str(row["status"]),
        claimed_at=str(row["claimed_at"] or ""),
        completed_at=str(row["completed_at"] or ""),
        attempt_count=int(row["attempt_count"] or 0),
        sent_message_id=row["sent_message_id"],
        failure_category=str(row["failure_category"] or ""),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def row_to_claim(row: sqlite3.Row, *, status: str, claimed_at: str) -> ClaimedReminderFire:
    reminder_values = {
        "id": row["r_id"],
        "chat_id": row["r_chat_id"],
        "created_by_user_id": row["r_created_by_user_id"],
        "created_from_message_id": row["r_created_from_message_id"],
        "target_user_id": row["r_target_user_id"],
        "target_username": row["r_target_username"],
        "target_label": row["r_target_label"],
        "kind": row["r_kind"],
        "trusted_instruction": row["r_trusted_instruction"],
        "source_message_id": row["r_source_message_id"],
        "due_at_utc": row["r_due_at_utc"],
        "timezone": row["r_timezone"],
        "recurrence": row["r_recurrence"],
        "status": row["r_status"],
        "created_at": row["r_created_at"],
        "updated_at": row["r_updated_at"],
    }
    fire_values = {
        "id": row["f_id"],
        "reminder_id": row["f_reminder_id"],
        "scheduled_for_utc": row["f_scheduled_for_utc"],
        "idempotency_key": row["f_idempotency_key"],
        "status": status,
        "claimed_at": claimed_at,
        "completed_at": row["f_completed_at"],
        "attempt_count": int(row["f_attempt_count"] or 0) + 1,
        "sent_message_id": row["f_sent_message_id"],
        "failure_category": row["f_failure_category"],
        "created_at": row["f_created_at"],
        "updated_at": claimed_at,
    }
    return ClaimedReminderFire(
        reminder=Reminder(**reminder_values),
        fire=ReminderFire(**fire_values),
    )
