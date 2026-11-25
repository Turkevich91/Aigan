"""Network-free coverage for public clip acquisition, policy and cleanup."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import httpx

import public_video_acquisition as acq
from tests.test_tiktok_acquisition import MP4, response


PAGE = "https://example.org/articles/clip"
MEDIA = "https://media.example.org/sample.mp4"


class PublicVideoTests(IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "clip.mp4"
        self.requests = []

    def serve(self, responses):
        @asynccontextmanager
        async def get(url):
            self.requests.append(url)
            result = responses[url]
            try:
                yield result
            finally:
                await result.aclose()
        return patch.object(acq, "pinned_get", get)

    async def fetch(self, url=PAGE, before_call=lambda: None):
        await acq.fetch_public_video(url, self.path, max_bytes=4096, max_duration=60, before_call=before_call)

    def test_url_checks_are_lexical_admit_public_hosts_and_canonicalize(self):
        self.assertEqual(acq.normalize_public_video_url("https://EXAMPLE.org:443/clip?sig=a%26b#part"),
                         "https://example.org/clip?sig=a%26b")
        self.assertEqual(acq.normalize_public_video_url("http://example.org:80/é"), "http://example.org/é")
        for url in ["https://127.0.0.1/", "http://[::1]/", "http://localhost/", "http://host.internal/",
                    "https://x.yandex.ru./video", "https://rt.com/video", "ftp://example.org/clip", "https://host:2/",
                    "https://u@video.example.org/", "https://video.example.org:999/", "https://example.org\\@other.org",
                    "https://m.youtube.com/v", "https://a.tiktokcdn-eu.com/v", "https://r.googlevideo.com/x",
                    "https://instagram.com/", "https://video.twimg.com/foo", "https://a.tiktok.com/v", PAGE + "\n"]:
            with self.subTest(url=url):
                self.assertIsNone(acq.normalize_public_video_url(url))

    async def test_direct_media_uses_hop_authorization(self):
        check = Mock()
        with self.serve({PAGE: response(302, location=MEDIA),
                         MEDIA: response(body=MP4, mime="video/mp4")}):
            await self.fetch(before_call=check)
        self.assertEqual(self.requests, [PAGE, MEDIA])
        self.assertEqual(self.path.read_bytes(), MP4)
        self.assertEqual(check.call_count, 3)

    async def test_one_byte_http_fragments_preserve_the_mp4_signature(self):
        class FragmentedResponse(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in MP4:
                    yield bytes((byte,))

        reply = httpx.Response(200, headers={"content-type": "video/mp4"}, stream=FragmentedResponse())
        with self.serve({MEDIA: reply}):
            await self.fetch(MEDIA)
        self.assertEqual(self.path.read_bytes(), MP4)

    async def test_selects_only_actual_player_and_resolves_base_after_redirect(self):
        canonical = "https://media.example.org/article/"
        html = b"""<base href="../"><template><video src="bait.mp4"></video></template>
        <script>var demo = '<video src="ignored.mp4">';</script>
        <div style="display: none"><video src="decoy.mp4"></video></div><video controls>
        <source src="ignored.webm" type="video/webm"><source src="sample.mp4" type="video/mp4"></video>"""
        with self.serve({PAGE: response(302, location=canonical), canonical: response(body=html),
                         MEDIA: response(body=MP4, mime="application/octet-stream")}):
            await self.fetch()
        self.assertEqual(self.path.read_bytes(), MP4)
        self.assertEqual(self.requests, [PAGE, canonical, MEDIA])

    async def test_denials_dead_links_and_corrupt_data_do_not_retry(self):
        for url, table, reason in [
            (PAGE, {PAGE: response(404)}, "not_found"),
            (PAGE, {PAGE: response(410)}, "gone"),
            (PAGE, {PAGE: response(200, **{"cf-mitigated":"challenge"})}, "denied"),
            (MEDIA, {MEDIA: response(403)}, "denied"),
            (MEDIA, {MEDIA: response(body=b'<video src="/advert.mp4"></video>')}, "unsupported"),
            (PAGE, {PAGE: response(body=b'<video controlslist="nodownload" src="/x.mp4">')}, "denied"),
            (PAGE, {PAGE: response(body=b'<video src="/1.mp4"></video><video src="/2.mp4"></video>')}, "ambiguous"),
            (PAGE, {PAGE: response(302, location="https://foo.su/clip")}, "url"),
            (PAGE, {PAGE: response(body=b'<video src="https://m.youtube.com/clip">')}, "url"),
            (PAGE, {PAGE: response(body=b'<video><source type="audio/ogg" src="/other"></video>')}, "unsupported"),
            (PAGE, {PAGE: response(body=b"<div id='player'></div><script>loadPlayer()</script>")}, "no_player"),
            (MEDIA, {MEDIA: response(body=b"#EXTM3U", mime="video/mp4")}, "unsupported"),
            (MEDIA, {MEDIA: response(body=MP4, mime="video/mp4", **{"content-encoding":"gzip"})}, "unsupported"),
            (MEDIA, {MEDIA: response(body=MP4, mime="video/mp4", **{"content-length":"4097"})}, "limits"),
            (MEDIA, {MEDIA: response(body=MP4, mime="video/mp4", **{"content-length":"600"})}, "limits"),
            (MEDIA, {MEDIA: response(body=MP4 + bytes(4096), mime="video/mp4")}, "limits"),
            (PAGE, {PAGE: response(body=b"a", **{"content-length":str(acq._PAGE_BYTES + 1)})}, "limits"),
        ]:
            with self.subTest(reason=reason), self.serve(table):
                self.requests.clear()
                with self.assertRaisesRegex(acq.PublicVideoUnavailable, "^" + reason + "$"):
                    await self.fetch(url)
                self.assertEqual(self.requests, [url])
                self.assertFalse(self.path.exists())

    async def test_preserves_existing_file_and_removes_transfers_revoked_or_cancelled(self):
        self.path.write_bytes(b"existing")
        with self.serve({MEDIA: response(body=MP4, mime="video/mp4")}):
            with self.assertRaises(acq.PublicVideoUnavailable):
                await self.fetch(MEDIA)
        self.assertEqual(self.path.read_bytes(), b"existing")
        self.path.unlink()
        with self.serve({PAGE: response(body=b'<video src="/sample.mp4">')}):
            self.requests.clear()
            with self.assertRaisesRegex(RuntimeError, "revoked"):
                await self.fetch(PAGE, Mock(side_effect=[None, RuntimeError("revoked")]))
            self.assertEqual(self.requests, [PAGE])
        with self.serve({MEDIA: response(body=MP4, mime="video/mp4")}):
            with self.assertRaisesRegex(RuntimeError, "revoked"):
                await self.fetch(MEDIA, Mock(side_effect=[None, RuntimeError("revoked")]))
        self.assertFalse(self.path.exists())
        started = asyncio.Event()

        class SlowResponse(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield MP4
                started.set()
                await asyncio.Event().wait()

        slow = httpx.Response(200, headers={"content-type":"video/mp4"}, stream=SlowResponse())
        with self.serve({MEDIA: slow}):
            task = asyncio.create_task(self.fetch(MEDIA))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(self.path.exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.path.exists())
