"""Bounded original-message retrieval, authorization scope and budget invariants."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chat_history import ChatHistorySession, HistoryLimits
from memory import MemoryStore


class ChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temporary.name) / "memory.sqlite3")
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def add(self, text="ordinary authored text", **kwargs):
        self.sequence += 1
        values = dict(chat_id=100, message_id=self.sequence, created_at=self.start + timedelta(minutes=self.sequence),
                      sender_label="Test participant", user_id=1, text=text)
        values.update(kwargs)
        return self.store.save_message(**values)

    def session(self, **kwargs):
        current = self.add("current request")
        values = dict(chat_id=100, cutoff_memory_id=current,
                      cutoff_created_at=(self.start + timedelta(minutes=self.sequence)).isoformat())
        values.update(kwargs)
        return ChatHistorySession(self.store, **values)

    def read(self, session=None, **kwargs):
        return json.loads((session or self.session()).read(**kwargs))

    def test_original_messages_include_bots_and_invocations_without_embeddings(self):
        human = self.add("@example_bot find blue lanterns")
        bot = self.add("A blue lantern album", is_bot=True, reply_to_message_id=1)
        result = self.read()
        self.assertEqual([human, bot], [row["id"] for row in result["messages"]])
        self.assertEqual(1, result["messages"][1]["reply_to_message_id"])
        self.assertEqual(0, self.store._conn.execute("SELECT count(*) FROM message_embeddings").fetchone()[0])

    def test_unicode_lexical_search_includes_bot_rows_omitted_from_fts(self):
        self.add("other topic")
        expected = self.add("СИНІ ЛІХТАРІ", is_bot=True)
        result = self.read(mode="search", query="сині ліхтарі")
        self.assertEqual([expected], [row["id"] for row in result["messages"]])
        self.assertTrue(result["coverage"]["lexical_only"])

    def test_search_is_parameterized_and_requires_all_terms(self):
        self.add("lantern festival")
        self.add("lantern with red decoration")
        self.assertEqual("no_match", self.read(mode="search", query="lantern blue")["status"])
        self.assertEqual("no_match", self.read(mode="search", query="' OR 1=1 --")["status"])
        self.assertEqual(4, self.store.chat_message_count(100))  # Includes both reads' requests.

    def test_current_future_and_other_chat_rows_cannot_leak(self):
        allowed = self.add("allowed")
        self.add("other chat", chat_id=200)
        self.add("future dated", created_at=self.start + timedelta(days=30))
        session = self.session()
        self.add("late backfill", created_at=self.start)
        result = self.read(session)
        self.assertEqual([allowed], [row["id"] for row in result["messages"]])

    def test_filters_narrow_participant_and_half_open_date_interval(self):
        self.add("early", user_id=2)
        wanted = self.add("inside", user_id=2)
        self.add("wrong participant", user_id=3)
        self.add("right edge", user_id=2)
        result = self.read(participant_id=2, after="2026-01-01T00:02:00Z", before="2026-01-01T00:04:00Z")
        self.assertEqual([wanted], [row["id"] for row in result["messages"]])

    def test_timezone_order_is_chronological_not_lexical(self):
        later = self.add("later", created_at="2026-01-01T00:30:00-02:00")
        earlier = self.add("earlier", created_at="2026-01-01T01:00:00+00:00")
        result = self.read(self.session(cutoff_created_at="2026-01-02T00:00:00Z"))
        self.assertEqual([earlier, later], [row["id"] for row in result["messages"]])

    def test_around_revalidates_anchor_chat_and_cutoff(self):
        rows = [self.add(str(i)) for i in range(8)]
        foreign = self.add("foreign", chat_id=200)
        future = self.add("future", created_at="2027-01-01T00:00:00Z")
        session = self.session()
        result = self.read(session, mode="around", anchor_id=rows[4], limit=5)
        self.assertEqual(rows[2:7], [row["id"] for row in result["messages"]])
        for anchor in (foreign, future, 999999):
            self.assertEqual("no_match", self.read(session, mode="around", anchor_id=anchor)["status"])

    def test_around_at_edge_uses_remaining_slots_without_duplicate_anchor(self):
        rows = [self.add(str(i)) for i in range(8)]
        result = self.read(mode="around", anchor_id=rows[0], limit=5)
        self.assertEqual(rows[:5], [row["id"] for row in result["messages"]])

    def test_anchor_cannot_bypass_participant_filter(self):
        anchor = self.add("other writer", user_id=2)
        self.add("target writer", user_id=1)
        self.assertEqual("no_match", self.read(mode="around", anchor_id=anchor, participant_id=1)["status"])

    def test_default_and_hard_message_limits_are_real_not_instructions(self):
        rows = [self.add(str(i)) for i in range(30)]
        session = self.session()
        default = self.read(session)
        maximum = self.read(session, limit=999999)
        self.assertEqual(rows[-10:], [row["id"] for row in default["messages"]])
        self.assertEqual(rows[-20:], [row["id"] for row in maximum["messages"]])
        self.assertTrue(maximum["coverage"]["has_more_matching"])
        self.assertFalse(maximum["coverage"]["complete_history"])

    def test_low_limits_and_json_control_characters_obey_actual_serialized_caps(self):
        for _ in range(24):
            self.add("\u0001\n\\\"" * 1000, sender_label="z" * 300,
                     vision_summary="q" * 4000, attachment_type="photo" * 30)
        session = self.session(limits=HistoryLimits(max_row_chars=384, max_response_chars=2048, max_total_chars=4096))
        outputs = [session.read(limit=20) for _ in range(8)]
        self.assertLessEqual(sum(map(len, outputs)), 4096)
        self.assertEqual(sum(map(len, outputs)), session.chars_used)
        for output in outputs:
            if not output:
                continue
            self.assertLessEqual(len(output), 2048)
            for row in json.loads(output)["messages"]:
                self.assertLessEqual(len(json.dumps(row, ensure_ascii=False, separators=(",", ":"))), 384)
                self.assertTrue(row["truncated"])

    def test_safe_projection_never_returns_private_transport_or_source_fields(self):
        self.add("own comment", source_text="VISIBLE_QUOTE", raw_note="FORBIDDEN_NOTE",
                 telegram_file_id="FORBIDDEN_TOKEN", local_media_path="FORBIDDEN_PATH",
                 username="FORBIDDEN_USERNAME", source_url="FORBIDDEN_URL")
        result = self.session().read()
        self.assertNotIn("FORBIDDEN", result)
        self.assertIn("own comment", result)
        self.assertIn("VISIBLE_QUOTE", result)
        self.assertIn("untrusted_chat_history", result)

    def test_forwarded_source_only_post_is_searchable_and_not_attributed_to_sender(self):
        source = self.add("", source_text="A quoted observation about amber lanterns", forward_origin="a source")
        result = self.read(mode="search", query="amber lanterns")
        self.assertEqual([source], [row["id"] for row in result["messages"]])
        row = result["messages"][0]
        self.assertEqual("", row["text"])
        self.assertEqual(1, row["user_id"])
        self.assertIn("amber lanterns", row["source_text"])
        self.assertIn("not sender-authored", result["instruction"])

    def test_minimum_row_budget_covers_maximum_sqlite_identity_metadata(self):
        self.add("full text" * 500, source_text="quoted" * 500,
                 message_id=2**63 - 1, user_id=2**63 - 1, reply_to_message_id=2**63 - 1,
                 sender_label="a" * 200)
        result = self.read(self.session(limits=HistoryLimits(max_row_chars=384)))
        self.assertLessEqual(len(json.dumps(result["messages"][0], ensure_ascii=False, separators=(",", ":"))), 384)

    def test_history_reads_do_not_mutate_database(self):
        anchor = self.add("amber lanterns")
        session = self.session()
        changes = self.store._conn.total_changes
        with self.store._lock:
            self.store._conn.execute("PRAGMA query_only=ON")
        for request in ({}, {"mode": "search", "query": "lanterns"}, {"mode": "around", "anchor_id": anchor}):
            self.assertEqual("ok", self.read(session, **request)["status"])
        self.assertEqual(changes, self.store._conn.total_changes)

    def test_fixed_character_target_excludes_bot_forward_service_and_source_material(self):
        wanted = self.add("genuine own words", source_text="UNTRUSTED QUOTATION")
        self.add("other person", user_id=2)
        self.add("bot output", is_bot=True)
        self.add("forward", forward_origin="forwarded author")
        self.add("service", content_kind="service")
        self.add("[message has sticker]")
        self.add("", source_text="repost only")
        session = self.session(target_user_id=1)
        result = self.read(session)
        self.assertEqual([wanted], [row["id"] for row in result["messages"]])
        self.assertEqual("invalid_filter", self.read(session, participant_id=2)["status"])
        self.assertEqual("no_match", self.read(session, mode="search", query="QUOTATION")["status"])

    def test_returned_ids_are_exactly_exposed_not_discarded_candidates(self):
        for _ in range(8):
            self.add("large " * 500)
        session = self.session(limits=HistoryLimits(max_response_chars=2048))
        output = self.read(session, limit=8)
        self.assertEqual({row["id"] for row in output["messages"]}, session.exposed_ids)
        self.assertLess(len(session.exposed_ids), 8)

    def test_four_call_limit_and_invalid_filters_do_not_trigger_unbounded_reads(self):
        self.add()
        session = self.session()
        with patch.object(self.store, "bounded_history_rows", wraps=self.store.bounded_history_rows) as reader:
            for _ in range(6):
                session.read()
        self.assertEqual(4, reader.call_count)
        self.assertEqual(4, session.calls_used)
        self.assertFalse(session.available)

    def test_invalid_filters_fail_closed(self):
        for request in (
            {"mode": "unknown"}, {"mode": "around"}, {"after": "yesterday"},
            {"after": "2026-01-02", "before": "2026-01-01"}, {"participant_id": "1 OR 1"},
            {"mode": "search", "query": ""}, {"mode": "search", "query": "a" * 257},
            {"after": "2026-01-01T02:00:00"}, {"limit": True},
            {"after": 1},
        ):
            with self.subTest(request=request):
                self.assertEqual("invalid_filter", self.read(**request)["status"])

    def test_configuration_can_only_reduce_hard_ceilings(self):
        for kwargs in ({"max_calls": 5}, {"max_total_chars": 30001}, {"max_messages": 21},
                       {"max_row_chars": 1001}, {"max_response_chars": 12001}, {"default_messages": True}):
            with self.assertRaises(ValueError):
                HistoryLimits(**kwargs)
        session = self.session(limits=HistoryLimits(default_messages=2, max_messages=3, max_calls=1))
        session.read()
        self.assertFalse(session.available)

    def test_no_match_and_partial_coverage_do_not_claim_absence_or_full_history(self):
        result = self.read(mode="search", query="unknown topic")
        self.assertEqual("no_match", result["status"])
        self.assertIn("not proof of absence", result["instruction"])
        self.assertIsNone(result["coverage"]["returned_start"])
        self.assertFalse(result["coverage"]["complete_history"])

    def test_sqlite_failure_is_sanitized_and_refunds_reservation(self):
        import sqlite3
        session = self.session()
        with patch.object(self.store, "bounded_history_rows", side_effect=sqlite3.OperationalError("PRIVATE_PATH")):
            output = session.read()
        self.assertEqual("history_unavailable", json.loads(output)["status"])
        self.assertNotIn("PRIVATE_PATH", output)
        self.assertEqual(len(output), session.chars_used)
        self.assertTrue(session.available)

    def test_parallel_threads_enforce_call_and_total_output_budgets(self):
        for _ in range(25):
            self.add("x" * 2000)
        session = self.session()
        with patch.object(self.store, "bounded_history_rows", wraps=self.store.bounded_history_rows) as reader:
            with ThreadPoolExecutor(max_workers=12) as pool:
                outputs = list(pool.map(lambda _: session.read(limit=20), range(12)))
        self.assertLessEqual(reader.call_count, 4)
        self.assertLessEqual(sum(map(len, outputs)), 30000)
        self.assertEqual(sum(map(len, outputs)), session.chars_used)
        self.assertTrue(all(len(output) <= 12000 for output in outputs))

    def test_async_parallel_tools_claim_before_first_await(self):
        self.add("evidence")
        session = self.session()
        event = threading.Event()
        original = self.store.bounded_history_rows

        def blocked(**kwargs):
            event.wait(timeout=3)
            return original(**kwargs)

        async def run():
            pending = [asyncio.create_task(session.aread()) for _ in range(10)]
            await asyncio.sleep(0.05)
            self.assertLessEqual(session.calls_used, 4)
            self.assertGreater(session.calls_used, 0)
            event.set()
            return await asyncio.gather(*pending)

        with patch.object(self.store, "bounded_history_rows", side_effect=blocked) as reader:
            outputs = asyncio.run(run())
        self.assertLessEqual(reader.call_count, 4)
        self.assertLessEqual(sum(map(len, outputs)), 30000)
        self.assertEqual(sum(map(len, outputs)), session.chars_used)


if __name__ == "__main__":
    unittest.main()
