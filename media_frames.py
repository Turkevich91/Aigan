"""Bounded local container inspection and interval-frame extraction.

Owner: Aigan media frame adapter.
"""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from system_log import sanitize_text


# Only standalone local media containers. Network protocols, playlist and
# concatenation demuxers are outside the Telegram attachment boundary.
LOCAL_MEDIA_OPTIONS = ("-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi,gif,mpeg,mpegts")
SPARSE_FRAME_MAX_SECONDS = 1800


MEDIA_FRAME_FAILURE_CATEGORIES = {
    "disabled",
    "unconfigured",
    "metadata_failed",
    "input_missing",
    "input_too_large",
    "duration_too_long",
    "resolution_too_large",
    "decode_failed",
    "scene_detection_failed",
    "no_frames_selected",
    "timeout",
    "cleanup_failed",
    "unexpected_error",
}


@dataclass(frozen=True)
class MediaFrameLimits:
    max_duration_seconds: int = 90
    hard_max_duration_seconds: int = 180
    max_bytes: int = 50_000_000
    hard_max_bytes: int = 100_000_000
    max_width: int = 3840
    max_height: int = 2160
    max_pixels: int = 8_300_000
    candidate_frame_count: int = 24
    selected_frame_count: int = 5
    max_selected_frame_count: int = 8
    output_width: int = 512
    timeout_seconds: int = 30

    def bounded(self) -> "MediaFrameLimits":
        max_duration = max(1, min(int(self.max_duration_seconds), int(self.hard_max_duration_seconds)))
        max_bytes = max(1, min(int(self.max_bytes), int(self.hard_max_bytes)))
        max_selected = max(1, int(self.max_selected_frame_count))
        selected = max(1, min(int(self.selected_frame_count), max_selected))
        candidates = max(selected, min(max(1, int(self.candidate_frame_count)), max(1, max_selected * 3)))
        return replace(
            self,
            max_duration_seconds=max_duration,
            max_bytes=max_bytes,
            candidate_frame_count=candidates,
            selected_frame_count=selected,
            max_selected_frame_count=max_selected,
            output_width=max(64, int(self.output_width)),
            timeout_seconds=max(1, int(self.timeout_seconds)),
        )


@dataclass(frozen=True)
class MediaFrameRequest:
    source_path: Path | str
    source_family: str = "local_media"
    provenance_label: str = "media"
    mime_type: str = ""
    declared_size_bytes: int | None = None
    max_duration_seconds: int | None = None
    max_bytes: int | None = None
    selected_frame_count: int | None = None
    max_selected_frame_count: int | None = None
    timeout_seconds: int | None = None
    mode: str = "visual_summary"
    sparse_frame_count: int = 5
    sparse_max_duration_seconds: int = 0


@dataclass(frozen=True)
class MediaFrameCandidate:
    path: Path
    timestamp_seconds: float
    index: int
    mime_type: str = "image/jpeg"
    width: int | None = None
    height: int | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "timestamp_seconds": round(float(self.timestamp_seconds), 3),
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class MediaFrameResult:
    ok: bool
    backend: str = "none"
    source_family: str = "media"
    failure_category: str = ""
    user_message: str = ""
    frames: tuple[MediaFrameCandidate, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    candidate_count: int = 0
    selected_count: int = 0
    skipped_duplicate_count: int = 0
    truncated: bool = False
    cleanup_status: str = "not_needed"
    diagnostics: dict[str, Any] = field(default_factory=dict)
    _temp_dir: Path | None = None

    @classmethod
    def unavailable(
        cls,
        *,
        failure_category: str,
        backend: str = "none",
        source_family: str = "media",
        user_message: str = "Media frame extraction is unavailable.",
        cleanup_status: str = "not_needed",
        diagnostics: dict[str, Any] | None = None,
    ) -> "MediaFrameResult":
        return cls(
            ok=False,
            backend=safe_code_label(backend, default="none"),
            source_family=safe_code_label(source_family, default="media"),
            failure_category=safe_failure_category(failure_category),
            user_message=sanitize_text(user_message, 160),
            cleanup_status=safe_code_label(cleanup_status, default="not_needed"),
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

    def public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "source_family": self.source_family,
            "failure_category": self.failure_category,
            "user_message": self.user_message,
            "metadata": sanitize_public_mapping(self.metadata),
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "skipped_duplicate_count": self.skipped_duplicate_count,
            "truncated": self.truncated,
            "cleanup_status": self.cleanup_status,
            "frames": [frame.public_dict() for frame in self.frames],
            "diagnostics": sanitize_public_mapping(self.diagnostics),
        }


class MediaFrameAdapter(Protocol):
    async def extract_frames(self, request: MediaFrameRequest) -> MediaFrameResult:
        ...

    def frame_session(self, request: MediaFrameRequest) -> "MediaFrameSession":
        ...

    def health_summary(self) -> dict[str, Any]:
        ...


class MediaFrameSession:
    def __init__(self, adapter: MediaFrameAdapter, request: MediaFrameRequest) -> None:
        self.adapter = adapter
        self.request = request
        self.result: MediaFrameResult | None = None

    async def __aenter__(self) -> MediaFrameResult:
        self.result = await self.adapter.extract_frames(self.request)
        return self.result

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.result is not None:
            await self.result.cleanup()


class NullMediaFrameAdapter:
    def __init__(self, *, name: str = "media_frames") -> None:
        self.name = name

    async def extract_frames(self, request: MediaFrameRequest) -> MediaFrameResult:
        return MediaFrameResult.unavailable(
            failure_category="disabled",
            backend="null",
            source_family=request.source_family,
            user_message="Media frame extraction is disabled.",
        )

    def frame_session(self, request: MediaFrameRequest) -> MediaFrameSession:
        return MediaFrameSession(self, request)

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

    async def cleanup(self) -> None:
        return None


@dataclass(frozen=True)
class CommandOutput:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], float], CommandOutput]


class FfmpegMediaFrameAdapter:
    def __init__(
        self,
        *,
        enabled: bool = True,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        limits: MediaFrameLimits | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.enabled = enabled
        self.ffmpeg_path = ffmpeg_path.strip() or "ffmpeg"
        self.ffprobe_path = ffprobe_path.strip() or "ffprobe"
        self.limits = (limits or MediaFrameLimits()).bounded()
        self.command_runner = command_runner or run_command
        self.error_count = 0
        self.warning_count = 0
        self.last_failure_category = ""

    async def extract_frames(self, request: MediaFrameRequest) -> MediaFrameResult:
        if not self.enabled:
            return self._failure(request, "disabled", backend="ffmpeg_interval", cleanup_status="not_needed")
        limits = request_limits(self.limits, request)
        source_path = Path(request.source_path)
        temp_dir: Path | None = None
        backend = "ffmpeg_interval"
        try:
            if not source_path.is_file():
                return self._failure(request, "input_missing", backend="ffmpeg_interval", cleanup_status="not_needed")
            actual_size_bytes = source_path.stat().st_size
            declared_size_bytes = max(0, int(request.declared_size_bytes or 0))
            size_bytes = max(actual_size_bytes, declared_size_bytes)
            if size_bytes > limits.max_bytes:
                return self._failure(
                    request,
                    "input_too_large",
                    backend="ffmpeg_interval",
                    cleanup_status="not_needed",
                    diagnostics={"max_bytes": limits.max_bytes},
                )
            sparse_enabled = request.sparse_max_duration_seconds > limits.max_duration_seconds
            probe_timeout = min(limits.timeout_seconds, 30) if sparse_enabled else limits.timeout_seconds
            deadline = time.monotonic() + probe_timeout
            metadata = await finish_media_worker(self._probe_metadata, source_path, probe_timeout)
            validation_failure = validate_metadata(metadata, limits)
            sparse = validation_failure == "duration_too_long" and sparse_enabled
            if sparse:
                backend = "ffmpeg_sparse"
                sparse_cap = min(request.sparse_max_duration_seconds, SPARSE_FRAME_MAX_SECONDS)
                if metadata["video_start_seconds"] > 0 and not (metadata["width"] and metadata["height"]):
                    # With a delayed picture some containers expose dimensions only in its first packet.
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired("ffprobe", limits.timeout_seconds)
                    analyze_us = int(min(metadata["video_start_seconds"] + 2, SPARSE_FRAME_MAX_SECONDS) * 1_000_000)
                    metadata = await finish_media_worker(self._probe_metadata, source_path, remaining, analyze_us)
                track = {**metadata, "duration_seconds": metadata.get("video_duration_seconds")}
                validation_failure = validate_metadata(track, replace(limits, max_duration_seconds=sparse_cap))
            if validation_failure:
                return self._failure(
                    request,
                    validation_failure,
                    backend=backend,
                    cleanup_status="not_needed",
                    metadata=metadata,
                )
            temp_dir = Path(tempfile.mkdtemp(prefix="aigan-media-frames-"))
            try:
                if sparse:
                    count = min(limits.selected_frame_count, max(1, min(request.sparse_frame_count, 5)))
                    await finish_media_worker(self._extract_sparse_frames, source_path, temp_dir, metadata, limits, count, deadline)
                else:
                    await finish_media_worker(self._extract_interval_frames, source_path, temp_dir / "frame_%03d.jpg",
                                              metadata, limits)
            except asyncio.CancelledError:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise
            frame_paths = sorted(temp_dir.glob("frame_*.jpg"))
            if sparse:
                # Variable frame rate and very short tracks can map multiple seeks to the same picture.
                by_time = {int(path.stem.rsplit("_", 1)[1]): path for path in frame_paths}
                start = metadata["video_start_seconds"]
                end = start + metadata["video_duration_seconds"]
                if any(stamp < 0 or not start - .001 <= stamp / 1000 <= end + .001 for stamp in by_time):
                    raise RuntimeError("decode_failed")
                frame_paths = [by_time[stamp] for stamp in sorted(by_time)]
            if not frame_paths:
                result = self._failure(
                    request,
                    "no_frames_selected",
                    backend=backend,
                    cleanup_status="pending",
                    metadata=metadata,
                )
                result._temp_dir = temp_dir
                await result.cleanup()
                return result
            count = min(len(frame_paths), limits.selected_frame_count)
            indices = ([len(frame_paths) // 2] if count == 1 else
                       [round(i * (len(frame_paths) - 1) / (count - 1)) for i in range(count)])
            frames = tuple(
                MediaFrameCandidate(
                    path=frame_paths[index],
                    timestamp_seconds=(int(frame_paths[index].stem.rsplit("_", 1)[1]) / 1000 if sparse
                                       else round(index / sample_fps(metadata, limits), 3)),
                    index=index + 1,
                )
                for index in indices
            )
            self.last_failure_category = ""
            return MediaFrameResult(
                ok=True,
                backend=backend,
                source_family=safe_code_label(request.source_family, default="media"),
                frames=frames,
                metadata=sanitize_metadata(metadata),
                candidate_count=len(frame_paths),
                selected_count=len(frames),
                skipped_duplicate_count=max(0, len(frame_paths) - len(frames)),
                truncated=len(frame_paths) > len(frames),
                cleanup_status="pending",
                diagnostics={
                    "mode": safe_code_label(request.mode, default="visual_summary"),
                    "provenance_label": safe_code_label(request.provenance_label, default="media"),
                },
                _temp_dir=temp_dir,
            )
        except subprocess.TimeoutExpired:
            result = self._failure(request, "timeout", backend=backend, cleanup_status="pending")
            result._temp_dir = temp_dir
            await result.cleanup()
            return result
        except RuntimeError as exc:
            category = safe_failure_category(str(exc))
            result = self._failure(
                request,
                category,
                backend=backend,
                cleanup_status="pending" if temp_dir is not None else "not_needed",
            )
            result._temp_dir = temp_dir
            await result.cleanup()
            return result
        except Exception as exc:
            result = self._failure(
                request,
                "unexpected_error",
                backend=backend,
                cleanup_status="pending" if temp_dir is not None else "not_needed",
                diagnostics={"error_type": safe_code_label(type(exc).__name__, default="error")},
            )
            result._temp_dir = temp_dir
            await result.cleanup()
            return result

    def frame_session(self, request: MediaFrameRequest) -> MediaFrameSession:
        return MediaFrameSession(self, request)

    def health_summary(self) -> dict[str, Any]:
        ffmpeg_available = executable_available(self.ffmpeg_path)
        ffprobe_available = executable_available(self.ffprobe_path)
        configured = bool(self.ffmpeg_path and self.ffprobe_path)
        available = self.enabled and configured and ffmpeg_available and ffprobe_available
        if not self.enabled:
            status = "disabled"
        elif not configured:
            status = "unconfigured"
        elif not available:
            status = "unavailable"
        elif self.error_count:
            status = "degraded"
        else:
            status = "ok"
        details: dict[str, Any] = {
            "name": "media_frames",
            "family": "media",
            "enabled": self.enabled,
            "configured": configured,
            "available": available,
            "adapter": type(self).__name__,
            "status": status,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "backend": "ffmpeg_interval",
            "ffmpeg_available": ffmpeg_available,
            "ffprobe_available": ffprobe_available,
            "temp_dir_writable": temp_dir_writable(),
            "max_bytes": self.limits.max_bytes,
            "max_duration_seconds": self.limits.max_duration_seconds,
            "max_frame_count": self.limits.max_selected_frame_count,
            "max_candidate_frames": self.limits.candidate_frame_count,
            "max_pixels": self.limits.max_pixels,
        }
        if self.last_failure_category:
            details["last_failure_category"] = self.last_failure_category
        return details

    async def cleanup(self) -> None:
        return None

    def _probe_metadata(self, source_path: Path, timeout_seconds: float, analyze_us: int | None = None) -> dict[str, Any]:
        output = self.command_runner(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                *(["-analyzeduration", str(analyze_us)] if analyze_us else []),
                *LOCAL_MEDIA_OPTIONS,
                str(source_path),
            ],
            timeout_seconds,
        )
        if output.returncode != 0:
            raise RuntimeError("metadata_failed")
        return parse_ffprobe_metadata(output.stdout)

    def _extract_interval_frames(
        self,
        source_path: Path,
        output_pattern: Path,
        metadata: dict[str, Any],
        limits: MediaFrameLimits,
    ) -> None:
        fps = sample_fps(metadata, limits)
        output = self.command_runner(
            [
                self.ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-nostdin",
                *LOCAL_MEDIA_OPTIONS,
                "-i",
                str(source_path),
                "-t",
                str(limits.max_duration_seconds),
                "-map",
                f"0:{metadata['video_stream_index']}",
                "-vf",
                f"fps={fps:.6f}:round=up,scale={limits.output_width}:{limits.output_width}:force_original_aspect_ratio=decrease",
                "-frames:v",
                str(limits.candidate_frame_count),
                str(output_pattern),
            ],
            limits.timeout_seconds,
        )
        if output.returncode != 0:
            raise RuntimeError("decode_failed")

    def _extract_sparse_frames(self, source_path, directory, metadata, limits, count, deadline):
        """Seek once per sample. Image filenames retain decoded presentation timestamps in ms."""
        duration = metadata["video_duration_seconds"]
        offset = metadata["video_start_seconds"]
        end = max(0, duration - max(1., 1 / (metadata["fps"] or 1.)))
        for i in range(count):
            seek = offset + (end / 2 if count == 1 else i * end / (count - 1))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("ffmpeg", min(limits.timeout_seconds, 30))
            pattern = directory / f"frame_{i:02d}_%d.jpg"
            output = self.command_runner([
                self.ffmpeg_path, "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1",
                "-ss", f"{seek:.6f}", "-copyts", "-start_at_zero", *LOCAL_MEDIA_OPTIONS, "-i", str(source_path),
                "-map", f"0:{metadata['video_stream_index']}", "-an", "-sn", "-dn",
                "-vf", f"scale={limits.output_width}:{limits.output_width}:force_original_aspect_ratio=decrease",
                "-frames:v", "1", "-threads", "1", "-fps_mode", "passthrough", "-enc_time_base", "1:1000", "-frame_pts", "1",
                str(pattern)], remaining)
            if output.returncode or not any(directory.glob(f"frame_{i:02d}_*.jpg")):
                raise RuntimeError("decode_failed")

    def _failure(
        self,
        request: MediaFrameRequest,
        category: str,
        *,
        backend: str,
        cleanup_status: str,
        metadata: dict[str, Any] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> MediaFrameResult:
        clean_category = safe_failure_category(category)
        if clean_category not in {"disabled"}:
            self.error_count += 1
            self.last_failure_category = clean_category
        return MediaFrameResult.unavailable(
            failure_category=clean_category,
            backend=backend,
            source_family=request.source_family,
            user_message=user_message_for_failure(clean_category),
            cleanup_status=cleanup_status,
            diagnostics={
                "mode": safe_code_label(request.mode, default="visual_summary"),
                "provenance_label": safe_code_label(request.provenance_label, default="media"),
                **sanitize_public_mapping(metadata or {}),
                **sanitize_public_mapping(diagnostics or {}),
            },
        )


def sample_fps(metadata: dict[str, Any], limits: MediaFrameLimits) -> float:
    return min(1.0, limits.candidate_frame_count / max(float(metadata["duration_seconds"]), 1.0))


async def finish_media_worker(function, *args):
    """Keep the local files and worker slot until the bounded subprocess has exited."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


def run_command(command: Sequence[str], timeout_seconds: float) -> CommandOutput:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return CommandOutput(completed.returncode, completed.stdout or "", completed.stderr or "")


def request_limits(base: MediaFrameLimits, request: MediaFrameRequest) -> MediaFrameLimits:
    updated = base
    if request.max_duration_seconds is not None:
        updated = replace(updated, max_duration_seconds=request.max_duration_seconds)
    if request.max_bytes is not None:
        updated = replace(updated, max_bytes=request.max_bytes)
    if request.selected_frame_count is not None:
        updated = replace(updated, selected_frame_count=request.selected_frame_count)
    if request.max_selected_frame_count is not None:
        updated = replace(updated, max_selected_frame_count=request.max_selected_frame_count)
    if request.timeout_seconds is not None:
        updated = replace(updated, timeout_seconds=request.timeout_seconds)
    return updated.bounded()


def parse_ffprobe_metadata(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("metadata_failed") from exc
    streams = data.get("streams", [])
    video_tracks = (item for item in streams if item.get("codec_type") == "video"
                    and not (item.get("disposition") or {}).get("attached_pic"))
    # Players prefer the default track. Without a default prefer the largest picture; ties keep stream order.
    stream = max(video_tracks, default=None, key=lambda item: (
        bool((item.get("disposition") or {}).get("default")),
        (parse_int(item.get("width")) or 0) * (parse_int(item.get("height")) or 0)))
    if not stream:
        raise RuntimeError("metadata_failed")
    fmt = data.get("format") or {}
    video_duration = parse_float(stream.get("duration")) or 0
    if not video_duration:
        ticks = parse_int(stream.get("duration_ts")) or 0
        video_duration = ticks * (parse_ratio(stream.get("time_base")) or 0)
    format_duration = parse_float(fmt.get("duration")) or 0
    if not all(math.isfinite(value) for value in (video_duration, format_duration)):
        raise RuntimeError("metadata_failed")
    duration = max(video_duration, format_duration)
    if not video_duration and len(streams) == 1:
        video_duration = format_duration
    video_start = (parse_float(stream.get("start_time")) or 0) - (parse_float(fmt.get("start_time")) or 0)
    if not math.isfinite(video_start):
        raise RuntimeError("metadata_failed")
    frame_count = parse_int(stream.get("nb_frames"))
    fps = parse_ratio(stream.get("avg_frame_rate")) or parse_ratio(stream.get("r_frame_rate")) or 0.0
    width = parse_int(stream.get("width")) or 0
    height = parse_int(stream.get("height")) or 0
    return {
        "duration_seconds": round(duration, 3) if duration else 0,
        "width": width,
        "height": height,
        "fps": round(fps, 3) if fps and math.isfinite(fps) and fps > 0 else 0,
        "frame_count": frame_count,
        "codec": safe_code_label(stream.get("codec_name") or "", default="unknown"),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
        "video_duration_seconds": video_duration,
        "video_start_seconds": max(0., video_start),
        "video_stream_index": parse_int(stream.get("index")) or 0,
    }


def validate_metadata(metadata: dict[str, Any], limits: MediaFrameLimits) -> str:
    duration = float(metadata.get("duration_seconds") or 0)
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    if not math.isfinite(duration) or duration <= 0:
        return "metadata_failed"
    if duration > limits.max_duration_seconds:
        return "duration_too_long"
    if width <= 0 or height <= 0:
        return "metadata_failed"
    if width > limits.max_width or height > limits.max_height or width * height > limits.max_pixels:
        return "resolution_too_large"
    return ""


def parse_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_ratio(value: Any) -> float | None:
    text = str(value or "")
    if "/" not in text:
        return parse_float(text)
    numerator, denominator = text.split("/", 1)
    top = parse_float(numerator)
    bottom = parse_float(denominator)
    if not top or not bottom:
        return None
    return top / bottom


def sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in ("duration_seconds", "width", "height", "fps", "frame_count", "codec", "has_audio"):
        if key in metadata and metadata[key] is not None:
            clean[key] = metadata[key]
    return clean


def sanitize_public_mapping(values: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in values.items():
        key_text = safe_code_label(key, default="")
        if not key_text:
            continue
        if isinstance(value, bool | int | float):
            clean[key_text] = value
        elif isinstance(value, str):
            clean[key_text] = sanitize_text(value, 160)
    return clean


def safe_code_label(value: Any, *, default: str) -> str:
    text = sanitize_text(str(value or ""), 120).strip().lower().replace("-", "_").replace(" ", "_")
    text = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "."})
    return text[:80] or default


def safe_failure_category(value: Any) -> str:
    category = safe_code_label(value, default="unexpected_error")
    return category if category in MEDIA_FRAME_FAILURE_CATEGORIES else "unexpected_error"


def executable_available(path: str) -> bool:
    if not path:
        return False
    return shutil.which(path) is not None or Path(path).is_file()


def temp_dir_writable() -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="aigan-media-frame-health-") as temp_dir:
            Path(temp_dir, "probe").write_text("ok", encoding="utf-8")
        return True
    except Exception:
        return False


def user_message_for_failure(category: str) -> str:
    messages = {
        "disabled": "Media frame extraction is disabled.",
        "unconfigured": "Media frame extraction is not configured.",
        "metadata_failed": "I could not read video metadata safely.",
        "input_missing": "The media file is unavailable.",
        "input_too_large": "The media file is too large for visual frame extraction.",
        "duration_too_long": "The video is too long for bounded visual frame extraction.",
        "resolution_too_large": "The video resolution is too large for bounded visual frame extraction.",
        "decode_failed": "Media frame decoding failed.",
        "scene_detection_failed": "I could not detect representative scenes.",
        "no_frames_selected": "Media frame selection failed.",
        "timeout": "Media frame extraction timed out.",
        "cleanup_failed": "Media frame extraction cleanup failed.",
    }
    return messages.get(category, "Media frame extraction failed.")
