"""Public caption coverage for an explicit request for the entire YouTube transcript."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from youtube_long_audio import LongAudioUnavailable, MAX_DURATION_MS, _command, _outbound


@dataclass(frozen=True)
class CaptionEvidence:
    status: str
    cues: tuple[tuple[int, int, str], ...] = ()
    duration_ms: int = 0
    covered_ms: int = 0
    track_id: str = ""
    language: str = ""


def classify_cues(cues, duration_ms):
    """Measure the last captioned instant. Interior gaps can be ordinary silence."""
    if type(duration_ms) is not int or not 0 < duration_ms <= MAX_DURATION_MS:
        return "unavailable", 0
    if not cues:
        return "absent", 0
    first = None
    previous = -1
    last = 0
    for start, end, text in cues:
        if (type(start) is not int or type(end) is not int or not 0 <= start < end or start < previous or
                not isinstance(text, str) or not text.strip()):
            return "unavailable", 0
        # A last cue commonly continues slightly beyond a whole-second player
        # duration. A distant outlier cannot establish end-of-recording coverage.
        if start >= duration_ms or end > duration_ms + 10000:
            return "unavailable", 0
        first = start if first is None else first
        previous = start
        last = max(last, end)
    # A suffix that starts far into a recording is not a verified prefix. It
    # may be silence or missing dialogue; neither condition authorizes billing.
    if first > max(30000, min(120000, duration_ms // 10)):
        return "unavailable", 0
    covered = min(last, duration_ms)
    margin = min(5000, duration_ms // 20)
    return ("complete" if duration_ms - covered <= margin else "partial"), covered


def _probe(video_id):
    from youtube_caption_tracks import CaptionTrackUnavailable, lookup_original_track

    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
        return CaptionEvidence("unavailable")
    try:
        track = lookup_original_track(video_id)
    except CaptionTrackUnavailable:
        return CaptionEvidence("unavailable")
    if track is None:
        return CaptionEvidence("absent")
    status, covered = classify_cues(track.cues, track.duration_ms)
    if status not in ("complete", "partial"):
        return CaptionEvidence(status)
    if status == "partial" and not track.all_tracks_read:
        status = "partial_unverified"
    language = track.language_code
    track_id = f"{language}:{'asr' if track.is_generated else 'manual'}"
    cues = tuple((start, min(end, track.duration_ms), text) for start, end, text in track.cues)
    return CaptionEvidence(status, cues, track.duration_ms, covered, track_id, language)


async def fetch_caption_evidence(video_id: str, *, before_call) -> CaptionEvidence:
    """Distinguish absent or partial captions from a failed public lookup."""
    if not isinstance(video_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
        return CaptionEvidence("unavailable")
    before_call()
    try:
        with TemporaryDirectory(prefix="aigan-caption-") as directory:
            raw = await _command([sys.executable, str(Path(__file__).resolve()), video_id],
                                 timeout=60, directory=Path(directory))
        fields = json.loads(raw)
        fields["cues"] = tuple(tuple(cue) for cue in fields["cues"])
        evidence = CaptionEvidence(**fields)
    except (LongAudioUnavailable, TimeoutError, ValueError, KeyError, TypeError):
        evidence = CaptionEvidence("unavailable")
    before_call()
    return evidence


if __name__ == "__main__":
    sys.addaudithook(_outbound)
    try:
        print(json.dumps(asdict(_probe(sys.argv[1])), ensure_ascii=True, separators=(",", ":")))
    except Exception:
        raise SystemExit(1) from None
