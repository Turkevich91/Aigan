"""Admission, bounded media processing and provider isolation for Telegram clips."""
import asyncio
import base64
from dataclasses import replace
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from tests.support import configure_test_environment, FakeMessage

configure_test_environment()
import main
from media_frames import FfmpegMediaFrameAdapter, MediaFrameLimits
from telegram_video import inspect_clip, VideoUnavailable


class TelegramVideoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.attachment = SimpleNamespace(file_size=1024, get_file=AsyncMock())
        self.profile = ("gpt-5.4-mini", "none")
        self.enterContext(patch.object(main, "CONFIG", replace(main.CONFIG,
            media_frame_extraction_enabled=True, chat_inflight_guard_enabled=False)))

    async def test_caption_and_direct_reply_freeze_specific_model_and_yield_ingress(self):
        for current in (True, False):
            msg = FakeMessage("Опиши, що тут відбувається")
            target = msg if current else FakeMessage()
            target.video = self.attachment
            if not current:
                msg.reply_to_message = target
            barrier = asyncio.Event()
            started = asyncio.Event()
            async def generate(*args, **kwargs):
                self.assertEqual(main.ACTIVE_VIDEO_REQUEST.get(), (self.attachment, self.profile))
                started.set()
                await barrier.wait()
            with patch.object(main, "video_answer_settings", create=True, return_value=self.profile) as select:
                with patch.object(main, "handle_prompt_generation", side_effect=generate):
                    await main.handle_prompt(msg, SimpleNamespace(), msg.text, allow_pending_wait=False)
                    await asyncio.wait_for(started.wait(), 2)
                    select.assert_called_once_with(msg.chat_id)
                    await main.handle_prompt(msg, SimpleNamespace(), msg.text, allow_pending_wait=False)
                    select.assert_called_once()
                    barrier.set()
                    await asyncio.gather(*main.telegram_turn_tasks)
            self.assertFalse(main.video_jobs)

    async def test_group_passive_video_and_bare_private_upload_never_inspect(self):
        with patch.object(main, "video_answer_settings", create=True) as select:
            for kind, text in (("supergroup", "what happens here?"), ("private", "")):
                msg = FakeMessage(text, chat_type=kind)
                msg.video = self.attachment
                context = SimpleNamespace(bot=SimpleNamespace(id=999, username="aigan"))
                with patch.object(main, "remember_message_persistently", new=AsyncMock()):
                    await main.text_message(SimpleNamespace(effective_message=msg), context)
            select.assert_not_called()
            self.attachment.get_file.assert_not_awaited()

    async def test_provider_receives_only_request_before_routing_and_never_downloads_unrelated_clip(self):
        client = MagicMock()
        client.__aenter__.return_value = client
        client.responses.create = AsyncMock(return_value=SimpleNamespace(
            output_text='{"inspect":"unrelated"}', status="completed"))
        with patch.object(main, "AsyncOpenAI", return_value=client):
            self.assertFalse(await main.respond_to_video_request(
                FakeMessage(), "What is twelve times three?", self.attachment, self.profile))
        kwargs = client.responses.create.await_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["reasoning"]["effort"]), self.profile)
        self.assertIsInstance(kwargs["input"], str)
        self.assertFalse(kwargs["store"])
        self.attachment.get_file.assert_not_awaited()

    async def test_oversize_rejected_before_download(self):
        self.attachment.file_size = 25_000_000
        with self.assertRaises(VideoUnavailable):
            await inspect_clip(self.attachment, request="describe", mode="frames", adapter=None,
                max_bytes=20_000_000, max_duration=90, ffmpeg="ffmpeg", answer=None, transcribe=None)
        self.attachment.get_file.assert_not_awaited()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg")
    async def test_real_container_samples_both_ends_and_transcribes_only_requested_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "two-scenes.mp4"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=red:s=80x60:d=4",
                "-f", "lavfi", "-i", "color=blue:s=80x60:d=4", "-f", "lavfi", "-i", "sine=duration=8",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
                "-c:v", "mpeg4", "-c:a", "aac", str(source)], check=True, timeout=20)
            class Remote:
                async def download_to_drive(self, custom_path):
                    self.downloaded = custom_path
                    shutil.copyfile(source, custom_path)
            remote = Remote()
            attachment = SimpleNamespace(file_size=source.stat().st_size, get_file=AsyncMock(return_value=remote))
            for mode in ("both", "frames", "speech"):
                adapter = FfmpegMediaFrameAdapter(limits=MediaFrameLimits(selected_frame_count=2))
                answer, stt = AsyncMock(return_value="answer"), AsyncMock(return_value="synthetic speech")
                self.assertEqual(await inspect_clip(attachment, request="describe", mode=mode, adapter=adapter,
                    max_bytes=20_000_000, max_duration=90, ffmpeg="ffmpeg", answer=answer, transcribe=stt), "answer")
                content = answer.await_args.args[0]
                images = [item for item in content if item["type"] == "input_image"]
                if mode == "speech":
                    self.assertFalse(images)
                else:
                    pixels = []
                    for image in images:
                        with Image.open(io.BytesIO(base64.b64decode(image["image_url"].split(",")[1]))) as decoded:
                            pixels.append(decoded.convert("RGB").getpixel((10, 10)))
                    self.assertEqual(len(pixels), 2)
                    self.assertGreater(pixels[0][0], 200)
                    self.assertGreater(pixels[-1][2], 200)
                evidence = json.loads(content[0]["text"])
                if mode == "frames":
                    stt.assert_not_awaited()
                else:
                    stt.assert_awaited_once()
                    self.assertEqual(evidence["speech_evidence"], "synthetic speech")
                self.assertFalse(remote.downloaded.exists())
