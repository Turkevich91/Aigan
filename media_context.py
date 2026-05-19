from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from media_acquisition import MediaAcquisitionResult, safe_code_label, safe_failure_category, safe_platform
from system_log import sanitize_text


MEDIA_CONTEXT_STATES = {"metadata_only", "transcript_summary", "visual_summary", "unavailable"}


@dataclass
class MediaContextResult:
    ok: bool
    state: str
    platform: str = "unknown"
    backend: str = "none"
    modality: str = "unavailable"
    failure_category: str = ""
    user_message: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": safe_state(self.state),
            "platform": safe_platform(self.platform),
            "backend": safe_code_label(self.backend, default="none"),
            "modality": safe_code_label(self.modality, default="unavailable"),
            "failure_category": safe_failure_category(self.failure_category) if self.failure_category else "",
            "user_message": sanitize_text(self.user_message, 240),
            "summary": sanitize_text(self.summary, 1200),
            "metadata": sanitize_context_mapping(self.metadata),
            "diagnostics": sanitize_context_mapping(self.diagnostics),
        }


def media_context_from_acquisition(result: MediaAcquisitionResult) -> MediaContextResult:
    if not result.ok:
        category = safe_failure_category(result.failure_category or "unexpected_error")
        return MediaContextResult(
            ok=False,
            state="unavailable",
            platform=safe_platform(result.platform),
            backend=safe_code_label(result.backend, default="none"),
            modality="unavailable",
            failure_category=category,
            user_message=media_context_unavailable_message(category),
            diagnostics=sanitize_context_mapping(result.diagnostics),
        )

    metadata = sanitize_context_mapping(result.metadata)
    diagnostics = sanitize_context_mapping(result.diagnostics)
    return MediaContextResult(
        ok=True,
        state="metadata_only",
        platform=safe_platform(result.platform),
        backend=safe_code_label(result.backend, default="none"),
        modality="metadata",
        user_message="",
        summary=metadata_only_summary(platform=safe_platform(result.platform), metadata=metadata),
        metadata=metadata,
        diagnostics=diagnostics,
    )


def metadata_only_summary(*, platform: str, metadata: dict[str, Any]) -> str:
    lines = [
        "Я зміг безпечно прочитати лише метадані медіа, але не повний зміст відео.",
        f"- platform: {safe_platform(platform)}",
    ]
    duration = metadata.get("duration_seconds")
    if isinstance(duration, int | float) and duration > 0:
        lines.append(f"- duration_seconds: {int(duration)}")
    extractor = safe_code_label(metadata.get("extractor") or "", default="")
    if extractor:
        lines.append(f"- extractor: {extractor}")
    if "has_subtitles" in metadata:
        lines.append(f"- subtitles_detected: {bool(metadata.get('has_subtitles'))}")
    if "has_auto_captions" in metadata:
        lines.append(f"- auto_captions_detected: {bool(metadata.get('has_auto_captions'))}")
    lines.append("У v1 я не буду вгадувати зміст за одними метаданими.")
    return "\n".join(lines)


def media_context_unavailable_message(category: str) -> str:
    messages = {
        "disabled": "Аналіз публічних медіапосилань зараз вимкнений.",
        "unconfigured": "Аналіз публічних медіапосилань ще не налаштований.",
        "unsupported_url": "Я не можу безпечно обробити це медіапосилання.",
        "metadata_failed": "Я не зміг безпечно прочитати метадані цього медіа.",
        "auth_or_rate_limited": "Сервіс вимагає логін, підтвердження або обмежив запит.",
        "challenge_required": "Сервіс показав challenge/captcha, який я не можу проходити.",
        "captions_unavailable": "Субтитри для цього медіа недоступні.",
        "duration_limit": "Медіа занадто довге для безпечної обробки.",
        "file_too_large": "Медіафайл занадто великий для безпечної обробки.",
        "download_failed": "Я не зміг безпечно отримати медіа.",
        "extractor_unavailable": "Медіа-екстрактор недоступний.",
        "private_or_drm": "Медіа виглядає приватним, захищеним або DRM-обмеженим.",
        "timeout": "Перевірка медіа вперлася у timeout.",
        "unexpected_error": "Перевірка медіа не вдалася.",
    }
    messages.update(
        {
            "visual_extraction_unavailable": "Я не зміг безпечно витягти репрезентативні кадри з цього медіа.",
            "visual_summary_failed": "Я отримав медіа, але не зміг надійно підсумувати візуальний зміст.",
        }
    )
    return messages.get(safe_failure_category(category), messages["unexpected_error"])


def public_media_context_response(result: MediaContextResult) -> str:
    public = result.public_dict()
    if public["ok"] and public["state"] in {"transcript_summary", "visual_summary"}:
        return sanitize_text(result.summary, 4000)
    if public["ok"] and public["state"] == "metadata_only":
        return str(public["summary"] or metadata_only_summary(platform=public["platform"], metadata=public["metadata"]))
    return str(public["user_message"] or media_context_unavailable_message(public["failure_category"] or "unexpected_error"))


def media_context_with_visual_failure(result: MediaContextResult, category: str) -> MediaContextResult:
    clean_category = safe_failure_category(category or "unexpected_error")
    metadata = sanitize_context_mapping(result.metadata)
    diagnostics = {
        **sanitize_context_mapping(result.diagnostics),
        "visual_failure_category": clean_category,
    }
    base_summary = sanitize_text(result.summary, 1200)
    if not base_summary and result.ok:
        base_summary = metadata_only_summary(platform=safe_platform(result.platform), metadata=metadata)
    failure_note = f"Візуальний аналіз недоступний: {media_context_unavailable_message(clean_category)}"
    summary = sanitize_text("\n\n".join(part for part in (base_summary, failure_note) if part), 1600)
    return MediaContextResult(
        ok=result.ok,
        state=safe_state(result.state),
        platform=safe_platform(result.platform),
        backend=safe_code_label(result.backend, default="none"),
        modality=safe_code_label(result.modality, default="metadata"),
        failure_category=clean_category,
        user_message=result.user_message,
        summary=summary,
        metadata=metadata,
        diagnostics=diagnostics,
    )


def build_youtube_media_context_prompt(*, user_prompt: str, url: str, context_result: MediaContextResult) -> str:
    public = context_result.public_dict()
    metadata = public.get("metadata") if isinstance(public.get("metadata"), dict) else {}
    prompt_preview = redact_urls_for_prompt_preview(user_prompt)
    return f"""Trusted current user request:
{prompt_preview}

Trusted public media URL from the current user request:
{url}

Sanitized media acquisition context:
platform={public["platform"]}
backend={public["backend"]}
state={public["state"]}
modality={public["modality"]}
duration_seconds={metadata.get("duration_seconds", 0)}
has_subtitles={metadata.get("has_subtitles", False)}
has_auto_captions={metadata.get("has_auto_captions", False)}

Task:
- Use the YouTube transcript tool for the URL when possible.
- If captions/transcript are unavailable, say that full validation is incomplete.
- Do not infer video content from search results, title guesses, or general web memory.
- If only metadata is available, answer from metadata only and say the limitation clearly.
- Reply in Ukrainian by default, English only if explicitly requested, never Russian.
"""


def redact_urls_for_prompt_preview(text: str) -> str:
    redacted = re.sub(r"\b(?:https?://|www\.)\S+", "[media_url]", text or "", flags=re.IGNORECASE)
    return sanitize_text(redacted, 1200)


def media_context_event_details(result: MediaContextResult) -> dict[str, Any]:
    public = result.public_dict()
    details: dict[str, Any] = {
        "tool": "media_context",
        "platform": public["platform"],
        "backend": public["backend"],
        "state": public["state"],
        "modality": public["modality"],
    }
    if public["failure_category"]:
        details["failure_category"] = public["failure_category"]
    metadata = public.get("metadata") if isinstance(public.get("metadata"), dict) else {}
    for key in (
        "duration_seconds",
        "format_count",
        "has_subtitles",
        "has_auto_captions",
        "frame_count",
        "candidate_frames",
        "selected_frames",
        "transcript_used",
        "visual_only",
    ):
        if key in metadata:
            details[key] = metadata[key]
    return details


def sanitize_context_mapping(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (values or {}).items():
        key_text = safe_code_label(key, default="")
        if not key_text:
            continue
        if isinstance(value, bool | int | float):
            clean[key_text] = value
        elif isinstance(value, str):
            clean[key_text] = safe_code_label(value, default="redacted")
    return clean


def safe_state(value: Any) -> str:
    state = safe_code_label(value, default="unavailable")
    return state if state in MEDIA_CONTEXT_STATES else "unavailable"
