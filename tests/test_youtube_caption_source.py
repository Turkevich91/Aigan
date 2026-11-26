"""Coverage and billing decisions for the isolated official-caption probe."""
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import youtube_caption_source as source
from youtube_caption_tracks import CaptionTrackUnavailable, OriginalCaptionTrack


VIDEO = "aBCdEFG-1x3"


class CaptionCoverageTests(unittest.TestCase):
    def test_published_cue_gaps_and_short_end_padding_can_be_silence(self):
        self.assertEqual(source.classify_cues(((200, 1200, "first"), (95000, 103000, "last")), 100000),
                         ("complete", 100000))
        self.assertEqual(source.classify_cues(((500, 60500, "words"), (3000000, 3597000, "end")), 3600000),
                         ("complete", 3597000))
        self.assertEqual(source.classify_cues(((200, 1600, "first"), (1790000, 1800000, "last")), 10800000),
                         ("partial", 1800000))

    def test_ambiguous_start_unknown_duration_and_invalid_times_do_not_authorize_audio(self):
        cases = [(((600000, 630000, "late"),), 1000000), (((0, 1000, "hi"),), None),
                 (((0, 1000000000, "far"),), 5000), (((0, -1, "bad"),), 1000),
                 (((0, 1100, ""),), 1500), (((1000, 1500, "a"), (500, 900, "b")), 2000)]
        for cues, duration in cases:
            with self.subTest(cues=cues, duration=duration):
                self.assertEqual(source.classify_cues(cues, duration), ("unavailable", 0))
        self.assertEqual(source.classify_cues((), 60000), ("absent", 0))

    def test_failed_alternative_makes_only_partial_coverage_uncertain(self):
        rows = ((0, 1000, "hola"), (97000, 101000, "fin"))
        for duration, all_read, expected, tail in (
                (100000, False, "complete", 100000),
                (600000, True, "partial", 101000),
                (600000, False, "partial_unverified", 101000)):
            with self.subTest(duration=duration, all_read=all_read):
                tr = OriginalCaptionTrack("es", False, duration, rows, all_read)
                with patch("youtube_caption_tracks.lookup_original_track", return_value=tr) as read:
                    evidence = source._probe(VIDEO)
                read.assert_called_once_with(VIDEO)
                self.assertEqual((evidence.status, evidence.language, evidence.track_id), (expected, "es", "es:manual"))
                self.assertEqual(evidence.cues[-1][1], tail)
                self.assertEqual(evidence.covered_ms, tail)

    def test_listing_failure_and_confirmed_absence_are_different(self):
        with patch("youtube_caption_tracks.lookup_original_track", return_value=None):
            self.assertEqual(source._probe(VIDEO).status, "absent")
        with patch("youtube_caption_tracks.lookup_original_track", side_effect=CaptionTrackUnavailable):
            self.assertEqual(source._probe(VIDEO).status, "unavailable")


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_never_reports_missing_captions_and_rechecks_permission(self):
        check = Mock()
        with patch.object(source, "_command", AsyncMock(side_effect=TimeoutError)) as worker:
            ev = await source.fetch_caption_evidence(VIDEO, before_call=check)
            self.assertEqual(ev.status, "unavailable")
            self.assertEqual(check.call_count, 2)
            args = worker.call_args.args[0]
            self.assertEqual(args[-1], VIDEO)
            self.assertGreater(worker.call_args.kwargs["timeout"], 0)

    async def test_worker_result_and_revoked_permission(self):
        result = {"status": "complete", "duration_ms": 2000, "covered_ms": 2000, "language": "ja",
                  "track_id": "ja:asr", "cues": [[200, 2000, "fixture"]]}
        with patch.object(source, "_command", AsyncMock(return_value=json.dumps(result).encode())):
            ev = await source.fetch_caption_evidence(VIDEO, before_call=Mock())
            self.assertEqual(ev.cues, ((200, 2000, "fixture"),))
            with self.assertRaises(PermissionError):
                await source.fetch_caption_evidence(VIDEO, before_call=Mock(side_effect=[None, PermissionError()]))
