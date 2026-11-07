"""Current-chat tool settings and revocation at real dispatch boundaries."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment

configure_test_environment()
import main
from agent_capabilities import ChatToolMCPServer
from agents.mcp import MCPServerStdio
from chat_model_settings import ChatModelSettingsStore, StaleSettingsRevision
from chat_tool_policy import tool_allowed
from tests.test_standard_tools import NoTransportMCP


class ToolPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.path = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / "settings.sqlite3"
        self.store = ChatModelSettingsStore(self.path)
        self.stack.enter_context(patch.object(main, "CHAT_MODEL_SETTINGS", self.store))
        self.chat = -1001
        self.message = FakeMessage(chat_id=self.chat)

    def switch(self, key, enabled):
        revision = self.store.tools.resolve(self.chat).revision
        return self.store.tools.set_enabled(self.chat, key, enabled, expected_revision=revision)

    def test_persistence_and_separate_settings_lifetimes(self):
        self.assertFalse(self.store.tools.resolve(self.chat).disabled)
        self.store.apply(self.chat, "astra", "low", 900, now=100)
        off = self.switch("web", False)
        again = ChatModelSettingsStore(self.path)
        self.assertEqual(off, again.tools.resolve(self.chat))
        with self.assertRaises(StaleSettingsRevision):
            again.tools.set_enabled(self.chat, "youtube", False, expected_revision=0)
        self.assertFalse(again.tools.resolve(-2002).disabled)
        self.assertFalse(again.resolve(self.chat, "baseline", "low", now=1000).overridden)
        again.reset(self.chat, now=1001)
        self.assertEqual({"web"}, again.tools.resolve(self.chat).disabled)
        reset = again.tools.reset(self.chat, expected_revision=off.revision)
        self.assertFalse(reset.disabled)
        self.assertGreater(reset.revision, off.revision)

    def test_revocation_survives_reenable_until_next_request(self):
        with main.chat_model_scope(self.chat):
            self.switch("randomizer", False)
            self.switch("randomizer", True)
            with main.chat_model_scope(self.chat):
                self.assertFalse(tool_allowed("randomizer", before_call=True))
            with main.chat_model_scope(-2002):
                self.assertTrue(tool_allowed("randomizer", before_call=True))
            with main.chat_model_scope(self.chat, inherit=False):
                self.assertTrue(tool_allowed("randomizer", before_call=True))
        self.switch("web", False)
        with main.chat_model_scope(self.chat):
            self.switch("web", True)
            self.assertFalse(tool_allowed("web"))
        with main.chat_model_scope(self.chat):
            self.assertTrue(tool_allowed("web", before_call=True))

    async def test_mcp_discovery_and_cached_dispatch_cannot_bypass_revocation(self):
        server = ChatToolMCPServer(name="web", params={"command": "unused"})
        fake_tools = [SimpleNamespace(name="search_web"), SimpleNamespace(name="search_images")]
        with main.chat_model_scope(self.chat), patch.object(
            MCPServerStdio, "list_tools", AsyncMock(return_value=fake_tools)
        ), patch.object(MCPServerStdio, "call_tool", AsyncMock()) as call:
            self.assertEqual(["search_web"], [item.name for item in await server.list_tools()])
            self.switch("web", False)
            self.assertEqual([], await server.list_tools())
            self.assertTrue((await server.call_tool("search_web", {})).isError)
            call.assert_not_awaited()
        self.switch("web", True)
        with main.chat_model_scope(self.chat), patch.object(
            MCPServerStdio, "call_tool", AsyncMock(return_value="ok")
        ) as call:
            self.assertEqual("ok", await server.call_tool("fetch_url", {"url": "synthetic"}))
            self.assertTrue((await server.call_tool("search_images", {})).isError)
            call.assert_awaited_once()

    async def test_direct_web_prefetch_checks_between_url_and_search(self):
        def fetch(*_):
            self.switch("web", False)
            return "synthetic evidence"

        with main.chat_model_scope(self.chat), patch.object(main, "fetch_url", side_effect=fetch) as url, \
                patch.object(main, "search_web") as search, patch.object(main, "web_prefetch_query", return_value="q"):
            prompt = "https://example.com/update"
            result = await main.maybe_prefetch_web_context(self.message, prompt, "time_sensitive")
            self.assertIn("synthetic evidence", result)
            self.assertEqual("(none)", await main.maybe_prefetch_web_context(self.message, prompt, "time_sensitive"))
            url.assert_called_once()
            search.assert_not_called()

    async def test_discovery_omits_disabled_tools_and_keeps_the_other_server(self):
        self.switch("web", False)
        self.switch("randomizer", False)
        with main.chat_model_scope(self.chat), patch.object(main, "MCPServerStdio", NoTransportMCP), \
                patch.object(main.Runner, "run", AsyncMock(return_value=SimpleNamespace(final_output="ok", new_items=[]))) as run, \
                patch.object(main, "configured_agents_model", return_value=SimpleNamespace(model="synthetic")):
            await main.run_agent("synthetic request")
            agent = run.call_args.args[0]
            self.assertEqual(["youtube_transcript"], [mcp.options["name"] for mcp in agent.mcp_servers])
            self.assertNotIn("randomize", [tool.name for tool in agent.tools])
            self.assertIn("disabled", agent.instructions)

    async def test_disabled_randomizer_cannot_fall_back_to_generated_numbers(self):
        self.switch("randomizer", False)
        prompt = "Pick a random integer from 1 to 30."
        with patch.object(main, "_handle_prompt_generation", AsyncMock()) as text, \
                patch.object(main, "_handle_image_prompt_generation", AsyncMock()) as image, \
                patch.object(main, "send_reply", AsyncMock()) as send:
            await main.handle_prompt_generation(self.message, None, prompt, False)
            await main.handle_image_prompt_generation(self.message, prompt, (), (), "synthetic")
            text.assert_not_awaited()
            image.assert_not_awaited()
            self.assertEqual(2, send.await_count)
            self.assertIn("вимкнено", send.await_args.args[1])

    async def test_image_direct_and_delivery_paths_respect_revocation(self):
        with main.chat_model_scope(self.chat), patch.object(main, "send_reply", AsyncMock()) as notice, \
                patch.object(main, "prepare_web_image_review", AsyncMock()) as prepare, \
                patch.object(main, "send_single_web_image", AsyncMock()) as send:
            self.switch("images", False)
            outcome = await main.maybe_send_internet_image(self.message, "", plan=object())
            self.assertFalse(outcome.request_fulfilled)
            result = await main.send_web_image_results(self.message, [object()])
            self.assertFalse(result.deliveries)
            self.assertIn("вимкнено", notice.call_args.args[1])
            prepare.assert_not_awaited()
            send.assert_not_awaited()
