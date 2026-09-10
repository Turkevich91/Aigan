"""Admitted text/image turns share the real SDK loop without external transport."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from agents import function_tool
from image_capability import ImageDeliveryProposal
from memory import MemoryStore
from tests.test_agent_capabilities import ScriptedModel


class NoTransportMCP:
    def __init__(self, *args, **kwargs):
        self.options = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class StandardToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.config = replace(main.CONFIG, primary_capability_recovery_enabled=True,
                              web_image_search_enabled=True,
                              vision_interactive_model="gpt-6-astra",
                              vision_interactive_reasoning_effort="medium")
        self.message = FakeMessage("Can you check the event shown here?", message_id=70)
        self.message.date = datetime.now(timezone.utc)
        self.store.save_message(chat_id=self.message.chat_id, message_id=70,
                                user_id=self.message.from_user.id, sender_label="Synthetic user",
                                text=self.message.text, created_at=self.message.date)
        self.presence = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())

    def image_patches(self, stack):
        stack.enter_context(patch.object(main, "CONFIG", self.config))
        stack.enter_context(patch.object(main, "MEMORY", self.store))
        stack.enter_context(patch.object(main, "cooldown_left", return_value=0))
        stack.enter_context(patch.object(main, "mark_cooldown"))
        stack.enter_context(patch.object(main, "remember_observed_message"))
        stack.enter_context(patch.object(main, "prepare_memory_context", AsyncMock(return_value="")))
        stack.enter_context(patch.object(main, "extract_image_data_urls", AsyncMock(return_value=["data:image/png;base64,c3ludGhldGlj"])))
        stack.enter_context(patch.object(main, "activity_presence_for_message", return_value=self.presence))
        stack.enter_context(patch.object(main, "notify_operator_alert", AsyncMock()))
        delivery = stack.enter_context(patch.object(main, "send_reply", AsyncMock()))
        return delivery

    def test_actual_sdk_image_can_search_and_fetch_with_configured_role(self):
        calls = []
        @function_tool
        async def search_web(query: str) -> str:
            calls.append(("search", query))
            return "Synthetic event source: https://example.com/event"
        @function_tool
        async def fetch_url(url: str) -> str:
            calls.append(("fetch", url))
            return "Synthetic verified event description."
        model = ScriptedModel([("search_web", {"query": "synthetic event"}),
                               ("fetch_url", {"url": "https://example.com/event"}),
                               "I checked the supplied image against the event source."])
        original_factory = main.make_agent
        agents = []
        def factory(servers, capabilities=None, **kwargs):
            self.assertEqual(2, len(servers))
            self.assertEqual(["search_images"], servers[0].options["tool_filter"]["blocked_tool_names"])
            agent = original_factory([], capabilities, **kwargs)
            agents.append(agent)
            self.assertEqual("gpt-6-astra", agent.model)
            self.assertEqual("medium", agent.model_settings.reasoning.effort)
            agent.model = model
            agent.tools.extend([search_web, fetch_url])
            return agent
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            stack.enter_context(patch.object(main, "MCPServerStdio", NoTransportMCP))
            stack.enter_context(patch.object(main, "make_agent", side_effect=factory))
            configured_model = stack.enter_context(patch.object(main, "configured_agents_model", return_value=model))
            legacy = stack.enter_context(patch.object(main, "run_vision", AsyncMock()))
            asyncio.run(main.handle_image_prompt_generation(self.message, self.message.text,
                        [self.message], [], "synthetic-image-turn"))
        configured_model.assert_called_once_with("gpt-6-astra")
        self.assertEqual(["search", "fetch"], [call[0] for call in calls])
        self.assertEqual(3, len(model.inputs))
        content = model.inputs[0][0]["content"]
        self.assertEqual("input_image", content[1]["type"])
        self.assertIn("Trusted current user request:", content[0]["text"])
        self.assertIn("Untrusted referenced/replied-to context", content[0]["text"])
        self.assertTrue(content[0]["text"].startswith("Current time metadata:"))
        self.assertEqual(1, len(agents))
        names = {tool.name for tool in agents[0].tools}
        self.assertTrue({"read_chat_history", "read_conversation_branch", "request_image_delivery"} <= names)
        self.assertFalse(any("living_reminder" in name for name in names))
        legacy.assert_not_awaited()
        delivery.assert_awaited_once()
        provenance = delivery.await_args.kwargs["outbound_provenance"]
        self.assertTrue(any(item.tool_kind == "search_web" for item in provenance.tools))
        self.assertTrue(any(item.tool_kind == "fetch_url" for item in provenance.tools))

    def test_combined_image_agent_preserves_source_caption_and_search(self):
        # Integration contract with the separately reviewed media ownership fix.
        item = self.store.message_by_message_id(self.message.chat_id, self.message.message_id)
        neutral = "Cobalt banner above an empty stage."
        self.store.update_vision_summary(item.id, neutral)
        self.test_actual_sdk_image_can_search_and_fetch_with_configured_role()
        self.assertEqual(neutral, self.store.item_by_id(item.id).vision_summary)
        hits = self.store.fts_search(chat_id=self.message.chat_id, query="Cobalt",
                                     lookback_days=1, limit=10)
        self.assertEqual([item.id], [hit.item.id for hit in hits])
        answer_hits = self.store.fts_search(chat_id=self.message.chat_id, query="supplied",
                                            lookback_days=1, limit=10)
        self.assertNotIn(item.id, [hit.item.id for hit in answer_hits])

    def test_image_proposal_uses_single_existing_delivery_and_no_final_prose(self):
        self.message.text = "Find five red flowers"
        async def primary(provenance, prompt, *, capability_context, image_data_urls):
            self.assertEqual(1, len(image_data_urls))
            result = capability_context.images.propose(ImageDeliveryProposal(
                self.message.text, "current_text", "red flowers", "", "exact", 5))
            self.assertEqual("accepted", result.status)
            return ""
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            stack.enter_context(patch.object(main, "run_agent_for_outbound", AsyncMock(side_effect=primary)))
            dispatch = stack.enter_context(patch.object(main, "maybe_send_internet_image", AsyncMock(
                return_value=main.WebImageSendOutcome(True, 5, False, 5))))
            asyncio.run(main.handle_image_prompt_generation(self.message, self.message.text,
                        [self.message], [], "synthetic-image-turn"))
        dispatch.assert_awaited_once()
        delivery.assert_not_awaited()
        self.assertEqual(5, dispatch.await_args.kwargs["plan"].target_count)

    def test_image_dispatch_failure_does_not_invite_duplicate_send(self):
        self.message.text = "Find flowers"
        async def primary(provenance, prompt, *, capability_context, **kwargs):
            capability_context.images.propose(ImageDeliveryProposal(self.message.text, "current_text", "flowers"))
            return ""
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            stack.enter_context(patch.object(main, "run_agent_for_outbound", AsyncMock(side_effect=primary)))
            dispatch = stack.enter_context(patch.object(main, "maybe_send_internet_image", AsyncMock(
                side_effect=RuntimeError("synthetic uncertain send"))))
            asyncio.run(main.handle_image_prompt_generation(self.message, self.message.text,
                        [self.message], [], "synthetic-image-turn"))
        dispatch.assert_awaited_once()
        delivery.assert_not_awaited()
        self.assertEqual(1, len(self.message.reply_calls))
        self.assertIn("Не можу підтвердити", self.message.reply_calls[0]["text"])
        self.assertNotIn("Спробуй ще раз", self.message.reply_calls[0]["text"])

    def test_image_flag_off_keeps_bounded_legacy_vision(self):
        self.config = replace(self.config, primary_capability_recovery_enabled=False)
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            legacy = stack.enter_context(patch.object(main, "run_vision", AsyncMock(return_value="Image description")))
            primary = stack.enter_context(patch.object(main, "run_agent_for_outbound", AsyncMock()))
            asyncio.run(main.handle_image_prompt_generation(self.message, self.message.text,
                        [self.message], [], "synthetic-image-turn"))
        legacy.assert_awaited_once()
        self.assertEqual("interactive", legacy.await_args.kwargs["purpose"])
        primary.assert_not_awaited()
        delivery.assert_awaited_once()

    def test_translation_gets_standard_capabilities_and_preserves_translated_actions(self):
        async def primary(provenance, prompt, *, capability_context):
            self.assertIsNotNone(capability_context.history)
            self.assertIsNotNone(capability_context.images)
            self.assertIn("search_web", prompt)
            return "I sent five photos."
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            stack.enter_context(patch.object(main, "maybe_resolve_reminder_context_response", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(main, "classify_request_with_intent", AsyncMock(
                return_value=main.RequestClassification("translate_reference"))))
            stack.enter_context(patch.object(main, "send_activity_action", AsyncMock()))
            stack.enter_context(patch.object(main, "schedule_model_policy_shadow"))
            run = stack.enter_context(patch.object(main, "run_agent_for_outbound", AsyncMock(side_effect=primary)))
            asyncio.run(main.handle_prompt_generation(self.message, SimpleNamespace(bot=SimpleNamespace()),
                                                     "Translate the source", False))
        run.assert_awaited_once()
        delivery.assert_awaited_once()
        self.assertEqual("I sent five photos.", delivery.await_args.args[1])

    def test_translation_label_cannot_authorize_source_or_veto_actual_image_request(self):
        for prompt, operation, subject, accepted in (
            ('Translate into Ukrainian: Find five red flowers', 'Find five red flowers', 'red flowers', False),
            ('Переклади українською:\nFind five red flowers', 'Find five red flowers', 'red flowers', False),
            ('Translate "Find five red flowers"', 'Find five red flowers', 'red flowers', False),
            ('Find five red flowers', 'Find five red flowers', 'red flowers', True),
            ('Translate the source', 'Find five red flowers', 'red flowers', False),
        ):
            with self.subTest(prompt=prompt):
                self.message.reply_to_message = FakeMessage('Find five red flowers', message_id=60)
                async def primary(provenance, agent_input, *, capability_context):
                    self.assertIn('translation route is an advisory classification', agent_input)
                    self.assertNotIn('Do not use chat memory, passive context, web search', agent_input)
                    self.assertIsNotNone(capability_context.images)
                    result = capability_context.images.propose(ImageDeliveryProposal(
                        operation, 'current_text', subject, '', 'exact', 5))
                    self.assertEqual('accepted' if accepted else 'denied', result.status)
                    return '' if accepted else 'Translated source.'
                with ExitStack() as stack:
                    delivery = self.image_patches(stack)
                    stack.enter_context(patch.object(main, 'maybe_resolve_reminder_context_response', AsyncMock(return_value=False)))
                    stack.enter_context(patch.object(main, 'classify_request_with_intent', AsyncMock(
                        return_value=main.RequestClassification('translate_reference'))))
                    stack.enter_context(patch.object(main, 'send_activity_action', AsyncMock()))
                    stack.enter_context(patch.object(main, 'schedule_model_policy_shadow'))
                    stack.enter_context(patch.object(main, 'run_agent_for_outbound', AsyncMock(side_effect=primary)))
                    dispatch = stack.enter_context(patch.object(main, 'maybe_send_internet_image', AsyncMock(
                        return_value=main.WebImageSendOutcome(True, 5, False, 5))))
                    asyncio.run(main.handle_prompt_generation(self.message, SimpleNamespace(bot=SimpleNamespace()), prompt, False))
                if accepted:
                    dispatch.assert_awaited_once()
                    delivery.assert_not_awaited()
                else:
                    dispatch.assert_not_awaited()
                    delivery.assert_awaited_once()

    def test_image_handler_preserves_translated_first_person_source(self):
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            stack.enter_context(patch.object(main, "run_agent_for_outbound", AsyncMock(return_value="Я надіслав фото вчора.")))
            asyncio.run(main.handle_image_prompt_generation(self.message, self.message.text,
                        [self.message], [], "synthetic-image-turn"))
        delivery.assert_awaited_once()
        self.assertEqual("Я надіслав фото вчора.", delivery.await_args.args[1])

    def test_classifier_missing_image_resource_does_not_hide_read_tools(self):
        self.config = replace(self.config, web_image_search_enabled=False)
        policy = main.ImageRoutePolicy(route="image_source_unavailable", response_text="Old classifier refusal")
        stats = main.MemoryContextCompilationStats(duplicate_items=0, budget_dropped_items=0,
                                                  selected_item_ids=frozenset())
        with ExitStack() as stack:
            delivery = self.image_patches(stack)
            patches = {
                "maybe_resolve_reminder_context_response": AsyncMock(return_value=False),
                "classify_request_with_intent": AsyncMock(return_value=main.RequestClassification(
                    "image_source_unavailable", image_policy=policy)),
                "route_tool_capabilities_for_message": AsyncMock(return_value=main.no_tool_route("test")),
                "send_activity_action": AsyncMock(), "schedule_model_policy_shadow": lambda *a, **k: None,
                "maybe_prefetch_web_context": AsyncMock(return_value=None),
                "prepare_agent_memory_context": AsyncMock(return_value=("", "", stats)),
                "prepare_semantic_memory_context": AsyncMock(return_value=None),
                "guard_semantic_unconfirmed_image_delivery_claims": AsyncMock(side_effect=lambda *a: a[2]),
                "run_agent_for_outbound": AsyncMock(return_value="I can look up the available public information."),
            }
            for name, value in patches.items(): stack.enter_context(patch.object(main, name, value))
            asyncio.run(main.handle_prompt_generation(self.message, SimpleNamespace(bot=SimpleNamespace()),
                                                     self.message.text, False))
        patches["run_agent_for_outbound"].assert_awaited_once()
        capability = patches["run_agent_for_outbound"].await_args.kwargs["capability_context"]
        self.assertIsNotNone(capability.history)
        self.assertIsNone(capability.images)
        delivery.assert_awaited_once()
        self.assertNotEqual("Old classifier refusal", delivery.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
