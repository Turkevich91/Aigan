import asyncio
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import youtube_caption_tracks
from mcp_servers import youtube_transcript
from model_telemetry import ModelTelemetryStore


class SecretTranscriptionFailure(RuntimeError):
    pass


def fake_dependency_modules(*, response: object | None = None, error: Exception | None = None, duration=30):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def download(self, video_urls):
            self.options["match_filter"]({"duration": duration}, incomplete=False)
            output = self.options["outtmpl"].replace("%(ext)s", "mp3")
            Path(output).write_bytes(b"fake audio")

    class FakeTranscriptions:
        def create(self, *, model, file):
            if error is not None:
                raise error
            return response

    class FakeOpenAI:
        def __init__(self):
            self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())

    return {
        "openai": SimpleNamespace(OpenAI=FakeOpenAI),
        "yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    }


class ExplodingTelemetry:
    def __init__(self, *, explode_at: str):
        self.explode_at = explode_at

    def begin_stage(self, **kwargs):
        if self.explode_at == "begin":
            raise RuntimeError("private begin sink detail")
        return object()

    def finish_stage(self, handle, **kwargs):
        if self.explode_at == "finish":
            raise RuntimeError("private finish sink detail")
        return True


class YoutubeTranscriptTelemetryTests(unittest.TestCase):
    def test_downloader_diagnostics_remain_private(self):
        marker = "synthetic downloader detail"
        modules = fake_dependency_modules()

        def fail_download(downloader, _urls):
            logger = downloader.options["logger"]
            logger.debug(marker)
            logger.warning(marker)
            logger.error(marker)
            raise RuntimeError(marker)

        with (
            patch.dict(sys.modules, modules),
            patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}),
            patch.object(modules["yt_dlp"].YoutubeDL, "download", fail_download),
            patch.object(youtube_transcript, "_fetch_caption_transcript", return_value=[]),
            contextlib.redirect_stderr(io.StringIO()) as err,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            result = youtube_transcript.get_youtube_transcript("abcdefghijk")
        self.assertTrue(result.endswith("Audio transcription failed for abcdefghijk: RuntimeError."))
        self.assertEqual("", out.getvalue())
        self.assertEqual("", err.getvalue())

    def test_regional_captions_need_one_listing_and_no_audio(self):
        track = MagicMock(language_code="en-US", is_generated=False)
        track.fetch.return_value.to_raw_data.return_value = [{"text": "caption", "start": 2, "duration": 3}]
        tracks = [track]
        with (
            patch.object(youtube_caption_tracks, "YouTubeTranscriptApi") as api,
            patch.object(youtube_transcript, "_transcribe_audio_fallback") as audio,
            patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}),
        ):
            api.return_value.list.return_value = tracks
            result = youtube_transcript.get_youtube_transcript("abcdefghijk", languages="en")
        self.assertIn("[00:02] caption", result)
        api.return_value.list.assert_called_once_with("abcdefghijk")
        track.fetch.assert_called_once_with()
        audio.assert_not_called()

    def test_only_missing_captions_allow_a_canonical_audio_request(self):
        with (
            patch.object(youtube_transcript, "_fetch_caption_transcript", return_value=[]) as captions,
            patch.object(youtube_transcript, "_transcribe_audio_fallback", return_value="spoken") as audio,
            patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}),
        ):
            result = youtube_transcript.get_youtube_transcript("https://youtu.be/abcdefghijk?t=2")
            self.assertIn("spoken", result)
            audio.assert_called_once_with("https://www.youtube.com/watch?v=abcdefghijk", 16000)
            captions.side_effect = ConnectionError("private network detail")
            result = youtube_transcript.get_youtube_transcript("abcdefghijk")
            self.assertIn("Caption lookup failed", result)
            self.assertNotIn("private", result)
            self.assertEqual(1, audio.call_count)
            with patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "false"}):
                captions.side_effect = youtube_transcript.TranscriptsDisabled("abcdefghijk")
                result = youtube_transcript.get_youtube_transcript("abcdefghijk")
                self.assertIn("fallback is disabled", result)
            self.assertEqual(1, audio.call_count)

    def test_other_caption_language_precedes_paid_recognition(self):
        alternative = MagicMock(language_code="pl", is_generated=True)
        alternative.fetch.return_value.to_raw_data.return_value = [{"start": 0, "duration": 4, "text": "source"}]
        tracks = [alternative]
        with (
            patch.object(youtube_caption_tracks, "YouTubeTranscriptApi") as api,
            patch.object(youtube_transcript, "_transcribe_audio_fallback") as audio,
        ):
            api.return_value.list.return_value = tracks
            result = youtube_transcript.get_youtube_transcript("abcdefghijk", languages="uk")
        self.assertIn("source", result)
        alternative.translate.assert_not_called()
        audio.assert_not_called()

    def test_exhausts_listed_originals_and_never_treats_unreadable_tracks_as_absent(self):
        from youtube_transcript_api._transcripts import Transcript, TranscriptList
        http = MagicMock()
        def track(code, generated):
            return Transcript(http, "abcdefghijk", f"https://example.invalid/captions/{code}", code, code,
                              generated, [])
        spanish, polish, italian = track("es", False), track("pl", False), track("it", True)
        tracks = TranscriptList("abcdefghijk", {"es": spanish, "pl": polish}, {"it": italian}, [])
        cue = {"start": 0, "duration": 3, "text": "testo originale"}
        with (
            patch.object(youtube_caption_tracks, "YouTubeTranscriptApi") as api,
            patch.object(spanish, "fetch", side_effect=ConnectionError("private")),
            patch.object(polish, "fetch") as empty,
            patch.object(italian, "fetch") as readable,
            patch.object(Transcript, "translate") as translate,
            patch.object(youtube_transcript, "_transcribe_audio_fallback") as audio,
            patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}),
        ):
            api.return_value.list.return_value = tracks
            empty.return_value.to_raw_data.return_value = []
            readable.return_value.to_raw_data.return_value = [cue]
            result = youtube_transcript.get_youtube_transcript("abcdefghijk", languages="uk,en,ru")
            self.assertIn("testo originale", result)
            self.assertIn("--- YouTube tool result ---", result)
            api.return_value.list.assert_called_once()
            readable.return_value.to_raw_data.return_value = [{"text": " "}]
            result = youtube_transcript.get_youtube_transcript("abcdefghijk", languages="uk,en,ru")
            self.assertIn("Caption lookup failed", result)
            self.assertNotIn("private", result)
            translate.assert_not_called()
            audio.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "media cache uses POSIX locks")
    def test_live_captions_first_and_reuse_only_matching_completed_supplement(self):
        from youtube_media_cache import open_video
        video_id = "abcdefghijk"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".aigan-media-cache").touch()
        fake_audio = root / "fixture"
        fake_audio.write_bytes(b"source")
        with open_video(root, 1234, video_id) as cache:
            cache.save_audio(fake_audio, 90000)
            cache.append_segment(0, 30000, "previous own start")
            cache.append_segment(30000, 90000, "previous own ending")
        with (
            patch.dict(os.environ, {"AIGAN_MEDIA_LIBRARY": str(root), "AIGAN_MEDIA_CHAT": "1234",
                                    "YOUTUBE_AUDIO_FALLBACK": "true"}),
            patch.object(youtube_transcript, "_fetch_caption_transcript") as captions,
            patch.object(youtube_transcript, "_transcribe_audio_fallback") as audio,
        ):
            captions.side_effect = ConnectionError("private detail")
            result = youtube_transcript.get_youtube_transcript(video_id)
            self.assertIn("Caption lookup failed", result)
            self.assertNotIn("previous own", result)
            self.assertNotIn("private detail", result)
            captions.side_effect = youtube_transcript.TranscriptsDisabled(video_id)
            result = youtube_transcript.get_youtube_transcript(video_id)
            self.assertIn("Saved audio transcription", result)
            self.assertIn("previous own start", result)
            captions.side_effect = None
            track = [{"start": 0, "duration": 30, "text": "native prefix"}]
            captions.return_value = track
            result = youtube_transcript.get_youtube_transcript(video_id)
            self.assertIn("native prefix", result)
            self.assertNotIn("previous own", result)
            with open_video(root, 1234, video_id) as cache:
                cache.save_public_transcript([(0, 30000, "native prefix")], duration_ms=90000,
                                            covered_ms=30000, complete=False, track_id="sample")
            result = youtube_transcript.get_youtube_transcript(video_id)
            self.assertIn("Official YouTube captions supplemented by saved audio transcription", result)
            self.assertIn("previous own ending", result)
            self.assertNotIn("previous own start", result)
            for replacement in ([{"start": 0, "duration": 90, "text": "native prefix"}],
                                [*track, {"start": 30, "duration": 60, "text": "new official ending"}]):
                captions.return_value = replacement
                result = youtube_transcript.get_youtube_transcript(video_id)
                self.assertNotIn("previous own", result)
                self.assertIn(replacement[-1]["text"], result)
            captions.side_effect = ConnectionError("private")
            self.assertIn("supplemented by saved", youtube_transcript.get_youtube_transcript(video_id))
            captions.side_effect = None
            captions.return_value = []
            with patch.dict(os.environ, {"AIGAN_MEDIA_CHAT": "4321", "YOUTUBE_AUDIO_FALLBACK": "false"}):
                result = youtube_transcript.get_youtube_transcript(video_id)
                self.assertNotIn("previous own", result)
                self.assertIn("fallback is disabled", result)
            audio.assert_not_called()

    def test_fuller_original_track_can_beat_a_truncated_preferred_language(self):
        preferred = MagicMock(language_code="uk", is_generated=False)
        preferred.fetch.return_value.to_raw_data.return_value = [
            {"start": 0, "duration": 5, "text": "pref one"}, {"start": 9, "duration": 2, "text": "pref end"}]
        other = MagicMock(language_code="de", is_generated=False)
        other.fetch.return_value.to_raw_data.return_value = [
            {"start": 0, "duration": 4, "text": "other one"}, {"start": 120, "duration": 2, "text": "other end"}]
        with patch.object(youtube_caption_tracks, "YouTubeTranscriptApi") as api:
            api.return_value.list.return_value = [other, preferred]
            result = youtube_transcript.get_youtube_transcript("abcdefghijk", languages="uk,en")
            self.assertIn("other end", result)
            self.assertNotIn("pref end", result)
            other.fetch.return_value.to_raw_data.return_value[-1]["start"] = 10
            result = youtube_transcript.get_youtube_transcript("abcdefghijk", languages="uk,en")
            self.assertIn("pref end", result)
            self.assertNotIn("other end", result)
        preferred.translate.assert_not_called()
        other.translate.assert_not_called()

    def test_complete_preferred_track_stops_after_the_existing_player_response(self):
        video_id = "abcdefghijk"
        preferred = MagicMock(language_code="uk", is_generated=False)
        preferred.fetch.return_value.to_raw_data.return_value = [
            {"start": 0, "duration": 2, "text": "first"}, {"start": 115, "duration": 4, "text": "ending"}]
        other = MagicMock(language_code="es", is_generated=True)
        def listed(*, http_client):
            def list_tracks(requested_id):
                self.assertEqual(requested_id, video_id)
                player = MagicMock(status_code=200, url="https://www.youtube.com/youtubei/v1/player")
                player.json.return_value = {"videoDetails": {"videoId": video_id, "lengthSeconds": "120"}}
                http_client.hooks["response"][0](player)
                return [other, preferred]
            return SimpleNamespace(list=list_tracks)
        with patch.object(youtube_caption_tracks, "YouTubeTranscriptApi", side_effect=listed) as api:
            record = youtube_caption_tracks.lookup_original_track(video_id)
        self.assertEqual((record.language_code, record.duration_ms), ("uk", 120000))
        self.assertIn("ending", [row[2] for row in record.cues])
        api.assert_called_once()
        preferred.fetch.assert_called_once()
        other.fetch.assert_not_called()

    def test_player_duration_of_another_video_or_endpoint_is_ignored(self):
        with youtube_caption_tracks._CaptionSession("abcdefghijk") as session:
            reply = MagicMock(status_code=200, url="https://www.youtube.com/youtubei/v1/player")
            reply.json.return_value = {"videoDetails": {"videoId": "ABCDEFGHIJK", "lengthSeconds": "15"}}
            session._capture_duration(reply)
            self.assertIsNone(session.duration_ms)
            reply.url = "https://www.youtube.com/api/timedtext"
            reply.json.return_value["videoDetails"]["videoId"] = "abcdefghijk"
            session._capture_duration(reply)
            self.assertIsNone(session.duration_ms)

    @unittest.skipUnless(os.name == "posix", "media cache uses POSIX locks")
    def test_complete_live_platform_track_is_saved_and_reused_only_in_the_same_chat(self):
        from youtube_media_cache import open_video
        video_id = "abcdefghijk"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".aigan-media-cache").touch()
        track = youtube_caption_tracks.OriginalCaptionTrack(
            "ko", True, 60000, ((0, 5000, "beginning"), (54000, 59000, "stored ending")))
        with (
            patch.dict(os.environ, {"AIGAN_MEDIA_LIBRARY": str(root), "AIGAN_MEDIA_CHAT": "1234",
                                    "YOUTUBE_AUDIO_FALLBACK": "false"}),
            patch.object(youtube_transcript, "lookup_original_track", return_value=track) as lookup,
        ):
            first = youtube_transcript.get_youtube_transcript(video_id)
            self.assertIn("stored ending", first)
            self.assertIn("stored ending", youtube_transcript.get_youtube_transcript(video_id))
            lookup.assert_called_once_with(video_id, ["uk", "en", "ru"])
            lookup.side_effect = ConnectionError("unavailable")
            with patch.dict(os.environ, {"AIGAN_MEDIA_CHAT":"4321"}):
                second = youtube_transcript.get_youtube_transcript(video_id)
                self.assertNotIn("stored ending", second)
                self.assertIn("Caption lookup failed", second)
                self.assertEqual(lookup.call_count, 2)
            lookup.side_effect = None
            with patch.dict(os.environ, {"AIGAN_MEDIA_CHAT":"7777"}), open_video(root, 7777, video_id) as held:
                self.assertIn("stored ending", youtube_transcript.get_youtube_transcript(video_id))
                self.assertIsNone(held.public_caption_document())
            lookup.return_value = youtube_caption_tracks.OriginalCaptionTrack(
                "es", False, 60000, ((0, 59000, "explicit alternative"),))
            self.assertIn("explicit alternative", youtube_transcript.get_youtube_transcript(video_id, languages="es"))
            lookup.assert_called_with(video_id, ["es"])

    def test_caption_http_limit_counts_decoded_body_and_closes_failed_response(self):
        with youtube_caption_tracks._CaptionSession("abcdefghijk") as session:
            response = MagicMock()
            response.iter_content.return_value = iter([b"1234", b"56"])
            session.remaining_bytes = 5
            with self.assertRaises(youtube_caption_tracks.CaptionTrackUnavailable):
                session._read_response(response)
            response.close.assert_called_once()

    def test_skill_guidance_is_confined_to_the_actual_tool_result(self):
        from tests.support import configure_test_environment
        configure_test_environment()
        import main
        hint = (Path(youtube_transcript.__file__).resolve().parents[1] /
                "prompts" / "youtube_transcript.md").read_text(encoding="utf-8").strip()
        self.assertTrue(hint)
        schema = next(tool for tool in asyncio.run(youtube_transcript.mcp.list_tools())
                      if tool.name == "get_youtube_transcript")
        self.assertNotIn(hint, schema.model_dump_json())
        self.assertNotIn(hint, main.SYSTEM_PROMPT)
        with patch.object(youtube_transcript, "_fetch_caption_transcript", return_value=[]), \
             patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK":"false"}):
            result = youtube_transcript.get_youtube_transcript("abcdefghijk")
        self.assertTrue(result.startswith(hint + "\n\n--- YouTube tool result ---\n"))
        self.assertIn("No caption transcript", result)

    def test_unavailable_audio_is_not_reported_as_transcribed(self):
        for duration in (None, 0, 61):
            with (
                self.subTest(duration=duration),
                patch.dict(os.environ, {"YOUTUBE_AUDIO_FALLBACK": "true"}),
                patch.dict(sys.modules, fake_dependency_modules(duration=duration)),
                patch.object(youtube_transcript, "_fetch_caption_transcript", return_value=[]),
            ):
                result = youtube_transcript.get_youtube_transcript("abcdefghijk")
                self.assertIn("Audio transcription unavailable", result)
                self.assertNotIn("Audio transcription.", result)

    def setUp(self) -> None:
        self.original_store = youtube_transcript.MODEL_TELEMETRY
        self.env = patch.dict(
            os.environ,
            {
                "AIGAN_MODEL_RUN_ID": "a" * 32,
                "AIGAN_MODEL_ROUTE_BUCKET": "tool",
                "MODEL_ROUTING_POLICY_VERSION": "primary_sol_low_v1",
                "YOUTUBE_MAX_DURATION_SECONDS": "60",
                "YOUTUBE_TRANSCRIPTION_MODEL": "gpt-4o-mini-transcribe",
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        youtube_transcript.MODEL_TELEMETRY = self.original_store
        self.env.stop()

    def new_store(self, directory: str) -> ModelTelemetryStore:
        store = ModelTelemetryStore(Path(directory) / "telemetry.sqlite3")
        youtube_transcript.MODEL_TELEMETRY = store
        return store

    def test_token_usage_is_correlated_without_claiming_actual_model(self) -> None:
        usage = SimpleNamespace(
            type="tokens",
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            input_token_details=SimpleNamespace(cached_tokens=10),
        )
        response = SimpleNamespace(text="transcribed text", usage=usage)
        with tempfile.TemporaryDirectory() as directory:
            store = self.new_store(directory)
            try:
                with patch.dict(
                    sys.modules,
                    fake_dependency_modules(response=response),
                ):
                    result = youtube_transcript._transcribe_audio_fallback(
                        "https://www.youtube.com/watch?v=PRIVATE_VIDEO_MARKER",
                        16000,
                    )

                self.assertEqual("transcribed text", result)
                stages = store.latest_stages()
                self.assertEqual(1, len(stages))
                stage = stages[0]
                self.assertEqual("a" * 32, stage.run_id)
                self.assertEqual("transcription", stage.stage_kind)
                self.assertEqual("gpt-4o-mini-transcribe", stage.intended_model)
                self.assertEqual("", stage.actual_model)
                self.assertEqual("not_reported", stage.actual_model_source)
                self.assertEqual("audio_transcriptions", stage.endpoint)
                self.assertEqual("succeeded", stage.status)
                self.assertEqual("reported", stage.usage_status)
                self.assertEqual(100, stage.input_tokens)
                self.assertEqual(10, stage.cached_input_tokens)
                self.assertEqual(20, stage.output_tokens)
                self.assertEqual(120, stage.total_tokens)
                self.assertIsNotNone(stage.estimated_cost_nano_usd)
                self.assertNotIn("PRIVATE_VIDEO_MARKER", repr(stage))
                self.assertEqual("tool", stage.route_bucket)
                self.assertEqual("youtube_transcript", stage.task_class_bucket)
                policy_version = store._conn.execute(
                    "SELECT policy_version FROM model_telemetry_runs WHERE run_id = ?",
                    ("a" * 32,),
                ).fetchone()[0]
                self.assertEqual("primary_sol_low_v1", policy_version)
            finally:
                store.close()

    def test_duration_usage_is_not_misreported_as_tokens_or_cost(self) -> None:
        response = SimpleNamespace(
            text="transcribed text",
            usage=SimpleNamespace(type="duration", seconds=12.5),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = self.new_store(directory)
            try:
                with patch.dict(
                    sys.modules,
                    fake_dependency_modules(response=response),
                ):
                    youtube_transcript._transcribe_audio_fallback(
                        "https://youtu.be/PRIVATE_DURATION",
                        16000,
                    )

                stage = store.latest_stages()[0]
                self.assertEqual("duration", stage.usage_status)
                self.assertEqual(12500, stage.audio_duration_ms)
                self.assertIsNone(stage.input_tokens)
                self.assertIsNone(stage.output_tokens)
                self.assertIsNone(stage.estimated_cost_nano_usd)
                self.assertEqual("unsupported_unit", stage.cost_status)
            finally:
                store.close()

    def test_api_failure_records_only_sanitized_failure_class(self) -> None:
        private_marker = "PRIVATE_URL_PATH_AND_EXCEPTION_MARKER"
        error = SecretTranscriptionFailure(private_marker)
        with tempfile.TemporaryDirectory() as directory:
            store = self.new_store(directory)
            try:
                with patch.dict(
                    sys.modules,
                    fake_dependency_modules(error=error),
                ):
                    with self.assertRaises(SecretTranscriptionFailure):
                        youtube_transcript._transcribe_audio_fallback(
                            f"https://www.youtube.com/watch?v={private_marker}",
                            16000,
                        )

                stage = store.latest_stages()[0]
                self.assertEqual("failed", stage.status)
                self.assertEqual("secrettranscriptionfailure", stage.failure_class)
                self.assertEqual("captions_unavailable", stage.fallback_reason)
                self.assertEqual("missing", stage.usage_status)
                self.assertNotIn(private_marker, repr(stage))
            finally:
                store.close()

    def test_telemetry_sink_failure_never_changes_transcription_result(self) -> None:
        response = SimpleNamespace(text="still works", usage=None)
        modules = fake_dependency_modules(response=response)
        for failure_point in ("begin", "finish"):
            with self.subTest(failure_point=failure_point):
                youtube_transcript.MODEL_TELEMETRY = ExplodingTelemetry(
                    explode_at=failure_point
                )
                with patch.dict(sys.modules, modules):
                    result = youtube_transcript._transcribe_audio_fallback(
                        "https://youtu.be/PRIVATE_TELEMETRY_FAILURE",
                        16000,
                    )
                self.assertEqual("still works", result)

    def test_store_builder_requires_explicit_enablement_and_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "telemetry.sqlite3")
            with patch.dict(
                os.environ,
                {
                    "MODEL_TELEMETRY_ENABLED": "true",
                    "MODEL_TELEMETRY_DB_PATH": db_path,
                    "MODEL_TELEMETRY_RETENTION_DAYS": "7",
                },
            ):
                store = youtube_transcript._build_model_telemetry_store()
            self.assertIsNotNone(store)
            assert store is not None
            try:
                self.assertEqual(7, store.retention_days)
            finally:
                store.close()

    def test_direct_script_opens_database_only_on_first_transcription(self) -> None:
        script_path = Path(youtube_transcript.__file__).resolve()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment["MODEL_TELEMETRY_ENABLED"] = "true"
        environment["MODEL_TELEMETRY_DB_PATH"] = str(Path(directory.name) / "stages.sqlite")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os, runpy; from pathlib import Path; "
                    "m = runpy.run_path('youtube_transcript.py', run_name='direct_import_test'); "
                    "db = Path(os.environ['MODEL_TELEMETRY_DB_PATH']); "
                    "assert not db.exists(); "
                    "m['_begin_transcription_stage']('gpt-transcribe'); "
                    "assert db.exists()"
                ),
            ],
            cwd=script_path.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(0, completed.returncode)


if __name__ == "__main__":
    unittest.main()
