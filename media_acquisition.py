from __future__ import annotations

import importlib.util
import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from system_log import sanitize_text


MEDIA_ACQUISITION_FAILURE_CATEGORIES = {
    "disabled",
    "unconfigured",
    "unsupported_url",
    "metadata_failed",
    "auth_or_rate_limited",
    "challenge_required",
    "captions_unavailable",
    "duration_limit",
    "file_too_large",
    "download_failed",
    "extractor_unavailable",
    "private_or_drm",
    "cleanup_failed",
    "timeout",
    "unexpected_error",
}


@dataclass(frozen=True)
class MediaAcquisitionLimits:
    max_duration_seconds: int = 180
    hard_max_duration_seconds: int = 1200
    max_download_bytes: int = 50_000_000
    hard_max_download_bytes: int = 100_000_000
    socket_timeout_seconds: int = 12

    def bounded(self) -> "MediaAcquisitionLimits":
        return MediaAcquisitionLimits(
            max_duration_seconds=max(1, min(int(self.max_duration_seconds), int(self.hard_max_duration_seconds))),
            hard_max_duration_seconds=max(1, int(self.hard_max_duration_seconds)),
            max_download_bytes=max(1, min(int(self.max_download_bytes), int(self.hard_max_download_bytes))),
            hard_max_download_bytes=max(1, int(self.hard_max_download_bytes)),
            socket_timeout_seconds=max(1, int(self.socket_timeout_seconds)),
        )


@dataclass(frozen=True)
class MediaAcquisitionRequest:
    url: str
    route: str = "media_context"
    max_duration_seconds: int | None = None
    max_download_bytes: int | None = None
    socket_timeout_seconds: int | None = None


@dataclass
class MediaAcquisitionResult:
    ok: bool
    backend: str = "none"
    platform: str = "unknown"
    failure_category: str = ""
    user_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unavailable(
        cls,
        *,
        failure_category: str,
        backend: str = "none",
        platform: str = "unknown",
        user_message: str = "",
        diagnostics: dict[str, Any] | None = None,
    ) -> "MediaAcquisitionResult":
        category = safe_failure_category(failure_category)
        return cls(
            ok=False,
            backend=safe_code_label(backend, default="none"),
            platform=safe_platform(platform),
            failure_category=category,
            user_message=sanitize_text(user_message_for_failure(category) if not user_message else user_message, 180),
            diagnostics=sanitize_public_mapping(diagnostics or {}),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "platform": self.platform,
            "failure_category": self.failure_category,
            "user_message": self.user_message,
            "metadata": sanitize_public_mapping(self.metadata),
            "diagnostics": sanitize_public_mapping(self.diagnostics),
        }


class MediaAcquisitionAdapter(Protocol):
    def probe_metadata(self, request: MediaAcquisitionRequest) -> MediaAcquisitionResult:
        ...

    def health_summary(self) -> dict[str, Any]:
        ...


class NullMediaAcquisitionAdapter:
    def __init__(self, *, name: str = "media_acquisition") -> None:
        self.name = name

    def probe_metadata(self, request: MediaAcquisitionRequest) -> MediaAcquisitionResult:
        return MediaAcquisitionResult.unavailable(
            failure_category="disabled",
            backend="null",
            platform=platform_from_url(request.url),
            user_message="Media acquisition is disabled.",
        )

    def health_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": "media",
            "enabled": False,
            "configured": False,
            "available": False,
            "adapter": "null",
            "status": "disabled",
            "error_count": 0,
            "backend": "none",
        }


class SilentYtDlpLogger:
    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


YdlFactory = Callable[[dict[str, Any]], Any]


class YtDlpMediaAcquisitionAdapter:
    def __init__(
        self,
        *,
        enabled: bool = True,
        limits: MediaAcquisitionLimits | None = None,
        ydl_factory: YdlFactory | None = None,
    ) -> None:
        self.enabled = enabled
        self.limits = (limits or MediaAcquisitionLimits()).bounded()
        self.ydl_factory = ydl_factory
        self.error_count = 0
        self.warning_count = 0
        self.last_failure_category = ""

    def probe_metadata(self, request: MediaAcquisitionRequest) -> MediaAcquisitionResult:
        if not self.enabled:
            return self._failure(request, "disabled", backend="yt_dlp")
        limits = request_limits(self.limits, request)
        platform = platform_from_url(request.url)
        validation_failure = validate_public_media_url(request.url)
        if validation_failure:
            return self._failure(request, validation_failure, backend="yt_dlp", platform=platform)
        if not yt_dlp_available() and self.ydl_factory is None:
            return self._failure(request, "extractor_unavailable", backend="yt_dlp", platform=platform)
        try:
            with self._ydl(options_for_probe(limits)) as ydl:
                info = ydl.extract_info(request.url, download=False)
        except Exception as exc:
            return self._failure(
                request,
                categorize_yt_dlp_exception(exc),
                backend="yt_dlp",
                platform=platform,
                diagnostics={"exception_type": safe_code_label(type(exc).__name__, default="error")},
            )
        if not isinstance(info, dict) or not info:
            return self._failure(request, "metadata_failed", backend="yt_dlp", platform=platform)
        file_size = max_known_file_size(info)
        if file_size and file_size > limits.max_download_bytes:
            return self._failure(
                request,
                "file_too_large",
                backend="yt_dlp",
                platform=platform,
                diagnostics={"max_download_bytes": limits.max_download_bytes, "file_size_bytes": file_size},
            )
        duration = safe_nonnegative_int(info.get("duration"))
        if duration and duration > limits.max_duration_seconds:
            return self._failure(
                request,
                "duration_limit",
                backend="yt_dlp",
                platform=platform,
                diagnostics={"max_duration_seconds": limits.max_duration_seconds, "duration_seconds": duration},
            )
        formats = info.get("formats") if isinstance(info.get("formats"), list) else []
        subtitles = info.get("subtitles") if isinstance(info.get("subtitles"), dict) else {}
        automatic_captions = (
            info.get("automatic_captions") if isinstance(info.get("automatic_captions"), dict) else {}
        )
        self.last_failure_category = ""
        return MediaAcquisitionResult(
            ok=True,
            backend="yt_dlp",
            platform=safe_platform(platform),
            metadata={
                "extractor": safe_code_label(info.get("extractor_key") or info.get("extractor") or "", default="unknown"),
                "duration_seconds": duration,
                "format_count": len(formats),
                "has_subtitles": any(bool(items) for items in subtitles.values()),
                "has_auto_captions": any(bool(items) for items in automatic_captions.values()),
            },
            diagnostics={
                "route": safe_code_label(request.route, default="media_context"),
                "metadata_only": True,
            },
        )

    def health_summary(self) -> dict[str, Any]:
        dependency_available = self.ydl_factory is not None or yt_dlp_available()
        configured = self.enabled and dependency_available
        available = self.enabled and dependency_available
        if not self.enabled:
            status = "disabled"
        elif not available:
            status = "unavailable"
        elif self.error_count:
            status = "degraded"
        else:
            status = "ok"
        details: dict[str, Any] = {
            "name": "media_acquisition",
            "family": "media",
            "enabled": self.enabled,
            "configured": configured,
            "available": available,
            "adapter": type(self).__name__,
            "status": status,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "backend": "yt_dlp",
            "yt_dlp_available": dependency_available,
            "max_duration_seconds": self.limits.max_duration_seconds,
            "max_download_bytes": self.limits.max_download_bytes,
        }
        if self.last_failure_category:
            details["last_failure_category"] = self.last_failure_category
        return details

    def _ydl(self, options: dict[str, Any]) -> Any:
        if self.ydl_factory is not None:
            return self.ydl_factory(options)
        from yt_dlp import YoutubeDL

        return YoutubeDL(options)

    def _failure(
        self,
        request: MediaAcquisitionRequest,
        category: str,
        *,
        backend: str,
        platform: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> MediaAcquisitionResult:
        clean_category = safe_failure_category(category)
        if clean_category != "disabled":
            self.error_count += 1
            self.last_failure_category = clean_category
        return MediaAcquisitionResult.unavailable(
            failure_category=clean_category,
            backend=backend,
            platform=platform or platform_from_url(request.url),
            diagnostics={
                "route": safe_code_label(request.route, default="media_context"),
                **sanitize_public_mapping(diagnostics or {}),
            },
        )


def request_limits(base: MediaAcquisitionLimits, request: MediaAcquisitionRequest) -> MediaAcquisitionLimits:
    limits = base
    if request.max_duration_seconds is not None:
        limits = MediaAcquisitionLimits(
            max_duration_seconds=request.max_duration_seconds,
            hard_max_duration_seconds=limits.hard_max_duration_seconds,
            max_download_bytes=limits.max_download_bytes,
            hard_max_download_bytes=limits.hard_max_download_bytes,
            socket_timeout_seconds=limits.socket_timeout_seconds,
        )
    if request.max_download_bytes is not None:
        limits = MediaAcquisitionLimits(
            max_duration_seconds=limits.max_duration_seconds,
            hard_max_duration_seconds=limits.hard_max_duration_seconds,
            max_download_bytes=request.max_download_bytes,
            hard_max_download_bytes=limits.hard_max_download_bytes,
            socket_timeout_seconds=limits.socket_timeout_seconds,
        )
    if request.socket_timeout_seconds is not None:
        limits = MediaAcquisitionLimits(
            max_duration_seconds=limits.max_duration_seconds,
            hard_max_duration_seconds=limits.hard_max_duration_seconds,
            max_download_bytes=limits.max_download_bytes,
            hard_max_download_bytes=limits.hard_max_download_bytes,
            socket_timeout_seconds=request.socket_timeout_seconds,
        )
    return limits.bounded()


def options_for_probe(limits: MediaAcquisitionLimits) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtDlpLogger(),
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": limits.socket_timeout_seconds,
        "max_filesize": limits.max_download_bytes,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "ignore_no_formats_error": True,
    }


def validate_public_media_url(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "unsupported_url"
    if parsed.username or parsed.password:
        return "unsupported_url"
    host = (parsed.hostname or "").strip().lower()
    if not host or host in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        return "unsupported_url"
    if platform_from_url(parsed.geturl()) not in {"tiktok", "youtube", "instagram"}:
        return "unsupported_url"
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return "unsupported_url"
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except (ValueError, IndexError, TypeError):
                return "unsupported_url"
            if is_private_network_address(address):
                return "unsupported_url"
        return ""
    if is_private_network_address(address):
        return "unsupported_url"
    return ""


def is_private_network_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def platform_from_url(value: str) -> str:
    host = (urlparse(str(value or "").strip()).hostname or "").lower()
    if host.endswith("tiktok.com") or host.endswith("tiktokv.com") or host.endswith("tiktokcdn.com"):
        return "tiktok"
    if host.endswith("youtube.com") or host.endswith("youtu.be"):
        return "youtube"
    if host.endswith("instagram.com"):
        return "instagram"
    if host:
        return "other"
    return "unknown"


def categorize_yt_dlp_exception(exc: Exception) -> str:
    text = str(exc or "").casefold()
    if "unsupported url" in text or "not a valid url" in text:
        return "unsupported_url"
    if "challenge" in text or "captcha" in text or "please wait" in text:
        return "challenge_required"
    if "drm" in text or "private" in text or "protected" in text:
        return "private_or_drm"
    if "login" in text or "authentication" in text or "not comfortable" in text:
        return "auth_or_rate_limited"
    if "rate" in text or "too many requests" in text or "429" in text:
        return "auth_or_rate_limited"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "unable to download webpage" in text or "download" in text:
        return "metadata_failed"
    if "unable to extract" in text or "extract" in text:
        return "metadata_failed"
    return "unexpected_error"


def yt_dlp_available() -> bool:
    return importlib.util.find_spec("yt_dlp") is not None


def safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def max_known_file_size(info: dict[str, Any]) -> int:
    sizes = [
        safe_nonnegative_int(info.get("filesize")),
        safe_nonnegative_int(info.get("filesize_approx")),
    ]
    formats = info.get("formats") if isinstance(info.get("formats"), list) else []
    for item in formats:
        if isinstance(item, dict):
            sizes.append(safe_nonnegative_int(item.get("filesize")))
            sizes.append(safe_nonnegative_int(item.get("filesize_approx")))
    return max(sizes or [0])


def safe_code_label(value: Any, *, default: str) -> str:
    raw = str(value or "")
    if any(marker in raw for marker in ("://", "/", "\\", "?", "=")):
        return default
    text = sanitize_text(raw, 120).strip().lower().replace("-", "_").replace(" ", "_")
    text = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "."})
    return text[:80] or default


def safe_platform(value: Any) -> str:
    platform = safe_code_label(value, default="unknown")
    return platform if platform in {"tiktok", "youtube", "instagram", "other", "unknown"} else "other"


def safe_failure_category(value: Any) -> str:
    category = safe_code_label(value, default="unexpected_error")
    return category if category in MEDIA_ACQUISITION_FAILURE_CATEGORIES else "unexpected_error"


def sanitize_public_mapping(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in values.items():
        key_text = safe_code_label(key, default="")
        if not key_text:
            continue
        if isinstance(value, bool | int | float):
            clean[key_text] = value
        elif isinstance(value, str):
            clean[key_text] = safe_code_label(value, default="redacted")
    return clean


def user_message_for_failure(category: str) -> str:
    messages = {
        "disabled": "Media acquisition is disabled.",
        "unconfigured": "Media acquisition is not configured.",
        "unsupported_url": "This media URL is not supported safely.",
        "metadata_failed": "I could not read media metadata safely.",
        "auth_or_rate_limited": "This media requires login, age confirmation, or is rate-limited.",
        "challenge_required": "This media provider requires a challenge that I cannot complete safely.",
        "captions_unavailable": "Captions are unavailable for this media.",
        "duration_limit": "The media is too long for bounded processing.",
        "file_too_large": "The media file is too large for bounded processing.",
        "download_failed": "I could not download the media safely.",
        "extractor_unavailable": "The media extractor is unavailable.",
        "private_or_drm": "This media appears private, protected, or DRM-restricted.",
        "cleanup_failed": "Media acquisition cleanup failed.",
        "timeout": "Media acquisition timed out.",
    }
    return messages.get(category, "Media acquisition failed.")
