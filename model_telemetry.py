from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from model_pricing import CostEstimate, TokenUsage, estimate_token_cost


SCHEMA_VERSION = 3
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "abandoned"}
ALLOWED_STAGE_KINDS = {
    "agent_turn",
    "agent_tool_turn",
    "embedding_index",
    "embedding_query",
    "final_answer",
    "plain",
    "router",
    "transcription",
    "vision",
}
TRACING_MODES = {"disabled", "metadata_only", "sensitive"}
ROUTE_BUCKETS = {
    "admin",
    "auto_response",
    "background",
    "character",
    "direct",
    "internet_image_analysis",
    "internet_image_send",
    "manual_proactive",
    "memory_index",
    "memory_query",
    "memory_recall",
    "memory_search",
    "normal",
    "other",
    "pre_route",
    "proactive",
    "prompt_privacy",
    "reaction_explanation",
    "reminder",
    "reminder_clarification",
    "selfcheck",
    "time_sensitive",
    "tool",
    "translate_reference",
    "vision",
    "youtube_audio_fallback",
}
TASK_CLASS_BUCKETS = {
    "agent",
    "agent_tool_turn",
    "background",
    "character",
    "embedding_index",
    "embedding_query",
    "final_answer",
    "other",
    "plain",
    "prompt",
    "proactive",
    "reminder",
    "router",
    "selfcheck",
    "transcription",
    "vision",
    "youtube_transcript",
}
POLICY_VERSIONS = {"primary_sol_low_v1"}
ENDPOINTS = {"agents", "audio_transcriptions", "embeddings", "responses"}
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
ACTUAL_MODEL_SOURCES = {"not_reported", "provider_response", "unavailable_sdk"}
FALLBACK_REASONS = {"captions_unavailable"}
RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
BUCKET_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,99}")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_datetime(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def normalize_tracing_mode(value: str | None) -> str:
    mode = str(value or "disabled").strip().casefold().replace("-", "_")
    if mode == "metadata":
        mode = "metadata_only"
    if mode not in TRACING_MODES:
        allowed = ", ".join(sorted(TRACING_MODES))
        raise ValueError(f"AGENTS_TRACING_MODE must be one of: {allowed}")
    return mode


def normalize_run_id(value: str | None) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if RUN_ID_RE.fullmatch(candidate) else secrets.token_hex(16)


def normalize_bucket(value: str | None, default: str = "other") -> str:
    candidate = str(value or "").strip().casefold().replace("-", "_")
    return candidate if BUCKET_RE.fullmatch(candidate) else default


def normalize_choice(value: str | None, allowed: set[str], default: str) -> str:
    candidate = normalize_bucket(value, "")
    return candidate if candidate in allowed else default


def normalize_route_bucket(value: str | None, default: str = "other") -> str:
    return normalize_choice(value, ROUTE_BUCKETS, default)


def normalize_task_class_bucket(value: str | None, default: str = "other") -> str:
    return normalize_choice(value, TASK_CLASS_BUCKETS, default)


def normalize_policy_version(value: str | None, default: str = "") -> str:
    return normalize_choice(value, POLICY_VERSIONS, default)


def normalize_model(value: str | None) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if MODEL_RE.fullmatch(candidate) else ""


def normalize_failure_class(value: str | type[BaseException] | BaseException | None) -> str:
    if isinstance(value, BaseException):
        value = type(value)
    if isinstance(value, type):
        value = value.__name__
    return normalize_bucket(str(value or ""), "")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


@dataclass(frozen=True)
class UsageMetrics:
    unit: str
    status: str
    request_count: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    audio_duration_ms: int | None
    request_entries: tuple[TokenUsage, ...]


def _token_usage_entry(value: Any, *, endpoint: str = "") -> TokenUsage:
    input_details = _field(value, "input_tokens_details") or _field(value, "input_token_details") or {}
    input_tokens = _optional_nonnegative_int(
        _field(value, "input_tokens", _field(value, "prompt_tokens"))
    )
    output_tokens = _optional_nonnegative_int(
        _field(value, "output_tokens", _field(value, "completion_tokens"))
    )
    if output_tokens is None and endpoint == "embeddings":
        output_tokens = 0
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=_optional_nonnegative_int(_field(input_details, "cached_tokens")),
        cache_write_tokens=_optional_nonnegative_int(_field(input_details, "cache_write_tokens")),
        output_tokens=output_tokens,
    )


def extract_usage_metrics(value: Any, *, endpoint: str = "") -> UsageMetrics:
    if value is None:
        return UsageMetrics("none", "missing", None, None, None, None, None, None, None, None, ())

    usage_type = str(_field(value, "type", "") or "").casefold()
    if usage_type == "duration":
        seconds = _field(value, "seconds")
        try:
            duration_ms = max(0, round(float(seconds) * 1000))
        except (TypeError, ValueError):
            duration_ms = None
        return UsageMetrics(
            "duration",
            "duration" if duration_ms is not None else "invalid",
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            duration_ms,
            (),
        )

    input_details = _field(value, "input_tokens_details") or _field(value, "input_token_details") or {}
    output_details = _field(value, "output_tokens_details") or {}
    input_tokens = _optional_nonnegative_int(
        _field(value, "input_tokens", _field(value, "prompt_tokens"))
    )
    output_tokens = _optional_nonnegative_int(
        _field(value, "output_tokens", _field(value, "completion_tokens"))
    )
    total_tokens = _optional_nonnegative_int(_field(value, "total_tokens"))
    request_count = _optional_nonnegative_int(_field(value, "requests"))
    raw_entries = _field(value, "request_usage_entries") or ()
    cached_tokens = _optional_nonnegative_int(_field(input_details, "cached_tokens"))
    cache_write_tokens = _optional_nonnegative_int(
        _field(input_details, "cache_write_tokens")
    )
    reasoning_tokens = _optional_nonnegative_int(
        _field(output_details, "reasoning_tokens")
    )
    if (
        endpoint == "agents"
        and request_count == 0
        and not raw_entries
        and all(
            item in {None, 0}
            for item in (
                input_tokens,
                cached_tokens,
                cache_write_tokens,
                output_tokens,
                reasoning_tokens,
                total_tokens,
            )
        )
    ):
        # Agents SDK constructs Usage() with zero-valued counters when the
        # provider did not report usage. Treat that sentinel as missing instead
        # of manufacturing a complete, zero-cost request.
        return UsageMetrics(
            "none",
            "missing",
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            (),
        )
    entries = tuple(_token_usage_entry(item, endpoint=endpoint) for item in raw_entries)
    if not entries and input_tokens is not None:
        entries = (
            TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=_optional_nonnegative_int(_field(input_details, "cached_tokens")),
                cache_write_tokens=_optional_nonnegative_int(_field(input_details, "cache_write_tokens")),
                output_tokens=0 if output_tokens is None and endpoint == "embeddings" else output_tokens,
            ),
        )
    if request_count is None and entries:
        request_count = len(entries)
    if output_tokens is None and endpoint == "embeddings":
        stored_output_tokens = None
    else:
        stored_output_tokens = output_tokens
    has_usage = input_tokens is not None or output_tokens is not None or total_tokens is not None
    return UsageMetrics(
        "tokens" if has_usage else "none",
        "reported" if has_usage else "missing",
        request_count,
        input_tokens,
        cached_tokens,
        cache_write_tokens,
        stored_output_tokens,
        reasoning_tokens,
        total_tokens,
        None,
        entries,
    )


@dataclass(frozen=True)
class StageHandle:
    run_id: str
    stage_id: str
    ordinal: int
    started_monotonic: float


@dataclass(frozen=True)
class ModelStageRecord:
    stage_id: str
    run_id: str
    ordinal: int
    route_bucket: str
    task_class_bucket: str
    stage_kind: str
    intended_model: str
    actual_model: str
    actual_model_source: str
    reasoning_effort: str
    actual_reasoning_effort: str
    endpoint: str
    status: str
    failure_class: str
    fallback_reason: str
    retry_count: int | None
    started_at: str
    completed_at: str
    latency_ms: int | None
    usage_status: str
    request_count: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    audio_duration_ms: int | None
    cost_status: str
    estimated_cost_nano_usd: int | None
    price_snapshot_version: str
    cost_basis_model: str


class ModelTelemetryStore:
    def __init__(self, db_path: Path | str, retention_days: int = 30) -> None:
        self.db_path = Path(db_path)
        self.retention_days = max(1, int(retention_days))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._write_failure_count = 0
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=0.25,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=250")
            self._migrate()

    @property
    def write_failure_count(self) -> int:
        return self._write_failure_count

    def _note_write_failure(self) -> None:
        self._write_failure_count += 1

    def _schema_version(self) -> int:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_telemetry_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        row = self._conn.execute(
            "SELECT value FROM model_telemetry_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid model telemetry schema version") from exc
        if version > SCHEMA_VERSION:
            raise RuntimeError("Model telemetry schema is newer than this code")
        return max(0, version)

    def _set_schema_version(self, version: int) -> None:
        self._conn.execute(
            """
            INSERT INTO model_telemetry_meta(key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(version),),
        )

    def _has_column(self, table: str, column: str) -> bool:
        return any(
            str(row[1]) == column
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        )

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        if not self._has_column(table, column):
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )

    def _migrate(self) -> None:
        version = self._schema_version()
        if version < 1:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_telemetry_runs (
                    run_id TEXT PRIMARY KEY,
                    route_bucket TEXT NOT NULL DEFAULT 'other',
                    task_class_bucket TEXT NOT NULL DEFAULT 'other',
                    policy_version TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_telemetry_stages (
                    stage_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES model_telemetry_runs(run_id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    stage_kind TEXT NOT NULL,
                    intended_model TEXT NOT NULL DEFAULT '',
                    actual_model TEXT,
                    actual_model_source TEXT NOT NULL DEFAULT 'not_reported',
                    reasoning_effort TEXT NOT NULL DEFAULT '',
                    endpoint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN (
                        'pending', 'succeeded', 'failed', 'cancelled', 'abandoned'
                    )),
                    failure_class TEXT NOT NULL DEFAULT '',
                    fallback_reason TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    latency_ms INTEGER,
                    usage_status TEXT NOT NULL DEFAULT 'missing',
                    request_count INTEGER,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    output_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    total_tokens INTEGER,
                    audio_duration_ms INTEGER,
                    cost_status TEXT NOT NULL DEFAULT 'missing_usage',
                    estimated_cost_nano_usd INTEGER,
                    price_snapshot_version TEXT NOT NULL DEFAULT '',
                    cost_basis_model TEXT NOT NULL DEFAULT '',
                    input_rate_nano_usd INTEGER,
                    cached_input_rate_nano_usd INTEGER,
                    cache_write_rate_nano_usd INTEGER,
                    output_rate_nano_usd INTEGER,
                    UNIQUE(run_id, ordinal)
                );
                """
            )
            self._set_schema_version(1)
            version = 1
        if version < 2:
            self._ensure_column(
                "model_telemetry_stages", "actual_reasoning_effort", "TEXT"
            )
            self._set_schema_version(2)
            version = 2
        if version < 3:
            self._ensure_column(
                "model_telemetry_stages",
                "route_bucket",
                "TEXT NOT NULL DEFAULT 'other'",
            )
            self._ensure_column(
                "model_telemetry_stages",
                "task_class_bucket",
                "TEXT NOT NULL DEFAULT 'other'",
            )
            self._conn.execute(
                """
                UPDATE model_telemetry_stages
                SET route_bucket = COALESCE(
                        (SELECT route_bucket FROM model_telemetry_runs run
                         WHERE run.run_id = model_telemetry_stages.run_id),
                        'other'
                    ),
                    task_class_bucket = COALESCE(
                        (SELECT task_class_bucket FROM model_telemetry_runs run
                         WHERE run.run_id = model_telemetry_stages.run_id),
                        'other'
                    )
                """
            )
            self._set_schema_version(3)
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_model_telemetry_stage_completed
                ON model_telemetry_stages(completed_at, stage_id);
            CREATE INDEX IF NOT EXISTS idx_model_telemetry_stage_model
                ON model_telemetry_stages(intended_model, completed_at);
            CREATE INDEX IF NOT EXISTS idx_model_telemetry_stage_kind
                ON model_telemetry_stages(stage_kind, completed_at);
            CREATE INDEX IF NOT EXISTS idx_model_telemetry_stage_route
                ON model_telemetry_stages(route_bucket, completed_at);
            CREATE INDEX IF NOT EXISTS idx_model_telemetry_stage_task
                ON model_telemetry_stages(task_class_bucket, completed_at);
            """
        )
        self._conn.commit()

    def begin_stage(
        self,
        *,
        run_id: str = "",
        route_bucket: str = "other",
        task_class_bucket: str = "other",
        policy_version: str = "",
        stage_kind: str,
        intended_model: str,
        reasoning_effort: str = "",
        endpoint: str = "",
    ) -> StageHandle | None:
        try:
            return self._begin_stage(
                run_id=run_id,
                route_bucket=route_bucket,
                task_class_bucket=task_class_bucket,
                policy_version=policy_version,
                stage_kind=stage_kind,
                intended_model=intended_model,
                reasoning_effort=reasoning_effort,
                endpoint=endpoint,
            )
        except Exception:
            self._note_write_failure()
            return None

    def _begin_stage(self, **values: Any) -> StageHandle:
        run_id = normalize_run_id(values.get("run_id"))
        route = normalize_route_bucket(values.get("route_bucket"), "other")
        task_class = normalize_task_class_bucket(
            values.get("task_class_bucket"), "other"
        )
        policy_version = normalize_policy_version(values.get("policy_version"), "")
        stage_kind = normalize_bucket(values.get("stage_kind"), "other")
        if stage_kind not in ALLOWED_STAGE_KINDS:
            stage_kind = "agent_turn"
        intended_model = normalize_model(values.get("intended_model"))
        reasoning_effort = normalize_choice(
            values.get("reasoning_effort"), REASONING_EFFORTS, ""
        )
        endpoint = normalize_choice(values.get("endpoint"), ENDPOINTS, "")
        now = format_datetime()
        stage_id = secrets.token_hex(16)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO model_telemetry_runs (
                        run_id, route_bucket, task_class_bucket, policy_version, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        route_bucket = CASE
                            WHEN model_telemetry_runs.route_bucket IN ('', 'other', 'pre_route')
                            THEN excluded.route_bucket
                            ELSE model_telemetry_runs.route_bucket
                        END,
                        task_class_bucket = CASE
                            WHEN model_telemetry_runs.task_class_bucket IN (
                                '', 'other', 'embedding_index', 'embedding_query', 'router'
                            )
                            THEN excluded.task_class_bucket
                            ELSE model_telemetry_runs.task_class_bucket
                        END,
                        policy_version = CASE
                            WHEN model_telemetry_runs.policy_version = ''
                            THEN excluded.policy_version
                            ELSE model_telemetry_runs.policy_version
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, route, task_class, policy_version, now, now),
                )
                ordinal = int(
                    self._conn.execute(
                        "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM model_telemetry_stages WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()[0]
                )
                self._conn.execute(
                    """
                    INSERT INTO model_telemetry_stages (
                        stage_id, run_id, ordinal, route_bucket, task_class_bucket,
                        stage_kind, intended_model,
                        reasoning_effort, endpoint, status, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        stage_id,
                        run_id,
                        ordinal,
                        route,
                        task_class,
                        stage_kind,
                        intended_model,
                        reasoning_effort,
                        endpoint,
                        now,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return StageHandle(run_id, stage_id, ordinal, time.monotonic())

    def finish_stage(
        self,
        handle: StageHandle | None,
        *,
        status: str,
        usage: Any = None,
        actual_model: str = "",
        actual_model_source: str = "not_reported",
        actual_reasoning_effort: str = "",
        failure_class: str | type[BaseException] | BaseException | None = None,
        fallback_reason: str = "",
        retry_count: int | None = None,
        latency_ms: int | None = None,
        stage_kind: str | None = None,
    ) -> bool:
        if handle is None:
            return False
        try:
            return self._finish_stage(
                handle,
                status=status,
                usage=usage,
                actual_model=actual_model,
                actual_model_source=actual_model_source,
                actual_reasoning_effort=actual_reasoning_effort,
                failure_class=failure_class,
                fallback_reason=fallback_reason,
                retry_count=retry_count,
                latency_ms=latency_ms,
                stage_kind=stage_kind,
            )
        except Exception:
            self._note_write_failure()
            return False

    def _finish_stage(self, handle: StageHandle, **values: Any) -> bool:
        status = normalize_bucket(values.get("status"), "failed")
        if status not in TERMINAL_STATUSES:
            status = "failed"
        metrics = extract_usage_metrics(values.get("usage"), endpoint=self.stage_endpoint(handle))
        actual_model = normalize_model(values.get("actual_model"))
        actual_model_source = normalize_choice(
            values.get("actual_model_source"), ACTUAL_MODEL_SOURCES, "not_reported"
        )
        actual_reasoning_effort = normalize_choice(
            values.get("actual_reasoning_effort"), REASONING_EFFORTS, ""
        )
        failure_class = normalize_failure_class(values.get("failure_class"))
        fallback_reason = normalize_choice(
            values.get("fallback_reason"), FALLBACK_REASONS, ""
        )
        retry_count = _optional_nonnegative_int(values.get("retry_count"))
        stage_kind = normalize_bucket(values.get("stage_kind"), "")
        if stage_kind not in ALLOWED_STAGE_KINDS:
            stage_kind = ""
        requested_latency = _optional_nonnegative_int(values.get("latency_ms"))
        measured_latency = max(0, round((time.monotonic() - handle.started_monotonic) * 1000))
        latency_ms = requested_latency if requested_latency is not None else measured_latency
        intended_model = self.stage_intended_model(handle)
        basis_model = actual_model or intended_model
        cost: CostEstimate
        if metrics.unit == "duration":
            cost = CostEstimate(None, "unsupported_unit", False, None, basis_model or None, None, None, None, None)
        else:
            cost = estimate_token_cost(basis_model, metrics.request_entries)
        completed_at = format_datetime()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE model_telemetry_stages SET
                        stage_kind = CASE WHEN ? = '' THEN stage_kind ELSE ? END,
                        actual_model = NULLIF(?, ''),
                        actual_model_source = ?,
                        actual_reasoning_effort = NULLIF(?, ''),
                        status = ?,
                        failure_class = ?,
                        fallback_reason = ?,
                        retry_count = ?,
                        completed_at = ?,
                        latency_ms = ?,
                        usage_status = ?,
                        request_count = ?,
                        input_tokens = ?,
                        cached_input_tokens = ?,
                        cache_write_tokens = ?,
                        output_tokens = ?,
                        reasoning_tokens = ?,
                        total_tokens = ?,
                        audio_duration_ms = ?,
                        cost_status = ?,
                        estimated_cost_nano_usd = ?,
                        price_snapshot_version = COALESCE(?, ''),
                        cost_basis_model = COALESCE(?, ''),
                        input_rate_nano_usd = ?,
                        cached_input_rate_nano_usd = ?,
                        cache_write_rate_nano_usd = ?,
                        output_rate_nano_usd = ?
                    WHERE stage_id = ? AND run_id = ? AND status = 'pending'
                    """,
                    (
                        stage_kind,
                        stage_kind,
                        actual_model,
                        actual_model_source,
                        actual_reasoning_effort,
                        status,
                        failure_class,
                        fallback_reason,
                        retry_count,
                        completed_at,
                        latency_ms,
                        metrics.status,
                        metrics.request_count,
                        metrics.input_tokens,
                        metrics.cached_input_tokens,
                        metrics.cache_write_tokens,
                        metrics.output_tokens,
                        metrics.reasoning_tokens,
                        metrics.total_tokens,
                        metrics.audio_duration_ms,
                        cost.status,
                        cost.nano_usd,
                        cost.snapshot_version,
                        cost.basis_model,
                        cost.input_rate_nano_usd,
                        cost.cached_input_rate_nano_usd,
                        cost.cache_write_rate_nano_usd,
                        cost.output_rate_nano_usd,
                        handle.stage_id,
                        handle.run_id,
                    ),
                )
                if cursor.rowcount:
                    self._conn.execute(
                        "UPDATE model_telemetry_runs SET updated_at = ? WHERE run_id = ?",
                        (completed_at, handle.run_id),
                    )
                self._conn.commit()
                return bool(cursor.rowcount)
            except Exception:
                self._conn.rollback()
                raise

    def stage_endpoint(self, handle: StageHandle) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT endpoint FROM model_telemetry_stages WHERE stage_id = ? AND run_id = ?",
                (handle.stage_id, handle.run_id),
            ).fetchone()
        return str(row["endpoint"]) if row is not None else ""

    def stage_intended_model(self, handle: StageHandle) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT intended_model FROM model_telemetry_stages WHERE stage_id = ? AND run_id = ?",
                (handle.stage_id, handle.run_id),
            ).fetchone()
        return str(row["intended_model"]) if row is not None else ""

    def recover_abandoned(self, stale_after_seconds: int = 300) -> int:
        now = format_datetime()
        cutoff = format_datetime(
            utc_now() - timedelta(seconds=max(0, int(stale_after_seconds)))
        )
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    UPDATE model_telemetry_stages
                    SET status = 'abandoned',
                        failure_class = 'process_interrupted',
                        completed_at = ?,
                        usage_status = 'missing',
                        cost_status = 'missing_usage'
                    WHERE status = 'pending' AND started_at < ?
                    """,
                    (now, cutoff),
                )
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except Exception:
                self._conn.rollback()
                raise

    def cleanup(self) -> int:
        cutoff = format_datetime(utc_now() - timedelta(days=self.retention_days))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """
                    DELETE FROM model_telemetry_runs
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM model_telemetry_stages stage
                        WHERE stage.run_id = model_telemetry_runs.run_id
                          AND (
                              stage.status = 'pending'
                              OR COALESCE(NULLIF(stage.completed_at, ''), stage.started_at) >= ?
                          )
                      )
                    """,
                    (cutoff,),
                )
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except Exception:
                self._conn.rollback()
                raise

    def latest_stages(self, limit: int = 50) -> list[ModelStageRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM model_telemetry_stages
                ORDER BY datetime(started_at) DESC, run_id DESC, ordinal DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [row_to_stage(row) for row in rows]

    def aggregate_since(self, lookback_seconds: int = 86400) -> dict[str, Any]:
        cutoff = format_datetime(utc_now() - timedelta(seconds=max(1, int(lookback_seconds))))
        stale_cutoff = format_datetime(utc_now() - timedelta(seconds=300))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM model_telemetry_stages
                WHERE completed_at >= ? AND status != 'pending'
                ORDER BY completed_at ASC, run_id ASC, ordinal ASC
                """,
                (cutoff,),
            ).fetchall()
            pending_row = self._conn.execute(
                """
                SELECT COUNT(*) AS pending_count,
                       SUM(CASE WHEN started_at < ? THEN 1 ELSE 0 END) AS stale_count
                FROM model_telemetry_stages
                WHERE status = 'pending'
                """,
                (stale_cutoff,),
            ).fetchone()
        status_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        route_counts: dict[str, int] = {}
        task_class_counts: dict[str, int] = {}
        model_counts: dict[str, int] = {}
        known_cost = 0
        unpriced = 0
        missing_usage = 0
        cost_incomplete = 0
        totals = {
            "requests": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
        for row in rows:
            status = str(row["status"])
            stage_kind = str(row["stage_kind"])
            route_bucket = str(row["route_bucket"] or "other")
            task_class_bucket = str(row["task_class_bucket"] or "other")
            model = str(row["intended_model"] or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            stage_counts[stage_kind] = stage_counts.get(stage_kind, 0) + 1
            route_counts[route_bucket] = route_counts.get(route_bucket, 0) + 1
            task_class_counts[task_class_bucket] = (
                task_class_counts.get(task_class_bucket, 0) + 1
            )
            model_counts[model] = model_counts.get(model, 0) + 1
            if row["estimated_cost_nano_usd"] is not None:
                known_cost += int(row["estimated_cost_nano_usd"])
            cost_status = str(row["cost_status"])
            if cost_status in {"missing_price", "unsupported_unit"}:
                unpriced += 1
            if cost_status != "estimated":
                cost_incomplete += 1
            if str(row["usage_status"]) in {"missing", "invalid"}:
                missing_usage += 1
            for key, column in (
                ("requests", "request_count"),
                ("input_tokens", "input_tokens"),
                ("cached_input_tokens", "cached_input_tokens"),
                ("cache_write_tokens", "cache_write_tokens"),
                ("output_tokens", "output_tokens"),
                ("reasoning_tokens", "reasoning_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                if row[column] is not None:
                    totals[key] += int(row[column])
        last_completed_at = max(
            (str(row["completed_at"]) for row in rows if row["completed_at"]),
            default="",
        )
        return {
            "lookback_seconds": max(1, int(lookback_seconds)),
            "stage_count": len(rows),
            "status_counts": status_counts,
            "stage_counts": stage_counts,
            "route_counts": route_counts,
            "task_class_counts": task_class_counts,
            "model_counts": model_counts,
            "pending_count": int(pending_row["pending_count"] or 0),
            "stale_pending_count": int(pending_row["stale_count"] or 0),
            "usage_missing_count": missing_usage,
            "unpriced_count": unpriced,
            "cost_incomplete_count": cost_incomplete,
            "known_cost_nano_usd": known_cost,
            "cost_complete": bool(rows) and cost_incomplete == 0,
            "token_totals": totals,
            "last_completed_at": last_completed_at,
            "write_failure_count": self.write_failure_count,
        }

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM model_telemetry_runs")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def row_to_stage(row: sqlite3.Row) -> ModelStageRecord:
    return ModelStageRecord(
        stage_id=str(row["stage_id"]),
        run_id=str(row["run_id"]),
        ordinal=int(row["ordinal"]),
        route_bucket=str(row["route_bucket"] or "other"),
        task_class_bucket=str(row["task_class_bucket"] or "other"),
        stage_kind=str(row["stage_kind"]),
        intended_model=str(row["intended_model"]),
        actual_model=str(row["actual_model"] or ""),
        actual_model_source=str(row["actual_model_source"]),
        reasoning_effort=str(row["reasoning_effort"]),
        actual_reasoning_effort=str(row["actual_reasoning_effort"] or ""),
        endpoint=str(row["endpoint"]),
        status=str(row["status"]),
        failure_class=str(row["failure_class"]),
        fallback_reason=str(row["fallback_reason"]),
        retry_count=int(row["retry_count"]) if row["retry_count"] is not None else None,
        started_at=str(row["started_at"]),
        completed_at=str(row["completed_at"]),
        latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
        usage_status=str(row["usage_status"]),
        request_count=int(row["request_count"]) if row["request_count"] is not None else None,
        input_tokens=int(row["input_tokens"]) if row["input_tokens"] is not None else None,
        cached_input_tokens=(
            int(row["cached_input_tokens"]) if row["cached_input_tokens"] is not None else None
        ),
        cache_write_tokens=int(row["cache_write_tokens"]) if row["cache_write_tokens"] is not None else None,
        output_tokens=int(row["output_tokens"]) if row["output_tokens"] is not None else None,
        reasoning_tokens=int(row["reasoning_tokens"]) if row["reasoning_tokens"] is not None else None,
        total_tokens=int(row["total_tokens"]) if row["total_tokens"] is not None else None,
        audio_duration_ms=int(row["audio_duration_ms"]) if row["audio_duration_ms"] is not None else None,
        cost_status=str(row["cost_status"]),
        estimated_cost_nano_usd=(
            int(row["estimated_cost_nano_usd"]) if row["estimated_cost_nano_usd"] is not None else None
        ),
        price_snapshot_version=str(row["price_snapshot_version"]),
        cost_basis_model=str(row["cost_basis_model"]),
    )
