"""Durable chat-scoped evidence from an explicitly requested YouTube video."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


class CacheUnavailable(OSError):
    """The requested evidence cannot be safely read or stored."""


class CacheBusy(CacheUnavailable):
    """Another job is already processing this chat and video."""


def _stamp(ms):
    seconds = ms // 1000
    value = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"
    return f"{value}.{ms % 1000:03}" if ms % 1000 else value


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


@contextmanager
def _atomic_file(path):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".write-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


def _store_blob(source, dest):
    size = 0
    digest = hashlib.sha256()
    with Path(source).open("rb") as reader, _atomic_file(dest) as writer:
        while block := reader.read(1024 * 1024):
            digest.update(block)
            writer.write(block)
            size += len(block)
        if not size:
            raise CacheUnavailable("empty media")
    return {"size": size, "sha256": digest.hexdigest()}


class _VideoCache:
    def __init__(self, directory):
        self.directory = directory
        self.manifest = directory / "manifest.json"
        if self.manifest.is_symlink():
            raise CacheUnavailable("invalid manifest path")
        try:
            self.state = json.loads(self.manifest.read_text(encoding="utf-8"))
            if not isinstance(self.state, dict) or self.state.get("version") != 1:
                raise CacheUnavailable("unsupported media manifest")
        except FileNotFoundError:
            self.state = {"version": 1}
        except (UnicodeError, ValueError) as exc:
            raise CacheUnavailable("invalid media manifest") from exc
        self._verified = {}

    def _publish(self, **changes):
        state = {**self.state, **changes}
        with _atomic_file(self.manifest) as out:
            out.write(json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        self.state = state

    def _verified_blob(self, path, record):
        if not isinstance(record, dict):
            raise CacheUnavailable("invalid media manifest")
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode):
            raise CacheUnavailable("invalid media path")
        fingerprint = (meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)
        if meta.st_size != record.get("size") or meta.st_size <= 0:
            raise CacheUnavailable("incomplete stored media")
        if self._verified.get(path) != fingerprint:
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() != record.get("sha256"):
                    raise CacheUnavailable("stored media checksum mismatch")
            self._verified[path] = fingerprint
        return path

    def audio(self) -> tuple[Path, int] | None:
        record = self.state.get("audio")
        if record is None:
            return None
        if not isinstance(record, dict) or not _integer(record.get("duration_ms"), 1):
            raise CacheUnavailable("invalid audio manifest")
        return self._verified_blob(self.directory / "audio.media", record), record["duration_ms"]

    def save_audio(self, source: Path, duration_ms: int) -> Path:
        if not _integer(duration_ms, 1):
            raise ValueError("positive audio duration required")
        existing = self.audio()
        if existing is not None:
            # One immutable track owns the transcript timeline for this entry.
            if existing[1] != duration_ms:
                raise CacheUnavailable("cached track has a different duration")
            with Path(source).open("rb") as reader:
                if hashlib.file_digest(reader, "sha256").hexdigest() != self.state["audio"]["sha256"]:
                    raise CacheUnavailable("cached track has different content")
            return existing[0]
        path = self.directory / "audio.media"
        record = _store_blob(source, path)
        self._publish(audio={**record, "duration_ms": duration_ms})
        return path

    def _speech_ranges(self):
        audio = self.state.get("audio")
        duration = audio.get("duration_ms") if isinstance(audio, dict) else None
        entries = []
        for key in ("segments", "supplemental_speech"):
            raw = self.state.get(key, [])
            if not isinstance(raw, list):
                raise CacheUnavailable("invalid speech manifest")
            for row in raw:
                if not isinstance(row, list) or len(row) != 3:
                    raise CacheUnavailable("invalid speech segment")
                start, end, text = row
                if not (_integer(duration, 1) and _integer(start) and _integer(end)
                        and start < end <= duration and isinstance(text, str)):
                    raise CacheUnavailable("invalid speech coverage")
                entries.append((start, end, text))
        entries.sort()
        if any(a[1] > b[0] for a, b in zip(entries, entries[1:])):
            raise CacheUnavailable("overlapping speech segments")
        return entries

    def segments(self) -> list[tuple[int, int, str]]:
        """Return the contiguous independent ASR transcript starting at zero."""
        result = []
        cursor = 0
        for row in self._speech_ranges():
            if row[0] != cursor:
                break
            result.append(row)
            cursor = row[1]
        return result

    def _public_track(self):
        path = self.directory / "captions.json"
        if path.is_symlink():
            raise CacheUnavailable("invalid captions path")
        try:
            track = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, UnicodeError) as exc:
            raise CacheUnavailable("invalid captions record") from exc
        self._validate_track(track)
        return track

    @staticmethod
    def _validate_track(track):
        if not isinstance(track, dict):
            raise CacheUnavailable("invalid captions record")
        duration, covered = track.get("duration_ms"), track.get("covered_ms")
        if not (_integer(duration, 1) and _integer(covered, 1) and covered <= duration
                and type(track.get("complete")) is bool and isinstance(track.get("track_id"), str)
                and isinstance(track.get("cues"), list) and track["cues"]):
            raise CacheUnavailable("invalid captions coverage")
        previous = -1
        for row in track["cues"]:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                raise CacheUnavailable("invalid caption cue")
            start, end, text = row
            if not (_integer(start) and _integer(end) and previous <= start < end <= covered
                    and isinstance(text, str) and text.strip()):
                raise CacheUnavailable("invalid caption cue")
            previous = start

    def save_public_transcript(self, cues, *, duration_ms: int, covered_ms: int, complete: bool, track_id: str):
        if not isinstance(track_id, str) or not track_id.strip():
            raise ValueError("native track identity required")
        track = {"track_id": hashlib.sha256(track_id.encode("utf-8")).hexdigest(),
                 "duration_ms": duration_ms, "covered_ms": covered_ms, "complete": complete,
                 "cues": [list(cue) for cue in cues]}
        self._validate_track(track)
        old = self._public_track()
        if old == track:
            return
        # Keep the exact prefix which owns a partially transcribed tail. A complete
        # native track always wins and never rewrites previously paid speech evidence.
        if old and not complete and (old["complete"] or self.state.get("native_prefix_pinned")):
            return
        with _atomic_file(self.directory / "captions.json") as out:
            out.write(json.dumps(track, ensure_ascii=False).encode("utf-8"))

    def _prefix(self, track, duration):
        if not track:
            return 0
        if abs(track["duration_ms"] - duration) > 2500:
            raise CacheUnavailable("native captions and audio timelines disagree")
        return min(track["covered_ms"], duration)

    def next_speech_range(self, max_ms, prefer_native):
        audio = self.state.get("audio") or {}
        duration = audio.get("duration_ms")
        if not _integer(max_ms, 1) or not _integer(duration, 1):
            raise ValueError("known audio timeline required")
        cursor = self._prefix(self._public_track(), duration) if prefer_native else 0
        for start, end, _ in self._speech_ranges():
            if start > cursor:
                return cursor, min(start, cursor + max_ms)
            cursor = max(cursor, end)
        return (cursor, min(duration, cursor + max_ms)) if cursor < duration else None

    def append_segment(self, start_ms: int, end_ms: int, text: str, *, prefer_native=False) -> None:
        if not (_integer(start_ms) and _integer(end_ms) and end_ms > start_ms and isinstance(text, str)):
            raise ValueError("invalid ASR segment")
        if self.next_speech_range(end_ms-start_ms, prefer_native) != (start_ms, end_ms):
            raise ValueError("audio segments must extend the requested timeline without gaps")
        previous = self.state.get("segments", [])
        cursor = previous[-1][1] if previous else 0
        key = "segments" if cursor == start_ms else "supplemental_speech"
        changes = {key: [*self.state.get(key, []), [start_ms, end_ms, text]]}
        if prefer_native and self._public_track():
            changes["native_prefix_pinned"] = True
        self._publish(**changes)

    def _render(self, name, rows):
        path = self.directory / name
        result = "".join(f"[{_stamp(start)} – {_stamp(end)}] {text or '[silence]'}\n" for start, end, text in rows)
        data = result.encode("utf-8")
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            with _atomic_file(path) as writer:
                writer.write(data)
        return path

    def transcript_path(self) -> Path | None:
        rows = self.segments()
        if not rows or rows[-1][1] != self.state["audio"]["duration_ms"]:
            return None
        return self._render("transcript.txt", rows)

    def public_caption_document(self) -> tuple[Path, bool] | None:
        """Return the verified platform transcript, including a clearly named partial track."""
        track = self._public_track()
        if not track:
            return None
        complete = track["complete"]
        name = "transcript-native.txt" if complete else "captions-partial.txt"
        return self._render(name, track["cues"]), complete

    def _public_rows(self, track=None):
        track = track or self._public_track()
        if not track:
            return None
        rows = [tuple(cue) for cue in track["cues"]]
        if track["complete"]:
            return rows, "native"
        audio = self.state.get("audio") or {}
        duration = audio.get("duration_ms")
        if not _integer(duration, 1):
            return None
        boundary = self._prefix(track, duration)
        if not boundary:
            return None
        if boundary == duration:
            return rows, "native"
        cursor = boundary
        for start, end, text in self._speech_ranges():
            if end <= cursor:
                continue
            if start > cursor:
                return None
            # Without word timestamps a previously paid chunk crossing the boundary
            # cannot be split faithfully. Keep its original time range and mark it.
            if start < boundary:
                prefix = f"[ASR overlaps captions through {_stamp(boundary)}] "
            else:
                prefix = "[ASR] "
            rows.append((start, end, prefix + (text or "[silence]")))
            cursor = end
        return (rows, "mixed") if cursor == duration else None

    def saved_public_transcript(self) -> tuple[Path, str] | None:
        public = self._public_rows()
        if public is None:
            return None
        rows, source = public
        return self._render(f"transcript-{source}.txt", rows), source

    def frame(self, timestamp_ms: int, *, kind="preview") -> Path | None:
        if kind not in ("preview", "decoded"):
            raise ValueError("invalid frame origin")
        if not _integer(timestamp_ms):
            raise ValueError("invalid frame timestamp")
        frames = self.state.get("frames", {})
        if not isinstance(frames, dict):
            raise CacheUnavailable("invalid frame manifest")
        record = frames.get(f"{kind}-{timestamp_ms}")
        if record is None:
            return None
        return self._verified_blob(self.directory / f"frame-{kind}-{timestamp_ms}.jpg", record)

    def save_frame(self, timestamp_ms: int, source: Path, *, kind="preview") -> Path:
        path = self.frame(timestamp_ms, kind=kind)
        if path is not None:
            return path
        with Path(source).open("rb") as reader:
            if reader.read(2) != b"\xff\xd8" or reader.seek(0, os.SEEK_END) < 4:
                raise ValueError("expected JPEG frame")
            reader.seek(-2, os.SEEK_END)
            if reader.read() != b"\xff\xd9":
                raise ValueError("incomplete JPEG frame")
        path = self.directory / f"frame-{kind}-{timestamp_ms}.jpg"
        record = _store_blob(source, path)
        self._publish(frames={**self.state.get("frames", {}), f"{kind}-{timestamp_ms}": record})
        return path


    def frame_set(self, requested_ms: int, *, kind) -> tuple[tuple[int, Path], ...] | None:
        if kind not in ("preview", "decoded") or not _integer(requested_ms):
            raise ValueError("invalid frame request")
        groups = self.state.get("frame_sets", {})
        if not isinstance(groups, dict):
            raise CacheUnavailable("invalid frame set manifest")
        actual = groups.get(f"{kind}-{requested_ms}")
        if actual is None:
            return None
        if (not isinstance(actual, list) or not 1 <= len(actual) <= 5
                or not all(_integer(ms) for ms in actual) or actual != sorted(set(actual))):
            raise CacheUnavailable("invalid frame set")
        result = []
        for ms in actual:
            path = self.frame(ms, kind=kind)
            if path is None:
                raise CacheUnavailable("incomplete frame set")
            result.append((ms, path))
        return tuple(result)

    def save_frame_set(self, requested_ms: int, actual_ms: tuple[int, ...], *, kind) -> None:
        if (kind not in ("preview", "decoded") or not _integer(requested_ms)
                or not 1 <= len(actual_ms) <= 5 or not all(_integer(ms) for ms in actual_ms)
                or tuple(actual_ms) != tuple(sorted(set(actual_ms)))
                or any(self.frame(ms, kind=kind) is None for ms in actual_ms)):
            raise ValueError("invalid or incomplete frame set")
        self._publish(frame_sets={**self.state.get("frame_sets", {}), f"{kind}-{requested_ms}": list(actual_ms)})


def _entry_directory(root, chat_id, video_id):
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("canonical YouTube ID required")
    if type(chat_id) is not int or not chat_id:
        raise ValueError("chat scope required")
    root = Path(root)
    if not root.is_absolute() or not (root / ".aigan-media-cache").is_file():
        raise CacheUnavailable("media storage is offline or unprovisioned")
    scope = hashlib.sha256(str(chat_id).encode("ascii")).hexdigest()[:32]
    directory = root / scope / video_id
    if directory.parent.is_symlink() or directory.is_symlink():
        raise CacheUnavailable("invalid media directory")
    return directory


@contextmanager
def open_video(root: Path, chat_id: int, video_id: str):
    """Lock the requested entry on a pre-provisioned private media volume."""
    import fcntl

    directory = _entry_directory(root, chat_id, video_id)
    directory.parent.mkdir(mode=0o700, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError) as exc:
            # SMB volumes can report EACCES for a conflicting nonblocking lock.
            raise CacheBusy("media job already in progress") from exc
        yield _VideoCache(directory)


def cached_transcript_read(root: Path, chat_id: int, video_id: str, max_chars: int,
                           include_timestamps: bool = True, *, prefer_native: bool = True,
                           official_text: str | None = None,
                           official_end_ms: int | None = None) -> tuple[str, str] | None:
    """Read an atomic, complete chat-scoped transcript; never create or download anything."""
    if not _integer(max_chars, 1):
        raise ValueError("positive excerpt budget required")
    try:
        directory = _entry_directory(root, chat_id, video_id)
        cache = _VideoCache(directory)
        track = cache._public_track() if prefer_native else None
        public = cache._public_rows(track) if track else None
        if public is not None:
            chunks, source = public
            if official_text is not None:
                captured = " ".join(" ".join(cue[2].split()) for cue in track["cues"])
                end_ms = max(cue[1] for cue in track["cues"])
                if (captured != " ".join(official_text.split()) or official_end_ms is None
                        or abs(end_ms - official_end_ms) > 1500):
                    return None
        else:
            if official_text is not None:
                return None
            chunks = cache.segments()
            if not chunks or chunks[-1][1] != cache.state["audio"]["duration_ms"]:
                return None
            source = "asr"
    except (OSError, ValueError):
        return None
    if not include_timestamps and source == "mixed":
        chunks = [(start, end, re.sub(r"^\[ASR overlaps captions through [0-9:.]+\] ",
                                       "[ASR overlaps captions] ", text)) for start, end, text in chunks]
    lines = [(f"[{_stamp(start)} – {_stamp(end)}] " if include_timestamps else "", text or "[silence]")
             for start, end, text in chunks]
    complete = "\n".join(prefix + text for prefix, text in lines)
    if len(complete) <= max_chars:
        return source, complete

    label = f"Excerpts sampled across the full transcript ({source}):"
    if max_chars <= len(label):
        return source, label[:max_chars]
    remaining = max_chars - len(label)
    count = min(len(lines), max(1, min(16, remaining // 120)))
    indices = [round(i * (len(lines) - 1) / max(1, count - 1)) for i in range(count)]
    width = max(0, remaining // count - 1)
    samples = []
    for index in indices:
        prefix, text = lines[index]
        budget = max(0, width - len(prefix))
        if len(text) > budget and budget >= 5:
            # Include the close of the final segment, including for a single long segment.
            tail = (budget - 3) // 2 if index == len(lines) - 1 else 0
            text = text[:budget - tail - 3] + "..." + (text[-tail:] if tail else "")
        samples.append((prefix + text)[:width])
    return source, "\n".join((label, *samples))[:max_chars]


def cached_excerpt(root: Path, chat_id: int, video_id: str, max_chars: int,
                   include_timestamps: bool = True, *, prefer_native: bool = True) -> str | None:
    record = cached_transcript_read(root, chat_id, video_id, max_chars, include_timestamps,
                                    prefer_native=prefer_native)
    return record[1] if record else None
