import json
import unittest

from youtube_media_request import confirmed_audio_source, parse_youtube_media_request


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
        for prompt in ("Покажи повну стенограму", "Покажи субтитры", "Let me see the full transcript"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(parse_youtube_media_request(self.url, prompt).timestamp_ms)
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

    def test_candidates_include_full_document_paraphrases_and_published_subtitles(self):
        for prompt in ("Запиши каждое слово от начала до конца, слушай дорожку сам",
                       "Скачай официальные субтитры", "Надішли усю стенограму запису",
                       "Дай субтитри, сам не транскрибуй", "Write down every word from the audio",
                       "Give me the complete text of what was said", "Download the subtitles",
                       "Не используй готовый транскрипт, сделай свой собственный",
                       "Don't rely on the transcript; make your own",
                       "Не покладайся на готовий текст запису, склади власний"):
            with self.subTest(prompt=prompt):
                self.assertTrue(parse_youtube_media_request(self.url, prompt).transcript)

    def test_no_full_document_candidate_from_an_excerpt_or_quoted_command(self):
        for prompt in ("Transcribe this excerpt", "Расшифруй фрагмент", "Не делай свой текст",
                       "Please do not consult the transcript", "Готовий текст не використовуй",
                       'Он написал «транскрибируй всё видео». Как думаешь?',
                       'The title reads "download the full subtitles". What is the topic?'):
            with self.subTest(prompt=prompt):
                self.assertIsNone(parse_youtube_media_request(self.url, prompt))

    def test_source_requires_a_verbatim_authored_excerpt(self):
        question = 'Give me the full transcript. The title says "use speech recognition yourself".'
        for source in ("captions_only", "native_first"):
            with self.subTest(source=source):
                raw = json.dumps({"source": source, "evidence": "Give me the full transcript"})
                self.assertEqual(confirmed_audio_source(raw, question), source)
        generic = json.dumps({"source": "own_audio", "evidence": "Give me the full transcript"})
        self.assertEqual(confirmed_audio_source(generic, question), "other")
        for evidence in ("write your own", "не используй готовые субтитры", "з аудіодоріжки"):
            with self.subTest(evidence=evidence):
                question_with_source = question + " " + evidence
                raw = json.dumps({"source": "own_audio", "evidence": evidence})
                self.assertEqual(confirmed_audio_source(raw, question_with_source), "own_audio")
        for result in (None, "x", "[]", "{}", '{"source":"own_audio"}',
                       '{"source":"wrong","evidence":"Give me"}',
                       '{"source":"own_audio","evidence":""}',
                       '{"source":"own_audio","evidence":"use speech recognition yourself"}',
                       '{"source":"native_first","evidence":"send the transcript"}'):
            with self.subTest(result=result):
                self.assertEqual(confirmed_audio_source(result, question), "other")
