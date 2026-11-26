"""Read original YouTube caption tracks and their timeline from one ordinary listing."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import re
import time
from urllib.parse import urlsplit

from requests import Session
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi

from youtube_caption_source import classify_cues


MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_LOOKUP_BYTES = 32 * 1024 * 1024


class CaptionTrackUnavailable(Exception):
    """The public source could not establish whether usable captions exist."""


@dataclass(frozen=True)
class OriginalCaptionTrack:
    language_code: str
    is_generated: bool
    duration_ms: int | None
    cues: tuple[tuple[int, int, str], ...]
    all_tracks_read: bool = True


class _CaptionSession(Session):
    """Read the duration from the player response already used by the caption library."""

    def __init__(self, video_id):
        super().__init__()
        self.trust_env = False
        self.video_id = video_id
        self.duration_ms = None
        self.deadline = time.monotonic() + 55
        self.remaining_bytes = MAX_LOOKUP_BYTES
        self.hooks["response"].append(self._read_response)

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        host = url.hostname or ""
        if (url.scheme != "https" or url.netloc != host or
                not (host == "youtube.com" or host.endswith(".youtube.com") or host == "youtubei.googleapis.com")):
            raise CaptionTrackUnavailable("unsupported caption endpoint")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.remaining_bytes <= 0:
            raise CaptionTrackUnavailable("caption lookup timed out")
        kwargs["stream"] = True
        kwargs["timeout"] = (min(4, remaining), min(12, remaining))
        return super().send(request, **kwargs)

    def _read_response(self, response, **_):
        # Bound decoded response bytes before the library parses HTML, XML or JSON.
        try:
            output = BytesIO()
            for block in response.iter_content(65536):
                self.remaining_bytes -= len(block)
                if (self.remaining_bytes < 0 or output.tell() + len(block) > MAX_RESPONSE_BYTES
                        or time.monotonic() >= self.deadline):
                    raise CaptionTrackUnavailable("caption response exceeds lookup limits")
                output.write(block)
            # Requests reads .text/.json from this buffer once the stream is consumed.
            response._content = output.getvalue()
        finally:
            response.close()
        self._capture_duration(response)

    def _capture_duration(self, response, **_):
        player = urlsplit(response.url)
        if (response.status_code != 200 or player.hostname not in {"youtube.com", "www.youtube.com", "youtubei.googleapis.com"}
                or player.path != "/youtubei/v1/player"):
            return
        try:
            details = response.json()["videoDetails"]
            seconds = int(details["lengthSeconds"])
            if details["videoId"] == self.video_id and seconds > 0:
                self.duration_ms = seconds * 1000
        except (KeyError, TypeError, ValueError):
            pass


def _language_rank(track, preferences):
    code = track.language_code.casefold()
    for i, preferred in enumerate(preferences):
        if preferred.split("-")[0] == code.split("-")[0]:
            return i, code != preferred, track.is_generated
    return len(preferences), False, track.is_generated


def _cue_rows(entries, duration_ms):
    cues = []
    for entry in entries:
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        start = float(entry["start"])
        length = float(entry["duration"])
        if not math.isfinite(start) or not math.isfinite(length) or start < 0 or length <= 0:
            raise CaptionTrackUnavailable("invalid caption timeline")
        begin, end = round(start * 1000), round((start + length) * 1000)
        if duration_ms is not None and duration_ms < end <= duration_ms + 5000 and begin < duration_ms:
            end = duration_ms  # The player duration is reported in whole seconds.
        if end <= begin or (duration_ms is not None and end > duration_ms + 5000):
            raise CaptionTrackUnavailable("invalid caption timeline")
        cues.append((begin, end, " ".join(text.split())))
    return tuple(sorted(cues, key=lambda cue: cue[0]))


def window_coverage(cues, left, right):
    """Captioned milliseconds inside the window, taking the union of overlapping cues."""
    covered, cursor = 0, left
    for start, end, _ in cues:
        if start >= right:
            break
        stop = min(end, right)
        if stop > cursor:
            covered += stop - max(cursor, start)
            cursor = stop
    return covered


def lookup_original_track(video_id: str, languages=("uk", "en", "ru"), *, window_ms=None) -> OriginalCaptionTrack | None:
    """Return a readable original track; None means the platform confirmed there are none."""
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("canonical YouTube ID required")
    preferences = [lang.strip().casefold() for lang in languages if lang.strip()]
    with _CaptionSession(video_id) as session:
        try:
            # This iterable contains manual and automatic originals. Translation
            # targets are separate metadata and never become source tracks.
            tracks = list(YouTubeTranscriptApi(http_client=session).list(video_id))
        except (TranscriptsDisabled, NoTranscriptFound):
            return None
        except Exception as exc:
            raise CaptionTrackUnavailable("caption listing unavailable") from exc
        if not tracks:
            return None
        duration = session.duration_ms
        best, best_score = None, None
        checked = True
        ordered = sorted(tracks, key=lambda track: _language_rank(track, preferences))
        for index, track in enumerate(ordered):
            if time.monotonic() >= session.deadline:
                checked = False
                break
            try:
                cues = _cue_rows(track.fetch().to_raw_data(), duration)
            except Exception:
                checked = False
                continue
            if not cues:
                checked = False
                continue
            if window_ms is not None:
                left, right = window_ms
                width = window_coverage(cues, left, right)
                if width < right - left:
                    if best_score is None or width > best_score:
                        best, best_score = (track, cues), width
                    continue
            status, covered = classify_cues(cues, duration) if duration else ("unavailable", 0)
            if window_ms is not None or status == "complete":
                return OriginalCaptionTrack(track.language_code, track.is_generated, duration, cues,
                                            checked and index + 1 == len(ordered))
            span = max(row[1] for row in cues) - cues[0][0]
            # A verified prefix can be supplemented from its exact end. When
            # duration is unknown the widest observed span supplies the most evidence.
            tier, width = (1, covered) if status == "partial" else (0, span)
            if (best_score is None or tier > best_score[0] or
                    (tier == best_score[0] and width > best_score[1] + max(2000, best_score[1] * .01))):
                best = (track, cues)
                best_score = (tier, width)
        if best is None:
            raise CaptionTrackUnavailable("listed caption tracks could not be read")
        track, cues = best
        return OriginalCaptionTrack(track.language_code, track.is_generated, duration, cues, checked)
