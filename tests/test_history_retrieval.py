"""Synthetic geometry validates retrieval contracts, not embedding language quality.

Development shapes: UA/RU mix, spelling variants, aliases and semantic descriptions.
There is no paid evaluation, linguistic holdout, or claim of learned typo tolerance.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from history_retrieval import (
    HISTORY_SCAN_LIMIT, HistorySearchScope, preflight_history_retrieval, retrieve_history,
    history_query_embedding_available, normalize_history_vector,
)
from memory import MemoryStore


def vector(a=1., b=0.):
    return [a, b] + [0.] * 510


class HistoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp.name) / "synthetic.sqlite3")
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, text="blue lantern", embedding=None, **overrides):
        self.sequence += 1
        values = dict(chat_id=100, message_id=self.sequence, user_id=1,
                      created_at=self.start + timedelta(minutes=self.sequence),
                      sender_label="Synthetic participant", text=text)
        values.update(overrides)
        item_id = self.store.save_message(**values)
        if embedding is not None:
            item = self.store.item_by_id(item_id)
            self.store.upsert_embedding(message_id=item_id, chat_id=item.chat_id,
                model="text-embedding-3-small", dimensions=512,
                content_hash=self.store.content_hash(self.store.searchable_text_for_item(item)),
                embedding=embedding)
            self.change_embedding(item_id, embedded_at="2026-01-01T00:00:00Z")
        return item_id

    def change_embedding(self, item_id, **values):
        self.store._conn.execute(
            "UPDATE message_embeddings SET " + ",".join(f"{key}=?" for key in values) + " WHERE message_id=?",
            (*values.values(), item_id))
        self.store._conn.commit()

    def scope(self, **overrides):
        current = self.add("current request")
        values = dict(chat_id=100, cutoff_memory_id=current, cutoff_created_at="2026-02-01T00:00:00Z")
        values.update(overrides)
        return HistorySearchScope(**values)

    def search(self, scope=None, **overrides):
        args = dict(store=self.store, scope=scope or self.scope(), query="blue lantern", query_vector=vector())
        args.update(overrides)
        return retrieve_history(**args)

    def test_older_semantic_hit_beats_recent_chronological_hard_negative(self):
        expected = self.add("давня пригода про світлячків", embedding=vector())
        self.add("latest unrelated racing game", embedding=vector(0, 1))
        result = self.search(mode="semantic", limit=1)
        self.assertEqual((expected,), result.relevance_order)
        self.assertTrue(result.has_more_matching)
        self.assertTrue(result.embeddings_used)

    def test_synthetic_multilingual_development_shapes_share_geometry_without_literal_hit(self):
        shapes = (("Грали у Deep Rock Galactic", "діпрок, та гра з дворфами"),
                  ("Обговорювали Factorio", "факторіо, автоматизація заводу"),
                  ("А ще була Субнавтика", "subnatica підводне виживання"))
        for text, query in shapes:
            with self.subTest(query=query):
                target = self.add(text, embedding=vector())
                result = self.search(query=query, mode="semantic", limit=20)
                self.assertIn(target, result.relevance_order)

    def test_filters_apply_before_ranking_and_cap(self):
        wanted = self.add(embedding=vector(.9, .1), user_id=2)
        self.add(embedding=vector(), chat_id=200)
        self.add(embedding=vector(), user_id=3)
        self.add(embedding=vector(), user_id=2, created_at="2027-01-01T00:00:00Z")
        scope = self.scope(participant_id=2, after="2026-01-01T00:01:00Z", before="2026-01-01T00:02:00Z")
        self.add(embedding=vector(), user_id=2, created_at="2026-01-01T00:01:30Z")
        result = self.search(scope, limit=1)
        self.assertEqual((wanted,), result.relevance_order)
        self.assertEqual(1, result.coverage.scoped_rows)

    def test_explicit_old_dates_have_no_rolling_thirty_day_filter(self):
        wanted = self.add(embedding=vector(), created_at="2020-01-01T00:00:00Z")
        result = self.search(self.scope(after="2020-01-01T00:00:00Z", before="2021-01-01T00:00:00Z"))
        self.assertEqual((wanted,), result.relevance_order)

    def test_scope_rejects_ambiguous_naive_dates_and_invalid_identity(self):
        for overrides in ({"cutoff_created_at": "2026-01-01"}, {"chat_id": True},
                          {"cutoff_memory_id": 0}, {"participant_id": "2"}, {"participant_id": 2 ** 80},
                          {"after": "2026-01-02T00:00:00Z", "before": "2026-01-01T00:00:00Z"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.scope(**overrides)

    def test_timezone_offsets_rank_same_instant_consistently(self):
        older = self.add(embedding=vector(), created_at="2026-01-01T00:00:00+00:00")
        newer = self.add(embedding=vector(), created_at="2026-01-01T00:30:00-01:00")
        result = self.search(query_vector=None)
        self.assertEqual((newer, older), result.relevance_order)

    def test_full_hash_not_truncated_display_text_controls_validity(self):
        wanted = self.add("x" * 5000, embedding=vector())
        result = self.search(mode="semantic")
        self.assertEqual((wanted,), result.relevance_order)
        self.assertEqual(1001, len(result.rows[0]["text"]))

    def test_invalid_index_variants_are_excluded_and_reported(self):
        variants = ({"content_hash": "stale"}, {"model": "another-model"}, {"dimensions": 1536},
                    {"embedding_blob": b"bad"}, {"embedding_blob": struct.pack("<512f", *vector(float("nan")))},
                    {"embedding_blob": struct.pack("<512f", *vector(float("inf")))},
                    {"embedding_blob": struct.pack("<512f", *vector(0, 0))},
                    {"embedded_at": "2027-01-01T00:00:00Z"}, {"embedded_at": "invalid"}, {"chat_id": 200})
        for values in variants:
            item_id = self.add(embedding=vector())
            self.change_embedding(item_id, **values)
        result = self.search(mode="semantic")
        self.assertFalse(result.embeddings_used)
        self.assertEqual("lexical_fallback", result.applied_mode)
        self.assertEqual(len(variants), result.coverage.unusable_index_rows)

    def test_partial_index_reports_coverage_and_keeps_valid_semantic_rows(self):
        wanted = self.add("valid semantic", embedding=vector())
        self.add("unindexed blue lantern")
        bad = self.add("stale semantic", embedding=vector())
        self.change_embedding(bad, content_hash="stale")
        result = self.search(mode="semantic")
        self.assertEqual((wanted,), result.relevance_order)
        self.assertEqual((3, 1, 1), (result.coverage.scoped_rows, result.coverage.indexed_rows,
                                    result.coverage.unusable_index_rows))

    def test_unavailable_or_invalid_query_vector_uses_explicit_lexical_fallback(self):
        wanted = self.add(embedding=vector())
        self.add("unrelated", embedding=vector())
        scope = self.scope()
        for query_vector in (None, [], vector(float("nan")), vector(0, 0), [1.] * 1536):
            with self.subTest(vector_type=type(query_vector)):
                result = self.search(scope, query_vector=query_vector)
                self.assertEqual((wanted,), result.relevance_order)
                self.assertEqual("query_embedding_unavailable", result.fallback_reason)
                self.assertFalse(result.embeddings_used)

    def test_preflight_contains_only_aggregates_and_retrieval_rechecks_edit_and_delete(self):
        edited = self.add("SECRET_OLD_TEXT", embedding=vector())
        deleted = self.add("SECRET_DELETED_TEXT", embedding=vector())
        scope = self.scope()
        preflight = preflight_history_retrieval(self.store, scope)
        self.assertTrue(preflight.can_embed)
        self.assertNotIn("SECRET", json.dumps(asdict(preflight)))
        self.store.save_message(chat_id=100, message_id=1, text="new text")
        self.store._conn.execute("DELETE FROM messages WHERE id=?", (deleted,))
        self.store._conn.commit()
        result = self.search(scope, query="SECRET", mode="semantic")
        self.assertEqual((), result.rows)
        self.assertNotIn(edited, result.relevance_order)

    def test_projection_has_only_existing_safe_fields_and_untrusted_source(self):
        self.add("own comment", embedding=vector(), source_text="visible quoted source",
                 raw_note="FORBIDDEN_NOTE", local_media_path="FORBIDDEN_PATH", username="FORBIDDEN_USERNAME",
                 telegram_file_id="FORBIDDEN_TOKEN", source_url="FORBIDDEN_URL")
        result = self.search(mode="semantic")
        self.assertNotIn("FORBIDDEN", json.dumps(asdict(result)))
        self.assertEqual("visible quoted source", result.rows[0]["source_text"])
        self.assertEqual({"id", "message_id", "user_id", "created_at", "sender_label", "text", "source_text",
                          "attachment_type", "attachment_summary", "reply_to_message_id", "sort_time",
                          "is_forwarded", "evidence_digest"}, set(result.rows[0]))

    def test_authored_scope_excludes_forwarded_bot_and_source_influenced_vectors(self):
        self.add("blue lantern", embedding=vector(), is_bot=True)
        self.add("blue lantern", embedding=vector(), forward_origin="source")
        source_mixed = self.add("own blue lantern", embedding=vector(), source_text="another author's words")
        result = self.search(self.scope(authored_only=True), mode="semantic")
        self.assertEqual((source_mixed,), result.relevance_order)  # Own text remains eligible lexically.
        self.assertFalse(result.embeddings_used)
        self.assertEqual("", result.rows[0]["source_text"])

    def test_bot_rows_remain_lexically_searchable_but_not_semantically_indexed(self):
        wanted = self.add(embedding=vector(), is_bot=True)
        result = self.search()
        self.assertEqual((wanted,), result.relevance_order)
        self.assertFalse(result.embeddings_used)

    def test_rrf_reuses_owner_and_deduplicates_cross_channel_hits(self):
        wanted = self.add(embedding=vector())
        self.add("semantic only", embedding=vector(.8, .2))
        result = self.search()
        self.assertEqual("rrf", result.fusion_policy)
        self.assertEqual(wanted, result.relevance_order[0])
        self.assertEqual(len(result.rows), len(set(result.relevance_order)))

    def test_numeric_exact_rescue_preserves_keyword_priority(self):
        semantic = self.add("A different numbered lantern", embedding=vector())
        exact = self.add("lantern 42", embedding=vector(0, 1))
        result = self.search(query="lantern 42", limit=1)
        self.assertEqual((exact,), result.relevance_order)
        self.assertNotEqual(semantic, exact)
        self.assertEqual("legacy", result.fusion_policy)
        self.assertEqual("numeric_protected", result.fallback_reason)

    def test_single_digit_numeric_rescue_is_literal_not_wildcard(self):
        expected = self.add("lantern 5", embedding=vector(0, 1))
        self.add("different lantern", embedding=vector())
        result = self.search(query="lantern 5")
        self.assertEqual(expected, result.relevance_order[0])

    def test_fts_failure_retains_semantic_and_keyword_channels(self):
        wanted = self.add(embedding=vector())
        scope = self.scope()
        self.store._conn.execute("DROP TABLE message_fts")
        result = self.search(scope)
        self.assertEqual((wanted,), result.relevance_order)
        self.assertEqual("fts_unavailable", result.fallback_reason)

    def test_no_match_is_explicit_only_for_literal_fallback(self):
        self.add("unrelated", embedding=vector())
        scope = self.scope()
        self.assertEqual("no_match", self.search(scope, query_vector=None).status)
        nearest = self.search(scope, mode="semantic", query_vector=vector(0, 1))
        self.assertEqual("ok", nearest.status)
        self.assertFalse(nearest.coverage.complete_history)  # Similarity is not proof of answerability.

    def test_scan_cap_refuses_instead_of_selecting_newest_slice(self):
        for i in range(5):
            self.add(f"blue lantern {i}", embedding=vector())
        scope = self.scope()
        with patch("history_retrieval.HISTORY_SCAN_LIMIT", 4):
            result = self.search(scope)
            self.assertEqual("scope_too_large", result.status)
            self.assertEqual((), result.rows)
            self.assertFalse(preflight_history_retrieval(self.store, scope).can_embed)
        rows, overflow, _, _ = self.store.read_history_retrieval_candidates(
            chat_id=100, cutoff_memory_id=scope.cutoff_memory_id, cutoff_created_at=scope.cutoff_created_at,
            scan_limit=5)
        self.assertFalse(overflow)
        self.assertEqual(5, len(rows))
        self.assertEqual(8192, HISTORY_SCAN_LIMIT)

    def test_result_limit_is_bounded_and_relevance_order_matches_rows(self):
        for i in range(25):
            self.add(str(i), embedding=vector())
        result = self.search(mode="semantic", limit=99999)
        self.assertEqual(20, len(result.rows))
        self.assertEqual(tuple(row["id"] for row in result.rows), result.relevance_order)
        self.assertTrue(result.has_more_matching)

    def test_actual_8192_boundary_and_hard_scan_maximum(self):
        self.store._conn.executemany(
            "INSERT INTO messages(chat_id,created_at,text) VALUES (100,'2026-01-01T00:00:00Z',?)",
            ((f"synthetic {i}",) for i in range(8193)))
        self.store._conn.commit()
        exact = HistorySearchScope(100, 8193, "2026-02-01T00:00:00Z")
        coverage = preflight_history_retrieval(self.store, exact)
        self.assertEqual(8192, coverage.scoped_rows)
        self.assertEqual("no_usable_index", coverage.status)
        overflow = self.scope()
        result = self.search(overflow)
        self.assertEqual("scope_too_large", result.status)
        self.assertEqual(8193, result.coverage.scoped_rows)
        rows, refused, _, _ = self.store.read_history_retrieval_candidates(
            chat_id=100, cutoff_memory_id=overflow.cutoff_memory_id,
            cutoff_created_at=overflow.cutoff_created_at, scan_limit=999999)
        self.assertTrue(refused)
        self.assertEqual([], rows)

    def test_empty_scope_avoids_embedding_and_reports_no_match(self):
        scope = self.scope()
        self.assertFalse(preflight_history_retrieval(self.store, scope).can_embed)
        result = self.search(scope)
        self.assertEqual("empty_scope", result.coverage.status)
        self.assertEqual("no_match", result.status)

    def test_provider_admission_stops_at_first_fully_valid_vector(self):
        for _ in range(4):
            self.add(embedding=vector())
        scope = self.scope()
        with patch("history_retrieval.normalize_history_vector", wraps=normalize_history_vector) as validate:
            self.assertTrue(history_query_embedding_available(self.store, scope))
            self.assertEqual(1, validate.call_count)
        self.assertEqual(4, preflight_history_retrieval(self.store, scope).indexed_rows)
        self.assertEqual(4, len(self.search(scope, mode="semantic").rows))

    def test_provider_admission_rejects_invalid_all_and_finds_valid_after_invalid(self):
        stale = self.add(embedding=vector())
        self.change_embedding(stale, content_hash="stale")
        self.add(embedding=vector(float("nan")))
        self.add(embedding=vector(0, 0))
        self.assertFalse(history_query_embedding_available(self.store, self.scope()))
        self.add(embedding=vector())
        self.assertTrue(history_query_embedding_available(self.store, self.scope()))

    def test_provider_admission_checks_complete_scope_cap_before_first_valid_vector(self):
        for _ in range(3):
            self.add(embedding=vector())
        scope = self.scope()
        with patch("history_retrieval.HISTORY_SCAN_LIMIT", 2), \
             patch("history_retrieval.normalize_history_vector", wraps=normalize_history_vector) as validate:
            self.assertFalse(history_query_embedding_available(self.store, scope))
            validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
