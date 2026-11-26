"""A bounded official storyboard, timed by the provider's tile interval."""
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
