"""Acquire one public TikTok MP4 for local inspection.

Owner: Aigan public media acquisition.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.extractor.tiktok import TikTokIE
from yt_dlp.utils import YoutubeDLError

from public_media_http import (
    PublicMediaTransferError as TikTokUnavailable,
    bounded_chunks as _chunks,
    pinned_get as _get,
)


_VIDEO_PATH = re.compile(r"/@([a-zA-Z0-9_.]{1,24})/video/([0-9]{15,22})/?")
_SHORT_CODE = r"[a-zA-Z0-9]{5,32}/?"
_CDN_HOST = re.compile(
    r"v[0-9]+(?:-[a-z0-9]+)*(?:\.[a-z]{2})?\.tiktok\.com"
    r"|v[0-9]+(?:-[a-z0-9]+)*\.tiktokcdn(?:-us|-eu)?\.com"
)
_STATE_TAG = re.compile(rb"""<script\b[^>]{0,1024}?\bid=["']__UNIVERSAL_DATA_FOR_REHYDRATION__["'][^>]*>""")
_PAGE_LIMIT = 2_000_000
TIKTOK_MAX_BYTES = 20_000_000
_MAX_DURATION = 180
_DEADLINE = 30


def _https_parts(url):
    if not isinstance(url, str) or not 0 < len(url) <= 8192:
        return None
    if not url.isascii() or not url.isprintable() or " " in url or "\\" in url:
        return None
    try:
        parts = urlsplit(url)
        # Exact authority excludes credentials, explicit ports and bracketed literals.
        if parts.scheme == "https" and parts.hostname and parts.netloc.lower() == parts.hostname:
            return parts
    except ValueError:
        pass
    return None


def normalize_tiktok_video_url(raw: str) -> str | None:
    """Classify a single public post/share URL without performing network I/O."""
    parts = _https_parts(raw)
    if not parts:
        return None
    host, path = parts.hostname, parts.path
    if host in ("www.tiktok.com", "tiktok.com"):
        if _VIDEO_PATH.fullmatch(path) or re.fullmatch("/t/" + _SHORT_CODE, path):
            return "https://www.tiktok.com" + path.rstrip("/")
    if host in ("vm.tiktok.com", "vt.tiktok.com") and re.fullmatch("/" + _SHORT_CODE, path):
        return "https://" + host + path.rstrip("/") + "/"
    return None


def _cdn_url(raw):
    parts = _https_parts(raw)
    if parts and _CDN_HOST.fullmatch(parts.hostname) and parts.path.startswith("/") and not parts.fragment:
        return raw
    return None


def _post_cookie(headers, canonical):
    # The pinned response uses a numeric request URL. Apply cookie semantics to
    # the authenticated page hostname, and accept only this public session token.
    values = [h for h in headers.get_list("set-cookie") if h.partition("=")[0].strip() == "tt_chain_token"]
    if len(values) != 1 or len(values[0]) > 4096:
        return None
    jar = httpx.Cookies()
    jar.extract_cookies(httpx.Response(200, headers={"set-cookie": values[0]},
                                      request=httpx.Request("GET", canonical)))
    for cookie in jar.jar:
        if (cookie.name == "tt_chain_token" and cookie.secure and cookie.domain_specified
                and cookie.domain.lstrip(".") == "tiktok.com"
                and re.fullmatch(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,2048}", cookie.value or "")):
            scoped = httpx.Cookies()
            scoped.jar.set_cookie(cookie)
            return scoped
    return None


@asynccontextmanager
async def _open(url, normalize, *, referrer=None, cookies=None):
    for _ in range(4):
        url = normalize(url)
        if not url:
            raise TikTokUnavailable("url")
        cookie_header = None
        if cookies and _cdn_url(url) and urlsplit(url).hostname.endswith(".tiktok.com"):
            target = httpx.Request("GET", url)
            cookies.set_cookie_header(target)
            cookie_header = target.headers.get("cookie")
        request = _get(url, referrer, cookie_header=cookie_header) if cookie_header else _get(url, referrer)
        async with request as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get("location")
                if not target:
                    raise TikTokUnavailable("redirect")
                url = urljoin(url, target)
                continue
            if response.status_code != 200:
                raise TikTokUnavailable("unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise TikTokUnavailable("encoding")
            yield url, response
            return
    raise TikTokUnavailable("redirect")


def _download_address(page, post_id, max_bytes, max_duration):
    tag = _STATE_TAG.search(page)
    end = page.find(b"</script>", tag.end()) if tag else -1
    if end < 0:
        raise TikTokUnavailable("unavailable")
    detail = json.loads(page[tag.end():end])["__DEFAULT_SCOPE__"]["webapp.video-detail"]
    item = detail["itemInfo"]["itemStruct"]
    if detail.get("statusCode") != 0 or item["id"] != post_id or item.get("privateItem") or item.get("secret"):
        raise TikTokUnavailable("unavailable")
    video = item["video"]
    if not 0 < int(video["duration"]) <= max_duration or int(video.get("size") or 0) > max_bytes:
        raise TikTokUnavailable("limits")
    for key in ("video_control", "videoControl"):
        control = item.get(key, {})
        if not isinstance(control, dict) or any(control.get(flag, True) is not True
                                               for flag in ("allow_download", "allowDownload")):
            raise TikTokUnavailable("unavailable")
    if video.get("bitrateInfo"):
        # Only parse the formats in this exact post. yt-dlp must not fetch pages,
        # handle challenges, select an external downloader or open media URLs.
        try:
            with YoutubeDL({"quiet": True, "cachedir": False}, auto_init=False) as ydl:
                formats = TikTokIE(ydl)._extract_web_formats(item)
        except (YoutubeDLError, ValueError, TypeError, LookupError, AttributeError, ArithmeticError, RecursionError):
            # Malformed optional renditions do not invalidate a verified post's direct URL.
            formats = []
        candidates = [f for f in formats if f.get("ext") == "mp4" and f.get("vcodec") in ("h264", "h265")
                      and 0 < (f.get("filesize") or 0) <= max_bytes and _cdn_url(f.get("url"))]
        if candidates:
            best = max(candidates, key=lambda f: (f.get("quality", -1), -f["filesize"]))
            return best["url"]
    # Older pages advertise only a download or playback address.
    media = _cdn_url(video.get("downloadAddr") or video.get("playAddr"))
    if not media:
        raise TikTokUnavailable("unavailable")
    return media


async def fetch_tiktok_video(url: str, destination: Path, *, max_bytes: int, max_duration: int) -> None:
    """Write a bounded standalone MP4 to a fresh path; remove partial files on failure."""
    limit = min(max_bytes, TIKTOK_MAX_BYTES)
    created = completed = False
    try:
        async with asyncio.timeout(_DEADLINE):
            async with _open(url, normalize_tiktok_video_url) as (canonical, response):
                post = _VIDEO_PATH.fullmatch(urlsplit(canonical).path)
                original = _VIDEO_PATH.fullmatch(urlsplit(url).path)
                if not post or (original and original[2] != post[2]):
                    raise TikTokUnavailable("post")
                if response.headers.get("content-type", "").partition(";")[0].lower() != "text/html":
                    raise TikTokUnavailable("page")
                page = bytearray()
                async for block in _chunks(response, _PAGE_LIMIT):
                    page.extend(block)
                media = await asyncio.to_thread(_download_address, page, post[2], limit, min(max_duration, _MAX_DURATION))
                cookies = _post_cookie(response.headers, canonical)
            async with _open(media, _cdn_url, referrer=canonical, cookies=cookies) as (_, response):
                mime = response.headers.get("content-type", "").partition(";")[0].lower()
                if mime not in ("video/mp4", "application/octet-stream"):
                    raise TikTokUnavailable("format")
                with destination.open("xb") as out:
                    created = True
                    async for block in _chunks(response, limit):
                        if out.tell() == 0 and block[4:8] != b"ftyp":
                            raise TikTokUnavailable("format")
                        out.write(block)
        completed = True
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise TikTokUnavailable("unavailable") from None
    finally:
        if created and not completed:
            destination.unlink(missing_ok=True)
