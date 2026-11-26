import asyncio
import json
import os
from unittest.mock import patch
import unittest

from tests.support import FakeMessage, configure_test_environment

configure_test_environment()
import main


class YoutubeAdmissionTests(unittest.TestCase):
    link = "https://youtu.be/A1b2C3d4E5F"

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"AIGAN_MEDIA_LIBRARY": "/app/media-library"}))

    def test_explicit_url_or_direct_same_chat_reply(self):
        message = FakeMessage("Transcribe the whole video " + self.link, chat_type="private")
        self.assertTrue(main.requested_youtube_media(message, message.text).transcript)
        message.text = "Покажи кадри на 1:23:45"
        message.reply_to_message = FakeMessage(self.link, chat_type="private")
        self.assertEqual(main.requested_youtube_media(message, message.text).timestamp_ms, 5_025_000)
        message.reply_to_message.chat_id -= 1
        self.assertIsNone(main.requested_youtube_media(message, message.text))

    def test_passive_link_ordinary_summary_and_ambiguous_sources_stay_on_their_existing_paths(self):
        message = FakeMessage(self.link, chat_type="private")
        for prompt in (self.link, "Summarize " + self.link, "Transcribe " + self.link + " https://example.org/"):
            message.text = prompt
            self.assertIsNone(main.requested_youtube_media(message, prompt))
        message.text = "Transcribe " + self.link
        with patch.dict(os.environ, {"AIGAN_MEDIA_LIBRARY": ""}):
            self.assertIsNone(main.requested_youtube_media(message, message.text))

    def test_scoped_window_uses_authored_markers_and_only_a_direct_source(self):
        question = "Про що говорили з 01:02 до 01:08?"
        msg = FakeMessage(question)
        msg.reply_to_message = FakeMessage(self.link + "?t=123")
        with patch.dict(os.environ, {"AIGAN_MEDIA_LIBRARY": ""}):
            self.assertEqual(main.requested_youtube_window(msg, question), ("A1b2C3d4E5F", 62, 68))
        msg.reply_to_message.chat_id -= 1
        self.assertIsNone(main.requested_youtube_window(msg, question))
        msg.reply_to_message = FakeMessage(self.link + " 01:02–01:08")
        self.assertIsNone(main.requested_youtube_window(msg, "Summarize this."))
        self.assertIsNone(main.requested_youtube_window(msg, question + " " + self.link + " https://example.com/"))
        msg.forward_origin = object()
        self.assertIsNone(main.requested_youtube_window(msg, question))

    def test_authored_window_cannot_start_paid_whole_video_fallback(self):
        token = main.YOUTUBE_WINDOW.set(("A1b2C3d4E5F", 62, 68))
        try:
            with patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}):
                env = main.youtube_mcp_environment(run_id="", route_bucket="memory_recall")
        finally:
            main.YOUTUBE_WINDOW.reset(token)
        self.assertEqual(env["YOUTUBE_AUDIO_FALLBACK"], "false")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_discussion_cannot_activate_paid_speech_in_the_ordinary_agent(self):
        token = main.YOUTUBE_CAPTION_READ_ONLY.set(True)
        try:
            with patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}):
                env = main.youtube_mcp_environment(run_id="", route_bucket="")
        finally:
            main.YOUTUBE_CAPTION_READ_ONLY.reset(token)
        self.assertEqual(env["YOUTUBE_AUDIO_FALLBACK"], "false")
        self.assertNotIn("OPENAI_API_KEY", env)


class YoutubeWindowRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_admissions_keep_separate_windows_on_ordinary_agent_path(self):
        first = FakeMessage("Поясни 3:00–4:00")
        first.reply_to_message = FakeMessage("https://youtu.be/A1b2C3d4E5F")
        second = FakeMessage("Summarize this", chat_id=-2002)
        both_started = asyncio.Event()
        seen = {}

        async def generate(message, *args, **kwargs):
            seen[message.chat_id] = None
            if len(seen) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 2)
            env = main.youtube_mcp_environment(run_id="", route_bucket="memory_recall")
            seen[message.chat_id] = env.get("AIGAN_YOUTUBE_WINDOW")

        with patch.object(main, "handle_prompt_generation", generate), patch.dict(
            os.environ, {"AIGAN_MEDIA_LIBRARY": "", "YOUTUBE_AUDIO_FALLBACK": "false"}
        ):
            await asyncio.gather(*(main.run_admitted_prompt(msg, None, msg.text, False, True, ())
                                   for msg in (first, second)))
            self.assertEqual(json.loads(seen[first.chat_id]), ["A1b2C3d4E5F", 180, 240])
            self.assertIsNone(seen[second.chat_id])
            self.assertNotIn("AIGAN_YOUTUBE_WINDOW", main.youtube_mcp_environment(run_id="", route_bucket="normal"))
