"""Time-scoped YouTube evidence via the same MCP entry point used by the agent."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from mcp_servers import youtube_transcript as tool
from youtube_caption_tracks import OriginalCaptionTrack
import youtube_caption_tracks as tracks
from youtube_media_cache import open_video


class TranscriptWindowTests(unittest.TestCase):
    video = "abcdefghijk"

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / ".aigan-media-cache").touch()
        self.enterContext(patch.dict(os.environ, AIGAN_MEDIA_LIBRARY=str(self.root), AIGAN_MEDIA_CHAT="-42",
                                    AIGAN_YOUTUBE_WINDOW="", YOUTUBE_AUDIO_FALLBACK="true"))
        self.lookup = self.enterContext(patch.object(tool, "lookup_original_track"))
        self.audio = self.enterContext(patch.object(tool, "_transcribe_audio_fallback",
                                                   side_effect=AssertionError("unscoped, paid fallback")))

    def save(self, rows, duration, complete):
        with open_video(self.root, -42, self.video) as cache:
            cache.save_public_transcript(rows, duration_ms=duration, covered_ms=rows[-1][1],
                                         complete=complete, track_id="en:asr")

    def result(self, **kwargs):
        return tool.get_youtube_transcript(self.video, **kwargs).split("--- YouTube tool result ---\n", 1)[1]

    def test_warm_long_timeline_filters_before_excerpt_limit_and_uses_authored_range(self):
        rows = [(i * 2000, (i + 1) * 2000, f"cue-{i:05}") for i in range(5100)]
        self.save(rows, rows[-1][1], True)
        with patch.dict(os.environ, AIGAN_YOUTUBE_WINDOW=json.dumps([self.video, 5341, 5400])):
            text = self.result(start_seconds=1, end_seconds=5, max_chars=1500)
        self.assertIn("cue-02670", text)  # began before the left edge
        self.assertIn("cue-02699", text)
        self.assertNotIn("cue-02669", text)
        self.assertNotIn("cue-02700", text)
        self.assertNotIn("Excerpts sampled", text)
        self.lookup.assert_not_called()
        self.audio.assert_not_called()

    def test_live_late_window_and_explicit_language_ignore_cached_default(self):
        self.save([(0, 200000, "cached default language")], 200000, True)
        self.lookup.return_value = OriginalCaptionTrack("fr", False, 200000, (
            (0, 190000, "early material " * 850), (190000, 195000, "late French cue"),
            (195000, 200000, "out of bounds")))
        text = self.result(languages="fr", start_seconds=190, end_seconds=195, max_chars=1000)
        self.assertIn("late French cue", text)
        for extra in ("early material", "cached default", "out of bounds"):
            self.assertNotIn(extra, text)
        self.lookup.assert_called_once_with(self.video, ["fr"], window_ms=(190000, 195000))
        self.assertIn("cached default language", self.result())
        self.assertEqual(self.lookup.call_count, 1)

    def test_sparse_cached_window_competes_with_other_tracks_without_overwriting_overview(self):
        self.save([(0, 13000, "native opening"), (59000, 60000, "overview ending")], 60000, True)
        query = {"start_seconds": 10, "end_seconds": 20}
        self.lookup.side_effect = ConnectionError("unavailable")
        partial = self.result(**query)
        self.assertIn("native opening", partial)
        self.assertIn("only partial coverage", partial)
        self.lookup.side_effect = None
        self.lookup.return_value = OriginalCaptionTrack("pl", True, 60000, ((10000, 11000, "shorter"),))
        self.assertIn("native opening", self.result(**query))
        self.lookup.return_value = OriginalCaptionTrack("pl", True, 60000, ((10000, 20000, "whole window"),))
        self.assertIn("whole window", self.result(**query))
        self.assertIn("overview ending", self.result())
        self.assertEqual(self.lookup.call_count, 3)


    def test_covered_partial_and_failed_lookup_keep_existing_evidence(self):
        self.save([(0, 2000, "earlier"), (2000, 6000, "known phrase")], 60000, False)
        self.assertIn("known phrase", self.result(start_seconds=3, end_seconds=4))
        self.lookup.assert_not_called()
        self.lookup.side_effect = ConnectionError("private failed lookup detail")
        text = self.result(start_seconds=4, end_seconds=9)
        self.assertIn("known phrase", text)
        self.assertIn("Saved YouTube captions", text)
        self.assertNotIn("private failed", text)

    def test_native_gap_looks_for_another_track_then_exact_cached_asr(self):
        self.save([(0, 1000, "distant"), (59000, 60000, "also distant")], 60000, True)
        self.lookup.return_value = OriginalCaptionTrack("pl", True, 60000, ((6000, 10000, "another track"),))
        self.assertIn("another track", self.result(start_seconds=5, end_seconds=10))
        self.lookup.return_value = OriginalCaptionTrack("en", False, 60000, ((59000, 60000, "outside"),))
        sample = self.root / "sample"
        sample.write_bytes(b"fake audio")
        with open_video(self.root, -42, self.video) as cache:
            cache.save_audio(sample, 60000)
            cache.append_segment(0, 5000, "before")
            cache.append_segment(5000, 10000, "saved speech")
        self.assertIn("saved speech", self.result(start_seconds=5, end_seconds=10))
        coarse = self.result(start_seconds=6, end_seconds=8)
        self.assertIn("No overlapping caption cues", coarse)
        self.assertNotIn("saved speech", coarse)
        self.assertNotIn("outside", coarse)
        self.audio.assert_not_called()

    def test_cached_speech_uses_only_whole_chunks_and_labels_unrecognized_audio(self):
        self.lookup.return_value = None
        sample = self.root / "sample"
        sample.write_bytes(b"fake audio")
        with open_video(self.root, -42, self.video) as cache:
            cache.save_audio(sample, 10000)
            cache.append_segment(0, 3000, "left boundary")
            cache.append_segment(3000, 5000, "inner words")
            cache.append_segment(5000, 7000, "")
            cache.append_segment(7000, 10000, "right boundary")
        text = self.result(start_seconds=2, end_seconds=8)
        self.assertIn("Saved audio transcription", text)
        self.assertIn("only partial coverage", text)
        self.assertIn("inner words", text)
        self.assertIn("[no speech recognized]", text)
        self.assertNotIn("boundary", text)
        self.assertNotIn("silence", text)
        self.assertNotIn("partial coverage", self.result(start_seconds=3, end_seconds=7))
        self.audio.assert_not_called()

    def test_unrecognized_audio_does_not_mask_native_words(self):
        self.lookup.return_value = None
        self.save([(5000, 6000, "native phrase")], 10000, False)
        sample = self.root / "sample"
        sample.write_bytes(b"fake audio")
        with open_video(self.root, -42, self.video) as cache:
            cache.save_audio(sample, 10000)
            cache.append_segment(0, 10000, "")
        self.assertIn("native phrase", self.result(start_seconds=0, end_seconds=10))

    def test_native_words_are_kept_and_only_disjoint_saved_speech_supplements_them(self):
        self.lookup.side_effect = ConnectionError("offline")
        self.save([(3000, 5000, "native phrase")], 10000, False)
        sample = self.root / "sample"
        sample.write_bytes(b"fake audio")
        with open_video(self.root, -42, self.video) as cache:
            cache.save_audio(sample, 10000)
            cache.append_segment(0, 3000, "opening")
            cache.append_segment(3000, 7000, "overlaps native")
            cache.append_segment(7000, 10000, "ending")
        text = self.result(start_seconds=0, end_seconds=10)
        self.assertIn("native phrase", text)
        self.assertNotIn("overlaps native", text)
        self.assertIn("[Saved audio transcription] opening", text)
        self.assertIn("[Saved audio transcription] ending", text)
        self.assertIn("only partial coverage", text)

    def test_cut_to_budget_after_matching_cues_and_label_shortened_output(self):
        self.lookup.return_value = OriginalCaptionTrack("en", True, 100000, (
            (0, 1000, "outside"), (16000, 18500, "speech " * 1100), (19000, 20000, "tail")))
        text = self.result(start_seconds=16, end_seconds=20, max_chars=1000)
        self.assertIn("2 overlapping cues", text)
        self.assertIn("Output shortened", text)
        self.assertNotIn("outside", text)
        self.assertIn("[00:00:16 – 00:00:18.500]", text)
        self.assertLessEqual(len(text.split("\n",1)[1]),1000)

    def test_invalid_windows_do_not_fetch_and_other_video_does_not_leak_its_window(self):
        for a, b in ((2, 2), (-1, 4), (None, 5), (1, None), (True, 20)):
            with self.subTest(a=a, b=b):
                self.assertIn("invalid time interval", self.result(start_seconds=a, end_seconds=b))
        self.lookup.assert_not_called()
        with patch.dict(os.environ, AIGAN_YOUTUBE_WINDOW=json.dumps(["differentID", 20, 25])):
            self.lookup.return_value = None
            self.result(start_seconds=3, end_seconds=4)
        self.lookup.assert_called_once_with(self.video, ["uk", "en", "ru"], window_ms=(3000,4000))

    def test_lookup_compares_overlapping_time_before_language_or_whole_track_length(self):
        def track(lang, cues):
            obj = MagicMock(language_code=lang, is_generated=True)
            obj.fetch.return_value.to_raw_data.return_value = [
                {"start": start, "duration": length, "text": lang} for start, length in cues]
            return obj
        preferred = track("uk", [(0, 1), (100, 1), (100, 1), (299, 1)])
        full_minute = track("fr", [(100, 20), (120, 40)])
        with patch.object(tracks, "YouTubeTranscriptApi") as api:
            api.return_value.list.return_value = [preferred, full_minute]
            result = tracks.lookup_original_track(self.video, ["uk"], window_ms=(100000,160000))
        self.assertEqual(result.language_code, "fr")
        self.assertEqual(tracks.window_coverage(((4000, 6000, ""), (5000, 8000, "")),5000,7000),2000)
