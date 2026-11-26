from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from youtube_media_cache import open_video
from youtube_media_delivery import deliver_youtube_media
from youtube_media_request import YoutubeMediaRequest


class YoutubeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / ".aigan-media-cache").touch()
        self.request = YoutubeMediaRequest("A1b2C3d4E5F", True, None)
        self.speech = AsyncMock()
        self.describe = AsyncMock()
        self.text = AsyncMock()
        self.doc = AsyncMock()
        self.probe = self.enterContext(patch("youtube_media_delivery.fetch_caption_evidence", new_callable=AsyncMock))
        self.probe.return_value = SimpleNamespace(status="absent")

    async def run_job(self, req=None, *, speech_check=lambda: None, mode="native_first"):
        await deliver_youtube_media(req or self.request, root=self.root, chat_id=101, source=mode,
            check_allowed=lambda: None, check_speech=speech_check, check_visual=lambda: None,
            transcribe=self.speech, describe_frames=self.describe,
            send_text=self.text, send_transcript=self.doc)

    async def test_repeat_delivers_complete_document_without_downloader_or_stt(self):
        source = self.root / "fixture.audio"
        source.write_bytes(b"bounded fixture")
        with open_video(self.root, 101, self.request.video_id) as entry:
            entry.save_audio(source, 10000)
            entry.append_segment(0, 10000, "Fixture text.")
        with patch("youtube_media_delivery.ensure_transcript", new_callable=AsyncMock) as ensure:
            await self.run_job()
            self.assertIn("Fixture text.", self.doc.await_args.args[0].read_text())
            ensure.assert_not_called()
            self.speech.assert_not_called()

    async def test_storage_offline_and_disabled_speech_do_not_start_work(self):
        def denied():
            raise PermissionError("disabled")
        with patch("youtube_media_delivery.ensure_transcript", new_callable=AsyncMock) as ensure:
            with self.assertRaises(PermissionError):
                await self.run_job(speech_check=denied)
            (self.root / ".aigan-media-cache").unlink()
            await self.run_job()
            self.assertIn("недоступна", self.text.await_args.args[0])
            ensure.assert_not_called()
            self.doc.assert_not_called()

    async def test_incomplete_transcript_never_reaches_document_delivery(self):
        with patch("youtube_media_delivery.ensure_transcript", new=AsyncMock(side_effect=OSError)):
            await self.run_job()
        self.doc.assert_not_called()
        self.assertIn("не вдалося", self.text.await_args.args[0])

    def captions(self, complete=True):
        self.probe.return_value = SimpleNamespace(status="complete" if complete else "partial",
            cues=[(0, 1000, "Publisher words.")], duration_ms=1000 if complete else 2000, covered_ms=1000,
            track_id="original")

    async def test_native_precedes_independent_speech_even_when_already_cached(self):
        source = self.root / "fixture.audio"
        source.write_bytes(b"audio")
        with open_video(self.root, 101, self.request.video_id) as entry:
            entry.save_audio(source, 1000)
            entry.append_segment(0, 1000, "Independent words.")
        self.captions()
        def disabled():
            raise PermissionError()
        await self.run_job(speech_check=disabled)
        path, label = self.doc.await_args.args
        self.assertEqual(label, "native")
        self.assertIn("Publisher words.", path.read_text())
        await self.run_job(mode="own_audio")
        path, label = self.doc.await_args.args
        self.assertEqual(label, "own_audio")
        self.assertIn("Independent words.", path.read_text())
        await self.run_job()
        self.assertEqual(self.doc.await_args.args[1], "native")
        self.probe.assert_awaited_once()
        self.speech.assert_not_awaited()

    async def test_failed_probe_never_triggers_automatic_asr(self):
        self.probe.return_value = SimpleNamespace(status="unavailable")
        with patch("youtube_media_delivery.ensure_transcript", new_callable=AsyncMock) as ensure:
            await self.run_job()
            self.assertIn("перевірити", self.text.await_args.args[0])
            self.assertFalse(self.doc.called)
            ensure.assert_not_awaited()

    async def test_captions_only_can_deliver_partial_and_decline_missing(self):
        with patch("youtube_media_delivery.ensure_transcript", new_callable=AsyncMock) as ensure:
            await self.run_job(mode="captions_only")
            self.assertFalse(self.doc.called)
            self.captions(complete=False)
            await self.run_job(mode="captions_only")
            self.assertEqual(self.doc.await_args.args[1], "partial")
            ensure.assert_not_awaited()
            self.speech.assert_not_awaited()

    async def test_forced_with_empty_cache_skips_native_probe(self):
        self.captions()
        path = self.root / "own.txt"
        with patch("youtube_media_delivery.ensure_transcript", new=AsyncMock(return_value=path)) as ensure:
            await self.run_job(mode="own_audio")
            self.assertFalse(ensure.await_args.kwargs["prefer_native"])
            self.doc.assert_awaited_once_with(path, "own_audio")
        self.probe.assert_not_awaited()

    async def test_explicit_captions_refresh_an_incomplete_saved_track(self):
        self.captions(complete=False)
        await self.run_job(mode="captions_only")
        self.assertEqual(self.doc.await_args.args[1], "partial")
        self.captions(complete=True)
        await self.run_job(mode="captions_only")
        self.assertEqual(self.doc.await_args.args[1], "native")
        self.assertEqual(self.probe.await_count, 2)
        self.speech.assert_not_awaited()

    async def test_visual_disable_does_not_block_requested_native_subtitles(self):
        request = YoutubeMediaRequest(self.request.video_id, True, 5000)
        self.captions()
        def disabled():
            raise PermissionError()
        await deliver_youtube_media(request, root=self.root, chat_id=101, check_allowed=lambda: None,
                                    check_speech=disabled, check_visual=disabled, transcribe=self.speech,
                                    describe_frames=self.describe, send_text=self.text, send_transcript=self.doc)
        self.assertEqual(self.doc.await_args.args[1], "native")
        self.assertIn("кадрів", self.text.await_args.args[0])
        self.describe.assert_not_awaited()
        self.speech.assert_not_awaited()

    async def test_unread_alternate_captions_do_not_trigger_paid_speech(self):
        self.captions(complete=False)
        self.probe.return_value.status = "partial_unverified"
        with patch("youtube_media_delivery.ensure_transcript", new_callable=AsyncMock) as ensure:
            await self.run_job()
            self.assertEqual(self.doc.await_args.args[1], "partial")
            ensure.assert_not_awaited()
