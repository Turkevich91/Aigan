"""Fusion invariants and the actual filtered retrieval adapter, without providers."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from memory import (
    MemorySearchBatch, MemoryStore, SemanticMemoryResult,
    fuse_memory_search_batches, legacy_memory_result_merge,
)


def result(key, score, source):
    return SemanticMemoryResult(SimpleNamespace(id=key), "synthetic", score, source)


class FusionPolicyTests(unittest.TestCase):
    def test_two_channel_evidence_can_beat_keyword_score_scale(self):
        batches = [MemorySearchBatch("keyword", [result(1, 100, "keyword")]),
                   MemorySearchBatch("semantic", [result(2, .9, "semantic")]),
                   MemorySearchBatch("fts", [result(2, .001, "fts")])]
        self.assertEqual(1, fuse_memory_search_batches(batches, limit=1).results[0].item.id)
        for policy in ("rrf", "normalized"):
            outcome = fuse_memory_search_batches(batches, limit=1, policy=policy)
            self.assertEqual(2, outcome.results[0].item.id)
            self.assertEqual("fts+semantic", outcome.results[0].source)
            self.assertEqual(policy, outcome.applied_policy)

    def test_duplicates_do_not_consume_rank_or_add_channel_votes(self):
        repeated = MemorySearchBatch("keyword", [result(1, 100, "keyword"), result(1, 100, "keyword"), result(2, 99, "keyword")])
        once = MemorySearchBatch("keyword", [result(1, 100, "keyword"), result(2, 99, "keyword")])
        semantic = MemorySearchBatch("semantic", [result(2, .8, "semantic")])
        for policy in ("rrf", "normalized"):
            expected = fuse_memory_search_batches([once, semantic], limit=6, policy=policy)
            actual = fuse_memory_search_batches([repeated, repeated, semantic], limit=6, policy=policy)
            self.assertEqual(expected.results, actual.results)
            self.assertEqual(2, len(actual.results))

    def test_query_batch_order_does_not_choose_ties_or_duplicate_provenance(self):
        a = MemorySearchBatch("keyword", [result(2, 100, "keyword")])
        b = MemorySearchBatch("keyword", [result(1, 100, "keyword")])
        for policy in ("rrf", "normalized"):
            first = fuse_memory_search_batches([a, b, a], limit=6, policy=policy)
            second = fuse_memory_search_batches([b, a], limit=6, policy=policy)
            self.assertEqual([1, 2], [row.item.id for row in first.results])
            self.assertEqual(first.results, second.results)

    def test_normalization_handles_negative_equal_singleton_and_missing_channels(self):
        values = [result(1, -.1, "semantic"), result(2, -.5, "semantic")]
        outcome = fuse_memory_search_batches([MemorySearchBatch("semantic", values)], limit=6, policy="normalized")
        self.assertEqual([1., 0.], [row.score for row in outcome.results])
        equal = [result(2, -.2, "semantic"), result(1, -.2, "semantic")]
        outcome = fuse_memory_search_batches([MemorySearchBatch("semantic", equal)], limit=6, policy="normalized")
        self.assertEqual([1., 1.], [row.score for row in outcome.results])
        self.assertEqual([], fuse_memory_search_batches([], limit=6, policy="rrf").results)

    def test_legacy_preserves_exact_first_occurrence_and_score_union_behavior(self):
        rows = [result(2, 100, "keyword"), result(1, 100, "keyword"), result(2, .9, "semantic"), result(2, .2, "fts")]
        merged = legacy_memory_result_merge(rows, 2)
        self.assertEqual([2, 1], [row.item.id for row in merged])
        self.assertEqual("keyword+semantic+fts", merged[0].source)
        self.assertEqual(100, merged[0].score)
        self.assertIs(rows[0].item, merged[0].item)

    def test_unknown_nonfinite_and_numeric_cases_report_exact_legacy_fallback(self):
        for policy, numeric, bad_score, reason in (
            ("typo", False, False, "unknown_policy"),
            ("rrf", True, False, "numeric_protected"),
            ("normalized", True, False, "numeric_protected"),
            ("rrf", False, True, "nonfinite_score"),
        ):
            rows = [result(1, 100, "keyword"), result(2, float("inf") if bad_score else .9, "semantic")]
            batches = [MemorySearchBatch("keyword", rows[:1]), MemorySearchBatch("semantic", rows[1:])]
            outcome = fuse_memory_search_batches(batches, limit=6, policy=policy, protect_numeric=numeric)
            self.assertEqual(legacy_memory_result_merge(rows, 6), outcome.results)
            self.assertEqual("legacy", outcome.applied_policy)
            self.assertEqual(reason, outcome.fallback_reason)
        outcome = fuse_memory_search_batches([MemorySearchBatch("semantic", [result(1, .7, "semantic")])], limit=6, policy="rrf", protect_numeric=True)
        self.assertEqual("rrf", outcome.applied_policy)


class FusionRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.support import configure_test_environment
        configure_test_environment()
        import main
        cls.app = main

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temporary.name) / "memory.sqlite3")
        self.memory_patch = patch.object(self.app, "MEMORY", self.store)
        self.memory_patch.start()

    def tearDown(self):
        self.memory_patch.stop()
        self.store.close()
        self.temporary.cleanup()

    def test_unconfigured_policy_is_legacy(self):
        with patch.dict(os.environ, {"MEMORY_SEARCH_FUSION_POLICY": ""}):
            self.assertEqual("legacy", self.app.Config.from_env().memory_search_fusion_policy)

    def test_actual_collectors_keep_variant_batches_and_filter_current_and_invocation_rows(self):
        for number, text in ((1, "silver earlier"), (2, "!m silver invocation"), (3, "silver current")):
            self.store.save_message(chat_id=-1001, message_id=number, text=text, sender_label="Synthetic")
        config = replace(self.app.CONFIG, memory_search_fusion_policy="rrf", memory_semantic_top_k=6)
        spy = Mock(wraps=fuse_memory_search_batches)
        with patch.object(self.app, "CONFIG", config), \
             patch.object(self.app, "extract_memory_topic_terms", return_value=["silver", "earlier"]), \
             patch.object(self.app, "fuse_memory_search_batches", spy), \
             patch.object(self.app, "system_event"):
            outcome = asyncio.run(self.app.semantic_memory_search_outcome(
                SimpleNamespace(chat_id=-1001), "silver", route="memory_recall",
                extra_queries=("earlier", "SILVER"), exclude_message_id=3))
        batches = spy.call_args.args[0]
        self.assertEqual(2, sum(batch.channel == "keyword" for batch in batches))
        self.assertEqual(2, sum(batch.channel == "fts" for batch in batches))
        self.assertEqual([1], [row.item.message_id for row in outcome.results])
        self.assertEqual("rrf", outcome.fusion_policy)

    def test_real_store_keeps_chat_age_bot_and_current_message_filters_in_every_policy(self):
        now = datetime.now(timezone.utc)
        examples = [(1, -1001, False, now), (2, -1002, False, now),
                    (3, -1001, False, now-timedelta(days=40)), (4, -1001, True, now),
                    (5, -1001, False, now)]
        for number, chat, is_bot, created in examples:
            self.store.save_message(chat_id=chat, message_id=number, text="silver evidence", sender_label="Synthetic", is_bot=is_bot, created_at=created)
            item = self.store.message_by_message_id(chat, number)
            self.store.upsert_embedding(message_id=item.id, chat_id=chat, model=self.app.CONFIG.memory_embedding_model,
                                        dimensions=4, content_hash="synthetic", embedding=[1., 0., 0., 0.])
        for policy in ("legacy", "rrf", "normalized"):
            with self.subTest(policy=policy), \
                 patch.object(self.app, "CONFIG", replace(self.app.CONFIG, memory_search_fusion_policy=policy)), \
                 patch.object(self.app, "create_embeddings", AsyncMock(return_value=[[1., 0., 0., 0.]])), \
                 patch.object(self.app, "system_event"):
                outcome = asyncio.run(self.app.semantic_memory_search_outcome(
                    SimpleNamespace(chat_id=-1001), "silver", route="direct", exclude_message_id=5))
            self.assertEqual([1], [row.item.message_id for row in outcome.results])
            self.assertTrue(outcome.embeddings_used)

    def test_numeric_source_text_rescue_and_candidate_exception_preserve_legacy(self):
        self.store.save_message(chat_id=-1001, message_id=1, text="source attachment", source_text="part 4070 price $250", sender_label="Synthetic")
        for policy in ("rrf", "normalized"):
            with patch.object(self.app, "CONFIG", replace(self.app.CONFIG, memory_search_fusion_policy=policy)), \
                 patch.object(self.app, "system_event"):
                outcome = asyncio.run(self.app.semantic_memory_search_outcome(SimpleNamespace(chat_id=-1001), "4070 250", route="memory_recall"))
            self.assertEqual([1], [row.item.message_id for row in outcome.results])
            self.assertEqual("numeric_protected", outcome.fusion_fallback_reason)
        with patch.object(self.app, "fuse_memory_search_batches", side_effect=RuntimeError("private detail")), \
             patch.object(self.app, "system_event"):
            outcome = asyncio.run(self.app.semantic_memory_search_outcome(SimpleNamespace(chat_id=-1001), "attachment", route="direct"))
        self.assertEqual([1], [row.item.message_id for row in outcome.results])
        self.assertEqual("candidate_exception", outcome.fusion_fallback_reason)


if __name__ == "__main__":
    unittest.main()
