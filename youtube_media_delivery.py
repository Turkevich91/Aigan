from __future__ import annotations

import asyncio
from pathlib import Path

from youtube_media_cache import CacheBusy, open_video
from youtube_long_audio import ensure_transcript
from youtube_timestamp_frames import youtube_frames


async def deliver_youtube_media(request, *, root: Path, chat_id: int, check_allowed, check_speech, check_visual,
                                transcribe, describe_frames, send_text, send_transcript):
    """Serve the explicit requests independently. Only a fully covered transcript is sent."""
    guard = open_video(root, chat_id, request.video_id)
    check_allowed()
    try:
        cache = await asyncio.to_thread(guard.__enter__)
    except CacheBusy:
        await send_text("Уже працюю з цим відео. Спробуй ще раз трохи пізніше.")
        return
    except (ValueError, OSError):
        await send_text("Медіатека зараз недоступна. Спробуй пізніше.")
        return
    try:
        if request.timestamp_ms is not None:
            try:
                check_allowed()
                check_visual()
                frames = await youtube_frames(request.video_id, request.timestamp_ms, cache, before_call=check_visual)
                check_allowed()
                answer = await describe_frames(frames)
            except PermissionError:
                raise
            except Exception:
                await send_text("Не вдалося отримати й описати кадри біля вказаного часу.")
            else:
                check_allowed()
                await send_text(answer)

        if request.transcript:
            check_allowed()
            cached = await asyncio.to_thread(cache.transcript_path)
            if cached is None:
                check_speech()
                await send_text("Готую повну розшифровку. Довгі записи обробляю частинами.")
            try:
                result = cached or await ensure_transcript(cache, request.video_id, transcribe=transcribe, before_call=check_speech)
            except PermissionError:
                raise
            except Exception:
                await send_text("Повну розшифровку отримати не вдалося. Можеш повторити запит пізніше.")
            else:
                check_allowed()
                await send_transcript(result)
    finally:
        await asyncio.to_thread(guard.__exit__, None, None, None)
