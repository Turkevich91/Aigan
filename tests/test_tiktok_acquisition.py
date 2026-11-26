"""Public TikTok acquisition boundaries with disconnected HTTP and DNS fixtures."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx

import tiktok_acquisition as acq


POST_ID = "1" * 19
PAGE = "https://www.tiktok.com/@fixture/video/" + POST_ID
SHORT = "https://vt.tiktok.com/Zfixture/"
MEDIA = "https://v16-webapp-fixture.us.tiktok.com/media/fixture"
MP4 = b"\x00\x00\x00\x18ftypisom" + bytes(40)


def page(**item_changes):
    item = {"id": POST_ID, "video": {"duration": 4, "downloadAddr": MEDIA}}
    item.update(item_changes)
    state = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"statusCode": 0, "itemInfo": {"itemStruct": item}}}}
    return b'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">' + json.dumps(state).encode() + b"</script>"


class Stream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


def response(code=200, body=b"", mime="text/html", **headers):
    return httpx.Response(code, headers={"content-type": mime, **headers}, stream=Stream(body))


class TikTokAcquisitionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "clip.mp4"
        self.calls = []
        self.cookies_sent = []

    def route(self, responses):
        @asynccontextmanager
        async def get(url, referrer, *, cookie_header=None):
            self.cookies_sent.append(cookie_header)
            self.calls.append((url, referrer))
            result = responses[url]
            try:
                yield result
            finally:
                await result.aclose()
        return patch.object(acq, "_get", get)

    async def fetch(self, url=PAGE, limit=1024):
        await acq.fetch_tiktok_video(url, self.path, max_bytes=limit, max_duration=10)

    def test_only_recognizes_specific_https_video_and_share_urls(self):
        for url in [PAGE, PAGE.replace("www.", ""), PAGE + "/?tracking=x#more"]:
            self.assertEqual(acq.normalize_tiktok_video_url(url), PAGE)
        for host, path in [("vm.tiktok.com", "/Zfixture/"), ("vt.tiktok.com", "/Zfixture/"),
                           ("www.tiktok.com", "/t/Zfixture")]:
            url = "https://" + host + path
            self.assertEqual(acq.normalize_tiktok_video_url(url + "?share=fixture"), url)
        for url in [PAGE.replace("https:", "http:"), PAGE.replace(".com", ".com.invalid"),
                    PAGE.replace("www.", "user@www."), PAGE.replace(".com/", ".com:8443/"),
                    PAGE.replace("/video/", "/photo/"), PAGE.replace(POST_ID, "123"),
                    PAGE.replace("www.", "\nwww."), PAGE + "/extra", MEDIA, "https://vt.tiktok.com/"]:
            with self.subTest(url=url):
                self.assertIsNone(acq.normalize_tiktok_video_url(url))

    async def test_follows_checked_share_redirects_and_handles_size_unknown_to_source(self):
        media2 = "https://v99.tiktokcdn-us.com/mp4/fixture"
        with self.route({SHORT: response(302, location=PAGE + "?share=fixture"),
                         PAGE: response(body=page()),
                         MEDIA: response(307, location=media2),
                         media2: response(body=MP4, mime="video/mp4")}):
            await self.fetch(SHORT)
        self.assertEqual(self.path.read_bytes(), MP4)
        self.assertEqual(self.calls, [(SHORT, None), (PAGE, None), (MEDIA, PAGE), (media2, PAGE)])

    async def test_rejects_cross_family_or_mismatched_post_redirects(self):
        other = PAGE.replace(POST_ID, "2" * 19)
        for initial, replies, expected in [
            (PAGE, {PAGE: response(302, location="https://example.invalid/post")}, [PAGE]),
            (SHORT, {SHORT: response(301, location=MEDIA)}, [SHORT]),
            (PAGE, {PAGE: response(302, location=other), other: response(body=page())}, [PAGE, other]),
            (PAGE, {PAGE: response(body=page()), MEDIA: response(307, location=PAGE)}, [PAGE, MEDIA]),
            (PAGE, {PAGE: response(body=page()), MEDIA: response(302, location=MEDIA)}, [PAGE] + [MEDIA] * 4),
        ]:
            with self.subTest(initial=initial, n=len(expected)), self.route(replies):
                self.calls.clear()
                with self.assertRaises(acq.TikTokUnavailable):
                    await self.fetch(initial)
                self.assertEqual([url for url, _ in self.calls], expected)
                self.assertFalse(self.path.exists())

    async def test_selects_one_bounded_public_video_format_from_yt_dlp(self):
        ranked = MEDIA + "/high"

        def variant(url, key, size):
            return {"PlayAddr": {"UrlKey": key, "DataSize": size, "UrlList": [url]}}

        public = page(video={"duration": 4, "height": 1280, "width": 720, "downloadAddr": MEDIA,
                             "bitrateInfo": [
                                 variant(MEDIA + "/low", "v0_h264_540p_150000", 700),
                                 variant(ranked, "v0_bytevc1_720p_200000", 900),
                                 variant(MEDIA + "/oversized", "v0_h264_1080p_420000", 1200),
                                 variant(MEDIA + "/unplayable", "v0_bytevc2_1080p_410000", 800),
                             ]})
        with self.route({PAGE: response(body=public), ranked: response(body=MP4, mime="video/mp4")}):
            await self.fetch()
        self.assertEqual(self.calls, [(PAGE, None), (ranked, PAGE)])
        self.assertEqual(self.path.read_bytes(), MP4)

    async def test_anonymous_media_cookie_obeys_scope_and_isolated_per_request(self):
        external = "https://v99.tiktokcdn-us.com/media"
        scoped = "tt_chain_token=sample; Domain=.tiktok.com; Path=/; Secure"
        headers = [("content-type", "text/html"), ("set-cookie", scoped),
                   ("set-cookie", "sessionid=unused; Domain=.tiktok.com; Path=/; Secure")]
        body = httpx.Response(200, headers=headers, stream=Stream(page()))
        with self.route({PAGE: body, MEDIA: response(302, location=external),
                         external: response(body=MP4, mime="video/mp4")}):
            await self.fetch()
        self.assertEqual(self.cookies_sent, [None, "tt_chain_token=sample", None])

        for cookie in (None, scoped.replace("Path=/;", "Path=/private;"),
                       scoped + "; Max-Age=0", scoped.replace("; Secure", ""),
                       scoped.replace(".tiktok.com", ".com")):
            with self.subTest(cookie=cookie):
                self.path.unlink()
                self.cookies_sent.clear()
                headers = {"set-cookie": cookie} if cookie else {}
                with self.route({PAGE: response(body=page(), **headers),
                                 MEDIA: response(body=MP4, mime="video/mp4")}):
                    await self.fetch()
                self.assertEqual(self.cookies_sent, [None, None])

    async def test_page_must_establish_visibility_identity_limits_and_public_media(self):
        for body in [b"login", page(id="2" * 19), page(secret=True), page(privateItem=True),
                     page(video_control={"allow_download": False}), page(videoControl={"allowDownload": False}),
                     page(video={"duration": 11, "downloadAddr": MEDIA}),
                     page(video={"duration": 4, "size": 1025, "downloadAddr": MEDIA}),
                     page(video={"duration": 4, "downloadAddr": PAGE}),
                     page(video={"duration": 4, "downloadAddr": MEDIA.replace(".com", ".com.invalid")})]:
            with self.subTest(body=body[-100:]), self.route({PAGE: response(body=body)}):
                self.calls.clear()
                with self.assertRaises(acq.TikTokUnavailable):
                    await self.fetch()
                self.assertEqual(self.calls, [(PAGE, None)])
                self.assertFalse(self.path.exists())

    async def test_does_not_retry_media_after_denial_or_treat_error_or_playlists_as_mp4(self):
        play = MEDIA + "/alternate"
        public = page(video={"duration": 4, "downloadAddr": MEDIA, "playAddr": play})
        for result in [response(403), response(429), response(body=MP4, mime="text/html"),
                       response(body=b"#EXTM3U", mime="video/mp4"), response(body=b"", mime="video/mp4"),
                       response(body=MP4, mime="video/mp4", **{"content-encoding": "gzip"}),
                       response(body=MP4, mime="video/mp4", **{"content-length": "1025"}),
                       response(body=MP4, mime="video/mp4", **{"content-length": "98"}),
                       response(body=MP4 + bytes(1025), mime="video/mp4")]:
            with self.subTest(code=result.status_code), self.route({PAGE: response(body=public), MEDIA: result}):
                self.calls.clear()
                with self.assertRaises(acq.TikTokUnavailable):
                    await self.fetch()
                self.assertEqual(self.calls, [(PAGE, None), (MEDIA, PAGE)])
                self.assertFalse(self.path.exists())

    async def test_accepts_only_an_advertised_play_url_and_preserves_existing_destination(self):
        public = page(video={"duration": 4, "playAddr": MEDIA})
        with self.route({PAGE: response(body=public), MEDIA: response(body=MP4, mime="video/mp4")}):
            await self.fetch()
        with self.route({PAGE: response(body=public), MEDIA: response(body=MP4, mime="video/mp4")}):
            with self.assertRaises(acq.TikTokUnavailable):
                await self.fetch()
        self.assertEqual(self.path.read_bytes(), MP4)

    async def test_enforces_hard_caps_even_if_caller_allows_more(self):
        for duration, size in ((181, 50), (90, 20_000_001)):
            public = page(video={"duration": duration, "size": size, "downloadAddr": MEDIA})
            with self.route({PAGE: response(body=public)}):
                with self.assertRaisesRegex(acq.TikTokUnavailable, "limits"):
                    await acq.fetch_tiktok_video(PAGE, self.path, max_bytes=100_000_000, max_duration=300)
                self.assertFalse(self.path.exists())

    async def test_pins_public_dns_ip_while_authenticating_hostname_and_omits_cookies(self):
        answer = [(2, 1, 6, "", ("8.8.8.8", 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=answer)) as resolve, \
             patch("tiktok_acquisition.httpx.AsyncHTTPTransport", autospec=True) as transport:
            handler = transport.return_value.__aenter__.return_value.handle_async_request
            handler.return_value = response(body=MP4)
            async with acq._get(MEDIA, PAGE):
                pass
            request = handler.call_args.args[0]
            self.assertEqual(request.url.host, "8.8.8.8")
            self.assertEqual(request.extensions["sni_hostname"], request.headers["host"])
            self.assertEqual(request.headers["host"], "v16-webapp-fixture.us.tiktok.com")
            self.assertEqual(request.headers["referer"], PAGE)
            self.assertNotIn("cookie", request.headers)
            transport.assert_called_once_with(trust_env=False)
            resolve.assert_awaited_once()
        for ip in ("127.0.0.1", "192.0.2.1", "224.0.0.1"):
            with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=answer + [(2, 1, 6, "", (ip, 443))])), \
                 patch("tiktok_acquisition.httpx.AsyncHTTPTransport") as transport:
                with self.assertRaises(acq.TikTokUnavailable):
                    async with acq._get(MEDIA, PAGE):
                        self.fail("invalid DNS answer reached transport")
                transport.assert_not_called()

    async def test_cancelled_transfer_finishes_cleanup_before_return(self):
        started = asyncio.Event()

        class SlowMP4(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield MP4 + bytes(65536)
                started.set()
                await asyncio.Event().wait()

        stream = httpx.Response(200, headers={"content-type": "video/mp4"}, stream=SlowMP4())
        with self.route({PAGE: response(body=page()), MEDIA: stream}):
            task = asyncio.create_task(self.fetch(limit=200000))
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(self.path.exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.path.exists())
