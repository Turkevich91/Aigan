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

    def test_discussion_cannot_activate_paid_speech_in_the_ordinary_agent(self):
        token = main.YOUTUBE_CAPTION_READ_ONLY.set(True)
        try:
            with patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}):
                env = main.youtube_mcp_environment(run_id="", route_bucket="")
        finally:
            main.YOUTUBE_CAPTION_READ_ONLY.reset(token)
        self.assertEqual(env["YOUTUBE_AUDIO_FALLBACK"], "false")
        self.assertNotIn("OPENAI_API_KEY", env)
