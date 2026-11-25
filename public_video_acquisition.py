"""Acquire one publicly served MP4 or the video selected by a public HTML player."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import httpx

from mcp_servers.web import _is_blocked_russian_host
from media_acquisition import host_matches_domain, platform_from_url
from public_media_http import PublicMediaTransferError, bounded_chunks, pinned_get


PUBLIC_VIDEO_MAX_BYTES = 20_000_000
_PAGE_BYTES = 512_000
_DEADLINE = 30
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b", re.I)
_EXCLUDED_DOMAINS = (
    "localhost", "local", "internal", "home.arpa", "onion",
    "youtube-nocookie.com", "googlevideo.com", "ytimg.com", "tiktokcdn-us.com",
    "tiktokcdn-eu.com", "cdninstagram.com", "twitter.com", "x.com", "twimg.com",
)


class PublicVideoUnavailable(Exception):
    """A fixed acquisition failure category, safe for routing to a user message."""


def normalize_public_video_url(raw: str) -> str | None:
    """Admit HTTP(S) on standard ports, without DNS or any other network I/O."""
    if not isinstance(raw, str) or not 1 <= len(raw) <= 8192 or not raw.isprintable():
        return None
    if any(character in raw for character in (" ", "\\")):
        return None
    try:
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
            return None
        if parts.port not in (None, 443 if parts.scheme == "https" else 80):
            return None
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not _HOST.fullmatch(host) or len(host) > 253 or _is_blocked_russian_host(host):
            return None
        if any(host_matches_domain(host, domain) for domain in _EXCLUDED_DOMAINS):
            return None
        normalized = parts._replace(netloc=host, fragment="").geturl()
        return normalized if platform_from_url(normalized) == "other" else None
    except (UnicodeError, ValueError):
        return None


@asynccontextmanager
async def _open(url, before_call):
    for _ in range(4):
        url = normalize_public_video_url(url)
        if not url:
            raise PublicVideoUnavailable("url")
        before_call()
        async with pinned_get(url) as response:
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise PublicVideoUnavailable("unavailable")
                url = _join(url, location)
                continue
            if status in (401, 403, 407, 429, 451) or response.headers.get("cf-mitigated") == "challenge":
                raise PublicVideoUnavailable("denied")
            if status == 404:
                raise PublicVideoUnavailable("not_found")
            if status == 410:
                raise PublicVideoUnavailable("gone")
            if status != 200:
                raise PublicVideoUnavailable("unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise PublicVideoUnavailable("unsupported")
            yield url, response
            return
    raise PublicVideoUnavailable("unavailable")


def _join(base, target):
    # urljoin trims controls and accepts malformed authorities on some Python versions.
    if not target or not target.isprintable() or target != target.strip() or "\\" in target:
        raise PublicVideoUnavailable("url")
    try:
        return urljoin(base, target)
    except ValueError:
        raise PublicVideoUnavailable("url") from None


def _static_source(page: bytes, page_url: str) -> str | None:
    soup = BeautifulSoup(page, "html.parser")
    for inactive in soup.find_all(["template", "noscript", "textarea", "iframe", "svg"]):
        inactive.decompose()
    for hidden in soup.select("[hidden], [aria-hidden=true]"):
        hidden.decompose()
    for hidden in soup.find_all(style=_HIDDEN_STYLE):
        hidden.decompose()
    players = soup.find_all("video", limit=2)
    if len(players) > 1:
        raise PublicVideoUnavailable("ambiguous")
    if not players:
        return None
    player = players[0]
    if "nodownload" in str(player.get("controlslist", "")).lower().split():
        raise PublicVideoUnavailable("denied")
    base = soup.find("base", href=True)
    base_url = _join(page_url, base["href"]) if base else page_url
    source = player.get("src")
    if not source:
        candidates = player.find_all("source", src=True, recursive=False)
        untyped = None
        for candidate in candidates:
            mime = candidate.get("type", "").partition(";")[0].strip().lower()
            if mime == "video/mp4" or (not mime and urlsplit(candidate["src"]).path.lower().endswith(".mp4")):
                source = candidate["src"]
                break
            if not mime and untyped is None:
                untyped = candidate["src"]
        source = source or untyped
        if candidates and not source:
            # A browser can negotiate playback formats. This adapter accepts MP4 only.
            raise PublicVideoUnavailable("unsupported")
    return _join(base_url, source) if source else None


def _mime(response):
    return response.headers.get("content-type", "").partition(";")[0].strip().lower()


async def _write_mp4(response, destination, limit, cleanup):
    if _mime(response) not in ("video/mp4", "application/octet-stream"):
        raise PublicVideoUnavailable("unsupported")
    with destination.open("xb") as output:
        cleanup.callback(destination.unlink, missing_ok=True)
        async for block in bounded_chunks(response, limit):
            if output.tell() == 0 and block[4:8] != b"ftyp":
                raise PublicVideoUnavailable("unsupported")
            output.write(block)


async def _acquire(url, destination, limit, before_call, cleanup):
    async with _open(url, before_call) as (page_url, response):
        if _mime(response) != "text/html":
            await _write_mp4(response, destination, limit, cleanup)
            return
        if any(urlsplit(value).path.lower().endswith(".mp4") for value in (url, page_url)):
            # A soft error or sign-in page at a file URL can advertise an unrelated clip.
            raise PublicVideoUnavailable("unsupported")
        page = bytearray()
        async for block in bounded_chunks(response, _PAGE_BYTES):
            page.extend(block)
    source = await asyncio.to_thread(_static_source, bytes(page), page_url)
    if not source:
        raise PublicVideoUnavailable("no_player")
    async with _open(source, before_call) as (_, response):
        await _write_mp4(response, destination, limit, cleanup)


async def fetch_public_video(url: str, destination: Path, *, max_bytes: int, max_duration: int,
                             before_call=lambda: None) -> None:
    """Write one bounded local MP4. The caller's frame session enforces actual duration before inference."""
    limit = min(max_bytes, PUBLIC_VIDEO_MAX_BYTES)
    if limit <= 0 or max_duration <= 0:
        raise PublicVideoUnavailable("limits")
    try:
        with ExitStack() as cleanup:
            async with asyncio.timeout(_DEADLINE):
                await _acquire(url, destination, limit, before_call, cleanup)
                before_call()
            cleanup.pop_all()
    except PublicMediaTransferError as exc:
        raise PublicVideoUnavailable("limits" if str(exc) == "size" else "url") from None
    except (TimeoutError, httpx.TimeoutException):
        raise PublicVideoUnavailable("timeout") from None
    except (httpx.HTTPError, OSError, ValueError):
        raise PublicVideoUnavailable("unavailable") from None
