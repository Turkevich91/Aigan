"""A bounded official storyboard, timed by the provider's tile interval."""
from contextlib import asynccontextmanager
import io
import json
import os
import shutil
import struct
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import youtube_timestamp_frames as module
from youtube_media_cache import open_video


VIDEO = "abcDEFG_123"
TEMPLATE = f"https://i.ytimg.com/sb/{VIDEO}/storyboard3_L$L/$N.jpg?test=fixture"
LEVELS = "|48#27#100#10#10#0#default#fixture|32#18#1022#3#3#10000#M$M#fixture"


def page(levels=LEVELS, template=TEMPLATE, **changes):
    player = {"playabilityStatus": {"status": "OK"}, "videoDetails": {"videoId": VIDEO, "lengthSeconds": "10208"},
              "storyboards": {"playerStoryboardSpecRenderer": {"spec": template + levels}}}
    player.update(changes)
    return b'<script>var ytInitialPlayerResponse = ' + json.dumps(player).encode() + b';</script>'


def sheet(rows=3):
    image = Image.new("RGB", (96, rows * 18))
    for i in range(3 * rows):
        x, y = (i % 3) * 32, (i // 3) * 18
        image.paste((i * 20, 32, 0), (x, y, x + 32, y + 18))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=97)
    return buf.getvalue()


@unittest.skipUnless(os.name == "posix", "cache requires POSIX locking")
class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        (root / ".aigan-media-cache").touch()
        self.cache = self.enterContext(open_video(root, 100, VIDEO))
        self.requests = []
        self.page = page()
        self.sheets = {}

        async def read(url, mime, limit, before_call):
            if before_call:
                before_call()
            self.requests.append(url)
            if url == "https://www.youtube.com/watch?v=" + VIDEO:
                return self.page
            key = url.split("/")[-1].split("?")[0]
            return self.sheets.get(key, sheet())
        self.enterContext(patch.object(module, "_read", read))

    async def fetch(self, timestamp):
        return await module.youtube_preview_frames(VIDEO, timestamp, self.cache)

    async def test_provider_interval_selects_target_and_reuses_verified_frames(self):
        frames = await self.fetch(9000000)
        self.assertEqual([f.timestamp_ms for f in frames], [8980000, 8990000, 9000000, 9010000, 9020000])
        self.assertEqual([p.split("/")[-1].split("?")[0] for p in self.requests[1:]], ["M99.jpg", "M100.jpg"])
        for f in frames:
            self.assertEqual(f.origin_label, "YouTube preview")
            with Image.open(f.path) as im:
                self.assertEqual(im.size, (32, 18))
                index = f.timestamp_ms // 10000 % 9
                self.assertAlmostEqual(im.getpixel((16, 9))[0], 20 * index, delta=7)
        self.requests.clear()
        self.assertEqual(await self.fetch(9000000), frames)
        self.assertEqual(len(self.requests), 1)

    async def test_first_and_last_partial_sheets_and_uncovered_time(self):
        self.assertEqual([f.timestamp_ms for f in await self.fetch(0)], [0, 10000, 20000])
        self.sheets["M113.jpg"] = sheet(rows=2)
        tail = await self.fetch(10208000)
        self.assertEqual([f.timestamp_ms for f in tail], [10180000, 10190000, 10200000])
        self.assertEqual(self.requests[-1].split("/")[-1].split("?")[0], "M113.jpg")
        for t in (-1, 10208001, True):
            with self.subTest(t=t), self.assertRaises(module.YouTubePreviewUnavailable):
                await self.fetch(t)
        self.page = page(levels=LEVELS.replace("#1022#", "#100#"))
        with self.assertRaisesRegex(module.YouTubePreviewUnavailable, "outside storyboard"):
            await self.fetch(9000000)

    async def test_rejects_private_or_mismatched_posts_and_untrusted_templates(self):
        for body in (page(playabilityStatus={"status": "LOGIN_REQUIRED"}),
                     page(videoDetails={"videoId": VIDEO, "lengthSeconds": "100", "isPrivate": True}),
                     page(videoDetails={"videoId": "b" * 11, "lengthSeconds": "100"}),
                     page(template=TEMPLATE.replace("i.ytimg.com", "not-i.ytimg.com")),
                     page(template=TEMPLATE.replace("i.ytimg.com", "i.ytimg.com:443")),
                     page(template=TEMPLATE.replace(VIDEO, "elsewhere")),
                     page(levels=LEVELS.replace("10000#M$M", "0#M$M")),
                     page(levels=LEVELS.replace("32#18#", "2000#1800#"))):
            with self.subTest(body=body), self.assertRaises(module.YouTubePreviewUnavailable):
                self.requests.clear()
                self.page = body
                await self.fetch(1000)
            self.assertEqual(len(self.requests), 1)
        self.requests.clear()
        with self.assertRaises(module.YouTubePreviewUnavailable):
            await module.youtube_preview_frames("https://example.invalid/a", 0, self.cache)
        self.assertFalse(self.requests)

    async def test_rejects_truncated_image_or_unexpected_geometry_before_cache(self):
        for payload in (b"unusable", sheet()[:-80], sheet(rows=1)):
            self.sheets["M0.jpg"] = payload
            with self.subTest(length=len(payload)), self.assertRaises(module.YouTubePreviewUnavailable):
                await self.fetch(1000)
            self.assertIsNone(self.cache.frame(0))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_range_accepts_mixed_case_identity_encoding(self):
        @asynccontextmanager
        async def get(url, *, byte_range):
            self.assertEqual(byte_range, (0, 2))
            yield httpx.Response(206, headers={"Content-Range": "bytes 0-2/8", "Content-Encoding": "Identity"},
                                 stream=httpx.ByteStream(b"abc"))
        with patch.object(module, "pinned_get", get):
            self.assertEqual(await module._range(TEMPLATE, 0, 2, 8, None), (b"abc", 8))

    async def test_redirects_and_large_bodies_are_rejected_without_second_request(self):
        for code, headers in ((302, {"location": "https://example.invalid/"}),
                              (200, {"content-type": "image/jpeg", "content-length": "3000000"})):
            calls = []
            @asynccontextmanager
            async def get(url):
                calls.append(url)
                yield httpx.Response(code, headers=headers)
            with patch.object(module, "pinned_get", get), self.assertRaises((
                    module.YouTubePreviewUnavailable, module.PublicMediaTransferError)):
                await module._read(TEMPLATE, "image/jpeg", 2000000, None)
            self.assertEqual(calls, [TEMPLATE])


@unittest.skipUnless(os.name == "posix" and shutil.which("ffmpeg"), "requires POSIX media tools")
class DecodedFrameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        (self.root / ".aigan-media-cache").touch()

    async def test_decodes_selected_fragments_on_original_clock_and_reopens_offline(self):
        source = self.root / "generated.mp4"
        await module.asyncio.to_thread(subprocess.run, ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=25:duration=24", "-an", "-c:v", "libx264",
                        "-threads", "1", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                        "-movflags", "dash+global_sidx", str(source)], check=True)
        media = source.read_bytes()
        remote = "https://fixture.googlevideo.com/videoplayback"
        reads = []
        async def read(url, first, last, expected_size, before):
            self.assertEqual(url, remote)
            reads.append((first, last))
            self.assertIn(expected_size, (None, len(media)))
            return media[first:last + 1], len(media)
        native = module._command
        async def command(args, **kwargs):
            if args[0] == module.sys.executable:
                return json.dumps({"url": remote, "available": 0, "duration": 24000}).encode()
            return await native(args, **kwargs)
        with open_video(self.root, 5, VIDEO) as cache, patch.object(module, "_range", read), \
                patch.object(module, "_command", command), patch.object(module, "_read", side_effect=AssertionError):
            result = await module.youtube_frames(VIDEO, 16000, cache)
            self.assertEqual([f.timestamp_ms for f in result], [12000, 14000, 16000, 18000, 20000])
            self.assertEqual({f.origin_label for f in result}, {"YouTube video frame"})
            self.assertEqual(len(reads), 2)
            self.assertGreater(reads[1][0], 1000)
            self.assertLess(reads[1][1] - reads[1][0], len(media))
            for frame in result:
                with Image.open(frame.path) as img:
                    self.assertEqual(img.size, (320, 180))
            self.assertFalse(list(cache.directory.glob(".video-job-*")))
        with open_video(self.root, 5, VIDEO) as cache, patch.object(module, "_section_frames", side_effect=AssertionError), \
                patch.object(module, "_read", side_effect=AssertionError):
            self.assertEqual(await module.youtube_frames(VIDEO, 16000, cache), result)

    async def test_unavailable_native_media_caches_explicit_preview_set(self):
        with open_video(self.root, 8, VIDEO) as cache:
            source = self.root / "test.jpg"
            source.write_bytes(sheet())
            path = cache.save_frame(10000, source, kind="preview")
            previews = (module.PreviewFrame(10000, path),)
            with patch.object(module, "_section_frames", side_effect=module._SectionUnavailable), \
                    patch.object(module, "youtube_preview_frames", AsyncMock(return_value=previews)):
                self.assertEqual(await module.youtube_frames(VIDEO, 15000, cache), previews)
            self.assertIsNone(cache.frame_set(15000, kind="decoded"))
            with patch.object(module, "_section_frames", side_effect=AssertionError), \
                    patch.object(module, "youtube_preview_frames", side_effect=AssertionError):
                self.assertEqual(await module.youtube_frames(VIDEO, 15000, cache), previews)
                with self.assertRaises(PermissionError):
                    await module.youtube_frames(VIDEO, 15000, cache, before_call=lambda: self._deny())

    async def test_revoked_permission_in_a_native_call_cannot_downgrade_to_previews(self):
        with open_video(self.root, 5, VIDEO) as cache, patch.object(module, "_section_frames", side_effect=PermissionError), \
                patch.object(module, "youtube_preview_frames", side_effect=AssertionError):
            with self.assertRaises(PermissionError):
                await module.youtube_frames(VIDEO, 15000, cache)

    @staticmethod
    def _deny():
        raise PermissionError("revoked")

    def test_rejects_hierarchical_or_oversized_media_indexes(self):
        def idx(span):
            payload = (bytes(4) + struct.pack(">II", 1, 1000) + struct.pack(">IIHH", 0, 0, 0, 1) +
                       struct.pack(">III", span, 1000, 0))
            return struct.pack(">I4s", 8, b"ftyp") + struct.pack(">I4s", len(payload) + 8, b"sidx") + payload
        for span in (0, 20_000_000, 0x80000001):
            with self.subTest(span=span), self.assertRaises(module._SectionUnavailable):
                module._section_index(idx(span), 30_000_000, 1000, [0])
