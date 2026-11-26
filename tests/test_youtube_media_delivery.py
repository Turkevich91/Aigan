from pathlib import Path
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

    async def run_job(self, req=None, *, speech_check=lambda: None):
        await deliver_youtube_media(req or self.request, root=self.root, chat_id=101,
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
