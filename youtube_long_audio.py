"""Acquire one bounded public YouTube audio track and checkpoint its full transcript."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
from urllib.parse import urlsplit
import wave


MAX_AUDIO_BYTES = 300_000_000
MAX_DURATION_MS = 4 * 3600 * 1000
CHUNK_MS = 5 * 60 * 1000
MAX_UPLOAD_BYTES = 25_000_000
_AUDIO_DEMUXERS = ("-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm")


class LongAudioUnavailable(Exception):
    """A fixed diagnostic without any request URL, source text, or provider output."""


def _public_host(host):
    return isinstance(host, str) and (host in {"youtubei.googleapis.com", "youtu.be"} or any(
        host == base or host.endswith("." + base) for base in ("youtube.com", "googlevideo.com", "ytimg.com")))


def _outbound(event, args):
    # This hook is installed only in the disposable extractor process. It covers
    # DNS and connection destinations used by the native Python download handlers.
    if event == "socket.getaddrinfo":
        host, port = args[:2]
        if not _public_host(host) or port not in (443, "443", "https"):
            raise LongAudioUnavailable("unexpected network destination")
    if event == "socket.connect":
        address = args[1]
        if not isinstance(address, tuple) or address[1] != 443:
            raise LongAudioUnavailable("unexpected network destination")
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise LongAudioUnavailable("unexpected network destination")


def _check_track(info, video_id):
    seconds = info.get("duration")
    if (info.get("id") != video_id or info.get("extractor_key") != "Youtube" or
            info.get("is_live") or info.get("live_status") not in (None, "not_live", "was_live") or
            info.get("availability") != "public" or
            type(seconds) not in (int, float) or not 0 < seconds <= MAX_DURATION_MS / 1000):
        raise LongAudioUnavailable("unsupported video or duration")
    address = urlsplit(info.get("url") or "")
    if (info.get("vcodec") != "none" or info.get("acodec") in (None, "none") or
            info.get("protocol") != "https" or info.get("fragments") or info.get("has_drm") or
            address.scheme != "https" or not address.hostname or not address.hostname.endswith(".googlevideo.com") or
            address.netloc != address.hostname or "\\" in address.netloc or
            (info.get("filesize") or 0) > MAX_AUDIO_BYTES):
        raise LongAudioUnavailable("no bounded public audio track")


def _download_worker(video_id, target):
    import resource
    from yt_dlp import YoutubeDL

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise LongAudioUnavailable("invalid video")
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    limit = min(v for v in (soft, hard, MAX_AUDIO_BYTES) if v >= 0)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
    sys.addaudithook(_outbound)
    duration = None

    class BoundedYoutubeDL(YoutubeDL):
        def process_info(self, info):
            nonlocal duration
            _check_track(info, video_id)
            duration = info["duration"]
            return super().process_info(info)

    def progress(info):
        if (info.get("downloaded_bytes") or 0) > MAX_AUDIO_BYTES:
            raise LongAudioUnavailable("audio byte limit")

    log = logging.getLogger("aigan.youtube.long.download")
    log.disabled = True
    opts = {
        "format": "bestaudio[ext=m4a][protocol=https]/bestaudio[protocol=https]",
        "outtmpl": str(target), "noplaylist": True,
        "allowed_extractors": ["youtube"], "quiet": True, "noprogress": True, "logger": log,
        "cachedir": False, "proxy": "", "usenetrc": False, "fixup": "never",
        "max_filesize": MAX_AUDIO_BYTES, "progress_hooks": [progress], "socket_timeout": 20,
        "retries": 2, "fragment_retries": 0, "extractor_retries": 1,
    }
    with BoundedYoutubeDL(opts) as ydl:
        ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=True)
    if not duration or not target.is_file() or not 0 < target.stat().st_size <= MAX_AUDIO_BYTES:
        raise LongAudioUnavailable("incomplete public audio")
    return duration


async def _command(command, *, timeout, directory):
    # Keep the bot's credentials, browser profiles and inherited proxies out of
    # media subprocesses. All children, including a JS helper, share a process group.
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH",
                                             "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
    env.update(HOME=str(directory), TMPDIR=str(directory), PYTHONDONTWRITEBYTECODE="1", LANG="C.UTF-8")
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, cwd=directory, env=env,
        start_new_session=True)
    try:
        async with asyncio.timeout(timeout):
            output, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        raise
    if process.returncode:
        raise LongAudioUnavailable("media process failed")
    return output


async def _download(video_id, path):
    result = await _command([sys.executable, str(Path(__file__).resolve()), "download", video_id, str(path)],
                            timeout=900, directory=path.parent)
    return float(result)


async def _duration_ms(source, directory):
    raw = await _command(["ffprobe", "-v", "error", *_AUDIO_DEMUXERS, "-show_entries",
        "stream=codec_type,duration:format=duration", "-of", "json", str(source)], timeout=30, directory=directory)
    try:
        data = json.loads(raw)
        streams = data["streams"]
        if len(streams) != 1 or streams[0]["codec_type"] != "audio":
            raise LongAudioUnavailable("audio-only track required")
        reported = streams[0].get("duration")
        duration = float(data["format"]["duration"] if reported in (None, "N/A") else reported)
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION_MS / 1000:
            raise LongAudioUnavailable("audio duration exceeds limit")
        duration_ms = round(duration * 1000)
        if duration_ms < 1:
            raise LongAudioUnavailable("audio duration unavailable")
        return duration_ms
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise LongAudioUnavailable("audio duration unavailable") from exc


async def _decode(source, start, end, wav):
    await _command(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y", "-ss", f"{start / 1000:.3f}",
                    *_AUDIO_DEMUXERS, "-i", str(source), "-t", f"{(end-start) / 1000:.3f}", "-map", "0:a:0",
                    "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout=120, directory=wav.parent)


def _silent_chunk(wav, length_ms):
    if not 0 < wav.stat().st_size < MAX_UPLOAD_BYTES:
        raise LongAudioUnavailable("invalid audio chunk size")
    with wave.open(str(wav), "rb") as pcm:
        frames = pcm.getnframes()
        if (pcm.getnchannels(), pcm.getsampwidth(), pcm.getframerate()) != (1, 2, 16000) or not frames:
            raise LongAudioUnavailable("invalid decoded audio")
        # A codec's final packet can differ from the measured timeline by a few
        # milliseconds. A short read must not advance the transcript checkpoint.
        if abs(frames / 16 - length_ms) > 80:
            raise LongAudioUnavailable("incomplete audio chunk")
        size, silent = 0, True
        while block := pcm.readframes(64 * 1024):
            size += len(block)
            silent = silent and block.count(0) == len(block)
        if size != frames * 2:
            raise LongAudioUnavailable("truncated audio chunk")
    return silent


async def _disk(func, *args):
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not release the entry lock or unlink its workspace mid-publication.
        await task
        raise


async def ensure_transcript(cache, video_id: str, *, transcribe, before_call) -> Path:
    """Return the complete UTF-8 transcript. The caller holds the chat/video cache lock."""
    if (not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id)
            or cache.directory.name != video_id):
        raise LongAudioUnavailable("invalid video")
    before_call()
    ready = await _disk(cache.transcript_path)
    if ready is not None:
        return ready
    with tempfile.TemporaryDirectory(dir=cache.directory, prefix=".audio-job-") as tmp:
        directory = Path(tmp)
        audio = await _disk(cache.audio)
        if audio is None:
            source = directory / "source.media"
            before_call()
            declared = await _download(video_id, source)
            duration = await _duration_ms(source, directory)
            if abs(duration - declared * 1000) > 2500:
                raise LongAudioUnavailable("incomplete source duration")
            before_call()
            source = await _disk(cache.save_audio, source, duration)
        else:
            source, duration = audio
        if not 0 < duration <= MAX_DURATION_MS or not 0 < source.stat().st_size <= MAX_AUDIO_BYTES:
            raise LongAudioUnavailable("cached audio exceeds limits")
        history = await _disk(cache.segments)
        cursor = history[-1][1] if history else 0
        wav = directory / "chunk.wav"
        while cursor < duration:
            before_call()
            end = min(cursor + CHUNK_MS, duration)
            await _decode(source, cursor, end, wav)
            silent = await _disk(_silent_chunk, wav, end-cursor)
            text = ""
            if not silent:
                before_call()
                with wav.open("rb") as stream:
                    text = await transcribe(stream)
                if not isinstance(text, str) or not text.strip():
                    raise LongAudioUnavailable("no words recognized in audible chunk")
                text = text.strip()
            await _disk(cache.append_segment, cursor, end, text)
            cursor = end
    ready = await _disk(cache.transcript_path)
    if ready is None:
        raise LongAudioUnavailable("transcript incomplete")
    return ready


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4 or sys.argv[1] != "download":
            raise SystemExit(2)
        print(_download_worker(sys.argv[2], Path(sys.argv[3])), flush=True)
    except Exception:
        raise SystemExit(1) from None
