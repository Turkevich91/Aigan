"""Verifiable history links and truthful retained-evidence coverage, without I/O."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from chat_history import ChatHistorySession
from history_citations import HistoryCitationSession
from memory import MemoryStore


class HistoryCitationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        self.old = self.save(11, "The agreed meeting place is Cedar Square.")
        self.current = self.save(30, "Where did we agree to meet?", created_at=self.now)
        self.history = ChatHistorySession(self.store, chat_id=-100123,
            cutoff_memory_id=self.current, cutoff_created_at=self.now.isoformat())
        self.citations = self.registry()

    def save(self, message_id, text, **kwargs):
        values = dict(chat_id=-100123, message_id=message_id, text=text, user_id=1,
                      sender_label="Synthetic speaker", created_at=self.now - timedelta(days=2))
        values.update(kwargs)
        return self.store.save_message(**values)

    def registry(self, chat_type="supergroup"):
        return HistoryCitationSession(self.store, chat_id=-100123, chat_type=chat_type,
            cutoff_memory_id=self.current, cutoff_created_at=self.now.isoformat(), history=self.history)

    def expose(self, row_id=None, registry=None):
        registry = registry or self.citations
        item = self.store.item_by_id(row_id or self.old)
        line = registry.decorate_context(item, "Synthetic visible evidence")
        registry.expose_contexts((line,))
        return re.search(r"\[\[history:[^\]]+\]\]", line).group()

    def test_preloaded_evidence_links_without_history_or_provider_call(self):
        ref = self.expose()
        result = self.citations.render("Cedar Square. " + ref)
        self.assertIn("Cedar Square. [1]", result)
        self.assertIn("https://t.me/c/123/11", result)
        self.assertIn("контексті: 1", result)
        self.assertEqual(self.history.calls_used, 0)
        self.assertNotIn("[[history:", result)

    def test_candidate_not_in_final_context_cannot_be_cited(self):
        line = self.citations.decorate_context(self.store.item_by_id(self.old), "omitted evidence")
        ref = re.search(r"\[\[history:[^\]]+\]\]", line).group()
        self.citations.expose_contexts(("different evidence",))
        result = self.citations.render(ref)
        self.assertNotIn("https://t.me", result)
        self.assertIn("недоступне", result)
        self.assertEqual(self.citations.exposed_ids, frozenset())

    def test_only_reference_in_untrusted_text_does_not_expose_candidate(self):
        line = self.citations.decorate_context(self.store.item_by_id(self.old), "trusted formatter line")
        ref = re.search(r"\[\[history:[^\]]+\]\]", line).group()
        self.citations.expose_contexts(("a quote containing only " + ref,))
        self.assertNotIn("https://t.me", self.citations.render(ref))

    def test_embedded_formatter_fragment_is_not_a_standalone_context_record(self):
        line = self.citations.decorate_context(self.store.item_by_id(self.old), "formatted source")
        ref = line.split()[-1]
        self.citations.expose_contexts(("other source quotes: " + line + " trailing text",))
        self.assertEqual(frozenset(), self.citations.exposed_ids)
        self.assertNotIn("https://t.me/c/", self.citations.render(ref))

    def test_complete_multiline_formatter_record_remains_citable(self):
        line = self.citations.decorate_context(self.store.item_by_id(self.old), "first line\nsecond line")
        ref = line.split()[-1]
        self.citations.expose_contexts(("context header\n" + line + "\nnext source",))
        self.assertEqual(frozenset({self.old}), self.citations.exposed_ids)
        self.assertIn("https://t.me/c/", self.citations.render(ref))

    def test_foreign_session_reference_is_rejected(self):
        ref = self.expose()
        self.assertNotIn("https://t.me", self.registry().render(ref))

    def test_context_scope_rejects_current_future_late_import_and_other_chat(self):
        rows = [self.current,
                self.save(31, "late imported text", created_at=self.now - timedelta(days=5)),
                self.save(32, "future", created_at=self.now + timedelta(days=1)),
                self.save(33, "other chat", chat_id=-100456)]
        for row_id in rows:
            with self.subTest(row_id=row_id):
                self.assertEqual(self.citations.decorate_context(self.store.item_by_id(row_id), "x"), "x")

    def test_edit_past_preview_invalidates_old_source(self):
        self.save(11, "x" * 1500 + "old tail")
        ref = self.expose()
        self.save(11, "x" * 1500 + "new tail")
        result = self.citations.render(ref)
        self.assertNotIn("https://t.me", result)
        self.assertIn("змінилося", result)

    def test_deleted_source_has_no_link(self):
        ref = self.expose()
        with patch.object(self.store, "item_by_id", return_value=None):
            result = self.citations.render(ref)
        self.assertNotIn("https://t.me", result)

    def test_unsupported_chat_has_dated_honest_fallback(self):
        for kind in ("private", "group", "unknown"):
            registry = self.registry(kind)
            result = registry.render(self.expose(registry=registry))
            self.assertNotIn("https://t.me", result)
            self.assertIn("18.08.2026", result)
            self.assertIn("пряме посилання недоступне", result)

    def test_forwarded_source_links_current_copy_and_labels_origin(self):
        self.save(11, "", source_text="Forwarded synthetic statement", forward_origin="Synthetic origin")
        result = self.citations.render(self.expose())
        self.assertIn("матеріал у чаті", result)
        self.assertIn("https://t.me/c/123/11", result)
        self.assertNotIn("Synthetic origin", result)

    def test_tool_and_preloaded_same_evidence_not_double_counted(self):
        original_ref = self.expose()
        payload = json.loads(self.history.read())
        tool_ref = payload["messages"][0]["citation_ref"]
        result = self.citations.render(f"Cedar Square {original_ref}; the original says so {tool_ref}")
        self.assertEqual(result.count("https://t.me"), 1)
        self.assertIn("контексті: 1", result)

    def test_search_coverage_is_not_full_period_claim(self):
        payload = json.loads(self.history.read(mode="search", query="Cedar", after="2026-08-01", before="2026-08-19"))
        result = self.citations.render(payload["messages"][0]["citation_ref"])
        self.assertIn("Останній фільтр дат", result)
        self.assertIn("верхня межа не включена", result)
        self.assertIn("вибірка зі збережених", result)

    def test_generated_private_message_url_cannot_bypass_registry(self):
        result = self.citations.render("Source https://t.me/c/456/900 " + self.expose())
        self.assertNotIn("/456/900", result)
        self.assertIn("https://t.me/c/123/11", result)

    def test_reply_cap_keeps_complete_verified_footer(self):
        result = self.citations.render("x" * 3000 + self.expose(), max_chars=420)
        self.assertLessEqual(len(result), 420)
        self.assertIn("https://t.me/c/123/11", result)
        self.assertIn("вибірка зі збережених повідомлень.", result)

    def test_no_history_use_adds_no_footer(self):
        self.expose()
        self.assertEqual(self.citations.render("Hello"), "Hello")

    def test_alternative_unverified_telegram_message_links_are_removed(self):
        for url in ("t.me/c/456/900", "telegram.me/c/456/900", "tg://privatepost?channel=456&post=900",
                    "https://t.me/other_chat/900", "other_chat.t.me/900", "tg://resolve?domain=other_chat&post=900"):
            with self.subTest(url=url):
                result = self.citations.render(url + " " + self.expose())
                self.assertNotIn(url, result)
                self.assertIn("https://t.me/c/123/11", result)

    def test_old_prefetch_reference_does_not_reauthorize_after_edit_and_reexposure(self):
        old_ref = self.expose()
        self.save(11, "The place changed.")
        new_ref = self.expose()
        self.assertNotEqual(old_ref, new_ref)
        self.assertNotIn("https://t.me", self.citations.render(old_ref))
        self.assertIn("https://t.me", self.citations.render(new_ref))

    def test_tool_only_forward_with_empty_source_is_labeled(self):
        self.save(11, "Forwarded text", forward_origin="Synthetic origin")
        payload = json.loads(self.history.read())
        result = self.citations.render(payload["messages"][0]["citation_ref"])
        self.assertIn("матеріал у чаті", result)
        self.assertNotIn("Synthetic origin", result)

    def test_open_upper_date_filter_is_not_called_exclusive(self):
        self.history.read(after="2026-08-01")
        result = self.citations.render("A bounded selection")
        self.assertNotIn("верхня межа не включена", result)


if __name__ == "__main__":
    unittest.main()
