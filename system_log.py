from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),
    re.compile(r"\b([A-Z][A-Z0-9_]*(?:TOKEN|SECRET|KEY|PASSWORD))=([^\s]+)", re.IGNORECASE),
]
MAX_TEXT_DETAIL = 500
REPORT_ATTEMPTED_SENTINEL = "urn:aigan:github-self-report-attempted"
REPORT_BLOCKING_TEMPERATURE = 2_147_483_647


@dataclass(frozen=True)
class SystemEvent:
    id: int
    created_at: str
    level: str
    component: str
    event_type: str
    chat_id: int | None
    user_id: int | None
    route: str
    duration_ms: int | None
    message: str
    details: dict[str, Any]


@dataclass(frozen=True)
class ComplaintCluster:
    id: int
    fingerprint: str
    category: str
    temperature: int
    first_seen: str
    last_seen: str
    sample: str
    github_issue_url: str
    last_reported_temperature: int


@dataclass(frozen=True)
class ComplaintReportClaim:
    fingerprint: str
    claim_id: str
    state: str
    attempted_at: str
    attempted_temperature: int
    issue_url: str
    failure_category: str
    completed_at: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_datetime(value: datetime | str | None = None) -> str:
    if value is None:
        value = utc_now()
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return utc_now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def redact_secrets(value: str) -> str:
    text = str(value)
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("\\b([A-Z]"):
            text = pattern.sub(lambda match: f"{match.group(1)}=[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def sanitize_text(value: str, limit: int = MAX_TEXT_DETAIL) -> str:
    text = " ".join(redact_secrets(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + " [trimmed]"


def sanitize_details(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = sanitize_text(str(key), 80)
            if any(secret in key_text.casefold() for secret in ("token", "secret", "password", "api_key", "apikey")):
                clean[key_text] = "[redacted]"
            else:
                clean[key_text] = sanitize_details(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_details(item) for item in list(value)[:20]]
    return sanitize_text(repr(value))


class SystemLogStore:
    def __init__(self, db_path: Path | str, retention_days: int = 14) -> None:
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
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                level TEXT NOT NULL DEFAULT 'info',
                component TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                chat_id INTEGER,
                user_id INTEGER,
                route TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER,
                message TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_system_events_created
                ON system_events(created_at, id);
            CREATE INDEX IF NOT EXISTS idx_system_events_level_created
                ON system_events(level, created_at, id);

            CREATE TABLE IF NOT EXISTS complaint_clusters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL DEFAULT 'general',
                temperature INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                sample TEXT NOT NULL DEFAULT '',
                github_issue_url TEXT NOT NULL DEFAULT '',
                last_reported_temperature INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_complaint_clusters_last_seen
                ON complaint_clusters(last_seen, id);

            CREATE TABLE IF NOT EXISTS complaint_report_claims (
                fingerprint TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('attempted', 'retryable', 'unknown', 'sent')),
                attempted_at TEXT NOT NULL,
                attempted_temperature INTEGER NOT NULL,
                issue_url TEXT NOT NULL DEFAULT '',
                failure_category TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO complaint_report_claims (
                fingerprint, claim_id, state, attempted_at, attempted_temperature,
                issue_url, failure_category, completed_at
            )
            SELECT
                fingerprint, lower(hex(randomblob(16))), 'sent', last_seen,
                last_reported_temperature, github_issue_url, '', last_seen
            FROM complaint_clusters
            WHERE github_issue_url != ''
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO complaint_report_claims (
                fingerprint, claim_id, state, attempted_at, attempted_temperature,
                issue_url, failure_category, completed_at
            )
            SELECT
                fingerprint, lower(hex(randomblob(16))), 'unknown', last_seen,
                last_reported_temperature, '', 'legacy_inconsistent_state', last_seen
            FROM complaint_clusters
            WHERE github_issue_url = '' AND last_reported_temperature > 0
            """
        )
        self._conn.execute(
            """
            UPDATE complaint_clusters
            SET github_issue_url = COALESCE(
                    NULLIF((
                        SELECT report.issue_url
                        FROM complaint_report_claims report
                        WHERE report.fingerprint = complaint_clusters.fingerprint
                    ), ''),
                    ?
                ),
                last_reported_temperature = ?
            WHERE EXISTS (
                SELECT 1 FROM complaint_report_claims report
                WHERE report.fingerprint = complaint_clusters.fingerprint
                  AND report.state IN ('attempted', 'unknown', 'sent')
            )
            """,
            (REPORT_ATTEMPTED_SENTINEL, REPORT_BLOCKING_TEMPERATURE),
        )
        self._conn.commit()

    def record_event(
        self,
        *,
        level: str = "info",
        component: str,
        event_type: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        route: str = "",
        duration_ms: int | None = None,
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> int:
        payload = json.dumps(sanitize_details(details or {}), ensure_ascii=False, sort_keys=True)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO system_events (
                    created_at, level, component, event_type, chat_id, user_id,
                    route, duration_ms, message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    format_datetime(),
                    normalize_level(level),
                    sanitize_text(component, 80),
                    sanitize_text(event_type, 120),
                    chat_id,
                    user_id,
                    sanitize_text(route, 80),
                    duration_ms,
                    sanitize_text(message, 300),
                    payload,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def latest_events(self, limit: int = 20) -> list[SystemEvent]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM system_events ORDER BY datetime(created_at) DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [row_to_event(row) for row in rows]

    def latest_event(
        self,
        *,
        component: str,
        event_type: str,
        chat_id: int | None = None,
        user_id: int | None = None,
        message: str = "",
    ) -> SystemEvent | None:
        conditions = ["component = ?", "event_type = ?"]
        params: list[Any] = [sanitize_text(component, 80), sanitize_text(event_type, 120)]
        if chat_id is not None:
            conditions.append("chat_id = ?")
            params.append(chat_id)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if message:
            conditions.append("message = ?")
            params.append(sanitize_text(message, 300))
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT *
                FROM system_events
                WHERE {" AND ".join(conditions)}
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return row_to_event(row) if row is not None else None

    def events_since(self, seconds: int, min_level: str = "info", limit: int = 200) -> list[SystemEvent]:
        cutoff = format_datetime(utc_now() - timedelta(seconds=max(1, int(seconds))))
        allowed = allowed_levels(min_level)
        placeholders = ",".join("?" for _ in allowed)
        params: list[Any] = [cutoff, *allowed, max(1, min(int(limit), 500))]
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM system_events
                WHERE created_at >= ? AND level IN ({placeholders})
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [row_to_event(row) for row in rows]

    def events_since_for_components(
        self,
        seconds: int,
        components: list[str] | tuple[str, ...] | set[str],
        min_level: str = "info",
        limit: int = 200,
        include_tool_details: bool = False,
    ) -> list[SystemEvent]:
        clean_components = sorted({sanitize_text(str(item), 80) for item in components if str(item or "").strip()})
        if not clean_components:
            return []
        cutoff = format_datetime(utc_now() - timedelta(seconds=max(1, int(seconds))))
        allowed = allowed_levels(min_level)
        level_placeholders = ",".join("?" for _ in allowed)
        component_placeholders = ",".join("?" for _ in clean_components)
        tool_details_filter = " OR details_json LIKE ?" if include_tool_details else ""
        params: list[Any] = [cutoff, *allowed, *clean_components]
        if include_tool_details:
            params.append('%"tool"%')
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM system_events
                WHERE created_at >= ?
                  AND level IN ({level_placeholders})
                  AND (component IN ({component_placeholders}){tool_details_filter})
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [row_to_event(row) for row in rows]

    def health_summary(self, lookback_seconds: int = 21600) -> dict[str, Any]:
        events = self.events_since(lookback_seconds, "info", 500)
        counts = {"info": 0, "warning": 0, "error": 0, "critical": 0}
        components: dict[str, int] = {}
        recent_problems: list[str] = []
        for event in events:
            counts[event.level] = counts.get(event.level, 0) + 1
            if event.level in {"warning", "error", "critical"}:
                components[event.component] = components.get(event.component, 0) + 1
                if len(recent_problems) < 6:
                    recent_problems.append(f"{event.created_at} {event.component}/{event.event_type}: {event.message}")
        status = "ok"
        if counts["error"] or counts["critical"]:
            status = "failing" if counts["error"] + counts["critical"] >= 3 else "degraded"
        elif counts["warning"]:
            status = "degraded"
        return {
            "status": status,
            "lookback_seconds": lookback_seconds,
            "counts": counts,
            "top_components": sorted(components.items(), key=lambda item: (-item[1], item[0]))[:5],
            "recent_problems": recent_problems,
            "active_complaints": [cluster_to_dict(item) for item in self.active_complaints(5)],
        }

    def upsert_complaint(
        self,
        *,
        fingerprint: str,
        category: str,
        sample: str,
        window_seconds: int,
    ) -> ComplaintCluster:
        now = format_datetime()
        clean_sample = sanitize_text(sample, 300)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM complaint_clusters WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    """
                    INSERT INTO complaint_clusters (
                        fingerprint, category, temperature, first_seen, last_seen, sample
                    ) VALUES (?, ?, 1, ?, ?, ?)
                    """,
                    (fingerprint, category, now, now, clean_sample),
                )
                self._conn.commit()
                return self.get_complaint_by_id(int(cursor.lastrowid))

            last_seen = parse_datetime(str(row["last_seen"]))
            expired = utc_now() - last_seen > timedelta(seconds=max(1, int(window_seconds)))
            temperature = 1 if expired else int(row["temperature"]) + 1
            first_seen = now if expired else row["first_seen"]
            self._conn.execute(
                """
                UPDATE complaint_clusters SET
                    category = ?, temperature = ?, first_seen = ?, last_seen = ?, sample = ?
                WHERE fingerprint = ?
                """,
                (category, temperature, first_seen, now, clean_sample, fingerprint),
            )
            self._conn.commit()
            return self.get_complaint_by_id(int(row["id"]))

    def get_complaint_by_id(self, row_id: int) -> ComplaintCluster:
        row = self._conn.execute("SELECT * FROM complaint_clusters WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(row_id)
        return row_to_cluster(row)

    def claim_complaint_report(
        self,
        *,
        fingerprint: str,
        temperature: int,
    ) -> ComplaintReportClaim | None:
        clean_fingerprint = sanitize_text(fingerprint, 128)
        claim_id = secrets.token_hex(16)
        attempted_at = format_datetime()
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO complaint_report_claims (
                        fingerprint, claim_id, state, attempted_at, attempted_temperature,
                        issue_url, failure_category, completed_at
                    ) VALUES (?, ?, 'attempted', ?, ?, '', '', '')
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        claim_id = excluded.claim_id,
                        state = 'attempted',
                        attempted_at = excluded.attempted_at,
                        attempted_temperature = excluded.attempted_temperature,
                        issue_url = '',
                        failure_category = '',
                        completed_at = ''
                    WHERE complaint_report_claims.state = 'retryable'
                    """,
                    (clean_fingerprint, claim_id, attempted_at, max(1, int(temperature))),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        """
                        UPDATE complaint_clusters
                        SET github_issue_url = ?, last_reported_temperature = ?
                        WHERE fingerprint = ? AND github_issue_url = ''
                        """,
                        (
                            REPORT_ATTEMPTED_SENTINEL,
                            REPORT_BLOCKING_TEMPERATURE,
                            clean_fingerprint,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            if not cursor.rowcount:
                return None
            return self.get_complaint_report_claim(clean_fingerprint)

    def get_complaint_report_claim(self, fingerprint: str) -> ComplaintReportClaim | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM complaint_report_claims WHERE fingerprint = ?",
                (sanitize_text(fingerprint, 128),),
            ).fetchone()
        return row_to_complaint_report_claim(row) if row is not None else None

    def release_complaint_report_claim(
        self,
        claim: ComplaintReportClaim,
        failure_category: str,
    ) -> bool:
        completed_at = format_datetime()
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE complaint_report_claims
                    SET state = 'retryable', failure_category = ?, completed_at = ?
                    WHERE fingerprint = ? AND claim_id = ? AND state = 'attempted'
                    """,
                    (
                        normalize_failure_category(failure_category),
                        completed_at,
                        claim.fingerprint,
                        claim.claim_id,
                    ),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        """
                        UPDATE complaint_clusters
                        SET github_issue_url = '', last_reported_temperature = 0
                        WHERE fingerprint = ? AND github_issue_url = ?
                        """,
                        (claim.fingerprint, REPORT_ATTEMPTED_SENTINEL),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return bool(cursor.rowcount)

    def mark_complaint_report_unknown(
        self,
        claim: ComplaintReportClaim,
        failure_category: str,
    ) -> bool:
        completed_at = format_datetime()
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE complaint_report_claims
                    SET state = 'unknown', failure_category = ?, completed_at = ?
                    WHERE fingerprint = ? AND claim_id = ? AND state = 'attempted'
                    """,
                    (
                        normalize_failure_category(failure_category),
                        completed_at,
                        claim.fingerprint,
                        claim.claim_id,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return bool(cursor.rowcount)

    def mark_complaint_reported(self, claim: ComplaintReportClaim, issue_url: str) -> bool:
        completed_at = format_datetime()
        clean_url = sanitize_text(issue_url, 300)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE complaint_report_claims
                    SET state = 'sent', issue_url = ?, failure_category = '', completed_at = ?
                    WHERE fingerprint = ? AND claim_id = ? AND state = 'attempted'
                    """,
                    (clean_url, completed_at, claim.fingerprint, claim.claim_id),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        """
                        UPDATE complaint_clusters
                        SET github_issue_url = ?, last_reported_temperature = ?
                        WHERE fingerprint = ?
                        """,
                        (clean_url, REPORT_BLOCKING_TEMPERATURE, claim.fingerprint),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return bool(cursor.rowcount)

    def active_complaints(self, limit: int = 10) -> list[ComplaintCluster]:
        limit = max(1, min(int(limit), 50))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM complaint_clusters
                ORDER BY temperature DESC, datetime(last_seen) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row_to_cluster(row) for row in rows]

    def cleanup(self) -> int:
        cutoff = format_datetime(utc_now() - timedelta(days=self.retention_days))
        with self._lock:
            event_cursor = self._conn.execute("DELETE FROM system_events WHERE created_at < ?", (cutoff,))
            cluster_cursor = self._conn.execute(
                """
                DELETE FROM complaint_clusters
                WHERE last_seen < ?
                  AND github_issue_url = ''
                  AND NOT EXISTS (
                      SELECT 1 FROM complaint_report_claims report
                      WHERE report.fingerprint = complaint_clusters.fingerprint
                        AND report.state IN ('attempted', 'unknown', 'sent')
                  )
                """,
                (cutoff,),
            )
            self._conn.commit()
            return int(event_cursor.rowcount or 0) + int(cluster_cursor.rowcount or 0)

    def clear_all(self) -> None:
        """Clear diagnostics while retaining minimal at-most-once report tombstones."""
        now = format_datetime()
        with self._lock:
            self._conn.execute("DELETE FROM system_events")
            self._conn.execute(
                """
                DELETE FROM complaint_clusters
                WHERE NOT EXISTS (
                    SELECT 1 FROM complaint_report_claims report
                    WHERE report.fingerprint = complaint_clusters.fingerprint
                      AND report.state IN ('attempted', 'unknown', 'sent')
                )
                """
            )
            self._conn.execute(
                """
                UPDATE complaint_clusters
                SET category = 'general',
                    temperature = 1,
                    first_seen = ?,
                    last_seen = ?,
                    sample = '',
                    github_issue_url = COALESCE(
                        NULLIF((
                            SELECT report.issue_url
                            FROM complaint_report_claims report
                            WHERE report.fingerprint = complaint_clusters.fingerprint
                        ), ''),
                        ?
                    ),
                    last_reported_temperature = ?
                WHERE EXISTS (
                    SELECT 1 FROM complaint_report_claims report
                    WHERE report.fingerprint = complaint_clusters.fingerprint
                      AND report.state IN ('attempted', 'unknown', 'sent')
                )
                """,
                (now, now, REPORT_ATTEMPTED_SENTINEL, REPORT_BLOCKING_TEMPERATURE),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def normalize_level(level: str) -> str:
    normalized = (level or "info").strip().casefold()
    return normalized if normalized in {"info", "warning", "error", "critical"} else "info"


def normalize_failure_category(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").casefold()).strip("_")
    return normalized[:80] or "outcome_unknown"


def allowed_levels(min_level: str) -> list[str]:
    order = ["info", "warning", "error", "critical"]
    normalized = normalize_level(min_level)
    return order[order.index(normalized) :]


def row_to_event(row: sqlite3.Row) -> SystemEvent:
    try:
        details = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        details = {}
    return SystemEvent(
        id=int(row["id"]),
        created_at=str(row["created_at"]),
        level=str(row["level"]),
        component=str(row["component"]),
        event_type=str(row["event_type"]),
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        route=str(row["route"]),
        duration_ms=row["duration_ms"],
        message=str(row["message"]),
        details=details,
    )


def row_to_cluster(row: sqlite3.Row) -> ComplaintCluster:
    return ComplaintCluster(
        id=int(row["id"]),
        fingerprint=str(row["fingerprint"]),
        category=str(row["category"]),
        temperature=int(row["temperature"]),
        first_seen=str(row["first_seen"]),
        last_seen=str(row["last_seen"]),
        sample=str(row["sample"]),
        github_issue_url=str(row["github_issue_url"]),
        last_reported_temperature=int(row["last_reported_temperature"]),
    )


def row_to_complaint_report_claim(row: sqlite3.Row) -> ComplaintReportClaim:
    return ComplaintReportClaim(
        fingerprint=str(row["fingerprint"]),
        claim_id=str(row["claim_id"]),
        state=str(row["state"]),
        attempted_at=str(row["attempted_at"]),
        attempted_temperature=int(row["attempted_temperature"]),
        issue_url=str(row["issue_url"]),
        failure_category=str(row["failure_category"]),
        completed_at=str(row["completed_at"]),
    )


def cluster_to_dict(cluster: ComplaintCluster) -> dict[str, Any]:
    return {
        "fingerprint": cluster.fingerprint,
        "category": cluster.category,
        "temperature": cluster.temperature,
        "first_seen": cluster.first_seen,
        "last_seen": cluster.last_seen,
        "sample": cluster.sample,
        "github_issue_url": cluster.github_issue_url,
    }
