import asyncio
import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test-token"
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["ALLOWED_CHAT_IDS"] = "-1001"
os.environ["ADMIN_USER_IDS"] = "407892151"
os.environ["AUTO_REACT_ENABLED"] = "false"

import httpx
from telegram.constants import ChatType

import main
from mcp_servers import web


class FakeUser:
    def __init__(self, user_id: int = 407892151) -> None:
        self.id = user_id
        self.is_bot = False
        self.full_name = "Test User"
        self.username = "tester"


class FakeMessage:
    def __init__(self, text: str = "", chat_type: str = ChatType.SUPERGROUP, chat_id: int = -1001) -> None:
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.chat = SimpleNamespace(type=chat_type, title="Test Chat")
        self.from_user = FakeUser()
        self.photo = []
        self.document = None
        self.reply_to_message = None
        self.entities = None

    async def reply_text(self, text: str) -> None:
        self.last_reply = text


class PendingFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        main.pending_requests.clear()
        main.passive_contexts.clear()
        main.histories.clear()
        main.last_user_call.clear()
        main.last_chat_call.clear()
        main.last_auto_react_chat.clear()

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


if __name__ == "__main__":
    unittest.main()
