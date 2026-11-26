"""Deliver YouTube evidence with explicit source selection and per-chat authorization."""
from __future__ import annotations

from pathlib import Path

from youtube_long_audio import _disk, ensure_transcript
from youtube_media_cache import CacheBusy, open_video
from youtube_timestamp_frames import youtube_frames


async def fetch_caption_evidence(video_id, *, before_call):
    from youtube_caption_source import fetch_caption_evidence as probe
    return await probe(video_id, before_call=before_call)


async def _transcript(cache, video_id, source, *, check_allowed, check_speech, transcribe, send_text, send_transcript):
    async def document(path, label):
        check_allowed()
        await send_transcript(path, label)

    check_allowed()
    if source not in {"native_first", "captions_only", "own_audio"}:
        raise ValueError("unknown transcript source")
    if source != "own_audio":
        ready = await _disk(cache.saved_public_transcript)
        if ready and ready[1] == "native":
            await document(*ready)
            return
        native = await _disk(cache.public_caption_document)
        # A publisher may later replace a truncated track with a complete one.
        check_allowed()
        evidence = await fetch_caption_evidence(video_id, before_call=check_allowed)
        check_allowed()
        if evidence.status in {"complete", "partial"}:
            await _disk(cache.save_public_transcript, evidence.cues, duration_ms=evidence.duration_ms,
                        covered_ms=evidence.covered_ms, complete=evidence.status == "complete", track_id=evidence.track_id)
            native = await _disk(cache.public_caption_document)
        elif evidence.status != "absent":
            if ready and source == "native_first":
                await document(*ready)
            elif native:
                await document(native[0], "partial")
            else:
                await send_text("Не вдалося перевірити субтитри YouTube. Спробуй пізніше або прямо попроси зробити власну розшифровку з аудіо.")
            return
        if native and (native[1] or source == "captions_only"):
            await document(native[0], "native" if native[1] else "partial")
            return
        if source == "captions_only":
            await send_text("Для цього відео не знайшов субтитрів YouTube.")
            return
        # Newly discovered partial captions can reuse speech from a previous audio-only run.
        if ready := await _disk(cache.saved_public_transcript):
            await document(*ready)
            return
    else:
        native = None

    if not native:
        own = await _disk(cache.transcript_path)
        if own is not None:
            await document(own, "own_audio")
            return
    try:
        check_speech()
    except PermissionError:
        if native:
            check_allowed()
            await send_text("Субтитри покривають частину відео. Розпізнавання решти аудіо вимкнене.")
            await document(native[0], "partial")
            return
        raise
    await send_text("Доповнюю відсутню частину з аудіо." if native else "Готую власну розшифровку з аудіо.")
    path = await ensure_transcript(cache, video_id, transcribe=transcribe, before_call=check_speech,
                                   prefer_native=source != "own_audio")
    await document(path, "mixed" if native else "own_audio")


async def deliver_youtube_media(request, *, root: Path, chat_id: int, check_allowed, check_speech, check_visual,
                                transcribe, describe_frames, send_text, send_transcript, source="native_first"):
    guard = open_video(root, chat_id, request.video_id)
    check_allowed()
    try:
        try:
            cache = await _disk(guard.__enter__)
        except CacheBusy:
            await send_text("Уже працюю з цим відео. Спробуй ще раз трохи пізніше.")
            return
        except (ValueError, OSError):
            await send_text("Медіатека зараз недоступна. Спробуй пізніше.")
            return

        if request.timestamp_ms is not None:
            try:
                check_allowed()
                check_visual()
                frames = await youtube_frames(request.video_id, request.timestamp_ms, cache, before_call=check_visual)
                check_allowed()
                answer = await describe_frames(frames)
            except PermissionError:
                check_allowed()
                if not request.transcript:
                    raise
                await send_text("Аналіз кадрів вимкнено. Перевіряю запит на розшифровку.")
            except Exception:
                check_allowed()
                await send_text("Не вдалося отримати й описати кадри біля вказаного часу.")
            else:
                check_allowed()
                await send_text(answer)

        if request.transcript:
            try:
                await _transcript(cache, request.video_id, source, check_allowed=check_allowed, check_speech=check_speech,
                                  transcribe=transcribe, send_text=send_text, send_transcript=send_transcript)
            except PermissionError:
                raise
            except Exception:
                check_allowed()
                await send_text("Повну розшифровку отримати не вдалося. Можеш повторити запит пізніше.")
    finally:
        await _disk(guard.__exit__, None, None, None)
