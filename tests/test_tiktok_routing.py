"""TikTok-only admission, settings, bounded delivery and failure behavior."""
import asyncio
import io
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.support import configure_test_environment, FakeMessage

configure_test_environment()
import main
from chat_model_settings import ChatModelSettingsStore, SPARK_MODEL, media_model_allowed
from media_frames import FfmpegMediaFrameAdapter, MediaFrameCandidate, MediaFrameLimits, MediaFrameResult


LINK = "https://www.tiktok.com/@sample/video/7000000000000000001"


class TikTokRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.store = ChatModelSettingsStore(Path(tmp) / "settings.sqlite3")
        self.enterContext(patch.object(main, "CHAT_MODEL_SETTINGS", self.store))
        self.enterContext(patch.object(main, "CONFIG", replace(main.CONFIG, tiktok_video_enabled=True,
            media_frame_extraction_enabled=True, telegram_turn_assembly_enabled=False, chat_inflight_guard_enabled=False)))
        self.enterContext(patch.object(main, "video_jobs", set()))
        self.enterContext(patch.object(main, "telegram_turn_tasks", set()))
        self.context = SimpleNamespace(bot=SimpleNamespace(id=999, username="aigan"))
        self.message = FakeMessage("What happens in this clip? " + LINK, chat_type="private")
        self.normal = self.enterContext(patch.object(main, "run_admitted_prompt", new_callable=AsyncMock))
        self.delivery = self.enterContext(patch.object(main, "send_reply", new_callable=AsyncMock))
        self.delivery.return_value = SimpleNamespace(delivered_chunks=())
        self.presence = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        self.enterContext(patch.object(main, "activity_presence_for_message", return_value=self.presence))
        self.client = MagicMock()
        self.client.__aenter__.return_value = self.client
        self.client.responses.create = AsyncMock()
        self.enterContext(patch.object(main, "AsyncOpenAI", return_value=self.client))

    def model_outputs(self, *texts):
        self.client.responses.create.side_effect = [SimpleNamespace(output_text=text, status="completed") for text in texts]

    def select(self, key, model, revision=0):
        self.store.set_media_model(self.message.chat_id, key, model, expected_revision=revision,
                                   role="admin", available=(model,))

    async def dispatch(self):
        await main.handle_prompt(self.message, self.context, self.message.text, allow_pending_wait=False)

    def test_one_explicit_source_from_authored_request_or_direct_same_chat_reply(self):
        self.assertEqual(main.requested_tiktok_url(self.message, self.message.text), LINK)
        for prompt in (LINK, LINK + "?!", "Describe https://youtu.be/x", "What about " + LINK + " https://example.com"):
            self.assertIsNone(main.requested_tiktok_url(self.message, prompt))
        self.message.reply_to_message = FakeMessage(LINK)
        self.assertEqual(main.requested_tiktok_url(self.message, "What happens?"), LINK)
        self.assertIsNone(main.requested_tiktok_url(self.message, "Read https://example.com"))
        self.message.reply_to_message.chat_id = -2
        self.assertIsNone(main.requested_tiktok_url(self.message, "What happens?"))
        self.assertTrue(media_model_allowed("tiktok", SPARK_MODEL))
        self.assertEqual(main.video_answer_settings(-2, "tiktok"), ("gpt-6.1-terra", "none"))
        with patch.object(self.store, "media_selection", return_value=(SPARK_MODEL, 1)):
            self.assertEqual(main.video_answer_settings(-2, "tiktok"), (SPARK_MODEL, "auto"))
        with patch.object(self.store, "media_selection", return_value=("unknown-model", 1)):
            with self.assertRaises(main.ChatModelSettingsUnavailable):
                main.video_answer_settings(-2, "tiktok")

    async def test_malformed_local_intent_stops_without_falling_back_or_downloading(self):
        self.select("tiktok", SPARK_MODEL)
        for number, text in enumerate(("{}", '{"inspect":"unknown"}', '{"inspect":"unrelated"}')):
            self.message = FakeMessage("Inspect this clip " + LINK, chat_type="private", message_id=200+number)
            result = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
                                     usage=None, model="local")
            with patch.object(main.SPARK_MEDIA_VISION, "clip_response", new=AsyncMock(return_value=result)), \
                    patch.object(main, "fetch_tiktok_video", new_callable=AsyncMock) as fetch:
                await self.dispatch()
                await asyncio.gather(*main.telegram_turn_tasks)
                fetch.assert_not_awaited()
                self.client.responses.create.assert_not_awaited()
            if number < 2:
                self.normal.assert_not_awaited()
                self.assertIn("Не вдалося", self.delivery.await_args.args[1])
            else:
                self.normal.assert_awaited_once()

    async def test_local_stt_requires_spoken_request_and_current_permission(self):
        self.select("tiktok", SPARK_MODEL)
        public_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "frame.jpg"
        public_file.write_bytes(b"frame")

        @asynccontextmanager
        async def frame_session(request):
            yield MediaFrameResult(True, frames=(MediaFrameCandidate(public_file, 0, 0),), metadata={"has_audio": True})

        async def fetch(url, destination, **kwargs):
            destination.write_bytes(b"video")

        cloud_stt = AsyncMock(return_value=SimpleNamespace(text="The spoken line.", usage=None))
        self.client.audio.transcriptions.create = cloud_stt
        cases = [
            ("Describe the visible change in English.", "both", "visual", "", False),
            ("Переклади напис на екрані.", "speech", "visual", "", False),
            ("Is there instrumental music?", "both", "other_audio", "", False),
            ("What color is the sign?", "speech", "spoken_words", "transcribe", False),
            ("Переклади слова диктора.", "speech", "spoken_words", "слова диктора", True),
            ("Describe the scene and translate the dialogue.", "both", "spoken_words", "translate the dialogue", True),
            ("Transcribe the clip.", "speech", "spoken_words", "Transcribe", False),
        ]
        for i, (request, decision, source, quote, expected_cloud) in enumerate(cases):
            with self.subTest(request=request):
                cloud_stt.reset_mock()
                self.delivery.reset_mock()
                self.message = FakeMessage(request + " " + LINK, chat_type="private", message_id=400+i)
                async def local(instructions, model_input, *, schema, before_call):
                    before_call()
                    if schema == main.VIDEO_INTENT_FORMAT["format"]:
                        answer = json.dumps({"inspect": decision})
                    elif schema == main.SPEECH_SCOPE_FORMAT:
                        answer = json.dumps({"source": source, "evidence": quote})
                    else:
                        answer = "Answer from the available evidence."
                    return SimpleNamespace(model="local", usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])
                async def extract(source_path, ffmpeg, duration, transcribe):
                    if i == len(cases)-1:
                        policy = self.store.tools.resolve(self.message.chat_id)
                        self.store.tools.set_enabled(self.message.chat_id, "tiktok", False, expected_revision=policy.revision)
                    return await transcribe(io.BytesIO(b"audio"))
                with patch.object(main.SPARK_MEDIA_VISION, "clip_response", side_effect=local), \
                     patch.object(main, "fetch_tiktok_video", side_effect=fetch), \
                     patch.object(main, "runtime_media_frame_adapter", return_value=SimpleNamespace(frame_session=frame_session)), \
                     patch("telegram_video.transcribe_clip", side_effect=extract) as extraction:
                    await self.dispatch()
                    await asyncio.gather(*main.telegram_turn_tasks)
                self.assertEqual(int(expected_cloud), cloud_stt.await_count)
                if i < 4:
                    extraction.assert_not_awaited()
                delivered = self.delivery.await_args.args[1]
                if source == "other_audio":
                    self.assertIn("музику", delivered)
                elif i == len(cases)-1:
                    self.assertIn("недоступний", delivered)
                elif i != 3:
                    self.assertIn("Answer from the available evidence.", delivered)
                self.client.responses.create.assert_not_awaited()

    async def test_passive_group_and_private_bare_link_do_not_start_tiktok(self):
        self.model_outputs()
        with patch.object(main, "remember_message_persistently", new_callable=AsyncMock), \
             patch.object(main, "fetch_tiktok_video", new_callable=AsyncMock) as fetch:
            passive = FakeMessage(self.message.text, chat_type="supergroup")
            await main.text_message(SimpleNamespace(effective_message=passive), self.context)
            self.normal.assert_not_awaited()
            self.assertFalse(passive.reply_calls)
            self.message.text = LINK
            await self.dispatch()
            self.normal.assert_awaited_once()
            fetch.assert_not_awaited()
            self.client.responses.create.assert_not_awaited()

    async def test_frozen_tiktok_model_receives_frames_and_ordinary_chat_can_proceed(self):
        self.select("tiktok", "gpt-6-luna")
        self.select("video", "gpt-6.1-sol")
        self.model_outputs('{"inspect":"frames"}', "Test frame received.")
        started, proceed = asyncio.Event(), asyncio.Event()
        paths = []

        async def fetch(url, destination, **limits):
            self.assertEqual(url, LINK)
            self.assertGreater(limits["max_bytes"], 0)
            paths.append(destination)
            started.set()
            await proceed.wait()
            destination.write_bytes(b"synthetic clip")

        @asynccontextmanager
        async def frame_session(request):
            self.assertEqual(request.source_family, "tiktok_clip")
            self.assertEqual(request.source_path.read_bytes(), b"synthetic clip")
            frame = request.source_path.with_suffix(".jpg")
            frame.write_bytes(b"synthetic frame")
            yield MediaFrameResult(True, frames=(MediaFrameCandidate(frame, 0, 0),))

        with patch.object(main, "fetch_tiktok_video", side_effect=fetch), patch.object(
                main, "runtime_media_frame_adapter", return_value=SimpleNamespace(frame_session=frame_session)):
            await self.dispatch()
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.dispatch()
                ordinary = FakeMessage("How do thermometers work?", chat_type="private", message_id=102)
                await main.handle_prompt(ordinary, self.context, ordinary.text, allow_pending_wait=False)
                self.normal.assert_awaited_once()
                self.select("tiktok", "gpt-6.1-terra", revision=1)
            finally:
                proceed.set()
                await asyncio.gather(*main.telegram_turn_tasks)
        calls = self.client.responses.create.await_args_list
        self.assertEqual(["gpt-6-luna", "gpt-6-luna"], [call.kwargs["model"] for call in calls])
        self.assertNotIn(LINK, str(calls))
        self.assertTrue(any(part["type"] == "input_image" for part in calls[1].kwargs["input"][0]["content"]))
        self.assertEqual(self.delivery.await_args.kwargs["route"], "tiktok_video")
        self.assertEqual(self.delivery.await_args.args[1], "Test frame received.")
        self.assertFalse(paths[0].parent.exists())
        self.assertFalse(main.video_jobs)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires ffmpeg and ffprobe")
    async def test_actual_duration_uses_tiktok_budget_and_preserves_telegram_budget(self):
        adapter = FfmpegMediaFrameAdapter(limits=MediaFrameLimits(max_duration_seconds=90, candidate_frame_count=4,
            selected_frame_count=2, output_width=64, timeout_seconds=10))
        with tempfile.TemporaryDirectory() as tmp:
            for seconds, accepted in ((108, True), (180, True), (181, False)):
                with self.subTest(seconds=seconds):
                    fixture = Path(tmp) / f"clip-{seconds}.mp4"
                    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"color=blue:s=64x48:r=1:d={seconds}", "-c:v", "mpeg4", str(fixture)],
                        check=True, timeout=10)
                    async def fetch(_url, destination, *, max_bytes, max_duration):
                        self.assertEqual((max_bytes, max_duration), (20_000_000, 180))
                        shutil.copyfile(fixture, destination)
                    self.model_outputs('{"inspect":"frames"}', "Two blue samples.")
                    self.client.responses.create.reset_mock()
                    with patch.object(main, "runtime_media_frame_adapter", return_value=adapter), \
                         patch.object(main, "fetch_tiktok_video", side_effect=fetch):
                        await self.dispatch()
                        await asyncio.gather(*main.telegram_turn_tasks)
                    answer = self.delivery.await_args.args[1]
                    self.assertEqual(self.client.responses.create.await_count, 2 if accepted else 1)
                    self.assertIn("Two blue" if accepted else "задовге", answer)
                    if seconds == 108:
                        async def download(custom_path):
                            shutil.copyfile(fixture, custom_path)
                        remote = SimpleNamespace(file_size=fixture.stat().st_size, download_to_drive=download)
                        attachment = SimpleNamespace(file_size=remote.file_size, get_file=AsyncMock(return_value=remote))
                        self.model_outputs('{"inspect":"frames"}')
                        with patch.object(main, "runtime_media_frame_adapter", return_value=adapter):
                            await main.respond_to_video_request(self.message, "Describe this", attachment,
                                                                ("gpt-6.1-terra", "none"))
                        self.assertIn("задовге", self.delivery.await_args.args[1])

    async def test_cancelled_download_cleans_work_directory_and_worker_slot(self):
        started = asyncio.Event()
        paths = []
        self.model_outputs('{"inspect":"speech"}')

        async def fetch(_url, destination, **_limits):
            destination.write_bytes(b"partial")
            paths.append(destination)
            started.set()
            await asyncio.Future()

        with patch.object(main, "fetch_tiktok_video", side_effect=fetch):
            await self.dispatch()
            await asyncio.wait_for(started.wait(), 2)
            task, = main.telegram_turn_tasks
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(paths[0].parent.exists())
        self.assertFalse(main.video_jobs)
        self.delivery.assert_not_awaited()
        self.presence.stop.assert_awaited_once()

    async def test_unrelated_declined_then_inaccessible_clip_gives_upload_alternative(self):
        self.enterContext(patch.object(main, "CONFIG", replace(main.CONFIG,
            media_frame_max_duration_seconds=72, media_frame_max_bytes=5_000_000)))
        self.model_outputs('{"inspect":"unrelated"}', '{"inspect":"both"}')
        with patch.object(main, "fetch_tiktok_video", side_effect=main.TikTokUnavailable("unavailable")) as fetch:
            await self.dispatch()
            await asyncio.gather(*main.telegram_turn_tasks)
            fetch.assert_not_awaited()
            self.normal.assert_awaited_once()
            await self.dispatch()
            await asyncio.gather(*main.telegram_turn_tasks)
            fetch.assert_awaited_once()
        self.assertIn("відеофайлом", self.delivery.await_args.args[1])
        self.assertIn("до 72 с і 5 МБ на файл", self.delivery.await_args.args[1])
        self.assertIn("два або більше фрагментів", self.delivery.await_args.args[1])
        self.assertEqual(2, self.client.responses.create.await_count)
        self.presence.stop.assert_awaited_once()
        self.assertFalse(main.video_jobs)

    async def test_revoked_at_classification_stops_download_and_further_model_calls(self):
        async def classify(**_kwargs):
            chat = self.message.chat_id
            policy = self.store.tools.resolve(chat)
            self.store.tools.set_enabled(chat, "tiktok", False, expected_revision=policy.revision)
            return SimpleNamespace(status="completed", output_text='{"inspect":"frames"}')

        self.client.responses.create.side_effect = classify
        with patch.object(main, "fetch_tiktok_video", new_callable=AsyncMock) as fetch:
            await self.dispatch()
            await asyncio.gather(*main.telegram_turn_tasks)
            fetch.assert_not_awaited()
        self.assertIn("недоступний", self.delivery.await_args.args[1])
        self.client.responses.create.assert_awaited_once()
