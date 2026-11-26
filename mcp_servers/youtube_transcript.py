import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

if __package__ in {None, ""}:
    # MCPServerStdio executes this file directly, so expose the project modules.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP
from model_telemetry import ModelTelemetryStore, StageHandle
from provenance import canonical_source
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi


mcp = FastMCP("aigan-youtube-transcript")
MAX_AUDIO_BYTES = 25_000_000
# yt-dlp can log source URLs and media details even when running quietly.
DOWNLOADER_LOG = logging.Logger("aigan.youtube.downloader")
DOWNLOADER_LOG.disabled = True


class AudioFallbackUnavailable(Exception):
    """A bounded reason why this video cannot be transcribed."""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _build_model_telemetry_store() -> ModelTelemetryStore | None:
    if not _env_bool("MODEL_TELEMETRY_ENABLED", False):
        return None
    db_path = os.getenv("MODEL_TELEMETRY_DB_PATH", "").strip() or os.getenv(
        "MEMORY_DB_PATH", ""
    ).strip()
    if not db_path:
        return None
    try:
        retention_days = max(
            1,
            int(os.getenv("MODEL_TELEMETRY_RETENTION_DAYS", "30").strip()),
        )
        return ModelTelemetryStore(Path(db_path), retention_days=retention_days)
    except Exception:
        # Telemetry is deliberately best-effort and must never disable the tool.
        return None


MODEL_TELEMETRY: ModelTelemetryStore | None = None
_STORE_LOCK = threading.Lock()


def _begin_transcription_stage(model: str) -> StageHandle | None:
    global MODEL_TELEMETRY
    with _STORE_LOCK:
        if MODEL_TELEMETRY is None:
            MODEL_TELEMETRY = _build_model_telemetry_store()
        store = MODEL_TELEMETRY
    if store is None:
        return None
    try:
        return store.begin_stage(
            run_id=os.getenv("AIGAN_MODEL_RUN_ID", ""),
            route_bucket=os.getenv("AIGAN_MODEL_ROUTE_BUCKET", "tool"),
            task_class_bucket="youtube_transcript",
            policy_version=os.getenv("MODEL_ROUTING_POLICY_VERSION", ""),
            stage_kind="transcription",
            intended_model=model,
            endpoint="audio_transcriptions",
        )
    except Exception:
        return None


def _finish_transcription_stage(
    handle: StageHandle | None,
    *,
    status: str,
    usage: object | None = None,
    failure_class: type[BaseException] | None = None,
) -> None:
    store = MODEL_TELEMETRY
    if store is None or handle is None:
        return
    try:
        store.finish_stage(
            handle,
            status=status,
            usage=usage,
            # The transcription response does not report an effective model.
            actual_model="",
            actual_model_source="not_reported",
            failure_class=failure_class,
            fallback_reason="captions_unavailable",
            retry_count=0,
        )
    except Exception:
        # A telemetry sink failure must not change the transcription result.
        return


def _video_id(value: str) -> str:
    video_id = canonical_source("get_youtube_transcript", {"video": value})
    if video_id:
        return video_id
    raise ValueError("Could not extract YouTube video id")


def _format_entries(entries: list[dict], include_timestamps: bool, max_chars: int) -> str:
    lines: list[str] = []
    for item in entries:
        text = " ".join(str(item.get("text", "")).split())
        if not text:
            continue
        if include_timestamps:
            start = int(float(item.get("start", 0)))
            minutes, seconds = divmod(start, 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines)[:max_chars]


def _fetch_caption_transcript(video_id: str, languages: list[str]) -> list[dict]:
    tracks = YouTubeTranscriptApi().list(video_id)
    # A manual en-US track is also usable when the caller requests en.
    regional_codes = [
        track.language_code
        for lang in languages
        for track in tracks
        if track.language_code.casefold().startswith(lang.casefold() + "-")
    ]
    track = tracks.find_transcript(languages + regional_codes)
    return track.fetch().to_raw_data()


def _audio_fallback_enabled() -> bool:
    return os.getenv("YOUTUBE_AUDIO_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}


def _transcribe_audio_fallback(video_url: str, max_chars: int) -> str:
    from openai import OpenAI
    from yt_dlp import YoutubeDL

    max_duration = int(os.getenv("YOUTUBE_MAX_DURATION_SECONDS", "1200"))
    model = os.getenv("YOUTUBE_TRANSCRIPTION_MODEL", "gpt-transcribe")

    def require_bounded_video(info, *, incomplete):
        if incomplete:
            return
        duration = info.get("duration")
        within_limit = isinstance(duration, (float, int)) and 0 < duration <= max_duration
        if info.get("is_live") or not within_limit:
            raise AudioFallbackUnavailable(
                f"Audio requires a completed video with a known duration up to {max_duration}s."
            )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_template = str(tmp_path / "audio.%(ext)s")
        options = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "noprogress": True,
            "logger": DOWNLOADER_LOG,
            "match_filter": require_bounded_video,
            "max_filesize": MAX_AUDIO_BYTES,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        }
        with YoutubeDL(options) as ydl:
            ydl.download([video_url])

        audio = tmp_path / "audio.mp3"
        if not audio.is_file() or not 0 < audio.stat().st_size <= MAX_AUDIO_BYTES:
            raise AudioFallbackUnavailable("No complete audio file within the upload size limit was available.")

        client = OpenAI()
        stage = _begin_transcription_stage(model)
        try:
            with audio.open("rb") as audio_file:
                result = client.audio.transcriptions.create(model=model, file=audio_file)
        except Exception as exc:
            _finish_transcription_stage(
                stage,
                status="failed",
                failure_class=type(exc),
            )
            raise
        _finish_transcription_stage(
            stage,
            status="succeeded",
            usage=getattr(result, "usage", None),
        )
        text = getattr(result, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise AudioFallbackUnavailable("No speech was transcribed.")
        return text[:max_chars]


@mcp.tool()
def get_youtube_transcript(
    video: str,
    languages: str = "uk,en,ru",
    include_timestamps: bool = True,
    max_chars: int = 16000,
) -> str:
    """Get captions/transcript for YouTube; prefer Ukrainian/English captions, with Russian only as source material."""
    max_chars = max(1000, min(int(max_chars), 40000))
    try:
        video_id = _video_id(video)
    except ValueError as exc:
        return str(exc)

    library = os.getenv("AIGAN_MEDIA_LIBRARY")
    chat = os.getenv("AIGAN_MEDIA_CHAT")
    if library and chat:
        from youtube_media_cache import cached_excerpt
        try:
            cached = cached_excerpt(Path(library), int(chat), video_id, max_chars, include_timestamps)
        except (OSError, ValueError):
            cached = None
        if cached:
            return f"Saved YouTube audio transcription for {video_id}:\n\n{cached}"

    lang_list = [item.strip() for item in languages.split(",") if item.strip()]
    if not lang_list:
        lang_list = ["uk", "en", "ru"]

    try:
        entries = _fetch_caption_transcript(video_id, lang_list)
        transcript = _format_entries(entries, include_timestamps, max_chars)
        if transcript:
            return (
                f"YouTube video id: {video_id}\n"
                "Transcript/captions. If the transcript is Russian, summarize or explain it in Ukrainian, not Russian:\n\n"
                f"{transcript}"
            )
    except (TranscriptsDisabled, NoTranscriptFound):
        pass
    except Exception as exc:
        return f"Caption lookup failed for {video_id}: {type(exc).__name__}."

    if not _audio_fallback_enabled():
        return f"No caption transcript available for {video_id}. Audio fallback is disabled."

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        transcript = _transcribe_audio_fallback(video_url, max_chars)
        return (
            f"YouTube video id: {video_id}\n"
            "Audio transcription. If the transcription is Russian, summarize or explain it in Ukrainian, not Russian:\n\n"
            f"{transcript}"
        )
    except AudioFallbackUnavailable as exc:
        return f"Audio transcription unavailable for {video_id}: {exc}"
    except Exception as exc:
        return f"Audio transcription failed for {video_id}: {type(exc).__name__}."


if __name__ == "__main__":
    mcp.run()
