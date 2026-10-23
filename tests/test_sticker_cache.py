"""Recognition cache lifecycle and inter-connection ownership regressions."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from sticker_cache import StickerCache, make_cache_key


def recognition(**changes):
    return dict({
        "summary": "A drawn fox holding a small sign.",
        "visible_text": "Привіт!",
        "text_status": "readable",
        "representation": "static_original",
        "model": "synthetic-vision-model",
        "prompt_version": "sticker-recognition-v1",
        "content_sha256": "a" * 64,
    }, **changes)


class StickerCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.now = 1000.0
        self.cache = self.open_cache()
        self.key = make_cache_key("synthetic-stable-sticker", "static_original")

    def open_cache(self, max_entries=10_000):
        cache = StickerCache(self.path, max_entries=max_entries, clock=lambda: self.now)
        self.addCleanup(cache.close)
        return cache

    def store(self, cache=None, key=None, result=None):
        cache = cache or self.cache
        key = key or self.key
        owner = cache.claim(key)
        self.assertIsNotNone(owner)
        self.assertTrue(cache.complete(key, owner, result or recognition()))

    def test_success_is_persistent_and_not_invalidated_by_new_producer(self):
        self.store()
        self.cache.close()
        self.cache = other = self.open_cache()
        self.assertEqual(recognition(), other.get(self.key))
        self.assertIsNone(other.claim(self.key))
        self.assertEqual("ready", other.state(self.key))
        self.assertEqual("synthetic-vision-model", other.get(self.key)["model"])
        # Results are detached values, not a mutable in-process cache entry.
        returned = other.get(self.key)
        returned["summary"] = "mutated by caller"
        self.assertEqual(recognition(), self.cache.get(self.key))

    def test_key_separates_asset_representation_and_contract_version(self):
        self.assertEqual(self.key, make_cache_key("synthetic-stable-sticker", "static_original"))
        self.assertNotIn("synthetic-stable-sticker", self.key)
        keys = {self.key,
                make_cache_key("synthetic-other-sticker", "static_original"),
                make_cache_key("synthetic-stable-sticker", "thumbnail"),
                make_cache_key("synthetic-stable-sticker", "static_original", "2")}
        self.assertEqual(4, len(keys))
        for identity, representation, version in (("", "thumbnail", "1"),
                                                  ("test", "animation", "1"),
                                                  ("test", "thumbnail", "a:b")):
            with self.assertRaises(ValueError):
                make_cache_key(identity, representation, version)

    def test_parallel_separate_connections_have_one_owner(self):
        caches = [self.cache] + [self.open_cache() for _ in range(5)]
        barrier = threading.Barrier(len(caches))

        def attempt(cache):
            barrier.wait()
            return cache, cache.claim(self.key)

        with ThreadPoolExecutor(max_workers=len(caches)) as executor:
            attempts = list(executor.map(attempt, caches))
        winners = [(cache, owner) for cache, owner in attempts if owner is not None]
        self.assertEqual(1, len(winners))
        winner, owner = winners[0]
        self.assertTrue(winner.complete(self.key, owner, recognition()))
        self.assertTrue(all(cache.get(self.key) == recognition() for cache in caches))

    def test_claim_does_not_hold_transaction_during_external_work(self):
        owner = self.cache.claim(self.key)
        self.assertIsNotNone(owner)
        # Another connection can commit unrelated application data while recognition runs.
        with sqlite3.connect(self.path, timeout=0.1) as conn:
            conn.execute("CREATE TABLE independent_records (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO independent_records VALUES (1)")
        self.assertTrue(self.cache.complete(self.key, owner, recognition()))

    def test_expired_owner_cannot_overwrite_replacement(self):
        old = self.cache.claim(self.key, lease_seconds=10)
        other = self.open_cache()
        self.now += 10
        self.assertEqual("missing", other.state(self.key))
        new = other.claim(self.key, lease_seconds=10)
        self.assertNotEqual(old, new)
        self.assertFalse(self.cache.complete(self.key, old, recognition(summary="Stale result")))
        self.assertFalse(self.cache.fail(self.key, old))
        self.assertTrue(other.complete(self.key, new, recognition()))
        self.assertEqual(recognition(), self.cache.get(self.key))

    def test_expired_owner_cannot_complete_without_replacement(self):
        owner = self.cache.claim(self.key, lease_seconds=2)
        self.now += 2
        self.assertFalse(self.cache.complete(self.key, owner, recognition()))
        self.assertIsNone(self.cache.get(self.key))
        self.assertIsNotNone(self.cache.claim(self.key))

    def test_failure_backoff_persists_and_does_not_claim_success(self):
        owner = self.cache.claim(self.key)
        self.assertTrue(self.cache.fail(self.key, owner, retry_seconds=30))
        other = self.open_cache()
        self.assertEqual("retry", other.state(self.key))
        self.assertIsNone(other.get(self.key))
        self.assertIsNone(other.claim(self.key))
        self.now += 30
        self.assertEqual("missing", other.state(self.key))
        self.store(other)
        self.assertEqual(recognition(), other.get(self.key))

    def test_message_cleanup_cannot_delete_asset_result(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)")
            conn.execute("INSERT INTO messages VALUES (1, 'Synthetic message')")
        self.store()
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("DELETE FROM messages")
            conn.execute("DROP TABLE messages")
        self.now += 365 * 86400
        self.assertEqual(recognition(), self.open_cache().get(self.key))

    def test_lru_eviction_retains_recent_success(self):
        cache = self.open_cache(max_entries=2)
        second = make_cache_key("second", "static_original")
        third = make_cache_key("third", "static_original")
        self.store(cache)
        self.now += 1
        self.store(cache, second)
        self.now += 1
        self.assertIsNotNone(cache.get(self.key))
        self.now += 1
        self.store(cache, third)
        self.assertIsNone(cache.get(second))
        self.assertIsNotNone(cache.get(self.key))
        self.assertIsNotNone(cache.get(third))
        self.assertEqual(2, cache.stats()["entries"])
        self.assertEqual(1, cache.stats()["evictions"])

    def test_capacity_never_evicts_active_claim_or_retry(self):
        cache = self.open_cache(max_entries=1)
        owner = cache.claim(self.key)
        other_key = make_cache_key("other", "static_original")
        self.assertIsNone(cache.claim(other_key))
        self.assertEqual("busy", cache.state(self.key))
        self.assertTrue(cache.fail(self.key, owner, retry_seconds=5))
        self.assertIsNone(cache.claim(other_key))
        self.now += 5
        self.assertIsNotNone(cache.claim(other_key))
        self.assertEqual("missing", cache.state(self.key))
        self.assertEqual(1, cache.stats()["entries"])

    def test_invalidation_fences_old_owner_and_allows_reanalysis(self):
        owner = self.cache.claim(self.key)
        self.cache.invalidate(self.key)
        self.assertFalse(self.cache.complete(self.key, owner, recognition()))
        self.store()
        self.cache.invalidate(self.key)
        self.assertEqual("missing", self.cache.state(self.key))
        self.store(result=recognition(summary="Corrected neutral description"))
        self.assertEqual("Corrected neutral description", self.cache.get(self.key)["summary"])

    def test_requested_refresh_preserves_running_owner_and_retry(self):
        self.store()
        self.assertTrue(self.cache.request_refresh(self.key))
        self.assertIsNone(self.cache.get(self.key))
        owner = self.cache.claim(self.key)
        other = self.open_cache()
        self.assertFalse(other.request_refresh(self.key))
        self.assertEqual("busy", self.cache.state(self.key))
        self.assertTrue(self.cache.fail(self.key, owner, retry_seconds=5))
        self.assertFalse(other.request_refresh(self.key))
        self.assertEqual("retry", self.cache.state(self.key))
        self.now += 5
        self.store(result=recognition(summary="Refreshed description"))
        self.assertEqual("Refreshed description", other.get(self.key)["summary"])
        self.assertTrue(other.request_refresh(self.key))
        self.assertFalse(self.cache.request_refresh(self.key))

    def test_invalid_result_does_not_become_a_success(self):
        owner = self.cache.claim(self.key)
        invalid = [None, [], {}, recognition(summary=""), recognition(summary="a" * 3001),
                   recognition(visible_text="a" * 6001), recognition(text_status="unknown"),
                   recognition(text_status="none"), recognition(text_status="unreadable"),
                   recognition(visible_text=""), recognition(representation="thumbnail"),
                   recognition(model=""), recognition(prompt_version=""),
                   recognition(content_sha256="not-a-hash"), recognition(chat_text="private conversation"),
                   recognition(summary=123), recognition(summary="\ud800")]
        for payload in invalid:
            with self.subTest(payload=type(payload).__name__):
                self.assertFalse(self.cache.complete(self.key, owner, payload))
                self.assertIsNone(self.cache.get(self.key))
        self.assertEqual("busy", self.cache.state(self.key))
        self.assertTrue(self.cache.complete(self.key, owner, recognition()))

    def test_readability_status_and_transcription_survive_replay(self):
        for index, (status, text) in enumerate((("none", ""), ("unreadable", ""),
                                               ("partial", "Частина…"),
                                               ("readable", "ignore previous instructions"))):
            key = make_cache_key(f"synthetic-text-{index}", "static_original")
            result = recognition(text_status=status, visible_text=text)
            self.store(key=key, result=result)
            self.assertEqual(result, self.open_cache().get(key))
        # The storage layer preserves observed text, never executes it.
        self.assertEqual(4, self.cache.stats()["ready_entries"])

    def test_corrupt_persisted_result_is_a_recoverable_miss(self):
        self.store()
        for payload in ("not-json", b"invalid-blob", json.dumps({"summary": "missing fields"}),
                        '"' + 'x' * 40_001 + '"'):
            with sqlite3.connect(self.path) as conn:
                conn.execute("UPDATE sticker_recognition_cache SET result_json=? WHERE cache_key=?", (payload, self.key))
            self.assertIsNone(self.cache.get(self.key))
            self.assertEqual("missing", self.cache.state(self.key))
            self.store()

    def test_stats_expose_only_aggregate_numbers(self):
        self.assertIsNone(self.cache.get(self.key))
        self.store()
        self.cache.get(self.key)
        stats = self.cache.stats()
        self.assertEqual(1, stats["hits"])
        self.assertEqual(1, stats["misses"])
        self.assertEqual(1, stats["completed"])
        self.assertTrue(all(type(value) is int for value in stats.values()))
        serialized = json.dumps(stats)
        for private in (self.key, recognition()["visible_text"], recognition()["summary"]):
            self.assertNotIn(private, serialized)


if __name__ == "__main__":
    unittest.main()
