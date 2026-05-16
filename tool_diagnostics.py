from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from system_log import SystemEvent, sanitize_text


STATUS_ORDER = [
    "failing",
    "error",
    "degraded",
    "unavailable",
    "unconfigured",
    "warming",
    "unknown",
    "ok",
    "disabled",
    "not_implemented",
]
DEGRADED_STATUSES = {"degraded", "unavailable", "unconfigured", "warming", "unknown"}
FAILING_STATUSES = {"failing", "error"}
SAFE_HEALTH_FIELDS = {
    "mode",
    "backend",
    "model",
    "family",
    "configured",
    "available",
    "warning_count",
    "runtime_error_count",
    "adapter_count",
    "backlog",
    "dimensions",
    "max_bytes",
    "max_duration_seconds",
    "max_audio_bytes",
    "max_download_bytes",
    "max_pixels",
    "max_extracted_chars",
    "max_frame_count",
    "timestamps_supported",
    "diarization_supported",
    "streaming_supported",
    "local_backend_available",
    "yt_dlp_available",
    "ffmpeg_available",
    "vision_enabled",
    "ocr_enabled",
    "local_ocr_enabled",
    "caption_backend",
    "stt_backend",
    "telegram_download_available",
    "temp_dir_writable",
    "cache_version",
}
URL_VALUE_RE = re.compile(
    r"\b(?:https?|ftp|file)://|www\.|\b(?:[A-Za-z0-9-]{2,}\.)+[A-Za-z]{2,}(?::\d+)?(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)
WINDOWS_PATH_VALUE_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)")
POSIX_PATH_VALUE_RE = re.compile(r"(^|\s)(?:~(?:/|\b)|/(?:[A-Za-z0-9._-]+)(?:/[^/\s]+)*)")
RELATIVE_PATH_VALUE_RE = re.compile(
    r"(^|\s)(?:\.{1,2}[\\/]|(?:data|media|cache|tmp|private|logs?|db|sqlite|downloads?)[\\/]|"
    r"(?:[A-Za-z0-9_.-]+[\\/])+(?:[^\\/\s]*\.(?:sqlite3?|db|jpg|jpeg|png|webp|gif|mp4|mov|mkv|mp3|wav|ogg|json|log|env|txt|pdf)))",
    re.IGNORECASE,
)
TOKEN_VALUE_RE = re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}", re.IGNORECASE)
SAFE_CATEGORY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
ROW_DETAIL_FIELDS = ("adapter_count", "backlog", "dimensions", "max_bytes")


@dataclass
class CapabilityRow:
    name: str
    family: str
    enabled: bool
    configured: bool
    available: bool
    status: str
    adapter: str = ""
    mode: str = ""
    backend: str = ""
    error_count: int = 0
    warning_count: int = 0
    recent_error_count: int = 0
    recent_warning_count: int = 0
    recent_failure_categories: list[str] = field(default_factory=list)
    next_action: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def normalized(self) -> "CapabilityRow":
        self.name = safe_display_label(self.name, 80) or "unknown"
        self.family = clean_label(self.family, 60) or "other"
        self.status = normalize_status(self.status, self.enabled)
        self.adapter = safe_display_label(self.adapter, 80)
        self.mode = safe_display_label(self.mode, 80)
        self.backend = safe_display_label(self.backend, 80)
        self.error_count = max(0, int(self.error_count or 0))
        self.warning_count = max(0, int(self.warning_count or 0))
        self.recent_error_count = max(0, int(self.recent_error_count or 0))
        self.recent_warning_count = max(0, int(self.recent_warning_count or 0))
        if self.status == "ok" and (
            self.error_count or self.warning_count or self.recent_error_count or self.recent_warning_count
        ):
            self.status = "degraded"
        self.recent_failure_categories = [
            category
            for category in (safe_failure_category_label(item, 80) for item in self.recent_failure_categories[:5])
            if category
        ]
        self.next_action = clean_label(self.next_action or next_action_for_status(self.status), 120)
        self.details = sanitize_health_details(self.details)
        return self


def clean_label(value: Any, limit: int = 120) -> str:
    return sanitize_text(str(value or ""), limit)


def safe_display_label(value: Any, limit: int = 120) -> str:
    text = clean_label(value, limit)
    if not text:
        return ""
    if (
        URL_VALUE_RE.search(text)
        or WINDOWS_PATH_VALUE_RE.search(text)
        or POSIX_PATH_VALUE_RE.search(text)
        or RELATIVE_PATH_VALUE_RE.search(text)
        or TOKEN_VALUE_RE.search(text)
    ):
        return "[redacted]"
    return text


def safe_failure_category_label(value: Any, limit: int = 80) -> str:
    text = safe_display_label(value, limit).strip()
    if not text:
        return ""
    if text == "[redacted]":
        return text
    if SAFE_CATEGORY_RE.fullmatch(text):
        return text
    return "freeform"


def normalize_status(status: Any, enabled: bool = False) -> str:
    value = str(status or "").strip().casefold()
    if not value:
        return "ok" if enabled else "disabled"
    return value if value in set(STATUS_ORDER) else "unknown"


def sanitize_health_details(details: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for raw_key, raw_value in (details or {}).items():
        key = clean_label(raw_key, 80)
        if not key or key not in SAFE_HEALTH_FIELDS:
            continue
        if isinstance(raw_value, bool | int | float):
            clean[key] = raw_value
        elif isinstance(raw_value, str):
            clean[key] = safe_display_label(raw_value, 120)
    return clean


def static_capability_rows() -> list[CapabilityRow]:
    return [
        CapabilityRow("tool_runtime", "core", True, True, True, "ok", adapter="runtime"),
        CapabilityRow("system_log", "core", True, True, True, "ok", adapter="system_log"),
        CapabilityRow("web_search", "web", True, True, True, "ok", adapter="mcp_web"),
        CapabilityRow("web_image_search", "web", True, True, True, "ok", adapter="mcp_web"),
        CapabilityRow("youtube_captions", "media", True, True, True, "ok", adapter="youtube_transcript_mcp"),
        CapabilityRow("media_transcript", "media", False, False, False, "not_implemented"),
        CapabilityRow("telegram_transcription", "media", False, False, False, "not_implemented"),
        CapabilityRow("transcription_backend", "stt", False, False, False, "not_implemented"),
        CapabilityRow("stt_openai", "stt", False, False, False, "not_implemented"),
        CapabilityRow("stt_local", "stt", False, False, False, "disabled"),
        CapabilityRow("media_frames", "media", False, False, False, "not_implemented"),
        CapabilityRow("visual_media_summary", "media", False, False, False, "not_implemented"),
        CapabilityRow("document_ingest", "documents", False, False, False, "not_implemented"),
        CapabilityRow("image_understanding", "vision", False, False, False, "disabled"),
        CapabilityRow("ocr", "vision", False, False, False, "not_implemented"),
        CapabilityRow("fact_check", "fact_check", False, False, False, "not_implemented"),
        CapabilityRow("chat_digest", "digest", False, False, False, "not_implemented"),
        CapabilityRow("github_reporting", "reporting", False, False, False, "disabled"),
    ]


def adapter_row(item: dict[str, Any]) -> CapabilityRow:
    enabled = bool(item.get("enabled", False))
    name = safe_display_label(item.get("name") or "unknown", 80) or "unknown"
    status = normalize_status(item.get("status"), enabled)
    details = {key: value for key, value in item.items() if key in SAFE_HEALTH_FIELDS}
    configured = bool(item.get("configured", enabled)) if "configured" in item else enabled
    available_default = enabled and status not in {"disabled", "not_implemented", "unavailable", "unconfigured", "error", "failing"}
    available = bool(item.get("available", available_default)) if "available" in item else available_default
    return CapabilityRow(
        name=name,
        family=adapter_family(name),
        enabled=enabled,
        configured=configured,
        available=available,
        status=status,
        adapter=safe_display_label(item.get("adapter") or "", 80),
        mode=safe_display_label(item.get("mode") or "", 80),
        backend=safe_display_label(item.get("backend") or "", 80),
        error_count=int(item.get("error_count", 0) or 0),
        warning_count=int(item.get("warning_count", 0) or 0),
        details=details,
    ).normalized()


def adapter_family(name: str) -> str:
    if name in {"outbound_reactions", "reaction_memory"}:
        return "reactions"
    if name.startswith("web_"):
        return "web"
    if name == "telegram_transcription" or "transcript" in name or "media" in name or "youtube" in name:
        return "media"
    if name.startswith("stt_") or name == "transcription_backend":
        return "stt"
    if "document" in name:
        return "documents"
    if name == "fact_check" or name.startswith("fact_"):
        return "fact_check"
    if "digest" in name:
        return "digest"
    if "github" in name:
        return "reporting"
    if "memory" in name:
        return "memory"
    if "ocr" in name or "image" in name or "vision" in name:
        return "vision"
    return "tools"


def build_capability_rows(
    runtime_summary: dict[str, Any] | None,
    *,
    events: Iterable[SystemEvent] = (),
    extra_rows: Iterable[CapabilityRow] = (),
) -> list[CapabilityRow]:
    rows: dict[str, CapabilityRow] = {row.name: row.normalized() for row in static_capability_rows()}
    for row in extra_rows:
        rows[row.name] = row.normalized()
    summary = runtime_summary or {}
    rows["tool_runtime"] = CapabilityRow(
        name="tool_runtime",
        family="core",
        enabled=True,
        configured=True,
        available=True,
        status=normalize_status(summary.get("status"), True),
        adapter="runtime",
        error_count=int(summary.get("error_count", 0) or 0),
        details={"adapter_count": int(summary.get("adapter_count", 0) or 0)},
    ).normalized()
    for item in summary.get("adapters", []) or []:
        row = adapter_row(dict(item or {}))
        rows[row.name] = row
    apply_event_failures(rows, events)
    return sorted(rows.values(), key=sort_key)


def apply_event_failures(rows: dict[str, CapabilityRow], events: Iterable[SystemEvent]) -> None:
    for event in events:
        if event.level not in {"warning", "error", "critical"}:
            continue
        row_names = capability_names_for_event(event, rows)
        if not row_names:
            continue
        category = failure_category_for_event(event)
        for row_name in row_names:
            row = rows[row_name]
            if event.level in {"error", "critical"}:
                row.error_count += 1
                row.recent_error_count += 1
            else:
                row.recent_warning_count += 1
                if not is_runtime_error_event_already_counted(event, row):
                    row.warning_count += 1
            if category and category not in row.recent_failure_categories:
                row.recent_failure_categories.append(category)
            if row.status == "ok":
                row.status = "degraded"
                row.next_action = next_action_for_status(row.status)
            elif row.status in {"disabled", "not_implemented"}:
                row.next_action = next_action_for_status(row.status)
            row.normalized()


def capability_name_for_event(event: SystemEvent, rows: dict[str, CapabilityRow]) -> str:
    names = capability_names_for_event(event, rows)
    return names[0] if names else ""


def capability_names_for_event(event: SystemEvent, rows: dict[str, CapabilityRow]) -> list[str]:
    tool = safe_display_label(event.details.get("tool") if isinstance(event.details, dict) else "", 80)
    if tool in rows:
        return [tool]
    component_map: dict[str, str | list[str]] = {
        "tool_runtime": "tool_runtime",
        "memory_vector": "memory_embeddings",
        "memory": "memory_store",
        "outbound_reactions": "outbound_reactions",
        "image_search": "web_image_search",
        "github_reporting": "github_reporting",
        "agent": ["web_search", "youtube_captions"],
        "agent_tool": "tool_runtime",
        "startup": "tool_runtime",
        "shutdown": "tool_runtime",
    }
    mapped = component_map.get(event.component)
    if isinstance(mapped, list):
        return [name for name in mapped if name in rows]
    if mapped in rows:
        return [mapped]
    return [event.component] if event.component in rows else []


def is_runtime_error_event_already_counted(event: SystemEvent, row: CapabilityRow) -> bool:
    return event.component == "tool_runtime" and event.event_type == "tool_operation_failed" and row.error_count > 0


def failure_category_for_event(event: SystemEvent) -> str:
    details = event.details if isinstance(event.details, dict) else {}
    for key in ("failure_category", "category", "operation", "exception_type"):
        value = details.get(key)
        if value:
            return safe_failure_category_label(value, 80)
    return safe_failure_category_label(event.event_type, 80)


def sort_key(row: CapabilityRow) -> tuple[int, str, str]:
    try:
        status_rank = STATUS_ORDER.index(row.status)
    except ValueError:
        status_rank = STATUS_ORDER.index("unknown")
    return status_rank, row.family, row.name


def overall_status(rows: Iterable[CapabilityRow]) -> str:
    statuses = {row.status for row in rows}
    if statuses & FAILING_STATUSES:
        return "failing"
    if statuses & DEGRADED_STATUSES:
        return "degraded"
    return "ok"


def next_action_for_status(status: str) -> str:
    return {
        "ok": "none",
        "disabled": "disabled by config or design",
        "not_implemented": "planned capability",
        "unconfigured": "check configuration",
        "unavailable": "check dependency image or provider",
        "degraded": "inspect /logs",
        "failing": "inspect /logs and provider status",
        "error": "inspect adapter health",
        "warming": "retry after warmup",
        "unknown": "inspect /logs",
    }.get(status, "inspect /logs")


def filter_rows(rows: Iterable[CapabilityRow], query: str = "") -> list[CapabilityRow]:
    value = clean_label(query, 80).casefold()
    if not value:
        return list(rows)
    return [
        row
        for row in rows
        if value in {
            row.name.casefold(),
            row.family.casefold(),
            row.status.casefold(),
        }
        or value in row.name.casefold()
    ]


def render_capability_matrix(rows: Iterable[CapabilityRow], *, query: str = "", max_rows: int = 30) -> str:
    selected = filter_rows(rows, query)
    if not selected:
        return f"Tool capabilities\nOverall: unknown\nNo capabilities matched: {safe_display_label(query, 80)}"
    lines = [
        "Tool capabilities",
        f"Overall: {overall_status(selected)}",
    ]
    count = 0
    for status in STATUS_ORDER:
        group = [row for row in selected if row.status == status]
        if not group:
            continue
        lines.append("")
        lines.append(f"{status}:")
        for row in group:
            if count >= max_rows:
                lines.append(f"- ... {len(selected) - count} more")
                return "\n".join(lines)
            lines.append(render_row(row))
            count += 1
    return "\n".join(lines)


def render_row(row: CapabilityRow) -> str:
    enabled = "enabled" if row.enabled else "disabled"
    parts = [f"{row.name}: {enabled}"]
    if row.adapter:
        parts.append(f"adapter={row.adapter}")
    if row.mode:
        parts.append(f"mode={row.mode}")
    if row.backend:
        parts.append(f"backend={row.backend}")
    if row.error_count:
        parts.append(f"errors={row.error_count}")
    if row.warning_count:
        parts.append(f"warnings={row.warning_count}")
    if row.recent_failure_categories:
        parts.append("recent=" + ",".join(row.recent_failure_categories[:3]))
    for key in ROW_DETAIL_FIELDS:
        if key in row.details:
            parts.append(f"{key}={row.details[key]}")
    if row.next_action and row.next_action != "none":
        parts.append(f"next={row.next_action}")
    return "- " + ", ".join(parts)


def render_recent_failures(rows: Iterable[CapabilityRow]) -> str:
    failures = [row for row in rows if row.recent_warning_count or row.recent_error_count]
    if not failures:
        return "Recent tool failures\n- none"
    lines = ["Recent tool failures"]
    for row in sorted(failures, key=lambda item: (-(item.recent_error_count + item.recent_warning_count), item.name))[:8]:
        count = row.recent_error_count + row.recent_warning_count
        categories = ",".join(row.recent_failure_categories[:3]) or row.status
        lines.append(f"- {row.name}: {count} recent, {categories}, next={row.next_action or next_action_for_status(row.status)}")
    return "\n".join(lines)
