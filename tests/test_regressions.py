import asyncio
import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"aigan-test-{os.getpid()}.sqlite3")
try:
    os.remove(TEST_DB_PATH)
except FileNotFoundError:
    pass

os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test-token"
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["ALLOWED_CHAT_IDS"] = "-1001"
os.environ["ADMIN_USER_IDS"] = "407892151"
os.environ["AUTO_REACT_ENABLED"] = "false"
os.environ["BOT_TIMEZONE"] = "America/New_York"
os.environ["MAX_REPLY_CHARS"] = "12000"
os.environ["TELEGRAM_TEXT_CHUNK_CHARS"] = "3500"
os.environ["MAX_REPLY_CHUNKS"] = "4"
os.environ["FOLLOWUP_DEBOUNCE_SECONDS"] = "0.5"
os.environ["MEMORY_ENABLED"] = "true"
os.environ["MEMORY_DB_PATH"] = TEST_DB_PATH
os.environ["MEMORY_CONTEXT_MESSAGES"] = "10"
os.environ["MEMORY_FOLLOWUP_CONTEXT_MESSAGES"] = "40"
os.environ["MEMORY_THREAD_CONTEXT_DEPTH"] = "6"
os.environ["MEMORY_RETENTION_DAYS"] = "30"
os.environ["MEMORY_IMAGE_SUMMARY_LIMIT"] = "3"
os.environ["MEMORY_VECTOR_ENABLED"] = "true"
os.environ["MEMORY_EMBEDDING_MODEL"] = "text-embedding-3-small"
os.environ["MEMORY_EMBEDDING_DIMENSIONS"] = "4"
os.environ["MEMORY_SEMANTIC_LOOKBACK_DAYS"] = "30"
os.environ["MEMORY_SEMANTIC_TOP_K"] = "3"
os.environ["MEMORY_EMBEDDING_BATCH_SIZE"] = "2"
os.environ["MEMORY_VECTOR_BACKFILL_ON_START"] = "false"
os.environ["MEMORY_VECTOR_BACKFILL_LIMIT"] = "10"
os.environ["MEMORY_RECALL_INTENT_THRESHOLD"] = "0.62"
os.environ["MEMORY_RECALL_INTENT_AMBIGUOUS_THRESHOLD"] = "0.48"
os.environ["SYSTEM_LOG_ENABLED"] = "true"
os.environ["SYSTEM_LOG_RETENTION_DAYS"] = "14"
os.environ["GITHUB_REPORTING_ENABLED"] = "false"
os.environ["COMPLAINT_LOOKBACK_SECONDS"] = "86400"
os.environ["COMPLAINT_REPORT_TEMPERATURE"] = "3"

import httpx
from telegram import InputMediaPhoto
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest

import main
from memory import MemoryStore
from mcp_servers import web
from scripts import import_telegram_export
from scripts.import_telegram_export import ImportOptions
from self_analysis import SelfAnalysisService, classify_complaint
from system_log import SystemLogStore, redact_secrets

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"valid-jpeg"


class FakeUser:
    def __init__(self, user_id: int = 407892151, username: str = "tester") -> None:
        self.id = user_id
        self.is_bot = False
        self.full_name = "Test User"
        self.username = username


class FakeMessage:
    def __init__(
        self,
        text: str = "",
        chat_type: str = ChatType.SUPERGROUP,
        chat_id: int = -1001,
        message_id: int = 101,
    ) -> None:
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = datetime.now(timezone.utc)
        self.chat = SimpleNamespace(type=chat_type, title="Test Chat")
        self.from_user = FakeUser()
        self.photo = []
        self.document = None
        self.reply_to_message = None
        self.external_reply = None
        self.quote = None
        self.entities = None
        self.reply_calls = []
        self.photo_calls = []
        self.photo_failures = 0
        self.media_group_calls = []
        self.media_group_attempts = 0
        self.media_group_failures = 0
        self.bot = SimpleNamespace(send_chat_action=AsyncMock())

    async def reply_text(self, text: str, **kwargs) -> None:
        self.last_reply = text
        self.reply_calls.append({"text": text, **kwargs})

    async def reply_photo(self, photo, **kwargs) -> None:
        if self.photo_failures > 0:
            self.photo_failures -= 1
            raise BadRequest("failed to send photo")
        self.photo_calls.append({"photo": photo, **kwargs})

    async def reply_media_group(self, media, **kwargs):
        self.media_group_attempts += 1
        if self.media_group_failures > 0:
            self.media_group_failures -= 1
            raise BadRequest("failed to send media group")
        self.media_group_calls.append({"media": tuple(media), **kwargs})
        return tuple(SimpleNamespace(message_id=self.message_id + index) for index, _ in enumerate(media, start=1))

    def get_bot(self):
        return self.bot


class FakeTelegramFile:
    def __init__(self, data: bytes = b"fake-image") -> None:
        self.data = data

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self.data)


class FakePhoto:
    def __init__(self, file_id: str = "photo-file", unique_id: str = "photo-unique", data: bytes = b"fake-image") -> None:
        self.file_id = file_id
        self.file_unique_id = unique_id
        self._file = FakeTelegramFile(data)

    async def get_file(self) -> FakeTelegramFile:
        return self._file


class PendingFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        main.pending_requests.clear()
        main.passive_contexts.clear()
        main.histories.clear()
        main.last_user_call.clear()
        main.last_chat_call.clear()
        main.last_auto_react_chat.clear()
        if main.MEMORY is not None:
            main.MEMORY.clear_all()

    def test_passive_group_context_text_does_not_create_pending(self) -> None:
        message = FakeMessage("це що")

        consumed = asyncio.run(main.handle_pending_or_observe(message, SimpleNamespace()))

        self.assertFalse(consumed)
        self.assertEqual({}, main.pending_requests)
        self.assertIn("це що", main.format_passive_context(message.chat_id))

    def test_explicit_context_dependent_prompt_still_creates_pending(self) -> None:
        message = FakeMessage("поясни")

        asyncio.run(main.handle_prompt(message, SimpleNamespace(), "поясни"))

        self.assertIn((message.chat_id, message.from_user.id), main.pending_requests)

    def test_existing_pending_is_consumed_by_next_message(self) -> None:
        request = FakeMessage("поясни")
        followup = FakeMessage("forwarded payload")
        main.store_pending_request(request, "поясни", "context")

        with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
            consumed = asyncio.run(main.handle_pending_or_observe(followup, SimpleNamespace()))

        self.assertTrue(consumed)
        self.assertEqual({}, main.pending_requests)
        handle_prompt.assert_awaited_once_with(followup, ANY, "поясни", allow_pending_wait=False)

    def test_followup_debounce_seconds_is_float(self) -> None:
        self.assertEqual(0.5, main.CONFIG.followup_debounce_seconds)

    def test_debounce_continues_original_prompt_if_no_followup_arrives(self) -> None:
        message = FakeMessage("поясни")
        token = main.store_pending_request(message, "поясни", "context")
        self.assertIsNotNone(token)

        with patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep:
            with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
                asyncio.run(main.resolve_pending_after_debounce(message, SimpleNamespace(), "поясни", token))

        sleep.assert_awaited_once_with(0.5)
        handle_prompt.assert_awaited_once_with(message, ANY, "поясни", allow_pending_wait=False)
        self.assertTrue(main.has_pending_request(message))

    def test_followup_during_debounce_suppresses_original_prompt(self) -> None:
        message = FakeMessage("поясни")
        followup = FakeMessage("forwarded payload")
        token = main.store_pending_request(message, "поясни", "context")
        self.assertIsNotNone(token)
        main.pop_pending_request(followup)

        with patch.object(main.asyncio, "sleep", new=AsyncMock()):
            with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
                asyncio.run(main.resolve_pending_after_debounce(message, SimpleNamespace(), "поясни", token))

        handle_prompt.assert_not_awaited()

    def test_passive_context_does_not_skip_debounce_for_vague_request(self) -> None:
        message = FakeMessage("що це")
        main.passive_contexts[message.chat_id].append("Someone: prior context")

        with patch.object(main, "start_pending_debounce", new=AsyncMock()) as start_pending:
            asyncio.run(main.handle_prompt(message, SimpleNamespace(), "що це"))

        start_pending.assert_awaited_once_with(message, ANY, "що це", "followup_context")

    def test_direct_image_reference_and_url_paths_do_not_debounce(self) -> None:
        image_message = FakeMessage("що на фото")
        image_message.photo = [object()]
        reply_message = FakeMessage("поясни")
        reply_message.reply_to_message = FakeMessage("referenced text")

        self.assertFalse(main.should_wait_for_followup_context(image_message, "що на фото"))
        self.assertFalse(main.should_wait_for_followup_context(reply_message, "поясни"))
        self.assertFalse(main.should_wait_for_followup_context(FakeMessage("перекажи https://example.com"), "перекажи https://example.com"))


class WebSafetyTests(unittest.TestCase):
    @staticmethod
    def fake_getaddrinfo(host: str, *args, **kwargs):
        if host == "example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        if host in {"internal.test", "127.0.0.1"}:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        raise socket.gaierror(host)

    def test_safe_url_rejects_private_and_russian_hosts(self) -> None:
        with self.assertRaisesRegex(ValueError, "Russian domains"):
            web._safe_url("https://example.ru/news")

        with patch.object(web.socket, "getaddrinfo", side_effect=self.fake_getaddrinfo):
            with self.assertRaisesRegex(ValueError, "local/private"):
                web._safe_url("http://internal.test/metadata")

    def test_fetch_url_rejects_redirect_to_private_host(self) -> None:
        class RedirectClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def get(self, url: str) -> httpx.Response:
                request = httpx.Request("GET", url)
                return httpx.Response(
                    302,
                    headers={"location": "http://127.0.0.1/private"},
                    request=request,
                )

        with patch.object(web.socket, "getaddrinfo", side_effect=self.fake_getaddrinfo):
            with patch.object(web.httpx, "Client", RedirectClient):
                result = web.fetch_url("http://example.com/start")

        self.assertIn("Fetch failed: ValueError", result)
        self.assertIn("local/private", result)

    def test_image_search_filters_unsafe_hosts(self) -> None:
        class FakeDDGS:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def images(self, query: str, max_results: int, region: str):
                return [
                    {"title": "blocked", "image": "https://bad.ru/image.jpg", "url": "https://bad.ru/page"},
                    {"title": "ok", "image": "https://example.com/image.jpg", "url": "https://example.com/page"},
                ]

        with patch.object(web.socket, "getaddrinfo", side_effect=self.fake_getaddrinfo):
            with patch.object(web, "DDGS", FakeDDGS):
                results = web.search_image_candidates("test", max_results=3)

        self.assertEqual(1, len(results))
        self.assertEqual("ok", results[0]["title"])


class TimeContextTests(unittest.TestCase):
    def test_time_metadata_includes_configured_timezone_and_utc(self) -> None:
        context = main.current_time_context()

        self.assertIn("America/New_York", context)
        self.assertIn("Current UTC time:", context)
        self.assertIn("authoritative", context)

    def test_agent_prompt_is_wrapped_with_current_time_metadata(self) -> None:
        wrapped = main.with_current_time_metadata("Trusted request body")

        self.assertTrue(wrapped.startswith("Current time metadata:\n"))
        self.assertIn("Trusted request body", wrapped)


class TelegramFormattingTests(unittest.TestCase):
    def test_markdown_bold_is_rendered_as_telegram_html(self) -> None:
        rendered = main.render_telegram_html("Коротко: **важливо**")

        self.assertEqual("Коротко: <b>важливо</b>", rendered)
        self.assertNotIn("**", rendered)

    def test_plain_angle_ampersand_text_is_escaped(self) -> None:
        rendered = main.render_telegram_html("2 < 3 & <b>ok</b>")

        self.assertEqual("2 &lt; 3 &amp; <b>ok</b>", rendered)

    def test_send_reply_uses_telegram_html_parse_mode(self) -> None:
        message = FakeMessage()

        asyncio.run(main.send_reply(message, "**важливо**"))

        self.assertEqual("<b>важливо</b>", message.reply_calls[0]["text"])
        self.assertEqual(ParseMode.HTML, message.reply_calls[0]["parse_mode"])

    def test_bad_html_send_retries_plain_text_without_parse_mode(self) -> None:
        calls = []

        async def flaky_sender(text: str, **kwargs) -> None:
            calls.append({"text": text, **kwargs})
            if kwargs.get("parse_mode") == ParseMode.HTML:
                raise BadRequest("can't parse entities")

        asyncio.run(main.send_formatted_text(flaky_sender, "**важливо**"))

        self.assertEqual(2, len(calls))
        self.assertEqual("<b>важливо</b>", calls[0]["text"])
        self.assertEqual(ParseMode.HTML, calls[0]["parse_mode"])
        self.assertEqual("важливо", calls[1]["text"])
        self.assertNotIn("parse_mode", calls[1])

    def test_chat_sender_helper_uses_formatter(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())

        asyncio.run(main.send_chat_text(bot, -1001, "**auto**"))

        bot.send_message.assert_awaited_once_with(chat_id=-1001, text="<b>auto</b>", parse_mode=ParseMode.HTML)

    def test_send_reply_smart_splits_long_text_without_3600_truncation(self) -> None:
        message = FakeMessage()
        text = "A" * 4100

        asyncio.run(main.send_reply(message, text))

        self.assertGreater(len(message.reply_calls), 1)
        self.assertNotIn("[trimmed]", "\n".join(call["text"] for call in message.reply_calls))
        self.assertNotIn("[...] скорочено", "\n".join(call["text"] for call in message.reply_calls))
        self.assertTrue(all(len(call["text"]) <= main.CONFIG.telegram_text_chunk_chars for call in message.reply_calls))

    def test_split_text_prefers_paragraph_boundary(self) -> None:
        chunks = main.split_text_chunks("intro\n\n" + "body " * 12, chunk_chars=40, max_chunks=10, max_total_chars=500)

        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("intro"))

    def test_single_huge_paragraph_is_hard_wrapped(self) -> None:
        chunks = main.split_text_chunks("x" * 130, chunk_chars=50, max_chunks=10, max_total_chars=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 50 for chunk in chunks))

    def test_too_many_text_chunks_are_capped_with_marker(self) -> None:
        text = "\n\n".join(f"paragraph {index} " * 4 for index in range(10))

        chunks = main.split_text_chunks(text, chunk_chars=45, max_chunks=2, max_total_chars=1000)

        self.assertEqual(2, len(chunks))
        self.assertIn("[...] скорочено", chunks[-1])


class PersistentMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        if main.MEMORY is not None:
            main.MEMORY.clear_all()
        if main.SYSTEM_LOG is not None:
            main.SYSTEM_LOG.clear_all()
        main.passive_contexts.clear()
        main.histories.clear()
        main.pending_requests.clear()
        main.last_user_call.clear()
        main.last_chat_call.clear()
        main.last_auto_react_chat.clear()
        main.embedding_queue = None

    def tearDown(self) -> None:
        if main.MEMORY is not None:
            main.MEMORY.clear_all()
        if main.SYSTEM_LOG is not None:
            main.SYSTEM_LOG.clear_all()
        main.embedding_queue = None

    def test_messages_persist_after_ram_context_is_cleared(self) -> None:
        self.assertIsNotNone(main.MEMORY)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1,
            sender_label="Tester",
            text="persistent hello",
            created_at=datetime.now(timezone.utc),
        )
        main.passive_contexts.clear()
        main.histories.clear()

        context = main.format_memory_context(-1001, 10)

        self.assertIn("persistent hello", context)

    def test_user_messages_filter_by_user_and_limit(self) -> None:
        base = datetime.now(timezone.utc)
        for index in range(105):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=1000 + index,
                sender_label="Alpha",
                user_id=111,
                username="alpha",
                text=f"sample-{index:03d}",
                created_at=base + timedelta(seconds=index),
            )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2000,
            sender_label="Beta",
            user_id=222,
            username="beta",
            text="other user text",
            created_at=base + timedelta(seconds=200),
        )

        limited = main.MEMORY.user_messages(-1001, user_id=111, limit=100)
        all_items = main.MEMORY.user_stats(-1001, username="alpha")

        self.assertEqual(100, len(limited))
        self.assertEqual(105, len(all_items))
        self.assertNotIn("sample-000", [item.text for item in limited])
        self.assertEqual("sample-005", limited[0].text)
        self.assertEqual("sample-104", limited[-1].text)

    def test_user_messages_exclude_media_only_placeholders(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2100,
            sender_label="Alpha",
            user_id=111,
            username="alpha",
            text="[message has attachment(s): sticker]",
            content_kind="attachment",
            attachment_type="sticker",
            created_at=now,
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2101,
            sender_label="Alpha",
            user_id=111,
            username="alpha",
            text="[message has attachment(s): photo]",
            content_kind="image",
            attachment_type="photo",
            created_at=now + timedelta(seconds=1),
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2102,
            sender_label="Alpha",
            user_id=111,
            username="alpha",
            text="caption text should count",
            content_kind="image",
            attachment_type="photo",
            created_at=now + timedelta(seconds=2),
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2103,
            sender_label="Alpha",
            user_id=111,
            username="alpha",
            text="plain text should count",
            content_kind="text",
            created_at=now + timedelta(seconds=3),
        )

        items = main.MEMORY.user_stats(-1001, user_id=111)

        self.assertEqual(["caption text should count", "plain text should count"], [item.text for item in items])

    def test_agent_input_marks_persistent_memory_untrusted(self) -> None:
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2,
            sender_label="Tester",
            text="quoted source text",
            created_at=datetime.now(timezone.utc),
        )
        message = FakeMessage("поясни", message_id=3)

        agent_input = main.build_agent_input(message, "поясни", main.format_memory_context(-1001))

        self.assertIn("Untrusted persistent recent chat memory", agent_input)
        self.assertIn("quoted source text", agent_input)

    def test_normal_prompt_uses_normal_memory_window_only(self) -> None:
        base = datetime.now(timezone.utc)
        for index in range(12):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=index + 1,
                sender_label="Tester",
                text="old topic anchor" if index == 0 else f"recent filler {index}",
                created_at=base + timedelta(seconds=index),
            )
        message = FakeMessage("@thrd_ua_bot дай огляд", message_id=100)

        memory_context, expanded_context = asyncio.run(main.prepare_agent_memory_context(message, "дай огляд", "normal"))

        self.assertNotIn("old topic anchor", memory_context)
        self.assertIsNone(expanded_context)

    def test_short_followup_uses_expanded_memory_window(self) -> None:
        base = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1,
            sender_label="Vladimir",
            text="Subnautica topic anchor: перша частина була дуже давно",
            created_at=base,
        )
        for index in range(14):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=2 + index,
                sender_label="Tester",
                text=f"short filler {index}",
                created_at=base + timedelta(seconds=index + 1),
            )
        message = FakeMessage("@thrd_ua_bot скільки?", message_id=100)

        memory_context, expanded_context = asyncio.run(main.prepare_agent_memory_context(message, "скільки?", "normal"))
        agent_input = main.build_agent_input(
            message,
            "скільки?",
            memory_context=memory_context,
            expanded_memory_context=expanded_context,
        )

        self.assertNotIn("Subnautica topic anchor", memory_context)
        self.assertIsNotNone(expanded_context)
        self.assertIn("Subnautica topic anchor", expanded_context)
        self.assertIn("Untrusted expanded recent chat memory for short follow-up", agent_input)
        self.assertIn("ask one concise clarifying question", agent_input)

    def test_reply_chain_expansion_includes_parent_outside_normal_window(self) -> None:
        base = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1,
            sender_label="Alpha",
            text="reply-chain parent says the amount is five thousand",
            created_at=base,
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=2,
            sender_label="Beta",
            text="reply-chain child asks how much",
            reply_to_message_id=1,
            created_at=base + timedelta(seconds=1),
        )
        for index in range(50):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3 + index,
                sender_label="Filler",
                text=f"filler {index}",
                created_at=base + timedelta(seconds=index + 2),
            )
        message = FakeMessage("@thrd_ua_bot скільки?", message_id=200)
        message.reply_to_message = FakeMessage("reply-chain child asks how much", message_id=2)

        memory_context, expanded_context = asyncio.run(main.prepare_agent_memory_context(message, "скільки?", "normal"))

        self.assertNotIn("reply-chain parent says the amount", memory_context)
        self.assertIn("reply-chain parent says the amount", expanded_context)

    def test_translation_route_does_not_use_expanded_followup_memory(self) -> None:
        message = FakeMessage("@thrd_ua_bot переклади українською", message_id=300)

        _, expanded_context = asyncio.run(
            main.prepare_agent_memory_context(message, "переклади українською", "translate_reference")
        )

        self.assertIsNone(expanded_context)

    def test_ordinary_group_short_followup_stays_silent(self) -> None:
        message = FakeMessage("скільки?", message_id=400)
        context = SimpleNamespace(bot=SimpleNamespace(id=999, username="thrd_ua_bot"))

        asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))

        self.assertEqual([], message.reply_calls)
        self.assertEqual({}, main.pending_requests)
        self.assertIn("скільки?", main.format_passive_context(message.chat_id))

    def test_short_followup_expansion_records_system_event(self) -> None:
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1,
            sender_label="Tester",
            text="topic anchor",
            created_at=datetime.now(timezone.utc),
        )
        message = FakeMessage("@thrd_ua_bot що?", message_id=500)

        asyncio.run(main.prepare_agent_memory_context(message, "що?", "normal"))

        events = main.SYSTEM_LOG.latest_events(5)
        self.assertTrue(any(event.event_type == "memory_context_expanded" for event in events))

    def test_vector_schema_and_fts_are_created_without_losing_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.sqlite3", retention_days=30)
            store.save_message(chat_id=-1001, message_id=1, sender_label="Tester", text="semantic schema test")

            self.assertEqual(1, len(store.latest(-1001, 10)))
            self.assertTrue(store.fts_search(chat_id=-1001, query="semantic", lookback_days=30, limit=3))

    def test_rebuild_search_index_populates_existing_messages(self) -> None:
        main.MEMORY.save_message(chat_id=-1001, message_id=1000, sender_label="Tester", text="legacy semantic text")
        main.MEMORY._conn.execute("DELETE FROM message_fts")
        main.MEMORY._conn.commit()

        indexed = main.MEMORY.rebuild_search_index()

        self.assertGreaterEqual(indexed, 1)
        self.assertTrue(main.MEMORY.fts_search(chat_id=-1001, query="legacy semantic", lookback_days=30, limit=3))

    def test_embedding_candidates_include_only_user_messages(self) -> None:
        main.MEMORY.save_message(chat_id=-1001, message_id=1001, sender_label="User", text="user searchable text")
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1002,
            sender_label="Aigan",
            is_bot=True,
            text="bot searchable text",
        )

        candidates = main.MEMORY.pending_embedding_candidates(
            model=main.CONFIG.memory_embedding_model,
            dimensions=main.CONFIG.memory_embedding_dimensions,
            limit=10,
            lookback_days=30,
        )

        self.assertEqual(["user searchable text"], [candidate.search_text for candidate in candidates])

    def test_embedding_failure_does_not_prevent_message_persistence(self) -> None:
        main.MEMORY.save_message(chat_id=-1001, message_id=1010, sender_label="User", text="will survive failure")
        candidates = main.MEMORY.pending_embedding_candidates(
            model=main.CONFIG.memory_embedding_model,
            dimensions=main.CONFIG.memory_embedding_dimensions,
            limit=10,
            lookback_days=30,
        )

        with patch.object(main, "create_embeddings", new=AsyncMock(side_effect=RuntimeError("embedding down"))):
            stored = asyncio.run(main.process_embedding_candidates(candidates, "test"))

        self.assertEqual(0, stored)
        self.assertIn("will survive failure", main.format_memory_context(-1001, 10))

    def test_background_embedding_batch_stores_user_embedding(self) -> None:
        main.MEMORY.save_message(chat_id=-1001, message_id=1020, sender_label="User", text="indexed user text")
        candidates = main.MEMORY.pending_embedding_candidates(
            model=main.CONFIG.memory_embedding_model,
            dimensions=main.CONFIG.memory_embedding_dimensions,
            limit=10,
            lookback_days=30,
        )

        with patch.object(main, "create_embeddings", new=AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]])):
            stored = asyncio.run(main.process_embedding_candidates(candidates, "test"))

        self.assertEqual(1, stored)
        self.assertEqual(
            0,
            main.MEMORY.embedding_backlog_count(
                model=main.CONFIG.memory_embedding_model,
                dimensions=main.CONFIG.memory_embedding_dimensions,
                lookback_days=30,
            ),
        )

    def test_image_vision_summary_is_searchable(self) -> None:
        item_id = main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1030,
            sender_label="User",
            text="[message has attachment(s): photo]",
            content_kind="image",
        )
        main.MEMORY.update_vision_summary(item_id, "на фото унікальна зелена ракета")

        results = main.MEMORY.fts_search(chat_id=-1001, query="зелена ракета", lookback_days=30, limit=3)

        self.assertEqual(1, len(results))
        self.assertIn("зелена ракета", results[0].search_text)

    def test_semantic_search_returns_relevant_old_message(self) -> None:
        item_a = main.MEMORY.save_message(chat_id=-1001, message_id=1040, sender_label="A", text="Subnautica release context")
        item_b = main.MEMORY.save_message(chat_id=-1001, message_id=1041, sender_label="B", text="coffee machine context")
        for item_id, vector in ((item_a, [1.0, 0.0, 0.0, 0.0]), (item_b, [0.0, 1.0, 0.0, 0.0])):
            item = main.MEMORY.item_by_id(item_id)
            text = MemoryStore.searchable_text_for_item(item)
            main.MEMORY.upsert_embedding(
                message_id=item_id,
                chat_id=-1001,
                model=main.CONFIG.memory_embedding_model,
                dimensions=4,
                content_hash=MemoryStore.content_hash(text),
                embedding=vector,
            )

        results = main.MEMORY.semantic_search(
            chat_id=-1001,
            query_embedding=[1.0, 0.0, 0.0, 0.0],
            model=main.CONFIG.memory_embedding_model,
            dimensions=4,
            lookback_days=30,
            limit=1,
        )

        self.assertEqual("Subnautica release context", results[0].item.text)

    def test_fts_fallback_when_query_embedding_fails(self) -> None:
        message = FakeMessage("/memory_search subnautica", message_id=1050)
        main.MEMORY.save_message(chat_id=-1001, message_id=1051, sender_label="User", text="subnautica terraformer context")

        with patch.object(main, "create_embeddings", new=AsyncMock(side_effect=RuntimeError("no embeddings"))):
            results = asyncio.run(main.semantic_memory_results_for_query(message, "subnautica", route="normal"))

        self.assertEqual(1, len(results))
        self.assertIn("fts", results[0].source)

    def test_exact_topic_rescue_finds_old_memory_topic(self) -> None:
        message = FakeMessage("@thrd_ua_bot remember Pragmata", message_id=1055)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1056,
            sender_label="User",
            text="Pragmata sales and release context from an older discussion",
            created_at=datetime.now(timezone.utc) - timedelta(days=3),
        )

        context = asyncio.run(
            main.prepare_semantic_memory_context(
                message,
                "remember old conversation about Pragmata",
                "normal",
            )
        )

        self.assertIn("Pragmata sales", context)
        self.assertNotIn("(no semantic memory matches)", context)

    def test_semantic_recall_route_for_periphrased_memory_request(self) -> None:
        message = FakeMessage("@thrd_ua_bot а про 170 тис в казино ми щось обговорювали?", message_id=1057)

        with patch.object(main, "memory_recall_embedding_confidence", new=AsyncMock(return_value=0.75)):
            route, intent = asyncio.run(
                main.classify_request_with_intent(
                    message,
                    "а про 170 тис в казино ми щось обговорювали?",
                )
            )

        self.assertEqual("memory_recall", route)
        self.assertTrue(intent.is_recall)
        self.assertIn("170", intent.query)
        self.assertIn("казино", intent.query)

    def test_direct_recall_uses_old_memory_and_excludes_current_request(self) -> None:
        old_text = (
            "На Закарпатті поштарка проїбала в казино 170 тис. грн чужих пенсій. "
            "Їй дали пробаційний нагляд і штраф."
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1058,
            sender_label="Denis",
            text=old_text,
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1057,
            sender_label="Vitaliy",
            text='@thrd_ua_bot нагадай що там було з "в казино 170 тис" з чату',
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        current = FakeMessage('@thrd_ua_bot нагадай що там було з "в казино 170 тис" з чату', message_id=1059)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=current.message_id,
            sender_label="Vitaliy",
            text=current.text,
            created_at=datetime.now(timezone.utc),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        with patch.object(main, "memory_recall_embedding_confidence", new=AsyncMock(return_value=0.9)):
            with patch.object(main, "create_embeddings", new=AsyncMock(side_effect=RuntimeError("embedding down"))):
                with patch.object(main, "run_agent", new=AsyncMock(return_value="знайшов")) as run_agent:
                    asyncio.run(
                        main.handle_prompt(
                            current,
                            context,
                            'нагадай що там було з "в казино 170 тис" з чату',
                        )
                    )

        agent_input = run_agent.await_args.args[0]
        self.assertIn("Request route: memory_recall", agent_input)
        recalled_block = agent_input.split("Untrusted recalled long-term memory.", 1)[1].split(
            "Untrusted current web search results.",
            1,
        )[0]
        self.assertIn("поштарка проїбала в казино 170 тис", recalled_block)
        self.assertNotIn("@thrd_ua_bot нагадай", recalled_block)

    def test_recall_exact_rescue_searches_source_text_and_numbers(self) -> None:
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1060,
            sender_label="Sergey",
            text="дивись це",
            source_text="Репост: відеокарта 4070 коштувала $250 у старій новині",
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        message = FakeMessage("@thrd_ua_bot що там було про 4070 за $250?", message_id=1061)
        intent = main.MemoryRecallIntent(True, confidence=0.9, query="4070 250", reason="test")

        context = asyncio.run(main.prepare_recalled_memory_context(message, "що там було про 4070 за $250?", intent))

        self.assertIn("4070", context)
        self.assertIn("$250", context)

    def test_recall_intent_embedding_failure_uses_conservative_fallback(self) -> None:
        message = FakeMessage('@thrd_ua_bot нагадай що там було з "Pragmata"', message_id=1062)

        with patch.object(main, "memory_recall_embedding_confidence", new=AsyncMock(side_effect=RuntimeError("down"))):
            route, intent = asyncio.run(
                main.classify_request_with_intent(message, 'нагадай що там було з "Pragmata"')
            )

        self.assertEqual("memory_recall", route)
        self.assertTrue(intent.degraded)

    def test_ordinary_group_text_does_not_call_semantic_retrieval(self) -> None:
        message = FakeMessage("subnautica?", message_id=1060)
        context = SimpleNamespace(bot=SimpleNamespace(id=999, username="thrd_ua_bot"))

        with patch.object(main, "prepare_semantic_memory_context", new=AsyncMock()) as prepare:
            asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))

        prepare.assert_not_awaited()

    def test_excluded_routes_do_not_use_semantic_memory(self) -> None:
        message = FakeMessage("@thrd_ua_bot переклади українською", message_id=1070)

        translation = asyncio.run(main.prepare_semantic_memory_context(message, "переклади українською", "translate_reference"))
        image_send = asyncio.run(main.prepare_semantic_memory_context(message, "покажи фото кота", "internet_image_send"))

        self.assertIsNone(translation)
        self.assertIsNone(image_send)

    def test_short_followup_gets_semantic_memory_block(self) -> None:
        item_id = main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1080,
            sender_label="User",
            text="Subnautica коштувала п'ять тисяч у старому обговоренні",
        )
        item = main.MEMORY.item_by_id(item_id)
        text = MemoryStore.searchable_text_for_item(item)
        main.MEMORY.upsert_embedding(
            message_id=item_id,
            chat_id=-1001,
            model=main.CONFIG.memory_embedding_model,
            dimensions=4,
            content_hash=MemoryStore.content_hash(text),
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        message = FakeMessage("@thrd_ua_bot скільки?", message_id=1081)

        with patch.object(main, "create_embeddings", new=AsyncMock(return_value=[[1.0, 0.0, 0.0, 0.0]])):
            context = asyncio.run(main.prepare_semantic_memory_context(message, "скільки?", "normal"))

        self.assertIn("Subnautica коштувала", context)

    def test_memory_search_command_is_admin_only(self) -> None:
        non_admin = FakeMessage("/memory_search subnautica", message_id=1090)
        non_admin.from_user = FakeUser(user_id=999, username="notadmin")
        admin = FakeMessage("/memory_search subnautica", message_id=1091)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        asyncio.run(main.memory_search_command(SimpleNamespace(effective_message=non_admin), context))
        with patch.object(
            main,
            "semantic_memory_search_outcome",
            new=AsyncMock(return_value=main.MemorySearchOutcome(results=[])),
        ) as search:
            asyncio.run(main.memory_search_command(SimpleNamespace(effective_message=admin), context))

        self.assertIn("тільки адмінам", non_admin.reply_calls[0]["text"])
        search.assert_awaited_once()

    def test_memory_search_command_reports_fallback_diagnostics(self) -> None:
        item_id = main.MEMORY.save_message(
            chat_id=-1001,
            message_id=1095,
            sender_label="User",
            text="Pragmata release window and sales discussion",
            created_at=datetime.now(timezone.utc),
        )
        item = main.MEMORY.item_by_id(item_id)
        main.MEMORY.upsert_embedding(
            message_id=item_id,
            chat_id=-1001,
            model=main.CONFIG.memory_embedding_model,
            dimensions=4,
            content_hash=MemoryStore.content_hash(MemoryStore.searchable_text_for_item(item)),
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        message = FakeMessage("/memory_search Pragmata", message_id=1096)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        with patch.object(main, "create_embeddings", new=AsyncMock(side_effect=RuntimeError("embedding down"))):
            asyncio.run(main.memory_search_command(SimpleNamespace(effective_message=message), context))

        reply = message.reply_calls[0]["text"]
        self.assertIn("embeddings_used: no", reply)
        self.assertIn("fts_fallback: yes", reply)
        self.assertIn("embedding_error:", reply)
        self.assertIn("Pragmata release", reply)

    def write_export(self, tmpdir: str, messages: list[dict]) -> Path:
        path = Path(tmpdir) / "result.json"
        path.write_text(json.dumps({"messages": messages}), encoding="utf-8")
        return path

    def write_html_export(self, tmpdir: str, pages: dict[str, str]) -> Path:
        export_dir = Path(tmpdir) / "ChatExport"
        export_dir.mkdir()
        for name, body in pages.items():
            (export_dir / name).write_text(
                '<!DOCTYPE html><html><body><div class="history">' + body + "</div></body></html>",
                encoding="utf-8",
            )
        return export_dir

    def import_options(self, export_path: Path, db_path: Path, **overrides) -> ImportOptions:
        values = {
            "file": export_path,
            "chat_id": -1001,
            "db_path": db_path,
            "days": None,
            "retention_days": 30,
            "image_max_bytes": 6_000_000,
            "bot_username": "thrd_ua_bot",
            "embedding_dimensions": 4,
            "embedding_batch_size": 2,
            "semantic_lookback_days": 30,
        }
        values.update(overrides)
        return ImportOptions(**values)

    def test_telegram_export_import_parses_fragments_and_skips_service(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = self.write_export(
                tmpdir,
                [
                    {"id": 1, "type": "service", "date_unixtime": str(int(now.timestamp())), "actor": "Tester"},
                    {
                        "id": 2,
                        "type": "message",
                        "date_unixtime": str(int(now.timestamp())),
                        "from": "Tester",
                        "from_id": "user123",
                        "text": ["Hello ", {"type": "bold", "text": "semantic"}, " world"],
                    },
                ],
            )

            summary = import_telegram_export.import_export(self.import_options(export_path, Path(tmpdir) / "memory.sqlite3"))
            store = MemoryStore(Path(tmpdir) / "memory.sqlite3", retention_days=30)

            self.assertEqual(1, summary.imported)
            self.assertEqual(1, summary.skipped_service)
            self.assertEqual("Hello semantic world", store.latest(-1001, 1)[0].text)
            self.assertTrue(store.fts_search(chat_id=-1001, query="semantic", lookback_days=30, limit=3))

    def test_telegram_export_import_days_filter_and_idempotent_update(self) -> None:
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=40)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = self.write_export(
                tmpdir,
                [
                    {"id": 10, "type": "message", "date_unixtime": str(int(old.timestamp())), "from": "Old", "text": "old"},
                    {"id": 11, "type": "message", "date_unixtime": str(int(now.timestamp())), "from": "New", "text": "first"},
                ],
            )
            db_path = Path(tmpdir) / "memory.sqlite3"
            options = self.import_options(export_path, db_path, days=30)

            first = import_telegram_export.import_export(options)
            export_path = self.write_export(
                tmpdir,
                [
                    {"id": 11, "type": "message", "date_unixtime": str(int(now.timestamp())), "from": "New", "text": "updated"}
                ],
            )
            second = import_telegram_export.import_export(self.import_options(export_path, db_path, days=30))
            store = MemoryStore(db_path, retention_days=30)

            self.assertEqual(1, first.inserted)
            self.assertEqual(1, first.skipped_old)
            self.assertEqual(1, second.updated)
            self.assertEqual(1, len(store.latest(-1001, 10)))
            self.assertEqual("updated", store.latest(-1001, 1)[0].text)

    def test_telegram_export_import_preserves_reply_forward_user_and_excludes_bot_search(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = self.write_export(
                tmpdir,
                [
                    {
                        "id": 20,
                        "type": "message",
                        "date_unixtime": str(int(now.timestamp())),
                        "from": "Tester",
                        "from_id": "user407892151",
                        "reply_to_message_id": 19,
                        "forwarded_from": "Source Channel",
                        "text": "forwarded user text",
                    },
                    {
                        "id": 21,
                        "type": "message",
                        "date_unixtime": str(int(now.timestamp())),
                        "from": "Aigan",
                        "text": "bot self feedback should not index",
                    },
                ],
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            summary = import_telegram_export.import_export(self.import_options(export_path, db_path))
            store = MemoryStore(db_path, retention_days=30)
            user_item = store.message_by_message_id(-1001, 20)

            self.assertEqual(1, summary.bot_messages)
            self.assertEqual(407892151, user_item.user_id)
            self.assertEqual(19, user_item.reply_to_message_id)
            self.assertEqual("Source Channel", user_item.forward_origin)
            self.assertEqual("", user_item.text)
            self.assertEqual("forwarded user text", user_item.source_text)
            self.assertTrue(store.fts_search(chat_id=-1001, query="forwarded user text", lookback_days=30, limit=3))
            self.assertFalse(store.fts_search(chat_id=-1001, query="self feedback", lookback_days=30, limit=3))

    def test_telegram_export_import_copies_valid_image_media(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            photo = Path(tmpdir) / "photos" / "photo_1.png"
            photo.parent.mkdir()
            photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image")
            export_path = self.write_export(
                tmpdir,
                [
                    {
                        "id": 30,
                        "type": "message",
                        "date_unixtime": str(int(now.timestamp())),
                        "from": "Tester",
                        "text": "caption",
                        "photo": "photos/photo_1.png",
                    }
                ],
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            summary = import_telegram_export.import_export(self.import_options(export_path, db_path, copy_media=True))
            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 30)

            self.assertEqual(1, summary.media_copied)
            self.assertEqual("image", item.content_kind)
            self.assertEqual("image/png", item.mime_type)
            self.assertTrue(Path(item.local_media_path).is_file())

    def test_telegram_export_dry_run_does_not_create_database(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = self.write_export(
                tmpdir,
                [{"id": 40, "type": "message", "date_unixtime": str(int(now.timestamp())), "from": "Tester", "text": "dry"}],
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            summary = import_telegram_export.import_export(self.import_options(export_path, db_path, dry_run=True))

            self.assertEqual(1, summary.imported)
            self.assertFalse(db_path.exists())

    def test_telegram_export_embedding_backfill_can_be_mocked(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            export_path = self.write_export(
                tmpdir,
                [
                    {
                        "id": 50,
                        "type": "message",
                        "date_unixtime": str(int(now.timestamp())),
                        "from": "Tester",
                        "text": "embedding import text",
                    }
                ],
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            with patch.object(import_telegram_export, "create_embeddings", return_value=[[1.0, 0.0, 0.0, 0.0]]):
                summary = import_telegram_export.import_export(
                    self.import_options(export_path, db_path, embed_missing=True, embedding_limit=10)
                )
            store = MemoryStore(db_path, retention_days=30)

            self.assertEqual(1, summary.embeddings_stored)
            self.assertEqual(
                1,
                store.embedding_index_count(
                    chat_id=-1001,
                    model="text-embedding-3-small",
                    dimensions=4,
                    lookback_days=30,
                ),
            )

    def test_html_export_directory_imports_all_pages_and_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message service" id="message1"><div class="body details">13 May 2026</div></div>
                    <div class="message default clearfix" id="message101">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:00:00 UTC+00:00">10:00</div>
                        <div class="from_name">Tester</div>
                        <div class="text">Hello<br><strong>semantic</strong> <a href="https://example.com">link</a></div>
                      </div>
                    </div>
                    """,
                    "messages2.html": """
                    <div class="message default clearfix" id="message102">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:01:00 UTC+00:00">10:01</div>
                        <div class="from_name">Tester</div>
                        <div class="text">second page text</div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            summary = import_telegram_export.import_export(self.import_options(export_dir, db_path))
            store = MemoryStore(db_path, retention_days=30)
            items = store.latest(-1001, 10)

            self.assertEqual(2, summary.imported)
            self.assertEqual(1, summary.skipped_service)
            self.assertEqual([101, 102], [item.message_id for item in items])
            self.assertIn("semantic", items[0].text)
            self.assertIn("https://example.com", items[0].text)

    def test_html_export_inherits_sender_for_joined_messages_across_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message111">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:00:00 UTC+00:00">10:00</div>
                        <div class="from_name">Alice</div>
                        <div class="text">first text</div>
                      </div>
                    </div>
                    """,
                    "messages2.html": """
                    <div class="message default clearfix" id="message112">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:01:00 UTC+00:00">10:01</div>
                        <div class="text">joined text without author</div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            import_telegram_export.import_export(self.import_options(export_dir, db_path))
            store = MemoryStore(db_path, retention_days=30)

            self.assertEqual("Alice", store.message_by_message_id(-1001, 111).sender_label)
            self.assertEqual("Alice", store.message_by_message_id(-1001, 112).sender_label)

    def test_html_export_inherited_sender_uses_inferred_user_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            seeded = MemoryStore(db_path, retention_days=30)
            seeded.save_message(
                chat_id=-1001,
                message_id=90,
                sender_label="Display Name (@mapped, id=12345)",
                user_id=12345,
                username="mapped",
                text="live anchor",
                created_at=datetime.now(timezone.utc),
            )
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message301">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:03:00 UTC+00:00">10:03</div>
                        <div class="from_name">Display Name</div>
                        <div class="text">mapped user text</div>
                      </div>
                    </div>
                    <div class="message default clearfix" id="message302">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:04:00 UTC+00:00">10:04</div>
                        <div class="text">joined mapped text</div>
                      </div>
                    </div>
                    """,
                },
            )

            import_telegram_export.import_export(self.import_options(export_dir, db_path))
            store = MemoryStore(db_path, retention_days=30)

            self.assertEqual(12345, store.message_by_message_id(-1001, 301).user_id)
            self.assertEqual(12345, store.message_by_message_id(-1001, 302).user_id)
            self.assertEqual("mapped", store.message_by_message_id(-1001, 302).username)

    def test_html_export_preserves_reply_forward_and_copies_photo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = Path(tmpdir) / "ChatExport"
            photos = export_dir / "photos"
            photos.mkdir(parents=True)
            (photos / "photo_1.jpg").write_bytes(VALID_JPEG)
            (export_dir / "messages.html").write_text(
                """
                <html><body><div class="history">
                <div class="message default clearfix" id="message201">
                  <div class="body">
                    <div class="pull_right date details" title="13.05.2026 10:02:00 UTC+00:00">10:02</div>
                    <div class="from_name">Tester</div>
                    <div class="reply_to details">In reply to <a href="#go_to_message199">this message</a></div>
                    <div class="forwarded body"><div class="from_name">Source Channel <span class="date details">13.05.2026</span></div></div>
                    <div class="media_wrap clearfix"><a class="photo_wrap clearfix pull_left" href="photos/photo_1.jpg"><img src="photos/photo_1_thumb.jpg"/></a></div>
                    <div class="text">photo caption</div>
                  </div>
                </div>
                </div></body></html>
                """,
                encoding="utf-8",
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            summary = import_telegram_export.import_export(self.import_options(export_dir, db_path, copy_media=True))
            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 201)

            self.assertEqual(1, summary.media_copied)
            self.assertEqual(199, item.reply_to_message_id)
            self.assertEqual("Source Channel", item.forward_origin)
            self.assertEqual("image", item.content_kind)
            self.assertTrue(Path(item.local_media_path).is_file())

    def test_html_export_splits_author_comment_from_forwarded_source_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message251">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:02:00 UTC+00:00">10:02</div>
                        <div class="from_name">Sergey</div>
                        <div class="text">my own short comment</div>
                        <div class="forwarded body">
                          <div class="from_name">Ukraine Online <span class="date details">13.05.2026</span></div>
                          <div class="text"><strong>viral repost body</strong><br><a href="https://t.me/example">Ukraine Online | Subscribe</a></div>
                        </div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            import_telegram_export.import_export(self.import_options(export_dir, db_path))
            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 251)
            stats_items = store.user_stats(-1001, label_aliases=("Sergey",))

            self.assertEqual("my own short comment", item.text)
            self.assertIn("viral repost body", item.source_text)
            self.assertIn("Ukraine Online", item.source_text)
            self.assertEqual("Ukraine Online", item.forward_origin)
            self.assertEqual([251], [stat_item.message_id for stat_item in stats_items])
            self.assertTrue(store.fts_search(chat_id=-1001, query="viral repost body", lookback_days=30, limit=3))

    def test_html_forward_without_author_comment_is_source_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message252">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:02:00 UTC+00:00">10:02</div>
                        <div class="from_name">Sergey</div>
                        <div class="forwarded body">
                          <div class="from_name">Ukraine Online <span class="date details">13.05.2026</span></div>
                          <div class="text">channel only text subscribe online</div>
                        </div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            import_telegram_export.import_export(self.import_options(export_dir, db_path))
            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 252)

            self.assertEqual("", item.text)
            self.assertEqual("channel only text subscribe online", item.source_text)
            self.assertEqual([], store.user_stats(-1001, label_aliases=("Sergey",)))
            self.assertEqual(1, store.user_source_count(-1001, label_aliases=("Sergey",)))
            self.assertTrue(store.fts_search(chat_id=-1001, query="subscribe online", lookback_days=30, limit=3))

    def test_html_export_user_map_adds_user_id_and_username(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message301">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:03:00 UTC+00:00">10:03</div>
                        <div class="from_name">Display Name</div>
                        <div class="text">mapped user text</div>
                      </div>
                    </div>
                    """,
                },
            )
            user_map = Path(tmpdir) / "users.json"
            user_map.write_text(json.dumps({"Display Name": {"user_id": 12345, "username": "mapped"}}), encoding="utf-8")
            db_path = Path(tmpdir) / "memory.sqlite3"

            summary = import_telegram_export.import_export(
                self.import_options(export_dir, db_path, user_map_path=user_map)
            )
            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 301)

            self.assertEqual(1, summary.imported)
            self.assertEqual(12345, item.user_id)
            self.assertEqual("mapped", item.username)

    def test_html_export_unknown_sender_does_not_fake_user_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message401">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:04:00 UTC+00:00">10:04</div>
                        <div class="from_name">Unknown Person</div>
                        <div class="text">unknown sender text</div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            import_telegram_export.import_export(self.import_options(export_dir, db_path))
            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 401)

            self.assertIsNone(item.user_id)
            self.assertEqual("", item.username)

    def test_import_reports_unresolved_authors_without_tty_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message451">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:04:00 UTC+00:00">10:04</div>
                        <div class="from_name">Unknown Person</div>
                        <div class="text">unknown sender text</div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"

            with patch.object(import_telegram_export.sys.stdin, "isatty", return_value=False):
                with patch.object(import_telegram_export.sys.stdout, "isatty", return_value=False):
                    summary = import_telegram_export.import_export(self.import_options(export_dir, db_path))

            self.assertEqual({"Unknown Person": 1}, summary.unresolved_authors)

    def test_import_interactive_user_map_can_write_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message452">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:04:00 UTC+00:00">10:04</div>
                        <div class="from_name">Unknown Person</div>
                        <div class="text">unknown sender text</div>
                      </div>
                    </div>
                    """,
                },
            )
            db_path = Path(tmpdir) / "memory.sqlite3"
            user_map = Path(tmpdir) / "users.json"

            with patch.object(import_telegram_export.sys.stdin, "isatty", return_value=True):
                with patch.object(import_telegram_export.sys.stdout, "isatty", return_value=True):
                    with patch("builtins.input", return_value="777,mapped"):
                        summary = import_telegram_export.import_export(
                            self.import_options(
                                export_dir,
                                db_path,
                                interactive_user_map="always",
                                write_user_map_path=user_map,
                            )
                        )

            store = MemoryStore(db_path, retention_days=30)
            item = store.message_by_message_id(-1001, 452)
            written = json.loads(user_map.read_text(encoding="utf-8"))
            self.assertEqual({}, summary.unresolved_authors)
            self.assertEqual(777, item.user_id)
            self.assertEqual("mapped", item.username)
            self.assertEqual({"user_id": 777, "username": "mapped"}, written["Unknown Person"])

    def test_import_require_resolved_users_fails_on_unmapped_author(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            export_dir = self.write_html_export(
                tmpdir,
                {
                    "messages.html": """
                    <div class="message default clearfix" id="message453">
                      <div class="body">
                        <div class="pull_right date details" title="13.05.2026 10:04:00 UTC+00:00">10:04</div>
                        <div class="from_name">Unknown Person</div>
                        <div class="text">unknown sender text</div>
                      </div>
                    </div>
                    """,
                },
            )

            with self.assertRaisesRegex(ValueError, "Unresolved Telegram export authors"):
                import_telegram_export.import_export(
                    self.import_options(
                        export_dir,
                        Path(tmpdir) / "memory.sqlite3",
                        require_resolved_users=True,
                        interactive_user_map="never",
                    )
                )

    def test_image_metadata_and_summary_are_stored_and_reused(self) -> None:
        item_id = main.MEMORY.save_message(
            chat_id=-1001,
            message_id=4,
            sender_label="Tester",
            text="photo",
            content_kind="image",
            created_at=datetime.now(timezone.utc),
        )
        main.MEMORY.update_media(
            item_id,
            attachment_type="photo",
            telegram_file_id="file-id",
            telegram_unique_id="unique-id",
            local_media_path="/tmp/image.jpg",
            mime_type="image/jpeg",
        )
        main.MEMORY.update_vision_summary(item_id, "український опис")

        context = main.format_memory_context(-1001)

        self.assertIn("image_summary=український опис", context)
        self.assertNotIn("not summarized", context)

    def test_lazy_vision_summary_is_called_for_missing_recent_images(self) -> None:
        media_path = Path(tempfile.gettempdir()) / f"aigan-image-{os.getpid()}.jpg"
        media_path.write_bytes(b"fake-image")
        try:
            for index in range(4):
                item_id = main.MEMORY.save_message(
                    chat_id=-1001,
                    message_id=10 + index,
                    sender_label="Tester",
                    text=f"photo {index}",
                    content_kind="image",
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
                )
                main.MEMORY.update_media(
                    item_id,
                    attachment_type="photo",
                    telegram_file_id=f"file-{index}",
                    local_media_path=str(media_path),
                    mime_type="image/jpeg",
                )

            with patch.object(main, "run_vision", new=AsyncMock(return_value="lazy summary")) as run_vision:
                asyncio.run(main.ensure_recent_image_summaries(-1001))

            self.assertEqual(3, run_vision.await_count)
            self.assertEqual(1, len(main.MEMORY.unsummarized_recent_images(-1001, 10)))
        finally:
            try:
                media_path.unlink()
            except FileNotFoundError:
                pass

    def test_reply_to_image_media_is_cached_when_telegram_provides_file(self) -> None:
        replied = FakeMessage("caption", message_id=20)
        replied.photo = [FakePhoto()]
        message = FakeMessage("поясни", message_id=21)
        message.reply_to_message = replied

        asyncio.run(main.remember_message_persistently(message))

        items = main.MEMORY.latest(-1001, 10)
        self.assertEqual(1, len(items))
        self.assertEqual("image", items[0].content_kind)
        self.assertTrue(items[0].local_media_path)
        self.assertIn("reply_to_message", items[0].raw_note)

    def test_link_preview_without_delivered_image_keeps_text_context_only(self) -> None:
        message = FakeMessage("https://example.com/page", message_id=30)

        asyncio.run(main.remember_message_persistently(message))

        item = main.MEMORY.latest(-1001, 1)[0]
        self.assertEqual("text", item.content_kind)
        self.assertEqual("", item.local_media_path)
        self.assertIn("example.com", item.text)

    def test_retention_deletes_old_rows_and_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.sqlite3", retention_days=1)
            media = Path(tmpdir) / "old.jpg"
            media.write_bytes(b"old")
            item_id = store.save_message(
                chat_id=-1001,
                message_id=1,
                sender_label="Tester",
                text="old",
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
            )
            store.update_media(
                item_id,
                attachment_type="photo",
                telegram_file_id="file",
                local_media_path=str(media),
                mime_type="image/jpeg",
            )

            deleted = store.cleanup()

            self.assertEqual(1, deleted)
            self.assertFalse(media.exists())
            store.close()

    def test_internet_image_request_sends_photo_and_source(self) -> None:
        message = FakeMessage("покажи картинку кота", message_id=40)

        with patch.object(
            main,
            "search_image_candidates",
            return_value=[{"title": "Cat", "image": "https://example.com/cat.jpg", "source": "https://example.com/cat"}],
        ):
            with patch.object(
                main,
                "fetch_binary_url",
                return_value=(VALID_JPEG, "image/jpeg", "https://example.com/cat.jpg"),
            ):
                handled = asyncio.run(main.maybe_send_internet_image(message, message.text))

        self.assertTrue(handled)
        self.assertEqual(1, len(message.photo_calls))
        self.assertIn("Cat", message.photo_calls[0]["caption"])

    def test_translation_reply_route_excludes_memory_and_image_search(self) -> None:
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=60,
            sender_label="Aigan",
            text="old answer about AI-generated image",
            created_at=datetime.now(timezone.utc),
        )
        replied = FakeMessage("Structure and Details: The object looks handmade.", message_id=61)
        message = FakeMessage("@thrd_ua_bot переведи українською", message_id=62)
        message.reply_to_message = replied
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        with patch.object(main, "run_agent", new=AsyncMock(return_value="переклад")) as run_agent:
            with patch.object(main, "maybe_send_internet_image", new=AsyncMock()) as image_send:
                asyncio.run(main.handle_prompt(message, context, "переведи українською"))

        self.assertEqual("translate_reference", main.classify_request(message, "переведи українською"))
        image_send.assert_not_awaited()
        agent_input = run_agent.await_args.args[0]
        self.assertIn("Request route: translate_reference", agent_input)
        self.assertIn("Structure and Details", agent_input)
        self.assertNotIn("old answer about AI-generated image", agent_input)
        self.assertNotIn("Untrusted persistent recent chat memory", agent_input)

    def test_long_source_text_with_image_show_does_not_trigger_image_send(self) -> None:
        prompt = (
            "Structure and Details The structure in the image has a handmade appearance "
            "and the materials show wear from outdoor exposure. " * 3
        )

        self.assertFalse(main.is_internet_image_request(prompt))
        self.assertNotEqual("internet_image_send", main.classify_request(FakeMessage(prompt), prompt))

    def test_explicit_image_prompt_routes_to_image_send(self) -> None:
        prompt = "покажи картинку кота"

        self.assertTrue(main.is_internet_image_request(prompt))
        self.assertEqual("internet_image_send", main.classify_request(FakeMessage(prompt), prompt))

    def test_slang_multi_image_prompt_routes_to_image_send(self) -> None:
        prompt = "знайди в інеті 3 фотки капібар і запость сюди"

        self.assertTrue(main.is_internet_image_request(prompt))
        self.assertEqual(3, main.requested_image_count(prompt))
        self.assertEqual("капібар", main.image_search_query(prompt))
        self.assertEqual("internet_image_send", main.classify_request(FakeMessage(prompt), prompt))

    def test_multi_image_request_sends_requested_photos_as_album_bytes(self) -> None:
        message = FakeMessage("знайди в інеті 3 фотки капібар і запость сюди", message_id=72)

        with patch.object(
            main,
            "search_image_candidates",
            return_value=[
                {"title": "Capybara 1", "image": "https://example.com/capy1.jpg", "source": "https://example.com/capy1"},
                {"title": "Capybara 2", "image": "https://example.com/capy2.jpg", "source": "https://example.com/capy2"},
                {"title": "Capybara 3", "image": "https://example.com/capy3.jpg", "source": "https://example.com/capy3"},
            ],
        ) as search_images:
            with patch.object(
                main,
                "fetch_binary_url",
                side_effect=[
                    (VALID_JPEG, "image/jpeg", "https://example.com/capy1.jpg"),
                    (VALID_JPEG, "image/jpeg", "https://example.com/capy2.jpg"),
                    (VALID_JPEG, "image/jpeg", "https://example.com/capy3.jpg"),
                ],
            ):
                handled = asyncio.run(main.maybe_send_internet_image(message, message.text))

        self.assertTrue(handled)
        search_images.assert_called_once_with("капібар", 10)
        self.assertEqual(1, message.media_group_attempts)
        self.assertEqual(1, len(message.media_group_calls))
        self.assertEqual(0, len(message.photo_calls))
        media = message.media_group_calls[0]["media"]
        self.assertEqual(3, len(media))
        self.assertTrue(all(isinstance(item, InputMediaPhoto) for item in media))
        self.assertTrue(all(not str(item.media).startswith("http") for item in media))
        self.assertIn("Capybara 1", media[0].caption)
        self.assertLessEqual(len(media[0].caption), 1024)
        self.assertNotIn("<a href", media[0].caption)
        self.assertIsNone(media[1].caption)
        self.assertIsNone(media[2].caption)
        stored = main.MEMORY.latest(message.chat_id, 10)
        self.assertEqual(3, len([item for item in stored if item.attachment_type == "web_image"]))

    def test_album_failure_falls_back_to_individual_photos_and_memory(self) -> None:
        message = FakeMessage("знайди в інеті 3 фотки капібар і запость сюди", message_id=73)
        message.media_group_failures = 1

        with patch.object(
            main,
            "search_image_candidates",
            return_value=[
                {"title": "Capybara 1", "image": "https://example.com/capy1.jpg", "source": "https://example.com/capy1"},
                {"title": "Capybara 2", "image": "https://example.com/capy2.jpg", "source": "https://example.com/capy2"},
                {"title": "Capybara 3", "image": "https://example.com/capy3.jpg", "source": "https://example.com/capy3"},
            ],
        ):
            with patch.object(
                main,
                "fetch_binary_url",
                side_effect=[
                    (VALID_JPEG, "image/jpeg", "https://example.com/capy1.jpg"),
                    (VALID_JPEG, "image/jpeg", "https://example.com/capy2.jpg"),
                    (VALID_JPEG, "image/jpeg", "https://example.com/capy3.jpg"),
                ],
            ):
                handled = asyncio.run(main.maybe_send_internet_image(message, message.text))

        self.assertTrue(handled)
        self.assertEqual(1, message.media_group_attempts)
        self.assertEqual(0, len(message.media_group_calls))
        self.assertEqual(3, len(message.photo_calls))
        self.assertIn("1/3. Capybara 1", message.photo_calls[0]["caption"])
        stored = main.MEMORY.latest(message.chat_id, 10)
        self.assertEqual(3, len([item for item in stored if item.attachment_type == "web_image"]))

    def test_invalid_image_candidate_is_skipped_before_valid_photo(self) -> None:
        message = FakeMessage("покажи картинку кота", message_id=70)

        with patch.object(
            main,
            "search_image_candidates",
            return_value=[
                {"title": "Bad", "image": "https://example.com/bad.jpg", "source": "https://example.com/bad"},
                {"title": "Good", "image": "https://example.com/good.jpg", "source": "https://example.com/good"},
            ],
        ):
            with patch.object(
                main,
                "fetch_binary_url",
                side_effect=[
                    (b"not-an-image", "image/jpeg", "https://example.com/bad.jpg"),
                    (VALID_JPEG, "image/jpeg", "https://example.com/good.jpg"),
                ],
            ):
                handled = asyncio.run(main.maybe_send_internet_image(message, message.text))

        self.assertTrue(handled)
        self.assertEqual(1, len(message.photo_calls))
        self.assertIn("Good", message.photo_calls[0]["caption"])
        self.assertIn("Good", main.MEMORY.latest(message.chat_id, 1)[0].source_title)

    def test_telegram_photo_failure_tries_next_candidate_without_storing_failed_image(self) -> None:
        message = FakeMessage("покажи картинку кота", message_id=71)
        message.photo_failures = 2

        with patch.object(
            main,
            "search_image_candidates",
            return_value=[
                {"title": "Rejected", "image": "https://example.com/rejected.jpg", "source": "https://example.com/rejected"},
                {"title": "Accepted", "image": "https://example.com/accepted.jpg", "source": "https://example.com/accepted"},
            ],
        ):
            with patch.object(
                main,
                "fetch_binary_url",
                side_effect=[
                    (VALID_JPEG, "image/jpeg", "https://example.com/rejected.jpg"),
                    (VALID_JPEG, "image/jpeg", "https://example.com/accepted.jpg"),
                ],
            ):
                handled = asyncio.run(main.maybe_send_internet_image(message, message.text))

        self.assertTrue(handled)
        self.assertEqual(1, len(message.photo_calls))
        self.assertIn("Accepted", message.photo_calls[0]["caption"])
        stored = main.MEMORY.latest(message.chat_id, 5)
        self.assertEqual(1, len([item for item in stored if item.attachment_type == "web_image"]))
        self.assertIn("Accepted", stored[-1].source_title)

    def test_time_sensitive_prompt_prefetches_web_context(self) -> None:
        prompt = "яка погода зараз в Атланті?"
        message = FakeMessage(prompt)
        route = main.classify_request(message, prompt)

        with patch.object(main, "search_web", return_value="fresh weather result") as search_web:
            context = asyncio.run(main.maybe_prefetch_web_context(message, prompt, route))

        self.assertEqual("time_sensitive", route)
        search_web.assert_called_once()
        self.assertIn("fresh weather result", context)

    def test_stable_past_prompt_does_not_force_web_prefetch(self) -> None:
        prompt = "коли почалась друга світова війна?"
        route = main.classify_request(FakeMessage(prompt), prompt)

        with patch.object(main, "search_web") as search_web:
            context = asyncio.run(main.maybe_prefetch_web_context(FakeMessage(prompt), prompt, route))

        self.assertEqual("normal", route)
        self.assertEqual("(none)", context)
        search_web.assert_not_called()

    def test_private_forwarded_current_claim_routes_to_web_prefetch(self) -> None:
        claim = "Петер Мадяр офіційно став прем’єр-міністром Угорщини. Орбан на засідання парламенту не прийшов."
        message = FakeMessage(claim, chat_type=ChatType.PRIVATE, chat_id=407892151, message_id=80)
        message.forward_date = datetime.now(timezone.utc)
        context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))

        with patch.object(main, "search_web", return_value="fresh political result") as search_web:
            with patch.object(main, "run_agent", new=AsyncMock(return_value="перевірено")) as run_agent:
                asyncio.run(main.handle_prompt(message, context, main.DEFAULT_CONTEXT_PROMPT))

        search_web.assert_called_once()
        self.assertIn("Петер Мадяр", search_web.call_args.args[0])
        self.assertNotIn(main.DEFAULT_CONTEXT_PROMPT, search_web.call_args.args[0])
        agent_input = run_agent.await_args.args[0]
        self.assertIn("Request route: time_sensitive", agent_input)
        self.assertIn("fresh political result", agent_input)

    def test_group_mention_on_replied_current_claim_routes_to_web_prefetch(self) -> None:
        claim = "Петер Мадяр офіційно став прем’єр-міністром Угорщини."
        replied = FakeMessage(claim, message_id=81)
        message = FakeMessage("@thrd_ua_bot що це?", message_id=82)
        message.reply_to_message = replied
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))

        with patch.object(main, "search_web", return_value="fresh reply result") as search_web:
            with patch.object(main, "run_agent", new=AsyncMock(return_value="відповідь")) as run_agent:
                asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))

        search_web.assert_called_once()
        self.assertIn("Петер Мадяр", search_web.call_args.args[0])
        self.assertIn("Request route: time_sensitive", run_agent.await_args.args[0])

    def test_group_ordinary_current_claim_stays_silent_without_trigger(self) -> None:
        message = FakeMessage("Петер Мадяр офіційно став прем’єр-міністром Угорщини.", message_id=83)
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))

        with patch.object(main, "search_web") as search_web:
            with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
                asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))

        handle_prompt.assert_not_awaited()
        search_web.assert_not_called()
        self.assertIn("Петер Мадяр", main.format_passive_context(message.chat_id))

    def test_group_ordinary_forwarded_current_claim_stays_silent_without_trigger(self) -> None:
        message = FakeMessage("Петер Мадяр офіційно став прем’єр-міністром Угорщини.", message_id=84)
        message.forward_date = datetime.now(timezone.utc)
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))

        with patch.object(main, "search_web") as search_web:
            with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
                asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))

        handle_prompt.assert_not_awaited()
        search_web.assert_not_called()
        self.assertIn("Петер Мадяр", main.format_passive_context(message.chat_id))

    def test_explicit_verify_news_prompt_routes_to_time_sensitive(self) -> None:
        prompt = "перевір новину: Петер Мадяр офіційно став прем’єр-міністром Угорщини"
        message = FakeMessage(prompt)

        self.assertEqual("time_sensitive", main.classify_request(message, prompt))

    def test_translation_and_image_routes_do_not_web_prefetch(self) -> None:
        replied = FakeMessage("Петер Мадяр офіційно став прем’єр-міністром Угорщини.", message_id=85)
        translation = FakeMessage("@thrd_ua_bot переклади українською", message_id=86)
        translation.reply_to_message = replied
        image_prompt = "покажи фото прем’єр-міністра Угорщини"
        image_message = FakeMessage(image_prompt, message_id=87)

        self.assertEqual("translate_reference", main.classify_request(translation, "переклади українською"))
        self.assertEqual("internet_image_send", main.classify_request(image_message, image_prompt))

    def test_changelog_parser_returns_latest_entry(self) -> None:
        text = """# Changelog

## 2026-05-09 - Latest

- One

## 2026-05-08 - Older

- Two
"""
        entries = main.parse_changelog_entries(text)

        self.assertEqual(2, len(entries))
        self.assertIn("Latest", entries[0])
        self.assertNotIn("Older", entries[0])

    def test_version_command_replies_with_latest_entry(self) -> None:
        message = FakeMessage("/version")

        with patch.object(main, "read_changelog_entries", return_value=["## 2026-05-09 - Latest\n\n- One"]):
            asyncio.run(main.version_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        self.assertIn("Latest", message.reply_calls[0]["text"])

    def test_version_command_accepts_capped_count(self) -> None:
        message = FakeMessage("/version 3")

        with patch.object(main, "read_changelog_entries", return_value=["one", "two", "three"]) as read_entries:
            asyncio.run(main.version_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        read_entries.assert_called_once_with(3)
        self.assertIn("one", message.reply_calls[0]["text"])
        self.assertIn("three", message.reply_calls[0]["text"])

    def test_missing_changelog_returns_graceful_message(self) -> None:
        message = FakeMessage("/version")

        with patch.object(main, "read_changelog_entries", return_value=[]):
            asyncio.run(main.version_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        self.assertEqual("Немає записів про версію.", message.reply_calls[0]["text"])

    def test_localized_version_alias_accepts_count(self) -> None:
        message = FakeMessage("/версія 3")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        with patch.object(main, "read_changelog_entries", return_value=["one", "two", "three"]) as read_entries:
            asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        read_entries.assert_called_once_with(3)
        self.assertIn("one", message.reply_calls[0]["text"])

    def test_localized_alias_with_bot_suffix_matches_current_bot_only(self) -> None:
        self.assertEqual(("version", "2"), main.localized_command_match("/версія@thrd_ua_bot 2", "thrd_ua_bot"))
        self.assertIsNone(main.localized_command_match("/версія@other_bot 2", "thrd_ua_bot"))

    def test_localized_help_alias_replies_with_ukrainian_aliases(self) -> None:
        message = FakeMessage("/довідка")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        self.assertIn("/версія", message.reply_calls[0]["text"])
        self.assertIn("/питай", message.reply_calls[0]["text"])
        self.assertIn("/п", message.reply_calls[0]["text"])
        self.assertIn("/а", message.reply_calls[0]["text"])
        self.assertIn("/характер", message.reply_calls[0]["text"])
        self.assertIn("/стат", message.reply_calls[0]["text"])

    def test_localized_ai_alias_invokes_prompt_handler(self) -> None:
        message = FakeMessage("/питай яка погода зараз?")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
            asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        handle_prompt.assert_awaited_once_with(message, context, "яка погода зараз?")

    def test_short_localized_ai_aliases_invoke_prompt_handler(self) -> None:
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        for command in ("/п", "/а"):
            with self.subTest(command=command):
                message = FakeMessage(f"{command} тест")
                with patch.object(main, "handle_prompt", new=AsyncMock()) as handle_prompt:
                    asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

                handle_prompt.assert_awaited_once_with(message, context, "тест")

    def test_short_localized_ai_aliases_accept_bot_suffix(self) -> None:
        self.assertEqual(("ai", "тест"), main.localized_command_match("/п@thrd_ua_bot тест", "thrd_ua_bot"))
        self.assertEqual(("ai", "тест"), main.localized_command_match("/а@thrd_ua_bot тест", "thrd_ua_bot"))
        self.assertIsNone(main.localized_command_match("/п@other_bot тест", "thrd_ua_bot"))
        self.assertIsNone(main.localized_command_match("/а@other_bot тест", "thrd_ua_bot"))

    def test_short_localized_ai_aliases_require_slash(self) -> None:
        self.assertIsNone(main.localized_command_match("п тест", "thrd_ua_bot"))
        self.assertIsNone(main.localized_command_match("а тест", "thrd_ua_bot"))

    def test_localized_memory_search_aliases_parse(self) -> None:
        self.assertEqual(("memory_search", "subnautica"), main.localized_command_match("/\u043f\u0430\u043c\u044f\u0442\u044c subnautica", "thrd_ua_bot"))
        self.assertEqual(("memory_search", "subnautica"), main.localized_command_match("/\u043f\u0430\u043c\u02bc\u044f\u0442\u044c subnautica", "thrd_ua_bot"))
        self.assertEqual(
            ("memory_search", "subnautica"),
            main.localized_command_match("/\u043f\u043e\u0448\u0443\u043a_\u043f\u0430\u043c\u044f\u0442\u0456 subnautica", "thrd_ua_bot"),
        )

    def test_stats_command_counts_saved_self_messages(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3000,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="Альфа тест. Альфа ще раз!",
            created_at=now,
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3001,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="Бета тест про альфа.",
            created_at=now + timedelta(seconds=1),
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3002,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="[message has attachment(s): sticker]",
            content_kind="attachment",
            attachment_type="sticker",
            created_at=now + timedelta(seconds=2),
        )
        message = FakeMessage("/stat")

        asyncio.run(main.stats_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        reply = message.reply_calls[0]["text"]
        self.assertIn("повідомлень: 2", reply)
        self.assertIn("речень: 3", reply)
        self.assertIn("альфа - 3", reply)
        self.assertNotIn("attachment", reply)
        self.assertNotIn("sticker", reply)

    def test_stats_ignores_forwarded_source_text_but_reports_source_count(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3010,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="my own comment alpha",
            source_text="Ukraine Online subscribe viral repost body",
            forward_origin="Ukraine Online",
            created_at=now,
        )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3011,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="",
            source_text="channel only subscribe online",
            forward_origin="Ukraine Online",
            created_at=now + timedelta(seconds=1),
        )
        message = FakeMessage("/stat")

        asyncio.run(main.stats_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        reply = message.reply_calls[0]["text"].casefold()
        self.assertIn("репостів/джерел не в особистій статистиці: 2", reply)
        self.assertIn("alpha - 1", reply)
        self.assertNotIn("subscribe -", reply)
        self.assertNotIn("online -", reply)
        self.assertTrue(main.MEMORY.fts_search(chat_id=-1001, query="viral repost body", lookback_days=30, limit=3))

    def test_live_forwarded_message_is_saved_as_source_text_not_author_text(self) -> None:
        message = FakeMessage("forwarded channel body subscribe online", message_id=3020)
        message.forward_date = datetime.now(timezone.utc)

        item_id = main.save_memory_message(message)
        item = main.MEMORY.item_by_id(item_id)

        self.assertEqual("", item.text)
        self.assertEqual("forwarded channel body subscribe online", item.source_text)
        self.assertEqual("forwarded", item.forward_origin)
        self.assertEqual([], main.MEMORY.user_stats(-1001, user_id=message.from_user.id))
        self.assertTrue(main.MEMORY.fts_search(chat_id=-1001, query="channel body", lookback_days=30, limit=3))

    def test_stats_normalizes_mentions_commands_triggers_and_pasted_output(self) -> None:
        now = datetime.now(timezone.utc)
        samples = [
            "@thrd_ua_bot дай тези",
            "/п поясни це",
            "!m перевір це",
            "3. thrd - 6",
            "2. bot - 4",
            "@someuser подивись це",
        ]
        for index, text in enumerate(samples):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3050 + index,
                sender_label="Test User (@tester, id=407892151)",
                user_id=407892151,
                username="tester",
                text=text,
                created_at=now + timedelta(seconds=index),
            )
        message = FakeMessage("/stat")

        asyncio.run(main.stats_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        reply = message.reply_calls[0]["text"].casefold()
        self.assertIn("повідомлень: 4", reply)
        self.assertIn("дай - 1", reply)
        self.assertIn("тези - 1", reply)
        self.assertIn("поясни - 1", reply)
        self.assertIn("перевір - 1", reply)
        self.assertNotIn("thrd", reply)
        self.assertNotIn("bot", reply)
        self.assertNotIn("someuser", reply)

    def test_clean_user_text_for_stats_preserves_arguments(self) -> None:
        self.assertEqual("дай тези", main.clean_user_text_for_stats("@thrd_ua_bot дай тези"))
        self.assertEqual("поясни це", main.clean_user_text_for_stats("/п поясни це"))
        self.assertEqual("перевір це", main.clean_user_text_for_stats("!m перевір це"))
        self.assertEqual("", main.clean_user_text_for_stats("3. thrd - 6"))

    def test_localized_stats_alias_supports_admin_username_target(self) -> None:
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3010,
            sender_label="Target (@target, id=222)",
            user_id=222,
            username="target",
            text="ціль пише багато слів",
            created_at=datetime.now(timezone.utc),
        )
        message = FakeMessage("/стат @target")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        self.assertIn("Target (@target, id=222)", message.reply_calls[0]["text"])
        self.assertIn("повідомлень: 1", message.reply_calls[0]["text"])

    def test_stats_username_target_resolves_to_user_id_for_imported_rows(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3020,
            sender_label="Target (@target, id=222)",
            user_id=222,
            username="target",
            text="resolver anchor",
            created_at=now,
        )
        for index in range(2):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3021 + index,
                sender_label="Target Export",
                user_id=222,
                username="",
                text=f"imported row {index} pragmata",
                created_at=now + timedelta(seconds=index + 1),
            )
        message = FakeMessage("/stat @target")

        asyncio.run(main.handle_stats_command(message, "@target"))

        reply = message.reply_calls[0]["text"]
        self.assertIn("повідомлень: 3", reply)
        self.assertIn("pragmata - 2", reply)

    def test_character_username_target_uses_imported_rows_by_user_id(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3030,
            sender_label="Target (@target, id=222)",
            user_id=222,
            username="target",
            text="resolver anchor",
            created_at=now,
        )
        for index in range(9):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3031 + index,
                sender_label="Target Export",
                user_id=222,
                username="",
                text=f"imported profile memory {index}",
                created_at=now + timedelta(seconds=index + 1),
            )
        message = FakeMessage("/character @target")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))
        captured = {}

        async def fake_run_plain_model(prompt: str) -> str:
            captured["prompt"] = prompt
            return "profile ready"

        with patch.object(main, "run_plain_model", new=fake_run_plain_model):
            asyncio.run(main.handle_character_command(message, context, "@target"))

        self.assertIn("profile ready", message.reply_calls[0]["text"])
        self.assertIn("Cleaned messages: 10", captured["prompt"])
        self.assertIn("imported profile memory 8", captured["prompt"])

    def test_self_target_includes_imported_rows_by_base_sender_label(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3040,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="live anchor",
            created_at=now,
        )
        for index in range(9):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3041 + index,
                sender_label="Test User",
                user_id=None,
                username="",
                text=f"imported alias memory {index}",
                created_at=now + timedelta(seconds=index + 1),
            )
        message = FakeMessage("/character me")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))
        captured = {}

        async def fake_run_plain_model(prompt: str) -> str:
            captured["prompt"] = prompt
            return "profile ready"

        with patch.object(main, "run_plain_model", new=fake_run_plain_model):
            asyncio.run(main.handle_character_command(message, context, "me"))

        reply = message.reply_calls[0]["text"]
        self.assertIn("profile ready", reply)
        self.assertIn("label_alias=9", reply)
        self.assertIn("Cleaned messages: 10", captured["prompt"])
        self.assertIn("imported alias memory 8", captured["prompt"])

    def test_username_target_includes_imported_rows_by_base_sender_label(self) -> None:
        now = datetime.now(timezone.utc)
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3060,
            sender_label="Target (@target, id=222)",
            user_id=222,
            username="target",
            text="resolver anchor",
            created_at=now,
        )
        for index in range(2):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3061 + index,
                sender_label="Target",
                user_id=None,
                username="",
                text=f"alias stat row {index}",
                created_at=now + timedelta(seconds=index + 1),
            )
        message = FakeMessage("/stat @target")

        asyncio.run(main.handle_stats_command(message, "@target"))

        self.assertIn("повідомлень: 3", message.reply_calls[0]["text"])
        self.assertIn("alias - 2", message.reply_calls[0]["text"])

    def test_non_admin_cannot_request_other_user_stats_or_profile(self) -> None:
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))
        for text in ("/стат @tester", "/характер @tester"):
            with self.subTest(text=text):
                message = FakeMessage(text)
                message.from_user = FakeUser(user_id=999, username="other")

                asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

                self.assertIn("лише адмін", message.reply_calls[0]["text"])

    def test_unknown_username_returns_clear_stats_message(self) -> None:
        message = FakeMessage("/stat @missing")

        asyncio.run(main.stats_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        self.assertIn("Не знайшов", message.reply_calls[0]["text"])
        self.assertIn("@missing", message.reply_calls[0]["text"])

    def test_character_command_uses_full_retained_memory_profile_package(self) -> None:
        base = datetime.now(timezone.utc)
        for index in range(105):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3100 + index,
                sender_label="Test User (@tester, id=407892151)",
                user_id=407892151,
                username="tester",
                text=f"profile-sample-{index:03d}",
                created_at=base + timedelta(seconds=index),
            )
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3300,
            sender_label="Other",
            user_id=222,
            username="other",
            text="other-user-secret",
            created_at=base + timedelta(seconds=300),
        )
        message = FakeMessage("/характер мій")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))
        captured = {}

        async def fake_run_plain_model(prompt: str) -> str:
            captured["prompt"] = prompt
            return "портрет готовий"

        with patch.object(main, "run_plain_model", new=fake_run_plain_model):
            asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        self.assertIn("портрет готовий", message.reply_calls[0]["text"])
        self.assertIn("Cleaned messages: 105", captured["prompt"])
        self.assertIn("Fallback representative sample", captured["prompt"])
        self.assertIn("Chronological anchors from full retained period", captured["prompt"])
        self.assertIn("profile-sample-000", captured["prompt"])
        self.assertIn("profile-sample-104", captured["prompt"])
        self.assertLess(captured["prompt"].count("profile-sample-"), 105)
        self.assertNotIn("other-user-secret", captured["prompt"])
        self.assertIn("Do not infer or mention mental health", captured["prompt"])

    def test_character_profile_package_uses_embedding_diverse_sample(self) -> None:
        base = datetime.now(timezone.utc)
        item_ids: list[int] = []
        for index in range(12):
            item_id = main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3600 + index,
                sender_label="Test User (@tester, id=407892151)",
                user_id=407892151,
                username="tester",
                text=f"embedded profile topic {index}",
                created_at=base + timedelta(seconds=index),
            )
            item = main.MEMORY.item_by_id(item_id)
            item_ids.append(item_id)
            vector = [0.0, 0.0, 0.0, 0.0]
            vector[index % 4] = 1.0
            main.MEMORY.upsert_embedding(
                message_id=item_id,
                chat_id=-1001,
                model=main.CONFIG.memory_embedding_model,
                dimensions=4,
                content_hash=MemoryStore.content_hash(MemoryStore.searchable_text_for_item(item)),
                embedding=vector,
            )
        target = main.UserCommandTarget(user_id=407892151, username="tester", label="Test User", is_self=True)
        selection = main.target_memory_selection(FakeMessage("/character me"), target)

        prompt = main.build_character_profile_prompt(selection)

        self.assertIn("Embeddings available: 12/12", prompt)
        self.assertIn("Embedding-diverse sample", prompt)
        self.assertIn("embedded profile topic", prompt)

    def test_character_command_uses_cleaned_text(self) -> None:
        base = datetime.now(timezone.utc)
        samples = [
            "1. thrd - 6",
            "2. bot - 4",
            *[f"@thrd_ua_bot useful content {index}" for index in range(10)],
        ]
        for index, text in enumerate(samples):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3500 + index,
                sender_label="Test User (@tester, id=407892151)",
                user_id=407892151,
                username="tester",
                text=text,
                created_at=base + timedelta(seconds=index),
            )
        message = FakeMessage("/характер мій")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))
        captured = {}

        async def fake_run_plain_model(prompt: str) -> str:
            captured["prompt"] = prompt
            return "портрет готовий"

        with patch.object(main, "run_plain_model", new=fake_run_plain_model):
            asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        prompt = captured["prompt"].casefold()
        self.assertIn("useful content 0", prompt)
        self.assertIn("useful content 9", prompt)
        self.assertNotIn("@thrd_ua_bot", prompt)
        self.assertNotIn("thrd", prompt)
        self.assertNotIn("bot", prompt)
        self.assertNotIn("1. thrd - 6", prompt)

    def test_character_profile_ignores_repost_source_text(self) -> None:
        base = datetime.now(timezone.utc)
        for index in range(10):
            main.MEMORY.save_message(
                chat_id=-1001,
                message_id=3520 + index,
                sender_label="Test User (@tester, id=407892151)",
                user_id=407892151,
                username="tester",
                text=f"personal style sample {index}",
                source_text=f"channel repost body subscribe online {index}",
                forward_origin="Source Channel",
                created_at=base + timedelta(seconds=index),
            )
        message = FakeMessage("/character me")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238, send_chat_action=AsyncMock()))
        captured = {}

        async def fake_run_plain_model(prompt: str) -> str:
            captured["prompt"] = prompt
            return "profile ready"

        with patch.object(main, "run_plain_model", new=fake_run_plain_model):
            asyncio.run(main.character_command(SimpleNamespace(effective_message=message), context))

        prompt = captured["prompt"].casefold()
        self.assertIn("personal style sample 0", prompt)
        self.assertIn("source/repost items excluded from profile: 10", prompt)
        self.assertNotIn("channel repost body", prompt)
        self.assertNotIn("subscribe online", prompt)

    def test_character_command_requires_minimum_messages(self) -> None:
        main.MEMORY.save_message(
            chat_id=-1001,
            message_id=3400,
            sender_label="Test User (@tester, id=407892151)",
            user_id=407892151,
            username="tester",
            text="замало",
            created_at=datetime.now(timezone.utc),
        )
        message = FakeMessage("/profile me")

        with patch.object(main, "run_plain_model", new=AsyncMock()) as run_plain_model:
            asyncio.run(main.character_command(SimpleNamespace(effective_message=message), SimpleNamespace(bot=SimpleNamespace())))

        run_plain_model.assert_not_awaited()
        self.assertIn("щонайменше 10", message.reply_calls[0]["text"])

    def test_localized_ping_alias_uses_allowlisted_command(self) -> None:
        message = FakeMessage("/пінг")
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        asyncio.run(main.localized_command_alias(SimpleNamespace(effective_message=message), context))

        self.assertIn("pong", message.reply_calls[0]["text"])

class SystemHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        if main.SYSTEM_LOG is not None:
            main.SYSTEM_LOG.clear_all()
        if main.MEMORY is not None:
            main.MEMORY.clear_all()
        main.pending_requests.clear()
        main.passive_contexts.clear()
        main.last_user_call.clear()
        main.last_chat_call.clear()

    def test_redaction_hides_api_and_telegram_secrets(self) -> None:
        text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz TELEGRAM_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz"

        redacted = redact_secrets(text)

        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertNotIn("123456:abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertIn("[redacted]", redacted)

    def test_system_log_writes_reads_and_sanitizes_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SystemLogStore(Path(tmpdir) / "health.sqlite3", retention_days=14)

            store.record_event(
                level="error",
                component="web",
                event_type="prefetch_failed",
                message="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
                details={"GITHUB_TOKEN": "ghp_secretsecretsecret", "count": 2},
            )

            event = store.latest_events(1)[0]
            self.assertEqual("error", event.level)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", event.message)
            self.assertEqual("[redacted]", event.details["GITHUB_TOKEN"])
            self.assertEqual(2, event.details["count"])

    def test_complaint_classifier_detects_bot_web_issue(self) -> None:
        signal = classify_complaint("Aigan bot має problem: web search не працює", bot_username="thrd_ua_bot")

        self.assertIsNotNone(signal)
        self.assertEqual("web_search", signal.category)

    def test_complaint_temperature_reports_at_threshold(self) -> None:
        class FakeReporter:
            is_configured = True

            def __init__(self) -> None:
                self.calls = []

            def create_self_report_issue(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(url="https://github.com/Turkevich91/Aigan/issues/99")

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SystemLogStore(Path(tmpdir) / "health.sqlite3", retention_days=14)
            reporter = FakeReporter()
            service = SelfAnalysisService(
                store=store,
                reporter=reporter,
                complaint_lookback_seconds=86400,
                complaint_report_temperature=2,
            )

            first = service.record_complaint_signal(text="Aigan bot має problem: web search не працює")
            second = service.record_complaint_signal(text="Aigan bot має problem: web search не працює")

            self.assertEqual(1, first.temperature)
            self.assertEqual(2, second.temperature)
            self.assertEqual(1, len(reporter.calls))
            self.assertTrue(reporter.calls[0]["title"].startswith("[Aigan] self-report: web_search"))
            self.assertIn("not a confirmed bug", reporter.calls[0]["body"])
            self.assertIn("issues/99", store.active_complaints(1)[0].github_issue_url)

    def test_passive_group_complaint_stays_silent_but_records_temperature(self) -> None:
        message = FakeMessage("Aigan bot problem: web search не працює", message_id=5000)
        context = SimpleNamespace(bot=SimpleNamespace(username="thrd_ua_bot", id=8712856238))

        asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))

        self.assertEqual([], message.reply_calls)
        clusters = main.SYSTEM_LOG.active_complaints(1)
        self.assertEqual(1, len(clusters))
        self.assertEqual("web_search", clusters[0].category)
        self.assertEqual(1, clusters[0].temperature)

    def test_health_command_is_admin_only(self) -> None:
        admin_message = FakeMessage("/health")
        non_admin_message = FakeMessage("/health")
        non_admin_message.from_user = FakeUser(user_id=123, username="guest")

        asyncio.run(main.health_command(SimpleNamespace(effective_message=admin_message), SimpleNamespace()))
        asyncio.run(main.health_command(SimpleNamespace(effective_message=non_admin_message), SimpleNamespace()))

        self.assertIn("Status:", admin_message.reply_calls[0]["text"])
        self.assertTrue(non_admin_message.reply_calls)
        self.assertNotIn("Status:", non_admin_message.reply_calls[0]["text"])

    def test_selfcheck_uses_sanitized_context(self) -> None:
        main.SYSTEM_LOG.record_event(
            level="error",
            component="agent",
            event_type="run_error",
            message="OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
        )
        message = FakeMessage("/selfcheck")

        with patch.object(main, "run_plain_model", new=AsyncMock(return_value="health degraded")) as run_plain_model:
            asyncio.run(main.selfcheck_command(SimpleNamespace(effective_message=message), SimpleNamespace()))

        prompt = run_plain_model.await_args.args[0]
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", prompt)
        self.assertIn("[redacted]", prompt)
        self.assertIn("health degraded", message.reply_calls[0]["text"])


if __name__ == "__main__":
    unittest.main()
