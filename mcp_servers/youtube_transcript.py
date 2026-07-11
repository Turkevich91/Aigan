import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if __package__ in {None, ""}:
    # MCPServerStdio executes this file directly, so expose the project modules.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp.server.fastmcp import FastMCP
from model_telemetry import ModelTelemetryStore, StageHandle
from youtube_transcript_api import YouTubeTranscriptApi


mcp = FastMCP("aigan-youtube-transcript")


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


MODEL_TELEMETRY = _build_model_telemetry_store()


def _begin_transcription_stage(model: str) -> StageHandle | None:
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
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value

    parsed = urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")

    if host.endswith("youtu.be") and path:
        return path.split("/")[0]
    if "youtube.com" in host:
        query_id = parse_qs(parsed.query).get("v", [""])[0]
        if query_id:
            return query_id
        parts = path.split("/")
        for marker in ("shorts", "embed", "live"):
            if marker in parts:
                index = parts.index(marker)
                if len(parts) > index + 1:
                    return parts[index + 1]

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
    try:
        return YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
    except AttributeError:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=languages)
        return fetched.to_raw_data()


def _audio_fallback_enabled() -> bool:
    return os.getenv("YOUTUBE_AUDIO_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}


def _transcribe_audio_fallback(video_url: str, max_chars: int) -> str:
    from openai import OpenAI
    from yt_dlp import YoutubeDL

    max_duration = int(os.getenv("YOUTUBE_MAX_DURATION_SECONDS", "1200"))
    model = os.getenv("YOUTUBE_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(video_url, download=False)
            duration = int(info.get("duration") or 0)
            if duration > max_duration:
                return (
                    f"Video is {duration}s long, over YOUTUBE_MAX_DURATION_SECONDS={max_duration}. "
                    "Refusing audio transcription."
                )

        output_template = str(tmp_path / "audio.%(ext)s")
        options = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        }
        with YoutubeDL(options) as ydl:
            ydl.download([video_url])

        audio_files = list(tmp_path.glob("audio.*"))
        if not audio_files:
            return "Audio download finished, but no audio file was found."

        client = OpenAI()
        stage = _begin_transcription_stage(model)
        try:
            with audio_files[0].open("rb") as audio_file:
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
        text = getattr(result, "text", str(result))
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
        return "Transcript was fetched but contained no text."
    except Exception as exc:
        if not _audio_fallback_enabled():
            return (
                f"No caption transcript available for {video_id}: {type(exc).__name__}: {exc}\n"
                "Audio fallback is disabled. Set YOUTUBE_AUDIO_FALLBACK=true to use OpenAI transcription."
            )

    video_url = video if video.startswith("http") else f"https://www.youtube.com/watch?v={video_id}"
    try:
        transcript = _transcribe_audio_fallback(video_url, max_chars)
        return (
            f"YouTube video id: {video_id}\n"
            "Audio transcription. If the transcription is Russian, summarize or explain it in Ukrainian, not Russian:\n\n"
            f"{transcript}"
        )
    except Exception as exc:
        return f"Audio transcription failed for {video_id}: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
