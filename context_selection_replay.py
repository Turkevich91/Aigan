from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Iterable, Mapping


REVIEW_PACKET_SCHEMA_VERSION = "context-selection-review-packet-v3"
REVIEW_MANIFEST_SCHEMA_VERSION = "context-selection-review-manifest-v3"
PRIVATE_ROOT_RELATIVE = Path("data/research/context-selection-v1")
REVIEW_POOL_FILENAME = "review-pool-v3.jsonl"
REVIEW_MANIFEST_FILENAME = "review-pool-v3-manifest.json"
KEY_FILENAME = "replay-key.bin"
WORD_RE = re.compile(r"\w+", flags=re.UNICODE)
LEADING_INVOCATION_RE = re.compile(r"^\s*(?:/[A-Za-z0-9_@-]+|@[A-Za-z0-9_]+)\s*", flags=re.UNICODE)
CLUSTER_INACTIVITY_MINUTES = 30
_MESSAGE_REPLAY_COLUMNS = (
    "id",
    "chat_id",
    "message_id",
    "chat_type",
    "created_at",
    "sender_label",
    "user_id",
    "username",
    "is_bot",
    "text",
    "content_kind",
    "attachment_type",
    "vision_summary",
    "source_title",
    "reply_to_message_id",
    "forward_origin",
    "source_text",
)
_MESSAGE_REPLAY_COLUMNS_SQL = ", ".join(_MESSAGE_REPLAY_COLUMNS)
_QUALIFIED_MESSAGE_REPLAY_COLUMNS_SQL = ", ".join(
    f"m.{column}" for column in _MESSAGE_REPLAY_COLUMNS
)
SUMMARY_ROUTE_BUCKETS = frozenset(
    {
        "auto_response",
        "internet_image_analysis",
        "internet_image_send",
        "manual_proactive",
        "memory_recall",
        "memory_search",
        "normal",
        "proactive",
        "prompt_privacy",
        "reaction_explanation",
        "reminder",
        "reminder_clarification",
        "time_sensitive",
        "translate_reference",
        "unknown",
        "unknown_historical",
        "vision",
    }
)


class ContextSelectionReplayError(RuntimeError):
    """Bounded private-replay failure that must never include payload text."""


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _opaque(key: bytes, kind: str, value: Any) -> str:
    digest = hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{kind}-{digest[:32]}"


def _visible_text(row: Mapping[str, Any]) -> str:
    parts = [
        row.get("text") or "",
        row.get("source_text") or "",
        row.get("source_title") or "",
        row.get("vision_summary") or "",
    ]
    return "\n".join(part for part in parts if part).strip()


def _suggested_classes(row: Mapping[str, Any]) -> list[str]:
    text = LEADING_INVOCATION_RE.sub("", _visible_text(row), count=1)
    normalized = " ".join(WORD_RE.findall(text.casefold()))
    words = normalized.split()
    suggestions: list[str] = []
    if row.get("reply_to_message_id") is not None:
        suggestions.append("explicit_reply")
    if 0 < len(words) <= 4:
        suggestions.append("short_followup")
    if re.search(r"\b(?:same|again|так\s+само|те\s+ж|той\s+же|то\s+же|такой\s+же)\b", normalized):
        suggestions.append("same_question_transform")
    if re.search(r"\b(?:now|current|currently|latest|зараз|тепер|актуаль\w*|сейчас|теперь)\b", normalized):
        suggestions.append("knowledge_update")
    if re.search(r"\b(?:wrong|corrected?|fix|помил\w*|виправ\w*|неправ\w*|исправ\w*)\b", normalized):
        suggestions.append("correction_stale_guardrail")
    if not suggestions:
        suggestions.append("topic_shift_distractors")
    return list(dict.fromkeys(suggestions))


def _summary_route_bucket(route: str) -> str:
    return route if route in SUMMARY_ROUTE_BUCKETS else "other"


def _rows(connection: sqlite3.Connection, query: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(connection.execute(query, tuple(parameters)).fetchall())


HistoryEntry = tuple[datetime, int, sqlite3.Row]


class _MessageHistoryIndex:
    """One parsed chronological index shared by every replay packet."""

    def __init__(self, rows: Iterable[sqlite3.Row]) -> None:
        by_chat: dict[int, list[HistoryEntry]] = {}
        by_external_id: dict[tuple[int, int], list[HistoryEntry]] = {}
        for row in rows:
            created_at = _parse_timestamp(row["created_at"])
            if created_at is None or row["chat_id"] is None:
                continue
            chat_id = int(row["chat_id"])
            entry = (created_at, int(row["id"]), row)
            by_chat.setdefault(chat_id, []).append(entry)
            if row["message_id"] is not None:
                key = (chat_id, int(row["message_id"]))
                by_external_id.setdefault(key, []).append(entry)

        self._by_chat = {
            chat_id: tuple(sorted(entries, key=lambda item: (item[0], item[1])))
            for chat_id, entries in by_chat.items()
        }
        self._chat_keys = {
            chat_id: tuple((created_at, row_id) for created_at, row_id, _row in entries)
            for chat_id, entries in self._by_chat.items()
        }
        self._by_external_id = {
            key: tuple(sorted(entries, key=lambda item: (item[0], item[1])))
            for key, entries in by_external_id.items()
        }
        self._external_keys = {
            key: tuple((created_at, row_id) for created_at, row_id, _row in entries)
            for key, entries in self._by_external_id.items()
        }

    @staticmethod
    def _cutoff_position(
        keys: tuple[tuple[datetime, int], ...],
        *,
        cutoff_time: datetime,
        cutoff_id: int | None,
    ) -> int:
        if cutoff_id is None:
            return bisect_right(keys, (cutoff_time, 2**63 - 1))
        return bisect_left(keys, (cutoff_time, cutoff_id))

    def preceding(
        self,
        *,
        chat_id: int,
        cutoff_time: datetime,
        cutoff_id: int | None = None,
        is_bot: bool | None = None,
        user_id: int | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        entries = self._by_chat.get(chat_id, ())
        keys = self._chat_keys.get(chat_id, ())
        position = self._cutoff_position(keys, cutoff_time=cutoff_time, cutoff_id=cutoff_id)
        selected: list[sqlite3.Row] = []
        for index in range(position - 1, -1, -1):
            row = entries[index][2]
            if is_bot is not None and bool(row["is_bot"]) is not is_bot:
                continue
            if user_id is not None and row["user_id"] != user_id:
                continue
            selected.append(row)
            if limit is not None and len(selected) >= limit:
                break
        return selected

    def external_before(
        self,
        *,
        chat_id: int,
        message_id: int,
        cutoff_time: datetime,
        cutoff_id: int,
        is_bot: bool | None = None,
    ) -> sqlite3.Row | None:
        key = (chat_id, message_id)
        entries = self._by_external_id.get(key, ())
        keys = self._external_keys.get(key, ())
        position = self._cutoff_position(keys, cutoff_time=cutoff_time, cutoff_id=cutoff_id)
        for index in range(position - 1, -1, -1):
            row = entries[index][2]
            if is_bot is None or bool(row["is_bot"]) is is_bot:
                return row
        return None

    def cluster_key(self, key: bytes, row: Mapping[str, Any]) -> str:
        target_time = _parse_timestamp(row.get("created_at"))
        if target_time is None or row.get("chat_id") is None or row.get("id") is None:
            raise ContextSelectionReplayError("cluster key source is incomplete")
        chat_id = int(row["chat_id"])
        target_id = int(row["id"])
        entries = self._by_chat.get(chat_id, ())
        keys = self._chat_keys.get(chat_id, ())
        position = bisect_left(keys, (target_time, target_id))
        if position >= len(keys) or keys[position] != (target_time, target_id):
            raise ContextSelectionReplayError("cluster target is absent from replay history")
        session_start = entries[position][:2]
        inactivity = timedelta(minutes=CLUSTER_INACTIVITY_MINUTES)
        for index in range(position - 1, -1, -1):
            previous = entries[index][:2]
            if session_start[0] - previous[0] > inactivity:
                break
            session_start = previous
        material = f"{chat_id}:{session_start[0].isoformat()}:{session_start[1]}"
        return _opaque(key, "cluster", material)


def _target_candidates(
    connection: sqlite3.Connection,
    *,
    history: _MessageHistoryIndex,
    lookback_days: int,
    approximate_window_seconds: int,
    historical_output_window_seconds: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    exact_rows = _rows(
        connection,
        f"""
        SELECT p.run_id, p.input_memory_id, p.route, p.status,
               p.started_at, p.completed_at, {_QUALIFIED_MESSAGE_REPLAY_COLUMNS_SQL}
        FROM provenance_runs AS p
        JOIN messages AS m ON m.id = p.input_memory_id
        WHERE m.is_bot = 0
        ORDER BY p.started_at, p.input_memory_id
        """,
    )
    route_events = _rows(
        connection,
        """
        SELECT id, created_at, chat_id, user_id, route
        FROM system_events
        WHERE component = 'routing' AND event_type = 'route_decision'
        ORDER BY created_at, id
        """,
    )
    all_historical_outputs = _rows(
        connection,
        f"SELECT {_MESSAGE_REPLAY_COLUMNS_SQL} "
        "FROM messages WHERE is_bot = 1 ORDER BY created_at, id",
    )
    reference_times = [
        parsed
        for parsed in (
            *(_parse_timestamp(row["started_at"]) for row in exact_rows),
            *(_parse_timestamp(row["created_at"]) for row in route_events),
            *(_parse_timestamp(row["created_at"]) for row in all_historical_outputs),
        )
        if parsed is not None
    ]
    if not reference_times:
        raise ContextSelectionReplayError("no replay target evidence")
    cutoff = max(reference_times) - timedelta(days=lookback_days)

    candidates: list[dict[str, Any]] = []
    exact_target_ids: set[int] = set()
    for row in exact_rows:
        started_at = _parse_timestamp(row["started_at"])
        if started_at is None or started_at < cutoff:
            continue
        memory_id = int(row["input_memory_id"])
        exact_target_ids.add(memory_id)
        candidates.append(
            {
                "row": dict(row),
                "route": str(row["route"] or "unknown"),
                "correlation_kind": "exact_provenance",
                "correlation_gap_ms": 0,
                "run_id": str(row["run_id"]),
                "run_status": str(row["status"] or "unknown"),
                "run_started_at": str(row["started_at"] or ""),
                "run_completed_at": str(row["completed_at"] or ""),
                "packet_material": f"run:{row['run_id']}",
            }
        )

    approximate_target_ids = set(exact_target_ids)

    unmatched_route_events = 0
    approximate_matches = 0
    for event in route_events:
        event_time = _parse_timestamp(event["created_at"])
        if event_time is None or event_time < cutoff or event["chat_id"] is None:
            continue
        possible = history.preceding(
            chat_id=int(event["chat_id"]),
            cutoff_time=event_time,
            is_bot=False,
            user_id=int(event["user_id"]) if event["user_id"] is not None else None,
            limit=5,
        )
        selected: sqlite3.Row | None = None
        selected_gap_ms = 0
        for message in possible:
            message_time = _parse_timestamp(message["created_at"])
            if message_time is None or not _visible_text(dict(message)):
                continue
            gap_ms = int((event_time - message_time).total_seconds() * 1000)
            if 0 <= gap_ms <= approximate_window_seconds * 1000:
                selected = message
                selected_gap_ms = gap_ms
                break
        if selected is None:
            unmatched_route_events += 1
            continue
        memory_id = int(selected["id"])
        if memory_id in approximate_target_ids:
            continue
        approximate_target_ids.add(memory_id)
        candidates.append(
            {
                "row": dict(selected),
                "route": str(event["route"] or "unknown"),
                "correlation_kind": "approximate_route_event",
                "correlation_gap_ms": selected_gap_ms,
                "run_id": None,
                "run_status": None,
                "run_started_at": None,
                "run_completed_at": None,
                "packet_material": f"route-event:{event['id']}",
            }
        )
        approximate_matches += 1

    provenance_output_ids = {
        int(row["memory_id"])
        for row in _rows(
            connection,
            "SELECT memory_id FROM provenance_outputs WHERE memory_id IS NOT NULL",
        )
    }
    historical_matches = 0
    outbound_reply_matches = 0
    unmatched_historical_outputs = 0
    historical_outputs = [
        row
        for row in all_historical_outputs
        if (_parse_timestamp(row["created_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
    ]
    for output in historical_outputs:
        if int(output["id"]) in provenance_output_ids or output["chat_id"] is None:
            continue
        output_time = _parse_timestamp(output["created_at"])
        if output_time is None:
            continue
        selected: sqlite3.Row | None = None
        selected_gap_ms = 0
        correlation_kind = "outbound_reply_link"
        reply_target = output["reply_to_message_id"]
        if reply_target is not None:
            replied = history.external_before(
                chat_id=int(output["chat_id"]),
                message_id=int(reply_target),
                cutoff_time=output_time,
                cutoff_id=int(output["id"]),
                is_bot=False,
            )
            if replied is not None:
                replied_time = _parse_timestamp(replied["created_at"])
                if replied_time is not None and (
                    replied_time < output_time
                    or (replied_time == output_time and int(replied["id"]) < int(output["id"]))
                ):
                    selected = replied
                    selected_gap_ms = int((output_time - replied_time).total_seconds() * 1000)

        possible = history.preceding(
            chat_id=int(output["chat_id"]),
            cutoff_time=output_time,
            cutoff_id=int(output["id"]),
            is_bot=False,
            limit=12,
        )
        for message in (possible if selected is None else ()):
            message_time = _parse_timestamp(message["created_at"])
            if message_time is None or not _visible_text(dict(message)):
                continue
            gap_ms = int((output_time - message_time).total_seconds() * 1000)
            if gap_ms < 0 or gap_ms > historical_output_window_seconds * 1000:
                continue
            text = str(message["text"] or "").lstrip()
            likely_invocation = str(message["chat_type"] or "").casefold() == "private"
            likely_invocation = likely_invocation or bool(
                re.match(r"^(?:/[A-Za-z0-9_@-]+|@[A-Za-z0-9_]+)\b", text)
            )
            reply_target = message["reply_to_message_id"]
            if reply_target is not None:
                replied = history.external_before(
                    chat_id=int(message["chat_id"]),
                    message_id=int(reply_target),
                    cutoff_time=message_time,
                    cutoff_id=int(message["id"]),
                    is_bot=True,
                )
                likely_invocation = likely_invocation or replied is not None
            if likely_invocation:
                selected = message
                selected_gap_ms = gap_ms
                correlation_kind = "approximate_bot_output"
                break
        if selected is None:
            unmatched_historical_outputs += 1
            continue
        memory_id = int(selected["id"])
        if memory_id in approximate_target_ids:
            continue
        approximate_target_ids.add(memory_id)
        candidates.append(
            {
                "row": dict(selected),
                "route": "unknown_historical",
                "correlation_kind": correlation_kind,
                "correlation_gap_ms": selected_gap_ms,
                "run_id": None,
                "run_status": None,
                "run_started_at": None,
                "run_completed_at": None,
                "packet_material": f"historical-output:{output['id']}",
            }
        )
        if correlation_kind == "outbound_reply_link":
            outbound_reply_matches += 1
        else:
            historical_matches += 1

    ordered = sorted(
        candidates,
        key=lambda item: (
            _parse_timestamp(item["row"].get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            int(item["row"]["id"]),
            _parse_timestamp(item["run_started_at"])
            or datetime.min.replace(tzinfo=timezone.utc),
            str(item["packet_material"]),
        ),
    )
    exact_counts = Counter(int(item["row"]["id"]) for item in ordered if item["run_id"] is not None)
    return ordered, {
        "exact_provenance": sum(item["correlation_kind"] == "exact_provenance" for item in ordered),
        "exact_provenance_unique_targets": len(exact_counts),
        "repeated_exact_targets": sum(count > 1 for count in exact_counts.values()),
        "approximate_route_event": approximate_matches,
        "outbound_reply_link": outbound_reply_matches,
        "approximate_bot_output": historical_matches,
        "unmatched_route_events": unmatched_route_events,
        "unmatched_historical_outputs": unmatched_historical_outputs,
    }


def _reply_chain(
    history: _MessageHistoryIndex,
    *,
    chat_id: int,
    reply_to_message_id: int | None,
    target_time: datetime,
    target_id: int,
    depth: int,
) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current = reply_to_message_id
    cutoff_time = target_time
    cutoff_id = target_id
    seen: set[int] = set()
    while current is not None and len(chain) < depth and current not in seen:
        seen.add(current)
        row = history.external_before(
            chat_id=chat_id,
            message_id=int(current),
            cutoff_time=cutoff_time,
            cutoff_id=cutoff_id,
        )
        if row is None:
            break
        chain.append(dict(row))
        current = row["reply_to_message_id"]
        parsed = _parse_timestamp(row["created_at"])
        if parsed is None:
            break
        cutoff_time = parsed
        cutoff_id = int(row["id"])
    return chain


def _candidate_sources(
    history: _MessageHistoryIndex,
    target: Mapping[str, Any],
    *,
    recent_limit: int,
    reply_depth: int,
) -> list[dict[str, Any]]:
    target_time = _parse_timestamp(target.get("created_at"))
    if target_time is None:
        raise ContextSelectionReplayError("target has invalid timestamp")
    recent = [
        dict(row)
        for row in history.preceding(
            chat_id=int(target["chat_id"]),
            cutoff_time=target_time,
            cutoff_id=int(target["id"]),
            limit=recent_limit,
        )
    ]
    chain = _reply_chain(
        history,
        chat_id=int(target["chat_id"]),
        reply_to_message_id=target.get("reply_to_message_id"),
        target_time=target_time,
        target_id=int(target["id"]),
        depth=reply_depth,
    )
    history_rows = []
    for row in (*recent, *chain):
        created_at = _parse_timestamp(row.get("created_at"))
        if created_at is None:
            continue
        row_id = int(row["id"])
        if created_at < target_time or (created_at == target_time and row_id < int(target["id"])):
            history_rows.append(row)
    by_id = {int(row["id"]): row for row in history_rows}
    return sorted(
        by_id.values(),
        key=lambda row: (
            _parse_timestamp(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
            int(row["id"]),
        ),
    )


def _provenance_maps(
    connection: sqlite3.Connection,
) -> tuple[dict[int, int], dict[int, list[int]], dict[str, list[int]]]:
    run_inputs = {
        str(row["run_id"]): int(row["input_memory_id"])
        for row in _rows(
            connection,
            "SELECT run_id, input_memory_id FROM provenance_runs WHERE input_memory_id IS NOT NULL",
        )
    }
    output_to_input: dict[int, int] = {}
    output_lists: dict[int, list[int]] = {}
    run_output_lists: dict[str, list[int]] = {}
    for row in _rows(
        connection,
        "SELECT run_id, memory_id FROM provenance_outputs WHERE memory_id IS NOT NULL ORDER BY ordinal",
    ):
        input_id = run_inputs.get(str(row["run_id"]))
        if input_id is None:
            continue
        output_id = int(row["memory_id"])
        output_to_input[output_id] = input_id
        output_lists.setdefault(input_id, []).append(output_id)
        run_output_lists.setdefault(str(row["run_id"]), []).append(output_id)
    return output_to_input, output_lists, run_output_lists


def _speaker_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    if row.get("user_id") is not None:
        return "user_id", str(int(row["user_id"]))
    if row.get("username"):
        return "username", str(row["username"]).casefold()
    if row.get("sender_label"):
        return "sender_label", str(row["sender_label"]).casefold()
    if row.get("chat_id") is not None:
        return "unknown_chat", str(int(row["chat_id"]))
    return "unknown", "unknown"


def _message_packet(
    row: Mapping[str, Any],
    *,
    key: bytes,
    source_key_by_id: Mapping[int, str],
    message_key_by_telegram_id: Mapping[int, str],
    output_to_input: Mapping[int, int],
    input_to_outputs: Mapping[int, list[int]],
) -> dict[str, Any]:
    row = dict(row)
    memory_id = int(row["id"])
    speaker_identity_kind, speaker_identity_value = _speaker_identity(row)
    reply_target = row.get("reply_to_message_id")
    trigger_id = output_to_input.get(memory_id)
    output_ids = input_to_outputs.get(memory_id, [])
    return {
        "source_key": source_key_by_id[memory_id],
        "created_at": str(row.get("created_at") or ""),
        "speaker_key": _opaque(
            key,
            "speaker",
            f"{speaker_identity_kind}:{speaker_identity_value}",
        ),
        "speaker_identity_kind": speaker_identity_kind,
        "is_bot": bool(row.get("is_bot")),
        "source_kind": str(row.get("content_kind") or row.get("attachment_type") or "text"),
        "text": str(row.get("text") or ""),
        "source_text": str(row.get("source_text") or ""),
        "source_title": str(row.get("source_title") or ""),
        "vision_summary": str(row.get("vision_summary") or ""),
        "forward_origin": str(row.get("forward_origin") or ""),
        "reply_to_source_key": message_key_by_telegram_id.get(int(reply_target))
        if reply_target is not None
        else None,
        "provenance_trigger_source_key": source_key_by_id.get(trigger_id) if trigger_id is not None else None,
        "provenance_output_source_keys": [
            source_key_by_id[output_id] for output_id in output_ids if output_id in source_key_by_id
        ],
    }


def collect_review_packets(
    connection: sqlite3.Connection,
    *,
    hmac_key: bytes,
    lookback_days: int = 30,
    recent_limit: int = 80,
    reply_depth: int = 8,
    approximate_window_seconds: int = 20,
    historical_output_window_seconds: int = 180,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(hmac_key) < 32:
        raise ContextSelectionReplayError("invalid replay key")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    history = _MessageHistoryIndex(
        _rows(connection, f"SELECT {_MESSAGE_REPLAY_COLUMNS_SQL} FROM messages")
    )
    targets, correlation_counts = _target_candidates(
        connection,
        history=history,
        lookback_days=lookback_days,
        approximate_window_seconds=approximate_window_seconds,
        historical_output_window_seconds=historical_output_window_seconds,
    )
    output_to_input, input_to_outputs, run_to_outputs = _provenance_maps(connection)
    packets: list[dict[str, Any]] = []
    route_counts: Counter[str] = Counter()
    suggestion_counts: Counter[str] = Counter()
    source_total = 0
    for target_item in targets:
        target = target_item["row"]
        candidates = _candidate_sources(
            history,
            target,
            recent_limit=recent_limit,
            reply_depth=reply_depth,
        )
        all_rows = [*candidates, target]
        source_key_by_id = {
            int(row["id"]): _opaque(hmac_key, "source", int(row["id"])) for row in all_rows
        }
        message_key_by_telegram_id = {
            int(row["message_id"]): source_key_by_id[int(row["id"])]
            for row in all_rows
            if row.get("message_id") is not None
        }
        suggestions = _suggested_classes(target)
        route = str(target_item["route"] or "unknown")
        route_counts[_summary_route_bucket(route)] += 1
        suggestion_counts.update(suggestions)
        source_total += len(candidates)
        target_key = source_key_by_id[int(target["id"])]
        run_id = target_item["run_id"]
        packets.append(
            {
                "schema_version": REVIEW_PACKET_SCHEMA_VERSION,
                "packet_key": _opaque(hmac_key, "packet", target_item["packet_material"]),
                "case_key": _opaque(hmac_key, "case", int(target["id"])),
                "cluster_key": history.cluster_key(hmac_key, target),
                "cluster_method": "inactivity_session",
                "cluster_inactivity_minutes": CLUSTER_INACTIVITY_MINUTES,
                "run_key": _opaque(hmac_key, "run", run_id) if run_id is not None else None,
                "run_status": target_item["run_status"],
                "run_started_at": target_item["run_started_at"],
                "run_completed_at": target_item["run_completed_at"],
                "run_output_source_keys": [
                    _opaque(hmac_key, "source", output_id)
                    for output_id in run_to_outputs.get(str(run_id), ())
                ]
                if run_id is not None
                else [],
                "correlation_kind": target_item["correlation_kind"],
                "correlation_gap_ms": int(target_item["correlation_gap_ms"]),
                "route": route,
                "target_time": str(target.get("created_at") or ""),
                "target": _message_packet(
                    target,
                    key=hmac_key,
                    source_key_by_id=source_key_by_id,
                    message_key_by_telegram_id=message_key_by_telegram_id,
                    output_to_input=output_to_input,
                    input_to_outputs=input_to_outputs,
                ),
                "sources": [
                    _message_packet(
                        row,
                        key=hmac_key,
                        source_key_by_id=source_key_by_id,
                        message_key_by_telegram_id=message_key_by_telegram_id,
                        output_to_input=output_to_input,
                        input_to_outputs=input_to_outputs,
                    )
                    for row in candidates
                ],
                "suggested_classes": suggestions,
                "review": {
                    "include": None,
                    "eligibility_class": None,
                    "acceptable_anchor_source_keys": [],
                    "required_source_keys": [],
                    "optional_source_keys": [],
                    "forbidden_source_keys": [],
                    "stale_source_keys": [],
                    "wrong_speaker_source_keys": [],
                    "expected_action": None,
                    "notes": None,
                },
                "integrity": {
                    "target_source_key": target_key,
                    "history_only": True,
                    "recent_limit": recent_limit,
                    "reply_depth": reply_depth,
                },
            }
        )
    summary = {
        "packet_count": len(packets),
        "candidate_source_count": source_total,
        "correlation_counts": dict(sorted(correlation_counts.items())),
        "route_counts": dict(sorted(route_counts.items())),
        "suggested_class_counts": dict(sorted(suggestion_counts.items())),
        "lookback_days": lookback_days,
        "recent_limit": recent_limit,
        "reply_depth": reply_depth,
        "historical_output_window_seconds": historical_output_window_seconds,
        "cluster_method": "inactivity_session",
        "cluster_inactivity_minutes": CLUSTER_INACTIVITY_MINUTES,
        "run_level_packets_preserved": True,
    }
    return packets, summary


def _assert_private_permissions(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        raise ContextSelectionReplayError("private replay writing requires POSIX owner-only permissions")
    mode = path.stat().st_mode & 0o777
    expected = 0o700 if directory else 0o600
    if mode != expected:
        raise ContextSelectionReplayError("unsafe private artifact permissions")


def _secure_private_root(root: Path) -> None:
    if root.exists() and root.is_symlink():
        raise ContextSelectionReplayError("private root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(root, 0o700)
    _assert_private_permissions(root, directory=True)


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ContextSelectionReplayError("invalid private key artifact")
        _assert_private_permissions(path, directory=False)
        key = path.read_bytes()
        if len(key) != 32:
            raise ContextSelectionReplayError("invalid private key length")
        return key
    key = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, key)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _assert_private_permissions(path, directory=False)
    return key


def _write_exclusive(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ContextSelectionReplayError("private output already exists")
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        _assert_private_permissions(temporary, directory=False)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ContextSelectionReplayError("private output already exists") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    _assert_private_permissions(path, directory=False)


def build_private_review_pool(
    *,
    database_path: Path,
    private_root: Path,
    lookback_days: int = 30,
    recent_limit: int = 80,
    reply_depth: int = 8,
    historical_output_window_seconds: int = 180,
) -> dict[str, Any]:
    if os.name == "nt":
        raise ContextSelectionReplayError("private replay writing is unsupported on Windows")
    if database_path.is_symlink() or not database_path.is_file():
        raise ContextSelectionReplayError("invalid replay database")
    _secure_private_root(private_root)
    key = _load_or_create_key(private_root / KEY_FILENAME)
    output_path = private_root / REVIEW_POOL_FILENAME
    manifest_path = private_root / REVIEW_MANIFEST_FILENAME
    if output_path.exists() or manifest_path.exists():
        raise ContextSelectionReplayError("private review pool already exists")
    uri = f"{database_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("BEGIN")
        packets, summary = collect_review_packets(
            connection,
            hmac_key=key,
            lookback_days=lookback_days,
            recent_limit=recent_limit,
            reply_depth=reply_depth,
            historical_output_window_seconds=historical_output_window_seconds,
        )
        connection.rollback()
    finally:
        connection.close()
    payload = "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n" for packet in packets).encode(
        "utf-8"
    )
    pool_sha256 = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": REVIEW_MANIFEST_SCHEMA_VERSION,
        **summary,
        "pool_sha256": pool_sha256,
        "contains_private_payloads": True,
        "github_publishable": False,
        "labels_complete": False,
        "efficacy_evidence": False,
    }
    manifest_payload = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _write_exclusive(output_path, payload)
    _write_exclusive(manifest_path, manifest_payload)
    return manifest
