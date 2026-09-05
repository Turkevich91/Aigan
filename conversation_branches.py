"""Bounded reply edges from retained originals; this is not a forum-topic graph.

The caller authorizes an already examined, unchanged anchor and owns session
budgets/citations. This layer independently restricts every SQL read to its fixed
chat/cutoff and optional authored identity. It never returns complete MemoryItems.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or (positive and value <= 0):
        raise ValueError(f"Invalid {name}")
    return value


def _timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid date bound")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def select_conversation_branch(
    connection: sqlite3.Connection, *, chat_id: int, cutoff_memory_id: int,
    cutoff_created_at: str, anchor_id: int, limit: int = 20,
    participant_id: int | None = None, authored_only: bool = False,
    after: str = "", before: str = "", include_neighbors: bool = False,
    max_depth: int = 6, max_chars: int = 12000,
    expected_anchor_digest: str | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Select closest ancestors and breadth-first descendants under hard caps.

    ``relations`` and ``drop_priority`` are host metadata keyed by memory row ID,
    not model-authored graph claims. The session must prune both after its final
    character/citation wrapping. Chronological neighbors (at most two) are only
    added when explicitly requested and spare slots remain. A filtered edge is
    not traversed: excluded speakers cannot act as hidden context bridges.

    The caller holds the MemoryStore lock for the entire read. This function
    performs no writes, schema changes, provider calls, or global-state changes.
    """
    _integer(chat_id, "chat")
    _integer(cutoff_memory_id, "cutoff", positive=True)
    _integer(anchor_id, "anchor", positive=True)
    if participant_id is not None:
        _integer(participant_id, "participant", positive=True)
    if not isinstance(authored_only, bool) or not isinstance(include_neighbors, bool):
        raise ValueError("Flags must be boolean")
    if authored_only and participant_id is None:
        raise ValueError("Authored evidence requires a fixed identity")
    if expected_anchor_digest is not None and (
        not isinstance(expected_anchor_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_anchor_digest) is None
    ):
        raise ValueError("Invalid anchor digest")
    limit = max(1, min(20, _integer(limit, "limit")))
    max_depth = max(1, min(6, _integer(max_depth, "depth")))
    max_chars = max(1024, min(12000, _integer(max_chars, "character budget")))
    cutoff = _timestamp(cutoff_created_at)
    start, end = _timestamp(after) if after else "", _timestamp(before) if before else ""
    if start and end and start >= end:
        raise ValueError("Empty date interval")

    base = "m.chat_id = ? AND m.id < ? AND julianday(m.created_at) <= julianday(?)"
    base_params: list[object] = [chat_id, cutoff_memory_id, cutoff]
    restrictions: list[str] = []
    extra: list[object] = []
    if participant_id is not None:
        restrictions.append("m.user_id = ?")
        extra.append(participant_id)
    if start:
        restrictions.append("julianday(m.created_at) >= julianday(?)")
        extra.append(start)
    if end:
        restrictions.append("julianday(m.created_at) < julianday(?)")
        extra.append(end)
    if authored_only:
        restrictions.extend([
            "m.is_bot = 0", "trim(m.text) != ''", "m.text NOT LIKE '[message has %'",
            "m.forward_origin = ''", "m.content_kind = 'text'", "ltrim(m.text) NOT LIKE '/%'",
        ])
    filter_sql = " AND ".join(restrictions) if restrictions else "1"
    where = f"{base} AND ({filter_sql})"
    params = [*base_params, *extra]
    summary = "''" if authored_only else "substr(m.vision_summary, 1, 1001)"
    source = "''" if authored_only else "substr(m.source_text, 1, 1001)"
    projection = f"""m.id, m.message_id, m.user_id, m.created_at,
        substr(m.sender_label, 1, 97) AS sender_label, substr(m.text, 1, 1001) AS text,
        {source} AS source_text, substr(m.attachment_type, 1, 49) AS attachment_type,
        {summary} AS attachment_summary, m.reply_to_message_id,
        julianday(m.created_at) AS sort_time,
        (m.forward_origin != '') AS is_forwarded,
        history_evidence_digest(m.id,m.message_id,m.user_id,m.created_at,m.sender_label,
            m.text,m.source_text,m.attachment_type,m.vision_summary,m.reply_to_message_id,
            m.is_bot,m.content_kind,m.forward_origin) AS evidence_digest"""
    flags = dict(missing_parent=False, filtered_nodes=False, cycle_detected=False,
                 depth_cap=False, node_cap=False, character_cap=False)
    metadata: dict[str, object] = {
        "selection": "connected_reply_branch", "complete_history": False,
        "complete_topic": False, "retained_messages_only": True,
        "historical_thread_ids_available": False, "authored_only": authored_only,
        "neighbors_requested": include_neighbors,
        "anchor_available": False, "anchor_changed": False, "relations": {}, "drop_priority": [],
        "partial": False, "limits": flags,
    }
    anchor = connection.execute(
        f"SELECT {projection} FROM messages m WHERE {where} AND m.id = ? LIMIT 1",
        (*params, anchor_id),
    ).fetchone()
    if anchor is None:
        return [], metadata
    if expected_anchor_digest is not None and anchor["evidence_digest"] != expected_anchor_digest:
        metadata["anchor_changed"] = True
        metadata["partial"] = True
        return [], metadata
    metadata["anchor_available"] = True
    chosen: dict[int, dict[str, object]] = {}
    relations: dict[str, dict[str, object]] = {}
    admission_order: list[int] = []

    def admit(row: sqlite3.Row, relation: str, depth: int) -> None:
        chosen[row["id"]] = dict(row)
        relations[str(row["id"])] = {"relation": relation, "depth": depth}
        admission_order.append(row["id"])

    admit(anchor, "anchor", 0)
    parent_id = anchor["reply_to_message_id"]
    seen_message_ids = {anchor["message_id"]} if anchor["message_id"] is not None else set()
    depth = 0
    while parent_id is not None:
        if parent_id in seen_message_ids:
            flags["cycle_detected"] = True
            break
        if depth >= max_depth or len(chosen) >= limit:
            flags["depth_cap" if depth >= max_depth else "node_cap"] = True
            break
        parent = connection.execute(
            f"SELECT {projection} FROM messages m WHERE {where} AND m.message_id = ? LIMIT 1",
            (*params, parent_id),
        ).fetchone()
        if parent is None:
            # Do not look across chat/cutoff even to distinguish why an edge is absent.
            available = connection.execute(
                f"SELECT 1 FROM messages m WHERE {base} AND m.message_id = ? LIMIT 1",
                (*base_params, parent_id),
            ).fetchone()
            flags["filtered_nodes" if available else "missing_parent"] = True
            break
        depth += 1
        admit(parent, "ancestor", depth)
        seen_message_ids.add(parent_id)
        parent_id = parent["reply_to_message_id"]

    frontier = [anchor["message_id"]] if anchor["message_id"] is not None else []
    depth = 0
    while frontier:
        marks = ",".join("?" for _ in frontier)
        edge = f"m.reply_to_message_id IN ({marks})"
        # A boolean is sufficient; never expose excluded row IDs or text.
        if restrictions and connection.execute(
            f"SELECT 1 FROM messages m WHERE {base} AND {edge} AND NOT COALESCE(({filter_sql}), 0) LIMIT 1",
            (*base_params, *frontier, *extra),
        ).fetchone():
            flags["filtered_nodes"] = True
        remaining = max(0, limit - len(chosen))
        children = connection.execute(
            f"""SELECT {projection} FROM messages m WHERE {where} AND {edge}
                ORDER BY julianday(m.created_at), m.id LIMIT ?""",
            (*params, *frontier, remaining + 1),
        ).fetchall()
        if not children:
            break
        if depth >= max_depth:
            flags["depth_cap"] = True
            break
        depth += 1
        next_frontier: list[int] = []
        for child in children:
            if child["id"] in chosen:
                flags["cycle_detected"] = True
                continue
            if len(chosen) >= limit:
                flags["node_cap"] = True
                break
            admit(child, "direct_reply" if depth == 1 else "descendant", depth)
            if child["message_id"] is not None:
                next_frontier.append(child["message_id"])
        frontier = next_frontier

    if include_neighbors and len(chosen) >= limit:
        flags["node_cap"] = True
    if include_neighbors and len(chosen) < limit:
        # At most one nearest unselected row on either side of the anchor.
        for comparison, direction in (("<", "DESC"), (">", "ASC")):
            if len(chosen) >= limit:
                break
            excluded = ",".join("?" for _ in chosen)
            neighbor = connection.execute(
                f"""SELECT {projection} FROM messages m WHERE {where} AND m.id NOT IN ({excluded})
                    AND (julianday(m.created_at) {comparison} ? OR
                        (julianday(m.created_at) = ? AND m.id {comparison} ?))
                    ORDER BY julianday(m.created_at) {direction}, m.id {direction} LIMIT 1""",
                (*params, *chosen, anchor["sort_time"], anchor["sort_time"], anchor_id),
            ).fetchone()
            if neighbor is not None:
                admit(neighbor, "neighbor", 0)

    def payload() -> tuple[list[dict[str, object]], dict[str, object]]:
        metadata["relations"] = relations
        metadata["drop_priority"] = list(reversed(admission_order[1:]))
        metadata["partial"] = any(flags.values())
        rows = sorted(chosen.values(), key=lambda row: (row["sort_time"], row["id"]))
        return rows, metadata

    rows, metadata = payload()
    while len(_json({"messages": rows, "coverage": metadata})) > max_chars and chosen:
        flags["character_cap"] = True
        remove = admission_order.pop()
        del chosen[remove]
        del relations[str(remove)]
        rows, metadata = payload()
    return rows, metadata
