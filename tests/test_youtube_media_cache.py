"""Durability, exact coverage, cache isolation, and interrupted writes."""
from pathlib import Path
import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from youtube_media_cache import CacheBusy, CacheUnavailable, open_video


@unittest.skipUnless(os.name == "posix", "cache requires POSIX locking")
class VideoCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / ".aigan-media-cache").touch()
        self.audio = self.root / "source.m4a"
        self.audio.write_bytes(b"test-audio")
        self.video = "aBCde123-6_"
        self.jpeg = self.root / "source.jpg"
        self.jpeg.write_bytes(b"\xff\xd8fixture\xff\xd9")

    def entry(self, chat=-42):
        return open_video(self.root, chat, self.video)

    def test_resume_transcript_and_publish_only_after_exact_completion(self):
        with self.entry() as cache:
            self.assertIsNone(cache.audio())
            self.assertIsNone(cache.transcript_path())
            saved = cache.save_audio(self.audio, 120500)
            self.assertEqual(saved.read_bytes(), self.audio.read_bytes())
            cache.append_segment(0, 60000, "first minute")
            self.assertIsNone(cache.transcript_path())
        with self.entry() as cache:
            self.assertEqual(cache.audio(), (saved, 120500))
            self.assertEqual(cache.segments(), [(0, 60000, "first minute")])
            for start, end in ((0, 60000), (61000, 90000), (60000, 121000), (60000, 60000)):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    cache.append_segment(start, end, "invalid")
            cache.append_segment(60000, 120000, "")
            cache.append_segment(120000, 120500, "last words")
            output = cache.transcript_path()
            self.assertIn("last words", output.read_text(encoding="utf-8"))
        with self.entry() as cache:
            self.assertEqual(cache.transcript_path(), output)
            self.assertEqual(cache.segments()[-1], (120000, 120500, "last words"))
            with self.assertRaises(ValueError):
                cache.append_segment(120500, 140000, "outside media")

    def test_smb_lock_contention_signals_an_existing_job(self):
        with patch("fcntl.flock", side_effect=PermissionError(13, "denied")), self.assertRaises(CacheBusy):
            with self.entry():
                self.fail("conflicting cache entry opened")

    def test_immutable_audio_and_integrity(self):
        with self.entry() as cache:
            saved = cache.save_audio(self.audio, 1000)
            self.assertEqual(cache.save_audio(self.audio, 1000), saved)
            self.audio.write_bytes(b"other-file")
            with self.assertRaises(CacheUnavailable):
                cache.save_audio(self.audio, 1000)
        saved.write_bytes(b"wrong-data")
        with self.entry() as cache, self.assertRaisesRegex(CacheUnavailable, "checksum"):
            cache.audio()

    def test_stores_frames_independently_at_exact_instant_and_checks_integrity(self):
        with self.entry() as cache:
            frame = cache.save_frame(9000000, self.jpeg)
        with self.entry() as cache:
            self.assertEqual(cache.frame(9000000), frame)
            self.assertIsNone(cache.frame(9000001))
            self.assertIsNone(cache.audio())
        self.jpeg.write_bytes(b"\xff\xd8truncated")
        with self.entry() as cache, self.assertRaises(ValueError):
            cache.save_frame(9000001, self.jpeg)
        frame.write_bytes(b"broken")
        with self.entry() as cache, self.assertRaises(CacheUnavailable):
            cache.frame(9000000)

    def test_closed_storage_and_separate_chat_locks(self):
        with self.entry() as first:
            first.save_audio(self.audio, 1000)
            with self.assertRaises(CacheBusy), self.entry():
                self.fail("duplicate job was admitted")
            with self.entry(-43) as other:
                self.assertIsNone(other.audio())
                self.assertNotEqual(first.directory, other.directory)
        with self.assertRaises(ValueError), open_video(self.root, -42, "../relative"):
            pass
        (self.root / ".aigan-media-cache").unlink()
        with self.assertRaises(CacheUnavailable), self.entry():
            pass

    def test_interrupted_manifest_update_preserves_checkpoint_and_can_retry(self):
        replace = os.replace
        def interrupt_manifest(source, target):
            if target.name == "manifest.json":
                raise OSError("simulated interrupted write")
            replace(source, target)
        with self.entry() as cache:
            cache.save_audio(self.audio, 10)
            cache.append_segment(0, 5, "first")
            with patch("youtube_media_cache.os.replace", side_effect=interrupt_manifest), self.assertRaises(OSError):
                cache.append_segment(5, 10, "second")
            self.assertEqual(cache.segments(), [(0, 5, "first")])
            self.assertEqual(list(cache.directory.glob(".write-*")), [])
        with self.entry() as cache:
            self.assertIsNone(cache.transcript_path())
            cache.append_segment(5, 10, "second")
            self.assertEqual(cache.transcript_path().read_text().count("second"), 1)

    def test_read_only_excerpts_sample_beginning_and_end_and_honor_timestamp_flag(self):
        from youtube_media_cache import cached_excerpt
        prior = sorted(self.root.iterdir())
        self.assertIsNone(cached_excerpt(self.root, -42, self.video, 1000))
        self.assertEqual(sorted(self.root.iterdir()), prior)
        with self.entry() as cache:
            cache.save_audio(self.audio, 50000)
            for i in range(5):
                cache.append_segment(i * 10000, (i+1) * 10000, f"word{i} " * 150 + f"tail{i}")
                if i < 4:
                    self.assertIsNone(cached_excerpt(self.root, -42, self.video, 1000))
        result = cached_excerpt(self.root, -42, self.video, 1000, False)
        self.assertLessEqual(len(result), 1000)
        self.assertIn("Excerpts", result)
        self.assertIn("word0", result)
        self.assertIn("tail4", result)
        self.assertNotIn("00:00:", result)
        self.assertIn("00:00:40", cached_excerpt(self.root, -42, self.video, 1000))
        self.assertIsNone(cached_excerpt(self.root, -43, self.video, 1000))
        self.assertNotIn("Excerpts", cached_excerpt(self.root, -42, self.video, 10000))

    def test_exact_frame_sets_retain_separate_origin_and_require_complete_members(self):
        with self.entry() as cache:
            a = cache.save_frame(1000, self.jpeg, kind="preview")
            b = cache.save_frame(1000, self.jpeg, kind="decoded")
            self.assertNotEqual(a, b)
            cache.save_frame_set(1100, (1000,), kind="preview")
            with self.assertRaises(ValueError):
                cache.save_frame_set(1100, (1000,2000), kind="decoded")
        with self.entry() as cache:
            self.assertEqual(cache.frame_set(1100, kind="preview"), ((1000, a),))
            self.assertIsNone(cache.frame_set(1100, kind="decoded"))
            self.assertIsNone(cache.frame_set(1200, kind="preview"))
