"""Randomizer SDK, delivery, and same-chat continuation regressions.

Owner: conversational capability integration.
"""
import asyncio
from collections import defaultdict, deque
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from agent_capabilities import PrimaryCapabilities
from image_capability import ImageCapabilityContext, ImageCapabilitySession
from randomizer_capability import (
    CLARIFICATION_TEXT, FAILED_TEXT, RandomizerSession,
)
from tests.test_agent_capabilities import ScriptedModel
from tests.test_standard_tools import NoTransportMCP


DRAW = "Pick a random integer from 1 to 30."
DRAW_ARGS = {"operation": "integers", "minimum": 1, "maximum": 30}


class RandomizerAgentTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(
            main.CONFIG, primary_capability_recovery_enabled=False,
            allowed_chat_ids=frozenset({-1001, -2002}),
            chat_inflight_guard_enabled=True, chat_duplicate_suppress_seconds=120,
        )

    def run_sdk(self, session, steps, *, capabilities=None, prompt=DRAW):
        model = ScriptedModel(steps)
        original_factory = main.make_agent
        constructed = []
        provenance = SimpleNamespace(run_id="a" * 32, route="chat", tools=[])

        def factory(_servers, provided_capabilities=None, **kwargs):
            agent = original_factory([], provided_capabilities, **kwargs)
            agent.model = model
            constructed.append(agent)
            return agent

        token = main.ACTIVE_RANDOMIZER.set(session)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(main, "CONFIG", self.config))
                stack.enter_context(patch.object(main, "MCPServerStdio", NoTransportMCP))
                stack.enter_context(patch.object(main, "make_agent", side_effect=factory))
                stack.enter_context(patch.object(main, "configured_agents_model", return_value=model))
                stack.enter_context(patch.object(main, "MODEL_TELEMETRY", None))
                stack.enter_context(patch.object(main, "SYSTEM_LOG", None))
                output = asyncio.run(main.run_agent(
                    prompt, capability_context=capabilities, outbound_provenance=provenance))
        finally:
            main.ACTIVE_RANDOMIZER.reset(token)
        return output, model, constructed[0], provenance

    def test_required_draw_blocks_model_only_final_and_forces_tool(self):
        session = RandomizerSession(required=True)
        output, model, agent, provenance = self.run_sdk(session, ["17"])
        self.assertEqual(FAILED_TEXT, output)
        self.assertNotEqual("17", output)
        self.assertEqual("randomize", agent.model_settings.tool_choice)
        self.assertFalse(agent.model_settings.parallel_tool_calls)
        self.assertFalse(session.attempted)
        self.assertEqual([], provenance.tools)
        self.assertEqual(1, len(model.inputs))

    def test_successful_sdk_result_is_exact_and_stops_before_model_prose(self):
        for value in (17, 23):
            with self.subTest(value=value):
                source = Mock()
                source.randint.return_value = value
                session = RandomizerSession(required=True, rng=source)
                output, model, _, provenance = self.run_sdk(
                    session, [("randomize", DRAW_ARGS), "Invented result: 999"])
                self.assertEqual(str(value), output)
                self.assertEqual(1, len(model.inputs))
                self.assertEqual(["Invented result: 999"], model.steps)
                source.randint.assert_called_once_with(1, 30)
                self.assertTrue(any(tool.tool_kind == "randomize" and tool.result_status == "ok"
                                    for tool in provenance.tools))

    def test_distinct_requests_may_both_return_17_and_each_call_rng(self):
        source = Mock()
        source.randint.return_value = 17
        outputs = [
            self.run_sdk(RandomizerSession(required=True, rng=source),
                         [("randomize", DRAW_ARGS)])[0]
            for _ in range(2)
        ]
        self.assertEqual(["17", "17"], outputs)
        self.assertEqual(2, source.randint.call_count)

    def test_rng_failure_is_terminal_and_has_no_successful_provenance(self):
        source = Mock()
        source.randint.side_effect = OSError("synthetic unavailable source")
        output, model, _, provenance = self.run_sdk(
            RandomizerSession(required=True, rng=source),
            [("randomize", DRAW_ARGS), "17"])
        self.assertEqual(FAILED_TEXT, output)
        self.assertEqual(1, len(model.inputs))
        source.randint.assert_called_once()
        self.assertFalse(any(tool.tool_kind == "randomize" and tool.result_status == "ok"
                             for tool in provenance.tools))

    def test_missing_bounds_clarify_without_a_draw(self):
        source = Mock()
        output, model, _, _ = self.run_sdk(
            RandomizerSession(required=True, rng=source),
            [("randomize", {"operation": "integers", "minimum": 1}), "17"])
        self.assertEqual(CLARIFICATION_TEXT, output)
        self.assertEqual(1, len(model.inputs))
        source.randint.assert_not_called()

    def test_unforced_tool_is_available_with_recovery_disabled_and_renders_result(self):
        source = Mock()
        source.choice.return_value = "<b>кіт</b>"
        output, model, agent, _ = self.run_sdk(
            RandomizerSession(rng=source),
            [("randomize", {"operation": "choose", "items": ["<b>кіт</b>"],
                            "replacement": True}), "Tool narration"],
            prompt="Let chance decide between my entries.")
        self.assertEqual("<b>кіт</b>", output)
        self.assertEqual(1, len(model.inputs))
        self.assertIn("randomize", {tool.name for tool in agent.tools})
        self.assertNotEqual("randomize", agent.model_settings.tool_choice)
        source.choice.assert_called_once_with(["<b>кіт</b>"])

    def test_existing_image_callback_still_ends_on_accepted_delivery(self):
        prompt = "Find five red flowers"
        capabilities = PrimaryCapabilities(
            images=ImageCapabilitySession(ImageCapabilityContext(prompt, -1001)))
        output, model, _, _ = self.run_sdk(
            RandomizerSession(),
            [("request_image_delivery", {
                "operation_text": prompt, "grounding": "current_text",
                "subject_text": "red flowers", "modifier_text": "",
                "quantity_kind": "exact", "quantity_value": 5,
            }), "Unsupported delivery narration"],
            capabilities=capabilities, prompt=prompt)
        self.assertEqual("", output)
        self.assertEqual(1, len(model.inputs))
        self.assertIsNotNone(capabilities.images.claim_plan())

    def test_required_delivery_blocks_skipped_tool_even_outside_sdk(self):
        message = FakeMessage(DRAW)
        token = main.ACTIVE_RANDOMIZER.set(RandomizerSession(required=True))
        try:
            asyncio.run(main.send_reply(message, "17"))
        finally:
            main.ACTIVE_RANDOMIZER.reset(token)
        self.assertEqual(FAILED_TEXT, message.reply_calls[-1]["text"])

    def test_successful_delivery_keeps_literal_markup_and_overrides_candidate(self):
        message = FakeMessage("Choose one supplied entry")
        session = RandomizerSession()
        session.execute("choose", items=["<b>literal & value</b>"])
        token = main.ACTIVE_RANDOMIZER.set(session)
        try:
            asyncio.run(main.send_reply(message, "I used a tool and selected 999"))
        finally:
            main.ACTIVE_RANDOMIZER.reset(token)
        self.assertEqual("&lt;b&gt;literal &amp; value&lt;/b&gt;", message.reply_calls[-1]["text"])

    def test_handler_installs_requirement_and_restores_context(self):
        observed = []

        async def handler(message, _context, _prompt, *_args, **_kwargs):
            observed.append(main.ACTIVE_RANDOMIZER.get().required)
            await main.send_reply(message, "17")

        message = FakeMessage(DRAW)
        original = main.ACTIVE_RANDOMIZER.get()
        with patch.object(main, "CONFIG", self.config), \
             patch.object(main, "_handle_prompt_generation", side_effect=handler):
            asyncio.run(main.handle_prompt_generation(message, SimpleNamespace(), DRAW, allow_pending_wait=False))
        self.assertEqual([True], observed)
        self.assertEqual(FAILED_TEXT, message.reply_calls[-1]["text"])
        self.assertIs(original, main.ACTIVE_RANDOMIZER.get())

    def test_new_draw_bypasses_prompt_deduplication_but_ordinary_duplicate_does_not(self):
        message = FakeMessage(DRAW)
        with patch.object(main, "CONFIG", self.config), \
             patch.object(main, "recent_chat_answers", defaultdict(deque)):
            main.record_chat_answer(message, DRAW, "randomizer")
            self.assertEqual("", main.duplicate_prompt_reason(message, DRAW))
            self.assertEqual("", main.duplicate_prompt_reason(message, DRAW, exact_only=True))
            ordinary = "Summarize these observations."
            main.record_chat_answer(message, ordinary, "chat")
            self.assertEqual("exact_prompt", main.duplicate_prompt_reason(message, ordinary))

    def test_same_chat_repeat_retains_full_long_original_request(self):
        original = "Choose a random entry: " + ", ".join(f"candidate-{i:03d}" for i in range(70))
        self.assertGreater(len(original), 500)
        message = FakeMessage(original)
        repeat = FakeMessage("again", message_id=102)
        foreign = FakeMessage("again", chat_id=-2002, message_id=103)
        with patch.object(main, "CONFIG", self.config), \
             patch.object(main, "recent_chat_answers", defaultdict(deque)):
            main.record_chat_answer(message, original, "randomizer")
            self.assertEqual(original, main.randomizer_previous_request(repeat, "again"))
            self.assertEqual("", main.randomizer_previous_request(foreign, "again"))
            self.assertEqual("", main.duplicate_prompt_reason(repeat, "again"))
            main.record_chat_answer(message, "17", "chat")
            self.assertEqual("", main.randomizer_previous_request(repeat, "again"))

    def test_reply_repeat_requires_successful_same_chat_randomizer_evidence(self):
        message = FakeMessage("again", message_id=103)
        message.reply_to_message = FakeMessage("17", message_id=102)
        source = SimpleNamespace(is_bot=False, text=DRAW, reply_to_message_id=None)
        output = SimpleNamespace(id=12, is_bot=True)
        store = Mock()
        store.message_by_message_id.side_effect = lambda chat, mid: (
            output if (chat, mid) == (-1001, 102) else
            source if (chat, mid) == (-1001, 101) else None)
        provenance = SimpleNamespace(
            status="delivered", trigger_message_id=101,
            tools=[SimpleNamespace(tool_kind="randomize", result_status="ok")])
        store.provenance_for_output.return_value = provenance
        with patch.object(main, "CONFIG", self.config), patch.object(main, "MEMORY", store):
            self.assertEqual(DRAW, main.randomizer_previous_request(message, "again"))
            provenance.tools[0].result_status = "tool_failed"
            self.assertEqual("", main.randomizer_previous_request(message, "again"))
            provenance.tools[0].result_status = "ok"
            provenance.status = "failed"
            self.assertEqual("", main.randomizer_previous_request(message, "again"))
            provenance.status = "delivered"
            message.reply_to_message.chat_id = -2002
            store.reset_mock()
            self.assertEqual("", main.randomizer_previous_request(message, "again"))
            store.message_by_message_id.assert_not_called()


    def test_reply_repeat_accepts_explicit_bot_invocation_and_outside_hint_phrasing(self):
        message = FakeMessage("again", message_id=103)
        message.reply_to_message = FakeMessage("17", message_id=102)
        output = SimpleNamespace(id=12, is_bot=True)
        source = SimpleNamespace(is_bot=False, text="", reply_to_message_id=None)
        store = Mock()
        store.message_by_message_id.side_effect = lambda chat, mid: (
            output if (chat, mid) == (-1001, 102) else
            source if (chat, mid) == (-1001, 101) else None)
        store.provenance_for_output.return_value = SimpleNamespace(
            status="delivered", trigger_message_id=101,
            tools=[SimpleNamespace(tool_kind="randomize", result_status="ok")])
        with patch.object(main, "CONFIG", self.config), patch.object(main, "MEMORY", store), \
             patch.object(main, "BOT_USERNAME", "synthetic_random_bot"):
            source.text = "@synthetic_random_bot " + DRAW
            self.assertEqual(DRAW, main.randomizer_previous_request(message, "again"))
            source.text = "Let chance decide between these entries: oak, pine."
            self.assertFalse(main.is_randomization_request(source.text))
            self.assertEqual(source.text, main.randomizer_previous_request(message, "again"))

    def run_image_sdk(self, prompt, steps, source, *, recovery_enabled=True):
        model = ScriptedModel(steps)
        original_factory = main.make_agent
        constructed = []
        sessions = []
        config = replace(self.config, primary_capability_recovery_enabled=recovery_enabled,
                         web_image_search_enabled=False)
        message = FakeMessage(prompt, message_id=444)
        presence = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())

        def factory(_servers, capabilities=None, **kwargs):
            agent = original_factory([], capabilities, **kwargs)
            agent.model = model
            constructed.append(agent)
            sessions.append(main.ACTIVE_RANDOMIZER.get())
            return agent

        old_session = main.ACTIVE_RANDOMIZER.get()
        old_previous = main.ACTIVE_RANDOMIZER_PREVIOUS.get()
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "CONFIG", config))
            stack.enter_context(patch.object(main, "MEMORY", None))
            stack.enter_context(patch.object(main, "MODEL_TELEMETRY", None))
            stack.enter_context(patch.object(main, "SYSTEM_LOG", None))
            stack.enter_context(patch.object(main, "cooldown_left", return_value=0))
            stack.enter_context(patch.object(main, "mark_cooldown"))
            stack.enter_context(patch.object(main, "remember_observed_message"))
            stack.enter_context(patch.object(main, "prepare_memory_context", AsyncMock(return_value="")))
            stack.enter_context(patch.object(main, "extract_image_data_urls", AsyncMock(
                return_value=["data:image/png;base64,c3ludGhldGlj"])))
            stack.enter_context(patch.object(main, "activity_presence_for_message", return_value=presence))
            stack.enter_context(patch.object(main, "notify_operator_alert", AsyncMock()))
            stack.enter_context(patch.object(main, "MCPServerStdio", NoTransportMCP))
            stack.enter_context(patch.object(main, "make_agent", side_effect=factory))
            stack.enter_context(patch.object(main, "configured_agents_model", return_value=model))
            stack.enter_context(patch.object(main, "RandomizerSession", side_effect=lambda **kwargs:
                RandomizerSession(rng=source, **kwargs)))
            stack.enter_context(patch.object(main, "histories", defaultdict(lambda: deque(maxlen=30))))
            stack.enter_context(patch.object(main, "passive_contexts", defaultdict(lambda: deque(maxlen=30))))
            stack.enter_context(patch.object(main, "recent_chat_answers", defaultdict(deque)))
            legacy = stack.enter_context(patch.object(main, "run_vision", AsyncMock()))
            asyncio.run(main.handle_image_prompt_generation(message, prompt, [message], [], prompt))
            repeated_source = main.randomizer_previous_request(FakeMessage("again", message_id=445), "again")
        self.assertIs(old_session, main.ACTIVE_RANDOMIZER.get())
        self.assertEqual(old_previous, main.ACTIVE_RANDOMIZER_PREVIOUS.get())
        presence.stop.assert_awaited_once()
        legacy.assert_not_awaited()
        return message, model, constructed, sessions, repeated_source

    def test_image_sdk_random_result_stays_literal_through_delivery_and_scope_reset(self):
        items = ["<b>literal</b>"] + [f"entry-{index:03d}" for index in range(70)]
        prompt = "Choose a random item: " + ", ".join(items)
        self.assertGreater(len(prompt), 500)
        for recovery_enabled in (True, False):
            with self.subTest(recovery_enabled=recovery_enabled):
                source = Mock()
                source.sample.return_value = ["<b>literal</b>"]
                message, model, agents, sessions, repeated_source = self.run_image_sdk(
                    prompt, [("randomize", {"operation": "choose", "items": items}),
                             "I picked 999"], source, recovery_enabled=recovery_enabled)
                self.assertEqual(1, len(model.inputs))
                self.assertEqual("randomize", agents[0].model_settings.tool_choice)
                self.assertTrue(sessions[0].required)
                self.assertTrue(sessions[0].literal_output)
                source.sample.assert_called_once_with(items, 1)
                self.assertEqual("&lt;b&gt;literal&lt;/b&gt;", message.reply_calls[-1]["text"])
                self.assertEqual(prompt, repeated_source)

    def test_image_sdk_explicit_random_request_blocks_missing_call(self):
        for recovery_enabled in (True, False):
            with self.subTest(recovery_enabled=recovery_enabled):
                source = Mock()
                message, model, _, sessions, repeated_source = self.run_image_sdk(
                    DRAW, ["17"], source, recovery_enabled=recovery_enabled)
                self.assertEqual(1, len(model.inputs))
                self.assertTrue(sessions[0].required)
                self.assertFalse(sessions[0].attempted)
                source.randint.assert_not_called()
                self.assertEqual(FAILED_TEXT, message.reply_calls[-1]["text"])
                self.assertEqual("", repeated_source)


if __name__ == "__main__":
    unittest.main()
