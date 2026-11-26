"""Timestamped stills from the official storyboard of one public YouTube video."""
from __future__ import annotations

import asyncio
from bisect import bisect_left
import logging
import os
import struct
import sys
import time
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image

from public_media_http import PublicMediaTransferError, bounded_chunks, pinned_get
from youtube_long_audio import LongAudioUnavailable, MAX_DURATION_MS, _command, _disk, _outbound


class YouTubePreviewUnavailable(Exception):
    """A bounded public-source failure. Messages contain no upstream payloads."""


@dataclass(frozen=True)
class PreviewFrame:
    timestamp_ms: int
    path: Path
    origin_label: str = "YouTube preview"


@dataclass(frozen=True)
class _Level:
    index: int
    width: int
    height: int
    count: int
    columns: int
    rows: int
    interval_ms: int
    signature: str


async def _read(url, mime, limit, before_call):
    if before_call:
        before_call()
    async with pinned_get(url) as response:
        if (response.status_code != 200 or
                response.headers.get("content-type", "").split(";")[0].lower().strip() != mime or
                response.headers.get("content-encoding", "identity").lower() != "identity"):
            raise YouTubePreviewUnavailable("public resource unavailable")
        return b"".join([part async for part in bounded_chunks(response, limit)])


def _storyboard(page, video_id):
    text = page.decode("utf-8")
    match = re.search(r"\bvar\s+ytInitialPlayerResponse\s*=\s*", text)
    if not match:
        raise YouTubePreviewUnavailable("player unavailable")
    player, _ = json.JSONDecoder().raw_decode(text, match.end())
    video = player["videoDetails"]
    if (player["playabilityStatus"]["status"] != "OK" or video["videoId"] != video_id
            or video.get("isPrivate") or video.get("isLive")):
        raise YouTubePreviewUnavailable("video unavailable")
    duration_ms = int(video["lengthSeconds"]) * 1000
    if duration_ms <= 0:
        raise YouTubePreviewUnavailable("duration unavailable")
    spec = player["storyboards"]["playerStoryboardSpecRenderer"]["spec"]
    if not isinstance(spec, str) or len(spec) > 16_384:
        raise YouTubePreviewUnavailable("storyboard unavailable")
    template, *entries = spec.split("|")
    # Construct the entire network destination from a provider template verified
    # for this exact post. Only its opaque signed query is carried through.
    parts = urlsplit(template)
    if (not template.isascii() or not template.isprintable() or "\\" in template or
            parts.scheme != "https" or parts.netloc != "i.ytimg.com" or parts.fragment or
            parts.path != f"/sb/{video_id}/storyboard3_L$L/$N.jpg"):
        raise YouTubePreviewUnavailable("invalid storyboard address")
    levels = []
    for index, entry in enumerate(entries):
        fields = entry.split("#")
        if (len(fields) != 8 or fields[6] != "M$M" or
                not all(re.fullmatch("[0-9]{1,8}", value) for value in fields[:6])):
            continue
        level = _Level(index, *(int(x) for x in fields[:6]), fields[7])
        if (0 < level.width <= 640 and 0 < level.height <= 360 and 0 < level.count and
                0 < level.columns <= 12 and 0 < level.rows <= 12 and 0 < level.interval_ms <= 60000 and
                level.width * level.height * level.columns * level.rows <= 4_000_000):
            levels.append(level)
    if not levels:
        raise YouTubePreviewUnavailable("no timed previews")
    return duration_ms, parts.query, max(levels, key=lambda item: item.width * item.height)


def _save_sheet(data, level, indices, cache):
    per_sheet = level.columns * level.rows
    remaining = min(per_sheet, level.count - (indices[0] // per_sheet) * per_sheet)
    minimum = (min(level.columns, remaining) * level.width,
               ((remaining + level.columns - 1) // level.columns) * level.height)
    maximum = (level.columns * level.width, level.rows * level.height)
    paths = {}
    with Image.open(BytesIO(data)) as sheet, TemporaryDirectory(dir=cache.directory) as tmp:
        # The last sprite sheet can omit its unused rows or columns.
        if (sheet.format != "JPEG" or sheet.width % level.width or sheet.height % level.height
                or not minimum[0] <= sheet.width <= maximum[0]
                or not minimum[1] <= sheet.height <= maximum[1]):
            raise YouTubePreviewUnavailable("invalid storyboard sheet")
        sheet.load()
        for index in indices:
            row, col = divmod(index % per_sheet, level.columns)
            x, y = col * level.width, row * level.height
            path = Path(tmp) / f"{index}.jpg"
            with sheet.crop((x, y, x + level.width, y + level.height)) as crop:
                crop.convert("RGB").save(path, quality=94)
            paths[index] = cache.save_frame(index * level.interval_ms, path)
    return paths


async def youtube_preview_frames(video_id: str, timestamp_ms: int, cache, *, before_call=None) -> tuple[PreviewFrame, ...]:
    """Return nearest timed storyboard still and adjacent stills, at most five.

    The caller holds the video's cache lock until these paths have been consumed.
    """
    if (not isinstance(video_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id) or
            type(timestamp_ms) is not int or timestamp_ms < 0 or cache.directory.name != video_id):
        raise YouTubePreviewUnavailable("invalid video or time")
    try:
        async with asyncio.timeout(35):
            page = await _read(f"https://www.youtube.com/watch?v={video_id}", "text/html", 4_000_000, before_call)
            duration, query, level = await asyncio.to_thread(_storyboard, page, video_id)
            if timestamp_ms > duration:
                raise YouTubePreviewUnavailable("timestamp outside video")
            last = min(level.count - 1, (duration - 1) // level.interval_ms)
            if timestamp_ms - last * level.interval_ms > level.interval_ms:
                raise YouTubePreviewUnavailable("timestamp outside storyboard")
            center = min(last, (timestamp_ms + level.interval_ms // 2) // level.interval_ms)
            found = {}
            missing = {}
            for index in range(max(0, center - 2), min(last, center + 2) + 1):
                path = await _disk(cache.frame, index * level.interval_ms)
                if path is None:
                    number = index // (level.columns * level.rows)
                    missing.setdefault(number, []).append(index)
                else:
                    found[index] = path
            for number, indices in missing.items():
                url = f"https://i.ytimg.com/sb/{video_id}/storyboard3_L{level.index}/M{number}.jpg?"
                if query:
                    url += query + "&"
                url += "sigh=" + quote(level.signature, safe="")
                sheet = await _read(url, "image/jpeg", 2_000_000, before_call)
                found.update(await _disk(_save_sheet, sheet, level, indices, cache))
            return tuple(PreviewFrame(i * level.interval_ms, found[i]) for i in sorted(found))
    except (PublicMediaTransferError, httpx.HTTPError, OSError, ValueError, KeyError,
            TypeError, AttributeError, OverflowError, RecursionError, Image.DecompressionBombError) as exc:
        raise YouTubePreviewUnavailable("could not read video previews") from exc


class _SectionUnavailable(Exception):
    pass


def _video_track(video_id):
    # Same isolated Python networking policy and subprocess environment as audio.
    from yt_dlp import YoutubeDL

    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
        raise _SectionUnavailable()
    sys.addaudithook(_outbound)
    log = logging.Logger("aigan.youtube.video")
    log.disabled = True
    opts = {"format": "bestvideo[protocol=https][ext=mp4][vcodec^=avc1][height<=720][fps<=60]",
            "allowed_extractors": ["youtube"], "noplaylist": True, "quiet": True, "logger": log,
            "cachedir": False, "proxy": "", "usenetrc": False, "socket_timeout": 8,
            "retries": 0, "extractor_retries": 0}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + video_id, download=False)
    if (info.get("id") != video_id or info.get("extractor_key") != "Youtube" or info.get("is_live")
            or info.get("availability") not in (None, "public", "unlisted")
            or info.get("live_status") not in (None, "not_live", "was_live")
            or info.get("protocol") != "https" or info.get("has_drm") or info.get("fragments")
            or info.get("acodec") != "none" or not 0 < info["duration"] * 1000 <= MAX_DURATION_MS):
        raise _SectionUnavailable()
    url = info["url"]
    parts = urlsplit(url)
    if (not url.isascii() or not url.isprintable() or "\\" in url or parts.scheme != "https"
            or not parts.hostname or parts.netloc != parts.hostname or not parts.hostname.endswith(".googlevideo.com")
            or parts.path != "/videoplayback" or parts.fragment or len(url) > 16384):
        raise _SectionUnavailable()
    return {"url": url, "duration": round(info["duration"] * 1000), "available": info.get("available_at") or 0}


async def _range(url, first, last, expected_size, before_call):
    if before_call:
        before_call()
    async with pinned_get(url, byte_range=(first, last)) as response:
        span = re.fullmatch(r"bytes ([0-9]{1,12})-([0-9]{1,12})/([0-9]{1,12})", response.headers.get("content-range", ""))
        if response.status_code != 206 or not span or response.headers.get("content-encoding", "identity") != "identity":
            raise _SectionUnavailable()
        start, end, total = map(int, span.groups())
        if (not total or start != first or end != min(last, total - 1) or
                (expected_size is not None and total != expected_size)):
            raise _SectionUnavailable()
        data = b"".join([part async for part in bounded_chunks(response, end - start + 1)])
        if len(data) != end - start + 1:
            raise _SectionUnavailable()
        return data, total


def _section_index(header, size, duration_ms, targets):
    # DASH MP4 Segment Index (ISO/IEC 14496-12). Each complete fragment retains
    # its original tfdt presentation clock even when preceding fragments are omitted.
    pos = 0
    while pos + 8 <= len(header):
        length, tag = struct.unpack_from(">I4s", header, pos)
        if length < 8 or pos + length > len(header):
            raise _SectionUnavailable()
        if tag == b"sidx":
            break
        pos += length
    else:
        raise _SectionUnavailable()
    box = header[pos + 8:pos + length]
    if len(box) < 24 or box[0] not in (0, 1) or header[4:8] != b"ftyp":
        raise _SectionUnavailable()
    timescale = struct.unpack_from(">I", box, 8)[0]
    field = ">QQ" if box[0] else ">II"
    tick, relative = struct.unpack_from(field, box, 12)
    cursor = 12 + struct.calcsize(field)
    count = struct.unpack_from(">H", box, cursor + 2)[0]
    if not timescale or len(box) != cursor + 4 + count * 12:
        raise _SectionUnavailable()
    ranges = []
    offset = pos + length + relative
    for start in range(cursor + 4, len(box), 12):
        span, dt, _ = struct.unpack_from(">III", box, start)
        if not 0 < span < 2**31 or not dt or offset + span > size:
            raise _SectionUnavailable()
        if tick * 1000 <= (targets[-1] + 100) * timescale and (tick + dt) * 1000 > (targets[0] - 100) * timescale:
            ranges.append((offset, offset + span - 1))
        offset += span
        tick += dt
    if (not ranges or len(ranges) > 24 or sum(end - first + 1 for first, end in ranges) > 16_000_000 or
            abs(tick * 1000 / timescale - duration_ms) > 2000):
        raise _SectionUnavailable()
    return header[:pos], ranges


async def _decode_targets(directory, source, targets):
    options = ["-threads", "2", "-max_pixels", "2073600", "-protocol_whitelist", "file", "-format_whitelist", "mov"]
    raw = await _command(["ffprobe", "-v", "error", *options, "-select_streams", "v:0",
                          "-read_intervals", "%+#1800", "-show_entries", "frame=best_effort_timestamp_time",
                          "-of", "json", str(source)], timeout=12, directory=directory)
    frames = json.loads(raw)["frames"]
    pts = [round(float(f["best_effort_timestamp_time"]) * 1000) for f in frames]
    if not pts or pts != sorted(pts):
        raise _SectionUnavailable()
    chosen = []
    for target in targets:
        n = bisect_left(pts, target)
        idx = min((i for i in (n - 1, n) if 0 <= i < len(pts)), key=lambda i: abs(pts[i] - target))
        if abs(pts[idx] - target) > 150:
            raise _SectionUnavailable()
        if not chosen or pts[idx] != pts[chosen[-1]]:
            chosen.append(idx)
    select = "select=" + "+".join(f"eq(n\\,{idx})" for idx in chosen)
    await _command(["ffmpeg", "-nostdin", "-v", "error", *options, "-i", str(source),
                    "-map", "0:v:0", "-vf", select, "-fps_mode", "passthrough", "-frames:v", str(len(chosen)),
                    "-q:v", "3", str(directory / "frame-%d.jpg")], timeout=12, directory=directory)
    return tuple((pts[idx], directory / f"frame-{i}.jpg") for i, idx in enumerate(chosen, 1))


async def _section_frames(directory, video_id, timestamp, before_call):
    if before_call:
        before_call()
    raw = await _command([sys.executable, str(Path(__file__).resolve()), video_id], timeout=18, directory=directory)
    track = json.loads(raw)
    duration = track["duration"]
    if timestamp > duration:
        raise YouTubePreviewUnavailable("timestamp outside video")
    # Signed URLs can have a provider-imposed not-before time. Wait before
    # sending the first byte-range request, as yt-dlp's native downloader does.
    wait = max(0, track["available"] - int(time.time()))
    if wait > 12:
        raise _SectionUnavailable()
    await asyncio.sleep(wait)
    end = max(0, duration - 50)
    targets = sorted({min(end, max(0, timestamp + delta)) for delta in (-4000, -2000, 0, 2000, 4000)})
    url = track["url"]
    header, total = await _range(url, 0, 262143, None, before_call)
    init, spans = _section_index(header, total, duration, targets)
    # Consecutive indexed fragments can be retrieved with one bounded request.
    payload, _ = await _range(url, spans[0][0], spans[-1][1], total, before_call)
    path = directory / "section.mp4"
    await _disk(path.write_bytes, init + payload)
    return await _decode_targets(directory, path, targets)


def _persist_set(cache, requested_ms, frames, kind):
    saved = []
    for ms, path in frames:
        if kind == "decoded":
            if not 0 < path.stat().st_size <= 2_000_000:
                raise _SectionUnavailable()
            with Image.open(path) as image:
                if image.format != "JPEG" or image.width * image.height > 2_073_600:
                    raise _SectionUnavailable()
                image.verify()
            path = cache.save_frame(ms, path, kind=kind)
        saved.append((ms, path))
    cache.save_frame_set(requested_ms, tuple(ms for ms, _ in saved), kind=kind)
    return saved


async def youtube_frames(video_id: str, timestamp_ms: int, cache, *, before_call=None) -> tuple[PreviewFrame, ...]:
    """Use original decoded PTS where possible; mark a storyboard fallback explicitly."""
    if (not isinstance(video_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id)
            or type(timestamp_ms) is not int or not 0 <= timestamp_ms <= MAX_DURATION_MS or cache.directory.name != video_id):
        raise YouTubePreviewUnavailable("invalid video or time")
    if before_call:
        before_call()
    for kind, label in (("decoded", "YouTube video frame"), ("preview", "YouTube preview")):
        saved = await _disk(lambda: cache.frame_set(timestamp_ms, kind=kind))
        if saved:
            return tuple(PreviewFrame(ms, path, label) for ms, path in saved)
    try:
        async with asyncio.timeout(55):
            with TemporaryDirectory(dir=cache.directory, prefix=".video-job-") as tmp:
                pairs = await _section_frames(Path(tmp), video_id, timestamp_ms, before_call)
                if before_call:
                    before_call()
                saved = await _disk(_persist_set, cache, timestamp_ms, pairs, "decoded")
                return tuple(PreviewFrame(ms, path, "YouTube video frame") for ms, path in saved)
    except (_SectionUnavailable, LongAudioUnavailable, PublicMediaTransferError, httpx.HTTPError, TimeoutError,
            struct.error, KeyError, ArithmeticError, IndexError, ValueError):
        pass
    previews = await youtube_preview_frames(video_id, timestamp_ms, cache, before_call=before_call)
    if before_call:
        before_call()
    await _disk(_persist_set, cache, timestamp_ms, [(f.timestamp_ms, f.path) for f in previews], "preview")
    return previews


if __name__ == "__main__":
    try:
        print(json.dumps(_video_track(sys.argv[1])))
    except Exception:
        raise SystemExit(1) from None
