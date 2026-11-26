"""Bounded public audio acquisition and restart-safe whole-track transcription."""
import asyncio
from pathlib import Path
import os
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import youtube_long_audio as audio
from youtube_media_cache import open_video


ID = "aBCde123-6_"


@unittest.skipUnless(os.name == "posix", "media jobs require POSIX process groups and cache locking")
class LongAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / ".aigan-media-cache").touch()
        self.cache = self.enterContext(open_video(self.root, 123, ID))
        self.source = self.root / "source.m4a"
        self.source.write_bytes(b"fixture")
        self.gate = Mock()
        self.ranges = []
        self.silent = set()

    async def decode(self, source, start, end, dest):
        self.ranges.append((start, end))
        with wave.open(str(dest), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            sample = b"\0\0" if start in self.silent else b"\1\0"
            stream.writeframes(sample * (16 * (end-start)))

    def test_admits_only_complete_bounded_public_audio_from_expected_video(self):
        base = {"id": ID, "extractor_key": "Youtube", "availability": "public", "duration": 10208,
                "protocol": "https", "url": "https://rr1.googlevideo.com/media",
                "ext": "m4a", "acodec": "aac", "vcodec": "none", "filesize": 20}
        audio._check_track(base, ID)
        for diff in ({"id": "other"}, {"availability": "private"}, {"availability": "unlisted"},
                     {"is_live": True}, {"has_drm": True}, {"fragments": [{}]}, {"duration": None},
                     {"duration": 14401}, {"protocol": "m3u8"}, {"vcodec": "h264"},
                     {"filesize": audio.MAX_AUDIO_BYTES+1}, {"url": "https://other.invalid/media"}):
            with self.subTest(diff=diff), self.assertRaises(audio.LongAudioUnavailable):
                audio._check_track({**base, **diff}, ID)
        for event, args in [("socket.getaddrinfo", ("example.invalid", 443)),
                            ("socket.getaddrinfo", ("www.youtube.com", 80)),
                            ("socket.connect", (None, ("127.0.0.1", 443)))]:
            with self.assertRaises(audio.LongAudioUnavailable):
                audio._outbound(event, args)

    async def test_resumes_only_unfinished_audio_and_serves_complete_cache_without_network(self):
        self.cache.save_audio(self.source, 620)
        transcribe = AsyncMock(side_effect=["one", TimeoutError()])
        with patch.object(audio, "CHUNK_MS", 300), patch.object(audio, "_decode", self.decode), \
             patch.object(audio, "_download", new_callable=AsyncMock) as download:
            with self.assertRaises(TimeoutError):
                await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=self.gate)
            self.assertEqual(self.cache.segments(), [(0, 300, "one")])
            self.assertIsNone(self.cache.transcript_path())
            self.silent.add(600)
            second = AsyncMock(return_value="two")
            path = await audio.ensure_transcript(self.cache, ID, transcribe=second, before_call=self.gate)
            self.assertEqual(self.ranges, [(0, 300), (300, 600), (300, 600), (600, 620)])
            self.assertEqual(self.cache.segments(), [(0, 300, "one"), (300, 600, "two"), (600, 620, "")])
            self.assertIn("two", path.read_text())
            self.assertIn("silence", path.read_text())
            second.assert_awaited_once()
            self.assertEqual(await audio.ensure_transcript(self.cache, ID, transcribe=second, before_call=self.gate), path)
            second.assert_awaited_once()
            download.assert_not_awaited()
        self.assertFalse(list(self.cache.directory.glob(".audio-job-*")))

    async def test_empty_recognition_of_audible_chunk_does_not_claim_coverage(self):
        self.cache.save_audio(self.source, 100)
        with patch.object(audio, "_decode", self.decode), self.assertRaises(audio.LongAudioUnavailable):
            await audio.ensure_transcript(self.cache, ID, transcribe=AsyncMock(return_value=" "), before_call=self.gate)
        self.assertEqual(self.cache.segments(), [])
        self.assertIsNone(self.cache.transcript_path())

    async def test_attached_source_must_match_duration_before_any_paid_call(self):
        async def download(video, path):
            path.write_bytes(b"fixture")
            return 10
        transcribe = AsyncMock()
        with patch.object(audio, "_download", download), patch.object(audio, "_duration_ms", AsyncMock(return_value=5000)):
            with self.assertRaisesRegex(audio.LongAudioUnavailable, "source duration"):
                await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=self.gate)
        self.assertIsNone(self.cache.audio())
        transcribe.assert_not_awaited()

    async def test_rechecks_permission_before_speech(self):
        self.cache.save_audio(self.source, 200)
        gate = Mock(side_effect=[None, None, PermissionError("revoked")])
        transcribe = AsyncMock()
        with patch.object(audio, "_decode", self.decode), self.assertRaises(PermissionError):
            await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=gate)
        transcribe.assert_not_awaited()
        self.assertEqual(self.cache.segments(), [])

    async def test_cancelling_subprocess_waits_for_child_to_exit(self):
        pidfile = self.root / "worker"
        command = [sys.executable, "-c",
                   "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
                   str(pidfile)]
        task = asyncio.create_task(audio._command(command, timeout=60, directory=self.root))
        async with asyncio.timeout(4):
            while not pidfile.is_file() or not pidfile.read_text():
                await asyncio.sleep(0.01)
        pid = int(pidfile.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is optional in host tests")
    async def test_real_audio_and_probe_without_billing(self):
        await audio._command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=240",
                              "-t", "1.25", "-c:a", "aac", str(self.source)], timeout=10, directory=self.root)
        duration = await audio._duration_ms(self.source, self.root)
        self.assertEqual(duration, 1250)
        self.cache.save_audio(self.source, duration)
        transcribe = AsyncMock(return_value="synthetic answer")
        path = await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=self.gate)
        self.assertIn("synthetic answer", path.read_text())
        transcribe.assert_awaited_once()

    async def test_full_native_needs_no_audio_and_forced_asr_remains_independent(self):
        self.cache.save_public_transcript([(0, 600, "native words")], duration_ms=620,
                                          covered_ms=600, complete=True, track_id="fixture-uk")
        self.assertIsNone(self.cache.audio())
        self.assertIsNone(self.cache.transcript_path())
        transcribe = AsyncMock(return_value="own words")
        with patch.object(audio, "_download", new_callable=AsyncMock) as download, patch.object(audio, "_decode", self.decode):
            path = await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=self.gate)
            self.assertIn("native words", path.read_text())
            download.assert_not_awaited()
            transcribe.assert_not_awaited()
            self.cache.save_audio(self.source, 620)
            own = await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=self.gate,
                                                prefer_native=False)
            self.assertIn("own words", own.read_text())
            self.assertNotIn("native", own.read_text())
            self.assertNotEqual(path, own)
            self.assertEqual(self.cache.saved_public_transcript(), (path, "native"))
            self.assertEqual(self.cache.transcript_path(), own)
            transcribe.assert_awaited_once()

    async def test_partial_native_transcribes_only_tail_pins_prefix_and_reuses_tail_for_forced_asr(self):
        self.cache.save_audio(self.source, 1020)
        self.cache.save_public_transcript([(50, 390, "original prefix")], duration_ms=1020,
                                          covered_ms=390, complete=False, track_id="original")
        one = AsyncMock(side_effect=["tail one", TimeoutError()])
        with patch.object(audio, "CHUNK_MS", 300), patch.object(audio, "_decode", self.decode):
            with self.assertRaises(TimeoutError):
                await audio.ensure_transcript(self.cache, ID, transcribe=one, before_call=self.gate)
            self.assertEqual(self.ranges, [(390, 690), (690, 990)])
            self.cache.save_public_transcript([(0, 800, "changed partial")], duration_ms=1020,
                                               covered_ms=800, complete=False, track_id="different")
            self.assertEqual(self.cache.segments(), [])
            self.assertIsNone(self.cache.transcript_path())
            two = AsyncMock(side_effect=["tail two", "last"])
            path = await audio.ensure_transcript(self.cache, ID, transcribe=two, before_call=self.gate)
            self.assertEqual(self.ranges[-2:], [(690, 990), (990, 1020)])
            self.assertIn("original prefix", path.read_text())
            self.assertNotIn("changed partial", path.read_text())
            self.assertIn("last", path.read_text())
            self.assertEqual(self.cache.saved_public_transcript(), (path, "mixed"))
            self.assertIsNone(self.cache.transcript_path())
            own = await audio.ensure_transcript(self.cache, ID, transcribe=AsyncMock(side_effect=["self A","self B"]),
                                                before_call=self.gate, prefer_native=False)
            self.assertEqual(self.ranges[-2:], [(0,300),(300,390)])
            self.assertEqual([row[2] for row in self.cache.segments()], ["self A","self B","tail one","tail two","last"])
            self.assertNotIn("original prefix", own.read_text())
            before = own.read_bytes()
            self.cache.save_public_transcript([(0, 1020, "complete native")], duration_ms=1020,
                                              covered_ms=1020, complete=True, track_id="complete")
            unexpected = AsyncMock(side_effect=AssertionError("duplicate transcription"))
            native = await audio.ensure_transcript(self.cache, ID, transcribe=unexpected, before_call=self.gate)
            self.assertIn("complete native", native.read_text())
            self.assertEqual(self.cache.transcript_path().read_bytes(), before)
            unexpected.assert_not_awaited()

    async def test_native_over_existing_full_asr_keeps_crossing_chunk_truthful_without_a_new_call(self):
        from youtube_media_cache import cached_excerpt
        self.cache.save_audio(self.source, 1000)
        self.cache.append_segment(0, 500, "previous start")
        self.cache.append_segment(500, 1000, "original crossing block")
        pure = self.cache.transcript_path().read_bytes()
        self.cache.save_public_transcript([(5, 730, "preferred caption")], duration_ms=1000,
                                          covered_ms=730, complete=False, track_id="sample")
        no_stt = AsyncMock(side_effect=AssertionError("unexpected provider"))
        path = await audio.ensure_transcript(self.cache, ID, transcribe=no_stt, before_call=self.gate)
        self.assertIn("preferred caption", path.read_text())
        self.assertNotIn("previous start", path.read_text())
        self.assertIn("ASR overlaps captions", path.read_text())
        self.assertIn("original crossing block", path.read_text())
        caption_path, complete = self.cache.public_caption_document()
        self.assertFalse(complete)
        self.assertIn("preferred caption", caption_path.read_text())
        self.assertNotIn("original crossing block", caption_path.read_text())
        untimed = cached_excerpt(self.root, 123, ID, 500, include_timestamps=False)
        self.assertIn("ASR overlaps captions", untimed)
        self.assertNotIn("00:00", untimed)
        self.assertEqual(self.cache.transcript_path().read_bytes(), pure)
        self.assertIn("preferred caption", cached_excerpt(self.root, 123, ID, 500))
        self.assertNotIn("preferred caption", cached_excerpt(self.root, 123, ID, 500, prefer_native=False))
        no_stt.assert_not_awaited()

    async def test_partial_native_mismatched_timeline_never_starts_paid_speech(self):
        self.cache.save_audio(self.source, 8000)
        self.cache.save_public_transcript([(0, 1500, "caption prefix")], duration_ms=12000,
                                          covered_ms=1500, complete=False, track_id="example")
        transcribe = AsyncMock()
        with patch.object(audio, "_decode", self.decode), self.assertRaisesRegex(OSError, "timelines disagree"):
            await audio.ensure_transcript(self.cache, ID, transcribe=transcribe, before_call=self.gate)
        self.assertEqual(self.ranges, [])
        transcribe.assert_not_awaited()
