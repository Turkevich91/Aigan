import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_servers import youtube_transcript
from model_telemetry import ModelTelemetryStore


class SecretTranscriptionFailure(RuntimeError):
    pass


def fake_dependency_modules(*, response: object | None = None, error: Exception | None = None):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, video_url, download=False):
            return {"duration": 30}

        def download(self, video_urls):
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
    def setUp(self) -> None:
        self.original_store = youtube_transcript.MODEL_TELEMETRY
        self.env = patch.dict(
            os.environ,
            {
                "AIGAN_MODEL_RUN_ID": "a" * 32,
                "AIGAN_MODEL_ROUTE_BUCKET": "tool",
                "MODEL_ROUTING_POLICY_VERSION": "telemetry_v1",
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

    def test_direct_script_import_resolves_project_modules_without_pythonpath(self) -> None:
        script_path = Path(youtube_transcript.__file__).resolve()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["MODEL_TELEMETRY_ENABLED"] = "false"
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import runpy; "
                    "runpy.run_path('youtube_transcript.py', run_name='direct_import_test')"
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
