import asyncio
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
os.environ["MEMORY_RETENTION_DAYS"] = "30"
os.environ["MEMORY_IMAGE_SUMMARY_LIMIT"] = "3"

import httpx
from telegram import InputMediaPhoto
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest

import main
from memory import MemoryStore
from mcp_servers import web

VALID_JPEG = b"\xff\xd8\xff\xe0" + b"valid-jpeg"


class FakeUser:
    def __init__(self, user_id: int = 407892151) -> None:
        self.id = user_id
        self.is_bot = False
        self.full_name = "Test User"
        self.username = "tester"


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
        main.passive_contexts.clear()
        main.histories.clear()
        main.pending_requests.clear()
        main.last_user_call.clear()
        main.last_chat_call.clear()
        main.last_auto_react_chat.clear()

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


if __name__ == "__main__":
    unittest.main()
