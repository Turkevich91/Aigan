"""Demand-driven recognition with real cache ownership and fake provider I/O."""
import asyncio
import base64
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from sticker_cache import StickerCache, make_cache_key
from sticker_media import normalize_sticker_image
import sticker_recognition as service


def image_bytes():
    output = io.BytesIO()
    Image.new("RGBA", (8, 6), (120, 40, 20, 100)).save(output, format="PNG")
    return output.getvalue()


def provider_result(**changes):
    return dict(summary="A fox holding a small sign.", visible_text="Hello!",
                text_status="readable", model="synthetic-vision", **changes)


class StickerRecognitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.now = 1000.0
        self.cache = self.open_cache()
        self.identity = "synthetic-sticker-identity"
        self.image = image_bytes()
        self.load = AsyncMock(return_value=self.image)
        self.recognize = AsyncMock(return_value=provider_result())

    def open_cache(self):
        cache = StickerCache(self.path, clock=lambda: self.now)
        self.addCleanup(cache.close)
        return cache

    def key(self, representation="static_original", version=None):
        return make_cache_key(self.identity, representation, version or service.RECOGNITION_VERSION)

    async def call(self, **changes):
        args = dict(file_unique_id=self.identity, representation="static_original",
                    load_image=self.load, recognize=self.recognize)
        cache = changes.pop("cache", self.cache)
        args.update(changes)
        return await service.recognize_sticker(cache, **args)

    async def test_first_recognition_uses_normalized_pixels_and_persists_provenance(self):
        result = await self.call()
        self.load.assert_awaited_once_with()
        self.recognize.assert_awaited_once()
        data_url, representation = self.recognize.await_args.args
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        data = base64.b64decode(data_url.split(",", 1)[1])
        self.assertEqual(normalize_sticker_image(self.image).data, data)
        self.assertEqual("static_original", representation)
        self.assertEqual(hashlib.sha256(data).hexdigest(), result["content_sha256"])
        self.assertEqual(service.RECOGNITION_VERSION, result["prompt_version"])
        self.assertEqual("static_original", result["representation"])
        self.assertEqual(result, self.cache.get(self.key()))
        self.assertEqual(1, self.cache.stats()["recognition_calls"])

    async def test_repeat_after_close_reopen_uses_no_download_or_provider(self):
        first = await self.call()
        self.cache.close()
        self.cache = self.open_cache()
        changed_handle_loader = AsyncMock(side_effect=AssertionError("must not download again"))
        changed_model_provider = AsyncMock(side_effect=AssertionError("must not recognize again"))
        self.now += 365 * 86400
        second = await self.call(load_image=changed_handle_loader, recognize=changed_model_provider)
        self.assertEqual(first, second)
        changed_handle_loader.assert_not_awaited()
        changed_model_provider.assert_not_awaited()
        self.assertEqual(1, self.load.await_count)
        self.assertEqual(1, self.recognize.await_count)

    async def test_parallel_misses_share_one_paid_recognition_across_connections(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def recognize(data_url, representation):
            entered.set()
            await release.wait()
            return provider_result()

        self.recognize.side_effect = recognize
        caches = [self.cache] + [self.open_cache() for _ in range(4)]
        tasks = [asyncio.create_task(self.call(cache=cache)) for cache in caches]
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.sleep(0.01)
            self.assertEqual(1, self.recognize.await_count)
            self.assertEqual(1, self.load.await_count)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(1, self.recognize.await_count)
        self.assertEqual(1, self.load.await_count)

    async def test_representation_identity_and_schema_version_are_distinct(self):
        await self.call()
        preview = await self.call(representation="thumbnail")
        self.assertEqual("thumbnail", preview["representation"])
        await self.call(file_unique_id="synthetic-different-sticker")
        with patch.object(service, "RECOGNITION_VERSION", "2"):
            updated = await self.call()
            self.assertEqual("2", updated["prompt_version"])
        self.assertEqual(4, self.recognize.await_count)
        self.assertEqual(4, self.load.await_count)
        await self.call()
        self.assertEqual(4, self.recognize.await_count)

    async def test_download_failure_has_persistent_backoff_and_bounded_retry(self):
        self.load.side_effect = OSError("synthetic transfer failure")
        with self.assertRaisesRegex(OSError, "synthetic transfer failure"):
            await self.call()
        self.assertEqual("retry", self.cache.state(self.key()))
        self.recognize.assert_not_awaited()
        self.assertEqual(0, self.cache.stats()["recognition_calls"])
        other = self.open_cache()
        with self.assertRaisesRegex(service.StickerUnavailable, "sticker_recognition_retry_later"):
            await self.call(cache=other)
        self.assertEqual(1, self.load.await_count)
        self.now += 30
        self.load.side_effect = None
        self.assertEqual(provider_result()["summary"], (await self.call(cache=other))["summary"])
        self.assertEqual(2, self.load.await_count)
        self.assertEqual(1, self.recognize.await_count)

    async def test_cancelled_waiter_leaves_owned_recognition_to_persist_once(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_recognition(data_url, representation):
            entered.set()
            await release.wait()
            return provider_result()

        self.recognize.side_effect = blocked_recognition
        task = asyncio.create_task(self.call())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.cache.get(self.key()))
        self.assertEqual("busy", self.cache.state(self.key()))
        other = asyncio.create_task(self.call(cache=self.open_cache()))
        try:
            await asyncio.sleep(0.01)
            self.assertEqual(1, self.recognize.await_count)
            release.set()
            result = await asyncio.wait_for(other, timeout=1)
        finally:
            release.set()
            if not other.done():
                other.cancel()
            await asyncio.gather(other, return_exceptions=True)
        self.assertEqual(result, self.cache.get(self.key()))
        self.assertEqual(1, self.cache.stats()["recognition_calls"])
        self.assertEqual(1, self.load.await_count)
        self.assertEqual(1, self.recognize.await_count)

    async def test_bounded_wait_does_not_steal_or_fail_another_owner(self):
        owner = self.cache.claim(self.key())
        with self.assertRaisesRegex(service.StickerUnavailable, "sticker_recognition_retry_later"):
            await self.call(wait_seconds=0)
        self.assertEqual("busy", self.cache.state(self.key()))
        self.load.assert_not_awaited()
        self.recognize.assert_not_awaited()
        self.assertEqual(0, self.cache.stats()["recognition_calls"])
        result = dict(provider_result(), representation="static_original", prompt_version=service.RECOGNITION_VERSION,
                      content_sha256="a" * 64)
        self.assertTrue(self.cache.complete(self.key(), owner, result))

    async def test_cancelled_waiter_preserves_another_owner(self):
        owner = self.cache.claim(self.key())
        task = asyncio.create_task(self.call())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual("busy", self.cache.state(self.key()))
        self.load.assert_not_awaited()
        self.recognize.assert_not_awaited()
        self.assertEqual(0, self.cache.stats()["recognition_calls"])
        self.assertTrue(self.cache.fail(self.key(), owner, retry_seconds=0))

    async def test_invalid_provider_result_is_not_cached_as_a_success(self):
        self.recognize.return_value = {"summary": "Incomplete result"}
        with self.assertRaisesRegex(service.StickerUnavailable, "sticker_result_not_persisted"):
            await self.call()
        self.assertIsNone(self.cache.get(self.key()))
        self.assertEqual("retry", self.cache.state(self.key()))
        with self.assertRaises(service.StickerUnavailable):
            await self.call()
        self.assertEqual(1, self.recognize.await_count)

    async def test_invalid_image_or_input_cannot_call_provider(self):
        for kwargs, reason in (({"file_unique_id": ""}, "sticker_identity_unavailable"),
                               ({"representation": "unavailable"}, "sticker_preview_unavailable")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(service.StickerUnavailable, reason):
                    await self.call(**kwargs)
        self.load.assert_not_awaited()
        self.load.return_value = b"not an image"
        with self.assertRaisesRegex(ValueError, "sticker_image_invalid"):
            await self.call()
        self.recognize.assert_not_awaited()
        self.assertEqual(0, self.cache.stats()["recognition_calls"])
        self.assertIsNone(self.cache.get(self.key()))

    async def test_lost_claim_does_not_return_unpersisted_success(self):
        async def invalidated_recognition(data_url, representation):
            self.cache.invalidate(self.key())
            return provider_result()

        self.recognize.side_effect = invalidated_recognition
        with self.assertRaisesRegex(service.StickerUnavailable, "sticker_result_not_persisted"):
            await self.call()
        self.assertIsNone(self.cache.get(self.key()))
        self.assertEqual("missing", self.cache.state(self.key()))

    async def test_explicit_refresh_reanalyzes_once_and_unknown_reason_is_rejected(self):
        first = await self.call()
        self.assertEqual(first, await self.call())
        self.assertEqual(1, self.recognize.await_count)
        self.recognize.return_value = dict(provider_result(), summary="Corrected visual description.")
        updated = await self.call(refresh_reason="correction")
        self.assertEqual("Corrected visual description.", updated["summary"])
        self.assertEqual(2, self.recognize.await_count)
        self.assertEqual(2, self.load.await_count)
        self.assertEqual(updated, await self.call())
        with self.assertRaisesRegex(service.StickerUnavailable, "sticker_refresh_reason_invalid"):
            await self.call(refresh_reason="always")
        self.assertEqual(updated, await self.call())
        self.assertEqual(2, self.recognize.await_count)
        self.assertEqual(2, self.load.await_count)

    async def test_waiter_timeout_preserves_running_recognition_and_cached_result(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_recognition(data_url, representation):
            entered.set()
            await release.wait()
            return provider_result()

        self.recognize.side_effect = slow_recognition
        waiting = asyncio.create_task(self.call(wait_seconds=0.01))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            with self.assertRaises(asyncio.TimeoutError):
                await waiting
            self.assertEqual("busy", self.cache.state(self.key()))
            release.set()
            result = await asyncio.wait_for(self.call(cache=self.open_cache()), timeout=1)
            self.assertEqual(result, self.cache.get(self.key()))
            self.assertEqual(1, self.cache.stats()["recognition_calls"])
        finally:
            release.set()
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        self.assertEqual(1, self.load.await_count)
        self.assertEqual(1, self.recognize.await_count)


if __name__ == "__main__":
    unittest.main()
