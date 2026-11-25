"""Admission, bounded media processing and provider isolation for Telegram clips."""
import asyncio
import base64
from collections import defaultdict, deque
from contextlib import ExitStack
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
            async def generate(_msg, _prompt, clip, settings):
                self.assertIs(clip, self.attachment)
                self.assertEqual(settings, self.profile)
                started.set()
                await barrier.wait()
                return True
            with patch.object(main, "video_answer_settings", create=True, return_value=self.profile) as select:
                with patch.object(main, "respond_to_video_request", side_effect=generate):
                    await main.handle_prompt(msg, SimpleNamespace(), msg.text, allow_pending_wait=False)
                    await asyncio.wait_for(started.wait(), 2)
                    select.assert_called_once_with(msg.chat_id)
                    await main.handle_prompt(msg, SimpleNamespace(), msg.text, allow_pending_wait=False)
                    select.assert_called_once()
                    barrier.set()
                    await asyncio.gather(*main.telegram_turn_tasks)
            self.assertFalse(main.video_jobs)

    async def test_same_sender_completes_text_with_video_blocked_in_both_ingress_modes(self):
        from telegram_turn_assembly import TurnAssembler
        from tests.test_telegram_turn_assembly import ManualClock, AsyncioClock

        for assembled in (False, True):
            with self.subTest(assembled=assembled):
                clock = ManualClock()
                clock.advance(5000)
                started, release = asyncio.Event(), asyncio.Event()
                chat = -1001
                async def slow_video(msg, prompt, clip, settings):
                    self.assertIs(clip, self.attachment)
                    self.assertEqual(settings, self.profile)
                    started.set()
                    await release.wait()
                    return True
                def message(text, number):
                    msg = FakeMessage(text, chat_type="private", chat_id=chat, message_id=number)
                    msg.from_user.id = 1234
                    return msg
                async def settle():
                    for _ in range(30):
                        await asyncio.sleep(0)
                context = SimpleNamespace(bot=SimpleNamespace(id=99, username="test_bot", send_chat_action=AsyncMock()),
                                          application=SimpleNamespace(create_task=asyncio.create_task))
                async def ingest(msg):
                    await main.text_message(SimpleNamespace(effective_message=msg), context)
                    await settle()
                with ExitStack() as stack:
                    overrides = {
                        "CONFIG": replace(main.CONFIG, telegram_turn_assembly_enabled=assembled,
                            chat_inflight_guard_enabled=True, telegram_turn_assembly_seconds=2),
                        "time": clock, "asyncio": AsyncioClock(clock), "telegram_turn_stopping": False,
                        "telegram_turn_assembler": TurnAssembler(), "telegram_turn_ingress": {},
                        "telegram_turn_tasks": set(), "telegram_turn_dispatch_locks": {}, "chat_generation_locks": {},
                        "pending_requests": {}, "last_user_call": {}, "last_chat_call": {}, "MEMORY": None,
                        "remember_message_persistently": AsyncMock(), "remember_self_complaint_signal": MagicMock(),
                        "respond_to_video_request": slow_video, "BOT_USERNAME": "test_bot",
                        "classify_request_with_intent": AsyncMock(return_value=("normal", None)),
                        "run_agent_for_outbound": AsyncMock(return_value="Normal text answer."),
                    }
                    overrides["recent_chat_answers"] = defaultdict(deque)
                    for key, value in overrides.items():
                        stack.enter_context(patch.object(main, key, value))
                    stack.enter_context(patch.object(main, "video_answer_settings", create=True, return_value=self.profile))
                    try:
                        video_prompt = message("Describe the attached clip.", 801)
                        if assembled:
                            # Authored question followed by a separate source attachment.
                            await ingest(video_prompt)
                            source = message("", 802)
                            source.video = self.attachment
                            await ingest(source)
                        else:
                            video_prompt.video = self.attachment
                            await ingest(video_prompt)
                        clock.advance(5002)
                        await asyncio.wait_for(started.wait(), 2)
                        normal = message("Explain how a thermometer works.", 803)
                        await ingest(normal)
                        clock.advance(5004)
                        await settle()
                        self.assertEqual([call["text"] for call in normal.reply_calls], ["Normal text answer."])
                        self.assertFalse(release.is_set())
                    finally:
                        release.set()
                        await settle()
                        await asyncio.gather(*main.telegram_turn_tasks)

    async def test_unrelated_attachment_runs_normal_generation_once_and_preserves_turn_context(self):
        msg, source = FakeMessage("What is three plus four?"), FakeMessage()
        source.video = self.attachment
        normal = AsyncMock()
        with patch.object(main, "video_answer_settings", create=True, return_value=self.profile):
            with patch.object(main, "respond_to_video_request", new=AsyncMock(return_value=False)) as inspect:
                with patch.object(main, "handle_prompt_generation", normal):
                    await main.handle_prompt(msg, SimpleNamespace(), msg.text, allow_pending_wait=False,
                        assembled_turn=True, turn_context_messages=[source])
                    await asyncio.gather(*main.telegram_turn_tasks)
        inspect.assert_awaited_once_with(msg, msg.text, self.attachment, self.profile)
        normal.assert_awaited_once()
        self.assertEqual(normal.await_args.kwargs["turn_context_messages"], [source])

    def test_same_caption_on_distinct_clips_has_separate_deduplication_identity(self):
        first, other = FakeMessage("Describe this clip"), FakeMessage("Describe this clip")
        first.video = SimpleNamespace(file_unique_id="synthetic-one")
        other.video = SimpleNamespace(file_unique_id="synthetic-two")
        self.assertNotEqual(main.context_signature_for_dedupe(first), main.context_signature_for_dedupe(other))

    async def test_group_passive_video_and_bare_private_upload_never_inspect(self):
        with patch.object(main, "video_answer_settings", create=True) as select:
            for kind, text in (("supergroup", "what happens here?"), ("private", "")):
                msg = FakeMessage(text, chat_type=kind)
                msg.from_user.id = 1234
                msg.video = self.attachment
                context = SimpleNamespace(bot=SimpleNamespace(id=999, username="aigan"))
                with patch.object(main, "remember_message_persistently", new=AsyncMock()):
                    await main.text_message(SimpleNamespace(effective_message=msg), context)
            select.assert_not_called()
            self.attachment.get_file.assert_not_awaited()

    async def test_explicit_non_admin_group_video_request_respects_chat_allowlist(self):
        context = SimpleNamespace(bot=SimpleNamespace(id=999, username="aigan"))
        inspect = AsyncMock(return_value=True)
        with patch.object(main, "video_answer_settings", create=True, return_value=self.profile), \
             patch.object(main, "BOT_USERNAME", "aigan"), patch.object(main, "respond_to_video_request", inspect), \
             patch.object(main, "remember_message_persistently", new=AsyncMock()):
            for chat in (-1001, -1002):
                msg = FakeMessage("", chat_type="supergroup", chat_id=chat)
                msg.from_user.id = 1234
                msg.caption = "@aigan опиши відео"
                msg.video = self.attachment
                await main.text_message(SimpleNamespace(effective_message=msg), context)
                await asyncio.gather(*main.telegram_turn_tasks)
        inspect.assert_awaited_once()
        self.assertEqual(inspect.await_args.args[0].chat_id, -1001)
        self.assertEqual(inspect.await_args.args[1], "опиши відео")

    async def test_provider_receives_only_request_before_routing_and_never_downloads_unrelated_clip(self):
        client = MagicMock()
        client.__aenter__.return_value = client
        client.responses.create = AsyncMock(return_value=SimpleNamespace(
            output_text='{"inspect":"unrelated"}', status="completed"))
        with patch.object(main, "AsyncOpenAI", return_value=client), patch.object(main, "activity_presence_for_message") as presence:
            self.assertFalse(await main.respond_to_video_request(
                FakeMessage(), "What is twelve times three?", self.attachment, self.profile))
        presence.assert_not_called()
        kwargs = client.responses.create.await_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["reasoning"]["effort"]), self.profile)
        self.assertIsInstance(kwargs["input"], str)
        self.assertFalse(kwargs["store"])
        self.attachment.get_file.assert_not_awaited()

    async def test_accepted_video_presence_stops_on_success_failure_and_cancellation(self):
        for outcome in ("success", "failure", "cancel"):
            with self.subTest(outcome=outcome):
                msg = FakeMessage("Describe this clip.")
                client = MagicMock()
                client.__aenter__.return_value = client
                client.responses.create = AsyncMock(return_value=SimpleNamespace(
                    output_text='{"inspect":"frames"}', status="completed"))
                presence = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
                async def inspect(*args, **kwargs):
                    presence.start.assert_awaited_once()
                    presence.stop.assert_not_awaited()
                    if outcome == "cancel":
                        raise asyncio.CancelledError()
                    if outcome == "failure":
                        raise VideoUnavailable("Не вдалося прочитати відео.")
                    return "Видно два кольори."
                async def deliver(*args, **kwargs):
                    presence.stop.assert_awaited_once()
                    return SimpleNamespace(delivered_chunks=())
                with patch.object(main, "AsyncOpenAI", return_value=client), \
                     patch.object(main, "activity_presence_for_message", return_value=presence) as factory, \
                     patch.object(main, "inspect_clip", side_effect=inspect), \
                     patch.object(main, "send_reply", side_effect=deliver) as delivery:
                    work = main.respond_to_video_request(msg, msg.text, self.attachment, self.profile)
                    if outcome == "cancel":
                        with self.assertRaises(asyncio.CancelledError):
                            await work
                        delivery.assert_not_awaited()
                    else:
                        self.assertTrue(await work)
                        delivery.assert_awaited_once()
                    factory.assert_called_once_with(msg, action=main.ChatAction.TYPING)
                    presence.stop.assert_awaited_once()

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
