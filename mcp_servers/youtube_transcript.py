import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.server.fastmcp import FastMCP
from youtube_transcript_api import YouTubeTranscriptApi


mcp = FastMCP("aigan-youtube-transcript")


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
        with audio_files[0].open("rb") as audio_file:
            result = client.audio.transcriptions.create(model=model, file=audio_file)
        text = getattr(result, "text", str(result))
        return text[:max_chars]


@mcp.tool()
def get_youtube_transcript(
    video: str,
    languages: str = "ru,en",
    include_timestamps: bool = True,
    max_chars: int = 16000,
) -> str:
    """Get captions/transcript for a YouTube URL or video id; optionally fall back to audio transcription."""
    max_chars = max(1000, min(int(max_chars), 40000))
    try:
        video_id = _video_id(video)
    except ValueError as exc:
        return str(exc)

    lang_list = [item.strip() for item in languages.split(",") if item.strip()]
    if not lang_list:
        lang_list = ["ru", "en"]

    try:
        entries = _fetch_caption_transcript(video_id, lang_list)
        transcript = _format_entries(entries, include_timestamps, max_chars)
        if transcript:
            return f"YouTube video id: {video_id}\nTranscript/captions:\n\n{transcript}"
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
        return f"YouTube video id: {video_id}\nAudio transcription:\n\n{transcript}"
    except Exception as exc:
        return f"Audio transcription failed for {video_id}: {type(exc).__name__}: {exc}"


if __name__ == "__main__":
    mcp.run()
