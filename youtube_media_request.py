"""Select an explicit whole-transcript or visual timestamp request for one YouTube post."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlsplit

from provenance import canonical_source


@dataclass(frozen=True)
class YoutubeMediaRequest:
    video_id: str
    transcript: bool
    timestamp_ms: int | None


_CLOCK = re.compile(r"(?<![\d:])(?:(\d{1,2}):([0-5]\d):([0-5]\d)|(\d{1,3}):([0-5]\d))(?![\d:])")
_TRANSCRIBE = re.compile(r"\b(?:транскрибуй|транскрибируй|транскрибировать|розшифруй|расшифруй|transcribe)\b", re.I)
_TRANSCRIPT = re.compile(r"\b(?:транскрип[тц]\w*|розшифровк\w*|расшифровк\w*|transcript(?:ion)?)\b", re.I)
_WHOLE = re.compile(r"\b(?:повн\w*|полн\w*|ц[еі]л[іи]\w*|увесь|весь|всю|ціле|целиком|полностью|full|entire|whole|complete)\b", re.I)
_VISUAL = re.compile(r"\b(?:кадр\w*|екран\w*|экран\w*|показ\w*|покаж\w*|происход\w*|відбува\w*|happen\w*|подив\w*|посмотр\w*|видно|бач\w*|зображ\w*|frames?|shown|showing|screen|look|see|visible)\b", re.I)
_NO_TRANSCRIPT = re.compile(r"\b(?:(?:не|без)\s+(?:(?:треба|нужно|потрібно)\s+)?(?:транскри\w+|р[оа][зс]шифр\w+)|(?:without|do\s+not|don't|no|skip)\s+(?:a\s+|the\s+)?(?:full\s+)?transcri\w+)", re.I)
_NON_REQUEST = re.compile(r"\b(?:how (?:much|long)|how to transcribe|скільки (?:коштує|часу)|сколько (?:стоит|времени)|(?:як|как) (?:транскрибу|транскрибиро)|what is a transcript)\b", re.I)
_DECLINE = re.compile(r"\b(?:не|без|don't|without|do not|skip|no)\s+(?:[\w'-]+\s+){0,3}(?:транскри\w+|розшифр\w+|расшифр\w+|transcri\w+)", re.I)
_NO_VISUAL = re.compile(r"\b(?:(?:без|не)\s+(?:кадр\w*|смотри|дивись)|(?:without|skip|no)\s+(?:the\s+)?frames?)\b", re.I)


def parse_youtube_media_request(url: str, question: str) -> YoutubeMediaRequest | None:
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc.lower() != parts.hostname or len(url) > 2048:
            return None
    except ValueError:
        return None
    video_id = canonical_source("get_youtube_transcript", {"video": url})
    if not video_id or not question.strip():
        return None
    marks = list(_CLOCK.finditer(question))
    if len(marks) > 1:
        return None
    whole = bool(_WHOLE.search(question))
    transcript = bool((_TRANSCRIBE.search(question) and (whole or not marks))
                      or (whole and _TRANSCRIPT.search(question))) and not (_NO_TRANSCRIPT.search(question) or _DECLINE.search(question) or _NON_REQUEST.search(question))
    point = None
    if _VISUAL.search(question) and not _NO_VISUAL.search(question):
        if marks:
            hour, minute, second, short_min, short_sec = marks[0].groups()
            point = (3600 * int(hour) + 60 * int(minute) + int(second)) if hour else 60 * int(short_min) + int(short_sec)
        else:
            value = parse_qs(urlsplit(url).query).get("t", [""])[0]
            if re.fullmatch(r"[0-9]{1,5}s?", value):
                point = int(value.rstrip("s"))
    if not transcript and point is None:
        return None
    return YoutubeMediaRequest(video_id, transcript, None if point is None else point * 1000)
