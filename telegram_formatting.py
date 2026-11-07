"""Telegram answer markup and HTTP source links."""

import html
import re
from collections.abc import Iterator
from urllib.parse import urlsplit


_BASIC_TAG = r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote)>"
_ENTITY = r"&(?:amp|lt|gt|quot|#[0-9]+|#[xX][0-9a-fA-F]+);"
_BASIC_TOKEN = re.compile(rf"(?P<tag>{_BASIC_TAG})|(?P<entity>{_ENTITY})", re.IGNORECASE)
_OPEN_LINK = r"""<a\s+href\s*=\s*(?:"(?P<double>[^"<>]*)"|'(?P<single>[^'<>]*)')\s*>"""
_LINK = re.compile(
    _OPEN_LINK +
    r"(?P<label>(?:(?!<a\b|</a\s*>).)*)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+)$")


def _web_url(value: str) -> str | None:
    # Decode complete entities only. An ordinary '&not=' query parameter is literal.
    value = re.sub(_ENTITY, lambda match: html.unescape(match[0]), value)
    if re.search(r"""[\s\x00-\x1f\x7f<>"\\]""", value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"http", "https"} and url.hostname:
            return value
    except ValueError:
        pass
    return None


def _segments(text: str) -> Iterator[tuple[str, str | None]]:
    if "**" in text:
        # Emphasis can span a citation. Protect literal stars in its destination.
        text = re.sub(_OPEN_LINK, lambda m: m[0].replace("*", "&#42;"), text, flags=re.IGNORECASE)
    text = _HEADING.sub(lambda m: f"<b>{m[1].strip()}</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m[1]}</b>", text).replace("**", "")
    offset = 0
    for match in _LINK.finditer(text):
        yield text[offset:match.start()], None
        href = match["double"] if match["double"] is not None else match["single"]
        yield match["label"], _web_url(href)
        offset = match.end()
    yield text[offset:], None


def _basic_markup(text: str, *, plain: bool) -> str:
    chunks = []
    offset = 0
    for match in _BASIC_TOKEN.finditer(text):
        literal = text[offset:match.start()]
        chunks.append(literal if plain else html.escape(literal, quote=False))
        if match["tag"] and not plain:
            chunks.append(match[0].lower())
        elif match["entity"]:
            literal = html.unescape(match[0])
            chunks.append(literal if plain else html.escape(literal, quote=False))
        offset = match.end()
    chunks.append(text[offset:] if plain else html.escape(text[offset:], quote=False))
    return "".join(chunks)


def _anchor(label: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def render_telegram_html(text: str) -> str:
    chunks = []
    for segment, url in _segments(text):
        label = _basic_markup(segment, plain=False)
        chunks.append(_anchor(label, url) if url and label else label)
    return "".join(chunks)


def render_plain_fallback(text: str) -> str:
    labels = []
    chunks = []
    for segment, url in _segments(text):
        label = _basic_markup(segment, plain=True)
        labels.append(label)
        chunks.append(f"{label} ({url})" if url and url != label else label)
    expanded = "".join(chunks)
    # A URL excluded from Telegram's HTML length limit becomes visible in plain text.
    return expanded if len(expanded) <= 4096 else "".join(labels)


def split_linked_reply(
    text: str, *, limit: int, max_chunks: int, max_total_chars: int,
) -> list[str] | None:
    """Wrap link labels at visible text boundaries, preserving each fragment's URL."""
    if not _LINK.search(text):
        return None
    text = text.strip()
    segments = list(_segments(text))
    visible = "".join(label for label, _ in segments)
    if len(visible) <= min(limit, max_total_chars):
        return [text]

    spans = []
    offset = 0
    for label, url in segments:
        spans.append((offset, offset + len(label), url))
        offset += len(label)

    def markup(start: int, end: int) -> str:
        chunks = []
        for left, right, url in spans:
            if right <= start or end <= left:
                continue
            fragment = visible[max(start, left):min(end, right)]
            chunks.append(_anchor(fragment, url) if url else fragment)
        return "".join(chunks)

    end = min(len(visible), max_total_chars)
    marker = "\n\n[...] скорочено"
    needs_prefix = max_chunks > 1 and end + (len(marker) if end < len(visible) else 0) > limit
    prefix_width = len(f"{max_chunks}/{max_chunks}\n") if needs_prefix else 0
    width = limit - prefix_width
    chunks = []
    start = 0
    while start < end and len(chunks) < max_chunks:
        last_slot = len(chunks) + 1 == max_chunks
        shortened = end < len(visible) or (last_slot and start + width < end)
        final = last_slot or end - start + (len(marker) if shortened else 0) <= width
        capacity = max(1, width - (len(marker) if final and shortened else 0))
        cut = min(start + capacity, end)
        if cut < end and not final:
            for separator in ("\n\n", "\n", " "):
                boundary = visible.rfind(separator, start + capacity // 2, cut + 1)
                if boundary > start:
                    cut = boundary
                    break
        chunks.append(markup(start, cut).rstrip() + (marker if final and shortened else ""))
        if final:
            break
        start = cut
        while start < end and visible[start].isspace():
            start += 1
    if len(chunks) > 1:
        chunks = [f"{i}/{len(chunks)}\n{chunk}" for i, chunk in enumerate(chunks, start=1)]
    return chunks
