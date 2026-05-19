from __future__ import annotations

import importlib.util
import ipaddress
import shutil
import socket
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
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
    "visual_extraction_unavailable",
    "visual_summary_failed",
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
            user_message=sanitize_text(user_message_for_failure(category), 180),
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


@dataclass
class MediaAcquisitionFileResult:
    ok: bool
    backend: str = "none"
    platform: str = "unknown"
    failure_category: str = ""
    user_message: str = ""
    source_path: Path | None = None
    mime_type: str = ""
    file_size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    cleanup_status: str = "not_needed"
    _temp_dir: Path | None = None

    @classmethod
    def unavailable(
        cls,
        *,
        failure_category: str,
        backend: str = "none",
        platform: str = "unknown",
        diagnostics: dict[str, Any] | None = None,
    ) -> "MediaAcquisitionFileResult":
        category = safe_failure_category(failure_category)
        return cls(
            ok=False,
            backend=safe_code_label(backend, default="none"),
            platform=safe_platform(platform),
            failure_category=category,
            user_message=sanitize_text(user_message_for_failure(category), 180),
            diagnostics=sanitize_public_mapping(diagnostics or {}),
        )

    async def cleanup(self) -> None:
        if self._temp_dir is None:
            if self.cleanup_status == "pending":
                self.cleanup_status = "not_needed"
            return
        try:
            shutil.rmtree(self._temp_dir, ignore_errors=False)
        except FileNotFoundError:
            self.cleanup_status = "cleaned"
        except Exception:
            self.cleanup_status = "cleanup_failed"
        else:
            self.cleanup_status = "cleaned"
        finally:
            self._temp_dir = None
            self.source_path = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "platform": self.platform,
            "failure_category": self.failure_category,
            "user_message": self.user_message,
            "mime_type": safe_media_mime_type(self.mime_type),
            "file_size_bytes": self.file_size_bytes,
            "metadata": sanitize_public_mapping(self.metadata),
            "diagnostics": sanitize_public_mapping(self.diagnostics),
            "cleanup_status": safe_code_label(self.cleanup_status, default="not_needed"),
        }


class MediaAcquisitionAdapter(Protocol):
    def probe_metadata(self, request: MediaAcquisitionRequest) -> MediaAcquisitionResult:
        ...

    def acquire_media(self, request: MediaAcquisitionRequest) -> MediaAcquisitionFileResult:
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

    def acquire_media(self, request: MediaAcquisitionRequest) -> MediaAcquisitionFileResult:
        return MediaAcquisitionFileResult.unavailable(
            failure_category="disabled",
            backend="null",
            platform=platform_from_url(request.url),
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
            "download_supported": False,
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

    def acquire_media(self, request: MediaAcquisitionRequest) -> MediaAcquisitionFileResult:
        if not self.enabled:
            return self._file_failure(request, "disabled", backend="yt_dlp")
        limits = request_limits(self.limits, request)
        platform = platform_from_url(request.url)
        validation_failure = validate_public_media_url(request.url)
        if validation_failure:
            return self._file_failure(request, validation_failure, backend="yt_dlp", platform=platform)
        if not yt_dlp_available() and self.ydl_factory is None:
            return self._file_failure(request, "extractor_unavailable", backend="yt_dlp", platform=platform)

        try:
            with self._ydl(options_for_probe(limits)) as ydl:
                info = ydl.extract_info(request.url, download=False)
        except Exception as exc:
            return self._file_failure(
                request,
                categorize_yt_dlp_exception(exc),
                backend="yt_dlp",
                platform=platform,
                diagnostics={"exception_type": safe_code_label(type(exc).__name__, default="error"), "stage": "metadata"},
            )
        metadata_failure = metadata_validation_failure(info, limits)
        if metadata_failure:
            return self._file_failure(
                request,
                metadata_failure[0],
                backend="yt_dlp",
                platform=platform,
                diagnostics=metadata_failure[1],
            )

        temp_dir = Path(tempfile.mkdtemp(prefix="aigan-public-media-"))
        try:
            with self._ydl(options_for_download(limits, temp_dir)) as ydl:
                ydl.extract_info(request.url, download=True)
            files = downloaded_media_files(temp_dir)
            if not files:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return self._file_failure(
                    request,
                    "download_failed",
                    backend="yt_dlp",
                    platform=platform,
                    diagnostics={"stage": "download"},
                )
            source_path = max(files, key=lambda item: item.stat().st_size)
            actual_size = safe_nonnegative_int(source_path.stat().st_size)
            if actual_size <= 0:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return self._file_failure(
                    request,
                    "download_failed",
                    backend="yt_dlp",
                    platform=platform,
                    diagnostics={"stage": "download"},
                )
            if actual_size > limits.max_download_bytes:
                shutil.rmtree(temp_dir, ignore_errors=True)
                return self._file_failure(
                    request,
                    "file_too_large",
                    backend="yt_dlp",
                    platform=platform,
                    diagnostics={"max_download_bytes": limits.max_download_bytes, "file_size_bytes": actual_size},
                )
        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return self._file_failure(
                request,
                categorize_yt_dlp_download_exception(exc),
                backend="yt_dlp",
                platform=platform,
                diagnostics={"exception_type": safe_code_label(type(exc).__name__, default="error"), "stage": "download"},
            )

        self.last_failure_category = ""
        metadata = metadata_from_info(info)
        return MediaAcquisitionFileResult(
            ok=True,
            backend="yt_dlp",
            platform=safe_platform(platform),
            source_path=source_path,
            mime_type=mime_type_for_path(source_path),
            file_size_bytes=actual_size,
            metadata=metadata,
            diagnostics={
                "route": safe_code_label(request.route, default="media_context"),
                "metadata_only": False,
                "stage": "download",
            },
            cleanup_status="pending",
            _temp_dir=temp_dir,
        )

    def health_summary(self) -> dict[str, Any]:
        dependency_available = yt_dlp_available()
        backend_available = self.ydl_factory is not None or dependency_available
        configured = self.enabled
        available = configured and backend_available
        if not self.enabled:
            status = "disabled"
        elif not backend_available:
            status = "unconfigured"
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
            "download_supported": True,
            "backend_available": backend_available,
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

    def _file_failure(
        self,
        request: MediaAcquisitionRequest,
        category: str,
        *,
        backend: str,
        platform: str | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> MediaAcquisitionFileResult:
        clean_category = safe_failure_category(category)
        if clean_category != "disabled":
            self.error_count += 1
            self.last_failure_category = clean_category
        return MediaAcquisitionFileResult.unavailable(
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


def options_for_download(limits: MediaAcquisitionLimits, temp_dir: Path) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "logger": SilentYtDlpLogger(),
        "noplaylist": True,
        "skip_download": False,
        "socket_timeout": limits.socket_timeout_seconds,
        "max_filesize": limits.max_download_bytes,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "outtmpl": str(temp_dir / "source.%(ext)s"),
        "format": bounded_video_format_selector(limits.max_download_bytes),
        "format_sort": ["res:720", "size"],
        "merge_output_format": "mp4",
        "overwrites": False,
        "continuedl": False,
        "writethumbnail": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "cachedir": False,
    }


def bounded_video_format_selector(max_bytes: int) -> str:
    byte_limit = max(1, int(max_bytes))
    return (
        f"bestvideo[ext=mp4][height<=720][filesize<={byte_limit}]+bestaudio[filesize<={byte_limit}]/"
        f"bestvideo[height<=720][filesize<={byte_limit}]+bestaudio[filesize<={byte_limit}]/"
        f"best[ext=mp4][height<=720][vcodec!=none][filesize<={byte_limit}]/"
        f"best[height<=720][vcodec!=none][filesize<={byte_limit}]/"
        f"bestvideo[ext=mp4][height<=720][filesize_approx<={byte_limit}]+bestaudio/"
        f"bestvideo[height<=720][filesize_approx<={byte_limit}]+bestaudio/"
        f"best[ext=mp4][vcodec!=none][filesize_approx<={byte_limit}]/"
        f"best[height<=720][vcodec!=none][filesize_approx<={byte_limit}]/"
        "bestvideo[ext=mp4][height<=720]+bestaudio/"
        "bestvideo[height<=720]+bestaudio/"
        "best[ext=mp4][height<=720][vcodec!=none]/"
        "best[height<=720][vcodec!=none]/"
        "bestvideo[ext=mp4]/"
        "bestvideo/"
        "best[vcodec!=none]"
    )


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
    if any(host_matches_domain(host, domain) for domain in ("tiktok.com", "tiktokv.com", "tiktokcdn.com")):
        return "tiktok"
    if any(host_matches_domain(host, domain) for domain in ("youtube.com", "youtu.be")):
        return "youtube"
    if host_matches_domain(host, "instagram.com"):
        return "instagram"
    if host:
        return "other"
    return "unknown"


def host_matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


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


def categorize_yt_dlp_download_exception(exc: Exception) -> str:
    category = categorize_yt_dlp_exception(exc)
    return "download_failed" if category == "metadata_failed" else category


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


def metadata_validation_failure(
    info: Any, limits: MediaAcquisitionLimits
) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(info, dict) or not info:
        return "metadata_failed", {"stage": "metadata"}
    duration = safe_nonnegative_int(info.get("duration"))
    if duration and duration > limits.max_duration_seconds:
        return "duration_limit", {
            "max_duration_seconds": limits.max_duration_seconds,
            "duration_seconds": duration,
            "stage": "metadata",
        }
    file_size = max_known_file_size(info)
    if file_size and file_size > limits.max_download_bytes:
        return "file_too_large", {
            "max_download_bytes": limits.max_download_bytes,
            "file_size_bytes": file_size,
            "stage": "metadata",
        }
    return None


def metadata_from_info(info: dict[str, Any]) -> dict[str, Any]:
    formats = info.get("formats") if isinstance(info.get("formats"), list) else []
    subtitles = info.get("subtitles") if isinstance(info.get("subtitles"), dict) else {}
    automatic_captions = info.get("automatic_captions") if isinstance(info.get("automatic_captions"), dict) else {}
    return {
        "extractor": safe_code_label(info.get("extractor_key") or info.get("extractor") or "", default="unknown"),
        "duration_seconds": safe_nonnegative_int(info.get("duration")),
        "format_count": len(formats),
        "has_subtitles": any(bool(items) for items in subtitles.values()),
        "has_auto_captions": any(bool(items) for items in automatic_captions.values()),
    }


def downloaded_media_files(temp_dir: Path) -> list[Path]:
    suffixes = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}
    files: list[Path] = []
    for item in temp_dir.iterdir():
        if item.is_file() and item.suffix.lower() in suffixes and not item.name.endswith(".part"):
            files.append(item)
    return files


def mime_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mp4" or suffix == ".m4v":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".mkv":
        return "video/x-matroska"
    return "video/mp4"


def safe_media_mime_type(value: Any) -> str:
    text = sanitize_text(str(value or ""), 80).strip().lower()
    return text if text in {"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"} else ""


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
        "visual_extraction_unavailable": "Visual frame extraction is unavailable.",
        "visual_summary_failed": "Visual media summary failed.",
        "timeout": "Media acquisition timed out.",
    }
    return messages.get(category, "Media acquisition failed.")
