from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


KNOWN_SOURCE_FIELDS = {
    "search_web": "query",
    "search_images": "query",
    "fetch_url": "url",
    "get_youtube_transcript": "video",
}


@dataclass(frozen=True)
class ToolProvenance:
    ordinal: int
    tool_kind: str
    source_fingerprint: str
    result_status: str
    result_digest: str


def canonical_tool_kind(value: Any) -> str:
    name = str(value or "").strip().rsplit(".", 1)[-1].casefold()
    name = re.sub(r"[^a-z0-9_-]", "", name)[:64]
    for known in KNOWN_SOURCE_FIELDS:
        if name == known or name.endswith(f"__{known}"):
            return known
    return name or "unknown"


def _argument_mapping(raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    try:
        port = parsed.port
    except ValueError:
        return ""
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    if port and not default_port:
        host = f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.casefold(), host, parsed.path or "/", query, ""))


def _youtube_video_id(value: str) -> str:
    candidate = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return candidate
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id = ""
    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        video_id = path_parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = dict(parse_qsl(parsed.query)).get("v", "")
        elif len(path_parts) >= 2 and path_parts[0] in {"embed", "shorts", "live"}:
            video_id = path_parts[1]
    return video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id) else ""


def canonical_source(tool_kind: str, arguments: Any) -> str:
    kind = canonical_tool_kind(tool_kind)
    field = KNOWN_SOURCE_FIELDS.get(kind)
    if field is None:
        return ""
    value = _argument_mapping(arguments).get(field)
    if not isinstance(value, str) or not value.strip():
        return ""
    if kind in {"search_web", "search_images"}:
        return " ".join(value.casefold().split())[:1000]
    if kind == "fetch_url":
        return _canonical_url(value)
    if kind == "get_youtube_transcript":
        return _youtube_video_id(value)
    return ""


def source_fingerprint(tool_kind: str, arguments: Any, secret: str) -> str:
    source = canonical_source(tool_kind, arguments)
    if not source or not secret:
        return ""
    payload = f"prov-v1:{canonical_tool_kind(tool_kind)}:{source}"
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


def _size_bucket(size: int) -> str:
    if size <= 0:
        return "empty"
    if size <= 500:
        return "small"
    if size <= 4000:
        return "medium"
    return "large"


def bounded_result_digest(tool_kind: str, output: Any, status: str) -> str:
    text = "" if output is None else str(output)
    normalized = text.casefold()
    digest: dict[str, Any] = {
        "chars": _size_bucket(len(text)),
        "status": status[:48] if status else "unknown",
    }
    kind = canonical_tool_kind(tool_kind)
    if kind == "get_youtube_transcript" and status == "ok":
        if not text or "contained no text" in normalized:
            digest["source"] = "empty"
        elif "transcript/captions" in normalized:
            digest["source"] = "captions"
        elif "audio transcription" in normalized:
            digest["source"] = "audio"
        else:
            digest["source"] = "other"
        digest["timestamps"] = bool(re.search(r"\[\d{1,2}:\d{2}", text))
    serialized = json.dumps(digest, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return serialized[:256]


def make_tool_provenance(
    tool_kind: str,
    arguments: Any,
    output: Any,
    *,
    secret: str,
    ordinal: int = 0,
    status: str | None = None,
    failure_classifier: Callable[[str], str | None] | None = None,
) -> ToolProvenance:
    kind = canonical_tool_kind(tool_kind)
    output_text = "" if output is None else str(output)
    intrinsic_status = None
    normalized_output = " ".join(output_text.casefold().split())
    if kind == "get_youtube_transcript":
        if normalized_output.startswith("could not extract youtube video id"):
            intrinsic_status = "url_rejected"
        elif (
            normalized_output.startswith("no caption transcript available")
            or "contained no text" in normalized_output
        ):
            intrinsic_status = "no_results"
        elif normalized_output.startswith("audio transcription failed"):
            intrinsic_status = "tool_failed"
    resolved_status = (
        status
        or (failure_classifier(output_text) if failure_classifier is not None else None)
        or intrinsic_status
        or "ok"
    )
    resolved_status = re.sub(r"[^a-z0-9_-]", "", resolved_status.casefold())[:48] or "unknown"
    return ToolProvenance(
        ordinal=max(0, int(ordinal)),
        tool_kind=kind,
        source_fingerprint=source_fingerprint(kind, arguments, secret),
        result_status=resolved_status,
        result_digest=bounded_result_digest(kind, output, resolved_status),
    )


def _raw_value(raw_item: Any, field: str) -> Any:
    if isinstance(raw_item, dict):
        return raw_item.get(field)
    return getattr(raw_item, field, None)


def extract_tool_provenance(
    items: Iterable[Any] | None,
    *,
    secret: str,
    failure_classifier: Callable[[str], str | None] | None = None,
) -> list[ToolProvenance]:
    calls: list[tuple[str, str, Any]] = []
    outputs: dict[str, Any] = {}
    for item in items or ():
        item_type = getattr(item, "type", "")
        call_id = str(getattr(item, "call_id", "") or "")
        if item_type == "tool_call_item":
            raw_item = getattr(item, "raw_item", None)
            tool_name = getattr(item, "tool_name", None) or _raw_value(raw_item, "name")
            arguments = _raw_value(raw_item, "arguments")
            calls.append((call_id, canonical_tool_kind(tool_name), arguments))
        elif item_type == "tool_call_output_item" and call_id:
            outputs[call_id] = getattr(item, "output", None)

    observations: list[ToolProvenance] = []
    for ordinal, (call_id, tool_kind, arguments) in enumerate(calls):
        has_output = bool(call_id) and call_id in outputs
        output = outputs.get(call_id)
        observations.append(
            make_tool_provenance(
                tool_kind,
                arguments,
                output,
                secret=secret,
                ordinal=ordinal,
                status=None if has_output else "incomplete",
                failure_classifier=failure_classifier,
            )
        )
    return observations


def renumber_tool_provenance(items: Iterable[ToolProvenance]) -> tuple[ToolProvenance, ...]:
    return tuple(replace(item, ordinal=ordinal) for ordinal, item in enumerate(items))
