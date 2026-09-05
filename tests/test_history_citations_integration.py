"""Actual SDK and Telegram chunk adapter regressions, using synthetic transports."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from tests import test_history_citations as citation_fixtures
from tests.test_agent_capabilities import ScriptedModel
from agents import Agent, Runner, RunConfig
from agent_capabilities import PrimaryCapabilities
from history_citations import ACTIVE_HISTORY_CITATIONS


class HistoryCitationIntegrationTests(citation_fixtures.HistoryCitationTests):
    # Reuse fixture setup without inheriting its test cases a second time.
    for _name in tuple(name for name in vars(citation_fixtures.HistoryCitationTests) if name.startswith("test_")):
        locals()[_name] = None

    def test_real_chunking_keeps_verified_footer_after_paragraph_packing(self):
        text = ("x" * 1750 + "\n\n") * 7 + self.expose()
        fit = lambda value, footer: all(block in "\n\n".join(main.split_text_chunks(value))
                                       for block in footer.strip().split("\n\n"))
        output = self.citations.render(text, max_chars=12000, fits=fit)
        sender = AsyncMock(return_value=SimpleNamespace(message_id=77))
        delivered = asyncio.run(main.send_text_chunks(sender, output))
        self.assertTrue(delivered.complete)
        actual = "\n\n".join(call.kwargs["text"] for call in sender.await_args_list)
        self.assertIn("https://t.me/c/123/11", actual)
        self.assertIn("вибірка зі збережених", actual)

    def test_actual_sdk_tool_result_reference_resolves_after_run(self):
        class RefModel(ScriptedModel):
            async def get_response(self, system_instructions, input, *args, **kwargs):
                if self.steps == ["USE_RETURNED_REF"]:
                    encoded = json.dumps(input, default=str)
                    ref = re.search(r"\[\[history:[a-f0-9]{12}:[0-9]+\]\]", encoded).group()
                    self.steps = ["Cedar Square. " + ref]
                return await super().get_response(system_instructions, input, *args, **kwargs)
        capabilities = PrimaryCapabilities(history=self.history, citations=self.citations)
        model = RefModel([("read_chat_history", {"mode": "search", "query": "Cedar"}), "USE_RETURNED_REF"])
        agent = Agent(name="Synthetic history", model=model, tools=capabilities.tools())
        result = asyncio.run(Runner.run(agent, "Find the agreed location", run_config=RunConfig(tracing_disabled=True)))
        output = self.citations.render(str(result.final_output))
        self.assertIn("https://t.me/c/123/11", output)
        self.assertEqual(self.history.calls_used, 1)

    def test_formatter_and_context_builder_expose_only_selected_sources(self):
        with patch.object(main, "MEMORY", self.store):
            token = ACTIVE_HISTORY_CITATIONS.set(self.citations)
            try:
                rendered = main.format_memory_item_line(self.store.item_by_id(self.old))
            finally:
                ACTIVE_HISTORY_CITATIONS.reset(token)
        self.citations.expose_contexts((rendered, None))
        ref = re.search(r"\[\[history:[^\]]+\]\]", rendered).group()
        message = FakeMessage("Find the location", message_id=30)
        with patch.object(main, "MEMORY", self.store):
            prompt = main.build_agent_input(message, message.text, memory_context=rendered)
        self.assertIn(ref, prompt)
        self.assertIsNone(ACTIVE_HISTORY_CITATIONS.get())
        self.assertIn("https://t.me/c/123/11", self.citations.render(ref))

    def test_host_preloaded_source_reaches_delivery_without_extra_history_call(self):
        message = FakeMessage("Recall the location", message_id=30)
        message.chat_id = -100123
        message.chat.id = -100123
        message.chat.type = "supergroup"
        message.date = self.now
        stats = main.MemoryContextCompilationStats(0, 0, frozenset({self.old}))
        captured = []
        async def prepare(*_args, **_kwargs):
            return main.format_memory_item_line(self.store.item_by_id(self.old)), None, stats
        async def primary(_provenance, prompt, *, capability_context, **_kwargs):
            captured.append(capability_context)
            self.assertIsNone(ACTIVE_HISTORY_CITATIONS.get())
            return "Cedar Square " + re.search(r"\[\[history:[a-f0-9]{12}:[0-9]+\]\]", prompt).group()
        patches = {
            "should_allow_chat": lambda _: True,
            "maybe_resolve_reminder_context_response": AsyncMock(return_value=False),
            "classify_request_with_intent": AsyncMock(return_value=main.RequestClassification("normal")),
            "route_tool_capabilities_for_message": AsyncMock(return_value=main.no_tool_route("test")),
            "schedule_model_policy_shadow": lambda *_args, **_kwargs: None,
            "send_activity_action": AsyncMock(), "maybe_prefetch_web_context": AsyncMock(return_value=None),
            "prepare_agent_memory_context": AsyncMock(side_effect=prepare),
            "prepare_semantic_memory_context": AsyncMock(return_value=None),
            "run_agent_for_outbound": AsyncMock(side_effect=primary), "send_reply": AsyncMock(),
        }
        presence = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "CONFIG", replace(main.CONFIG, primary_capability_recovery_enabled=True)))
            stack.enter_context(patch.object(main, "MEMORY", self.store))
            stack.enter_context(patch.object(main, "activity_presence_for_message", return_value=presence))
            stack.enter_context(patch.object(main, "cooldown_left", return_value=0))
            for name, value in patches.items():
                stack.enter_context(patch.object(main, name, value))
            asyncio.run(main.handle_prompt_generation(message, SimpleNamespace(bot=SimpleNamespace()), message.text,
                                                      allow_pending_wait=False))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].history.calls_used, 0)
        self.assertIn("https://t.me/c/123/11", patches["send_reply"].call_args.args[1])
        self.assertIsNone(ACTIVE_HISTORY_CITATIONS.get())
