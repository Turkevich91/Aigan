"""Validated, persistent current-chat answer-model overrides.

Owner: chat_model_settings (execution policy and persistence).
Telegram authorization belongs to the calling adapter. This module never calls
a provider, changes global configuration, or touches conversation records.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Sequence

from chat_tool_policy import ChatToolPolicy, TOOL_KEYS


DEFAULT_DURATION_SECONDS = 15 * 60
MAX_DURATION_SECONDS = 7 * 24 * 60 * 60
_MAX_INTEGER = 2**63 - 1


class InvalidModelSelection(ValueError):
    """The requested profile, scope, duration, or revision is invalid."""


class StaleSettingsRevision(ValueError):
    """The settings changed after the caller observed their revision."""


@dataclass(frozen=True)
class ModelCapability:
    model: str
    aliases: tuple[str, ...]
    reasoning_efforts: tuple[str, ...]
    supports_images: bool = True


SPARK_MODEL = "spark-qwen"
_REASONING = ("low", "medium", "high", "xhigh", "max")
MODEL_CATALOG = (
    ModelCapability("gpt-6-astra", ("astra",), _REASONING),
    ModelCapability("gpt-6.1-sol", ("sol",), _REASONING),
    ModelCapability("gpt-6.1-terra", ("terra",), ("none", *_REASONING)),
    ModelCapability("gpt-6-luna", ("luna",), ("none", *_REASONING)),
    ModelCapability(SPARK_MODEL, ("spark", "qwen"), ("auto",), supports_images=False),
)


@dataclass(frozen=True)
class AnswerProfile:
    model: str
    reasoning_effort: str
    overridden: bool
    expires_at: float | None
    revision: int


@dataclass(frozen=True)
class ParsedModelSelection:
    model: str
    reasoning_effort: str
    duration_seconds: int | None


def get_model_capability(model: str) -> ModelCapability:
    """Resolve only curated aliases or canonical provider model identifiers."""
    if isinstance(model, str):
        normalized = model.strip().casefold()
        for entry in MODEL_CATALOG:
            if normalized == entry.model or normalized in entry.aliases:
                return entry
    raise InvalidModelSelection("unsupported_model")


def validate_selection(model: str, effort: str) -> tuple[str, str]:
    capability = get_model_capability(model)
    normalized = effort.strip().casefold() if isinstance(effort, str) else ""
    if normalized not in capability.reasoning_efforts:
        raise InvalidModelSelection("unsupported_reasoning_effort")
    return capability.model, normalized


def _profile_rank(profile: tuple[str, str]) -> tuple[int, int]:
    capability = get_model_capability(profile[0])
    return -MODEL_CATALOG.index(capability), capability.reasoning_efforts.index(profile[1])


def member_duration_limit(model: str, effort: str, ceiling: tuple[str, str]) -> int | None:
    """Return None for persistent access, the boost limit in seconds, or zero for no access."""
    requested = _profile_rank(validate_selection(model, effort))
    if requested <= _profile_rank(ceiling):
        return None
    if requested <= _profile_rank(("astra", "low")):
        return DEFAULT_DURATION_SECONDS
    return 0


def parse_duration(value: str) -> int | None:
    """Parse a bounded CLI duration; indefinite selection must be explicit."""
    if not isinstance(value, str):
        raise InvalidModelSelection("invalid_duration")
    value = value.strip().casefold()
    if value in {"persistent", "until-reset"}:
        return None
    match = re.fullmatch(r"([1-9][0-9]{0,5})([mhd])", value)
    if match is None:
        raise InvalidModelSelection("invalid_duration")
    seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if seconds > MAX_DURATION_SECONDS:
        raise InvalidModelSelection("invalid_duration")
    return seconds


def parse_model_selection(arguments: str | Sequence[str]) -> ParsedModelSelection:
    """Parse ``astra-low 15m`` or ``gpt-6-astra low 15m`` arguments.

    Missing reasoning uses the preset default. Missing duration is fifteen minutes.
    Reset/default is a separate operation handled by the Telegram adapter.
    """
    if isinstance(arguments, str):
        parts = arguments.split()
    else:
        parts = list(arguments)
    if not parts or len(parts) > 3 or any(not isinstance(part, str) for part in parts):
        raise InvalidModelSelection("invalid_selection_arguments")
    token = parts.pop(0).casefold()
    effort = "low"
    try:
        capability = get_model_capability(token)
    except InvalidModelSelection:
        model_token, separator, effort = token.rpartition("-")
        if not separator:
            raise InvalidModelSelection("unsupported_model") from None
        capability = get_model_capability(model_token)
    else:
        effort = "low" if "low" in capability.reasoning_efforts else capability.reasoning_efforts[0]
        if parts and parts[0].casefold() in {"auto", "none", "mid", *_REASONING}:
            effort = parts.pop(0).casefold()
    if len(parts) > 1:
        raise InvalidModelSelection("invalid_selection_arguments")
    model, effort = validate_selection(capability.model, "medium" if effort == "mid" else effort)
    duration = parse_duration(parts[0]) if parts else DEFAULT_DURATION_SECONDS
    return ParsedModelSelection(model, effort, duration)


def _chat_id(value: int) -> int:
    if type(value) is not int or value == 0 or not -_MAX_INTEGER <= value <= _MAX_INTEGER:
        raise InvalidModelSelection("invalid_chat_scope")
    return value


def _expected_revision(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= _MAX_INTEGER):
        raise InvalidModelSelection("invalid_settings_revision")


def _next_revision(revision: int) -> int:
    if revision >= _MAX_INTEGER:
        raise InvalidModelSelection("settings_revision_exhausted")
    return revision + 1


def _timestamp(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidModelSelection("invalid_clock")
    return float(value)


class ChatToolSettingsStore:
    """Tool preferences have their own revisions and outlive model overrides."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self._connect = connect
        with closing(connect()) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS chat_tool_switches (
                    chat_id INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    disabled INTEGER NOT NULL CHECK (disabled IN (0, 1)),
                    last_disabled INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(chat_id, tool)
                )
            """)

    @staticmethod
    def _state(connection: sqlite3.Connection, chat_id: int) -> ChatToolPolicy:
        rows = connection.execute(
            "SELECT tool, disabled, last_disabled, revision FROM chat_tool_switches WHERE chat_id=?",
            (chat_id,),
        ).fetchall()
        disabled: set[str] = set()
        last_disabled: list[tuple[str, int]] = []
        revision = 0
        for row in rows:
            last, rev = row["last_disabled"], row["revision"]
            if (row["disabled"] not in (0, 1) or type(last) is not int or type(rev) is not int
                    or not 0 <= last <= rev or not 0 < rev <= _MAX_INTEGER):
                raise InvalidModelSelection("malformed_tool_settings")
            revision = max(revision, rev)
            last_disabled.append((row["tool"], last))
            if row["disabled"]:
                disabled.add(row["tool"])
        return ChatToolPolicy(frozenset(disabled), revision, tuple(last_disabled))

    def resolve(self, chat_id: int) -> ChatToolPolicy:
        with closing(self._connect()) as connection:
            return self._state(connection, _chat_id(chat_id))

    def set_enabled(
        self, chat_id: int, key: str, enabled: bool, *, expected_revision: int,
    ) -> ChatToolPolicy:
        if key not in TOOL_KEYS or type(enabled) is not bool:
            raise InvalidModelSelection("invalid_tool_selection")
        return self._update(chat_id, expected_revision, key, enabled)

    def reset(self, chat_id: int, *, expected_revision: int) -> ChatToolPolicy:
        return self._update(chat_id, expected_revision, None, True)

    def _update(
        self, chat_id: int, expected_revision: int, key: str | None, enabled: bool,
    ) -> ChatToolPolicy:
        chat_id = _chat_id(chat_id)
        if expected_revision is None:
            raise InvalidModelSelection("invalid_revision")
        _expected_revision(expected_revision)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            policy = self._state(connection, chat_id)
            if policy.revision != expected_revision:
                raise StaleSettingsRevision("tool_settings_changed")
            if key is None:
                if policy.disabled:
                    revision = _next_revision(policy.revision)
                    connection.execute(
                        "UPDATE chat_tool_switches SET disabled=0, revision=? WHERE chat_id=? AND disabled=1",
                        (revision, chat_id),
                    )
            else:
                already_enabled = key not in policy.disabled
                if enabled == already_enabled:
                    return policy
                revision = _next_revision(policy.revision)
                last = dict(policy.last_disabled).get(key, 0) if enabled else revision
                connection.execute("""
                    INSERT INTO chat_tool_switches(chat_id, tool, disabled, last_disabled, revision)
                    VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, tool) DO UPDATE SET
                        disabled=excluded.disabled, last_disabled=excluded.last_disabled, revision=excluded.revision
                """, (chat_id, key, int(not enabled), last, revision))
            return self._state(connection, chat_id)


@dataclass(frozen=True)
class _ChatSettings:
    revision: int | None = 0
    owner: tuple[str, str] | None = None
    persistent: tuple[str, str] | None = None
    temporary: tuple[str, str] | None = None
    expires_at: float | None = None

    def columns(self) -> tuple:
        # Keep the effective profile in the original columns for older readers.
        active = self.temporary or self.persistent or (None, None)
        previous = self.persistent if self.temporary else None
        return (*active, self.expires_at, *(self.owner or (None, None)), *(previous or (None, None)))

    def answer(self, default: tuple[str, str]) -> AnswerProfile:
        selected = self.temporary or self.persistent
        return AnswerProfile(*(selected or default), selected is not None, self.expires_at, self.revision or 0)


def _stored_profile(model, effort) -> tuple[str, str] | None:
    try:
        canonical = validate_selection(model, effort)
    except InvalidModelSelection:
        return None
    return canonical if canonical == (model, effort) else None


class ChatModelSettingsStore:
    """Serialize settings, revisions and permission checks across processes."""

    def __init__(
        self, db_path: str | Path, *, clock: Callable[[], float] = time.time,
        member_default: tuple[str, str] = ("sol", "medium"), legacy_owner_settings: bool = False,
    ) -> None:
        self.path = Path(db_path)
        self.clock = clock
        self.member_default = validate_selection(*member_default)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.tools = ChatToolSettingsStore(self._connect)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS chat_model_settings (
                    chat_id INTEGER PRIMARY KEY,
                    model TEXT,
                    reasoning_effort TEXT,
                    expires_at REAL,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(chat_model_settings)")}
            if "owner_model" not in columns:
                for column in ("owner_model", "owner_effort", "persistent_model", "persistent_effort"):
                    connection.execute(f"ALTER TABLE chat_model_settings ADD COLUMN {column} TEXT")
                # Legacy settings have no author. Only an explicitly trusted
                # single-owner deployment can attribute its old choices.
                if legacy_owner_settings:
                    connection.execute("""
                        UPDATE chat_model_settings SET owner_model=model, owner_effort=reasoning_effort
                        WHERE expires_at IS NULL AND model IS NOT NULL
                    """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def close(self) -> None:
        """Compatibility lifecycle hook; no connection is retained between calls."""

    def _now(self, now: float | None) -> float:
        return _timestamp(self.clock() if now is None else now)

    @staticmethod
    def _write(connection: sqlite3.Connection, chat_id: int, state: _ChatSettings, now: float) -> None:
        connection.execute("""
            INSERT INTO chat_model_settings
                (chat_id, model, reasoning_effort, expires_at, owner_model, owner_effort,
                 persistent_model, persistent_effort, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                model=excluded.model, reasoning_effort=excluded.reasoning_effort, expires_at=excluded.expires_at,
                owner_model=excluded.owner_model, owner_effort=excluded.owner_effort,
                persistent_model=excluded.persistent_model, persistent_effort=excluded.persistent_effort,
                revision=excluded.revision, updated_at=excluded.updated_at
        """, (chat_id, *state.columns(), state.revision, now))

    def _state(self, connection: sqlite3.Connection, chat_id: int, now: float) -> _ChatSettings:
        row = connection.execute("""
            SELECT model, reasoning_effort, expires_at, owner_model, owner_effort,
                   persistent_model, persistent_effort, revision FROM chat_model_settings WHERE chat_id=?
        """, (chat_id,)).fetchone()
        if row is None:
            return _ChatSettings()
        revision = row["revision"]
        if type(revision) is not int or not 0 < revision <= _MAX_INTEGER:
            # Reads fall back to defaults; never reuse a potentially stale revision.
            return _ChatSettings(revision=None)
        active = _stored_profile(row["model"], row["reasoning_effort"])
        owner = _stored_profile(row["owner_model"], row["owner_effort"])
        if row["expires_at"] is None:
            state = _ChatSettings(revision, owner, persistent=active)
        else:
            previous = _stored_profile(row["persistent_model"], row["persistent_effort"])
            state = _ChatSettings(revision, owner, persistent=previous)
            try:
                expires_at = _timestamp(row["expires_at"])
            except InvalidModelSelection:
                expires_at = None
            if active and expires_at is not None and now < expires_at:
                state = replace(state, temporary=active, expires_at=expires_at)
        if state.columns() != tuple(row)[:-1]:
            state = replace(state, revision=_next_revision(revision))
            self._write(connection, chat_id, state, now)
        return state

    def _member_ceiling(self, state: _ChatSettings) -> tuple[str, str]:
        return max(self.member_default, state.owner or self.member_default, key=_profile_rank)

    def member_ceiling(self, chat_id: int) -> tuple[str, str]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection, _chat_id(chat_id), self._now(None))
            return self._member_ceiling(state)

    def resolve(
        self, chat_id: int, default_model: str, default_effort: str, *, now: float | None = None,
    ) -> AnswerProfile:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._state(connection, _chat_id(chat_id), self._now(now))
            return state.answer((default_model, default_effort))

    @staticmethod
    def _check_mutation(
        connection: sqlite3.Connection, state: _ChatSettings, expected_revision: int | None, role: str,
    ) -> None:
        if role not in ("owner", "admin", "member"):
            raise InvalidModelSelection("invalid_settings_role")
        if state.revision is None:
            raise InvalidModelSelection("malformed_settings_revision")
        if expected_revision is not None and state.revision != expected_revision:
            # _state may have restored an expired timer before detecting the stale card.
            connection.commit()
            raise StaleSettingsRevision("settings_changed")

    def apply(
        self, chat_id: int, model: str, effort: str,
        duration_seconds: int | None = None, *, expected_revision: int | None = None,
        role: str = "admin", now: float | None = None,
    ) -> AnswerProfile:
        chat_id = _chat_id(chat_id)
        requested = validate_selection(model, effort)
        _expected_revision(expected_revision)
        if duration_seconds is not None and (
            type(duration_seconds) is not int or not 1 <= duration_seconds <= MAX_DURATION_SECONDS
        ):
            raise InvalidModelSelection("invalid_duration")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = self._now(now)
            state = self._state(connection, chat_id, timestamp)
            self._check_mutation(connection, state, expected_revision, role)
            expires_at = timestamp + duration_seconds if duration_seconds is not None else None
            if role == "member":
                ceiling = self._member_ceiling(state)
                limit = member_duration_limit(*requested, ceiling)
                if limit == 0 or (limit is not None and (duration_seconds is None or duration_seconds > limit)):
                    raise InvalidModelSelection("member_profile_limit")
                if expires_at is not None and state.expires_at is not None:
                    continuing_boost = limit is not None and _profile_rank(state.temporary) > _profile_rank(ceiling)
                    if requested == state.temporary or continuing_boost:
                        expires_at = min(expires_at, state.expires_at)
            if duration_seconds is None:
                owner = requested if role == "owner" else state.owner
                changed = _ChatSettings(state.revision, owner, persistent=requested)
            else:
                changed = replace(state, temporary=requested, expires_at=expires_at)
            if changed != state:
                changed = replace(changed, revision=_next_revision(state.revision))
                self._write(connection, chat_id, changed, timestamp)
            return changed.answer(requested)

    def reset(
        self, chat_id: int, *, expected_revision: int | None = None,
        role: str = "admin", default_profile: tuple[str, str] | None = None, now: float | None = None,
    ) -> int:
        chat_id = _chat_id(chat_id)
        _expected_revision(expected_revision)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = self._now(now)
            state = self._state(connection, chat_id, timestamp)
            self._check_mutation(connection, state, expected_revision, role)
            owner = None if role == "owner" else state.owner
            persistent = owner
            if role == "member" and owner is None:
                # Clearing to the global runtime default must not select a profile
                # that the member could not have made persistent directly.
                default = _stored_profile(*default_profile) if default_profile else None
                if default is None or member_duration_limit(*default, self.member_default) is not None:
                    persistent = self.member_default
            revision = _next_revision(state.revision)
            self._write(connection, chat_id, _ChatSettings(revision, owner, persistent=persistent), timestamp)
            return revision
