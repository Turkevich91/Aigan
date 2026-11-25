"""Real seek positions, visual-track duration, request isolation and extraction budgets."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from media_frames import CommandOutput, FfmpegMediaFrameAdapter, MediaFrameRequest, parse_ffprobe_metadata, run_command
from telegram_video import VideoUnavailable, inspect_clip


class SparseTelegramTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.directory / "source.mp4"
        self.requests = []

        def runner(command, timeout):
            self.requests.append((command, timeout))
            return run_command(command, timeout)

        self.adapter = FfmpegMediaFrameAdapter(command_runner=runner)

    def media(self, *args):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *args, str(self.source)],
                       check=True, timeout=15)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires media tools")
    async def test_sparse_mp4_matches_real_screen_colors_and_five_timestamped_seeks(self):
        self.media("-f", "lavfi", "-i", "color=red:r=1:s=96x64:d=600",
                   "-f", "lavfi", "-i", "color=green:r=1:s=96x64:d=600",
                   "-f", "lavfi", "-i", "color=blue:r=1:s=96x64:d=600",
                   "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
                   "-g", "20", "-c:v", "mpeg4")
        req = MediaFrameRequest(self.source, max_duration_seconds=90, sparse_max_duration_seconds=1800)
        async with self.adapter.frame_session(req) as result:
            self.assertTrue(result.ok, result.failure_category)
            self.assertEqual("ffmpeg_sparse", result.backend)
            self.assertEqual(5, len(result.frames))
            times = [f.timestamp_seconds for f in result.frames]
            self.assertEqual([0, 450, 900, 1350, 1799], times)
            for slot, channel in ((0, 0), (2, 1), (4, 2)):
                with Image.open(result.frames[slot].path) as im:
                    rgb = im.getpixel((16, 16))
                    self.assertEqual(rgb.index(max(rgb)), channel)
            workdir = result.frames[0].path.parent
        self.assertFalse(workdir.exists())
        seeks = self.requests[1:]
        self.assertEqual(5, len(seeks))
        for command, timeout in seeks:
            self.assertLess(command.index("-ss"), command.index("-i"))
            self.assertEqual("1", command[command.index("-frames:v") + 1])
            self.assertLessEqual(timeout, 30)
        self.assertLess(seeks[-1][1], seeks[0][1])
        for cap in (0, 120):
            async with self.adapter.frame_session(MediaFrameRequest(self.source, sparse_max_duration_seconds=cap)) as failure:
                self.assertEqual("duration_too_long", failure.failure_category)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "requires media tools")
    async def test_video_document_with_short_picture_and_long_audio_uses_only_visual_track(self):
        self.media("-itsoffset", "10", "-f", "lavfi", "-i", "color=green:r=1:s=96x64:d=5", "-f", "lavfi", "-i",
                   "anullsrc=r=8000:cl=mono:d=140", "-c:v", "mpeg4", "-fps_mode:v", "passthrough", "-c:a", "aac")
        # Telegram documents have a size and no duration field.
        remote = AsyncMock()
        remote.file_size = self.source.stat().st_size
        async def download(custom_path):
            shutil.copyfile(self.source, custom_path)
        remote.download_to_drive.side_effect = download
        attachment = SimpleNamespace(file_size=remote.file_size, get_file=AsyncMock(return_value=remote))
        answer = AsyncMock(return_value="green")
        transcribe = AsyncMock()
        opts = dict(request="Describe the clip", max_bytes=20_000_000, max_duration=90, ffmpeg="ffmpeg",
                    answer=answer, transcribe=transcribe, adapter=self.adapter, sparse_max_duration=120)
        self.assertEqual("green", await inspect_clip(attachment, mode="frames", **opts))
        content = answer.await_args.args[0]
        timestamps = [float(part["text"].split()[1]) for part in content if part.get("text", "").startswith("Approx.")]
        self.assertEqual([10, 11, 12, 13, 14], timestamps)
        for mode in ("speech", "both"):
            with self.assertRaisesRegex(VideoUnavailable, "90 с"):
                await inspect_clip(attachment, mode=mode, **opts)
        transcribe.assert_not_awaited()

    async def test_missing_video_duration_and_seek_timeout_leave_no_frames(self):
        self.source.write_bytes(b"local input")
        def probe(duration):
            return CommandOutput(0, json.dumps({"streams": [{"codec_type": "video", "width":64, "height":48,
                "duration":duration, "index":0},{"codec_type":"audio"}], "format":{"duration":"150"}}), "")
        for value in ("inf", "nan"):
            with self.assertRaises(RuntimeError):
                parse_ffprobe_metadata(probe(value).stdout)
        for duration, expected in ((None, "metadata_failed"), ("1801", "duration_too_long"), ("150", "timeout")):
            workdirs = []
            def run(command, timeout):
                if command[0] == "ffprobe":
                    return probe(duration)
                workdirs.append(Path(command[-1]).parent)
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            adapter = FfmpegMediaFrameAdapter(command_runner=run)
            async with adapter.frame_session(MediaFrameRequest(self.source, sparse_max_duration_seconds=1800)) as result:
                self.assertEqual(expected, result.failure_category)
                self.assertFalse(result.frames)
            self.assertFalse(any(d.exists() for d in workdirs))
