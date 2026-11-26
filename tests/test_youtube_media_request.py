import unittest

from youtube_media_request import parse_youtube_media_request


class YoutubeMediaRequestTests(unittest.TestCase):
    url = "https://youtu.be/A1b2C3d4E5F?t=368"

    def test_full_transcription_and_timestamp_in_user_language(self):
        for prompt in ("Розшифруй усе відео повністю і покажи кадри на 2:30:00",
                       "Транскрибируй видео целиком и посмотри кадры на 2:30:00",
                       "Transcribe the entire video and show frames at 2:30:00"):
            with self.subTest(prompt=prompt):
                request = parse_youtube_media_request(self.url, prompt)
                self.assertTrue(request.transcript)
                self.assertEqual(request.timestamp_ms, 9_000_000)

    def test_independent_tasks_and_visual_link_time(self):
        only_text = parse_youtube_media_request(self.url, "Дай повну розшифровку")
        self.assertTrue(only_text.transcript)
        self.assertIsNone(only_text.timestamp_ms)
        for prompt, stamp in (("Посмотри кадры на 2:30:00 без транскрипции", 9_000_000),
                              ("Що видно на екрані?", 368_000)):
            request = parse_youtube_media_request(self.url, prompt)
            self.assertFalse(request.transcript)
            self.assertEqual(request.timestamp_ms, stamp)

    def test_ordinary_and_negated_requests_do_not_start_full_stt(self):
        for prompt in ("Summarize", "Не транскрибируй", "Що думаєш?", "Transcribe at 2:30:00",
                       "What did he say at 2:30:00?", "Не присылай полный транскрипт",
                       "How much does it cost to transcribe the whole video?"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(parse_youtube_media_request(self.url, prompt))
        self.assertIsNone(parse_youtube_media_request("https://example.org/video", "Transcribe the video"))
