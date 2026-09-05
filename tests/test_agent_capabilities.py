"""Real SDK loop and host routing regressions, with no provider or Telegram transport."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from agents import Agent, Runner, RunConfig, Model, ModelResponse, Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from agent_capabilities import PrimaryCapabilities
from chat_history import ChatHistorySession
from image_capability import ImageCapabilityContext, ImageCapabilitySession, ImageDeliveryProposal
from memory import MemoryStore


class ScriptedModel(Model):
    def __init__(self, steps):
        self.steps = list(steps)
        self.inputs = []

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs):
        self.inputs.append(input)
        item = self.steps.pop(0)
        if isinstance(item, tuple):
            name, arguments = item
            output = ResponseFunctionToolCall(type="function_call", name=name,
                arguments=json.dumps(arguments), call_id=f"call{len(self.inputs)}", id=f"fc{len(self.inputs)}")
        else:
            output = ResponseOutputMessage(type="message", id="final", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", text=item, annotations=[])])
        return ModelResponse(output=[output], usage=Usage(requests=1, input_tokens=10, output_tokens=5),
                             response_id=f"response{len(self.inputs)}")

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("Streaming is not used")
        yield


class AgentCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.now = datetime.now(timezone.utc)
        self.config = replace(main.CONFIG, primary_capability_recovery_enabled=True, web_image_search_enabled=True)

    def save(self, message_id, text, **kwargs):
        values = dict(chat_id=-1001, message_id=message_id, text=text, user_id=1,
                      sender_label="Synthetic participant", created_at=self.now - timedelta(minutes=2))
        values.update(kwargs)
        return self.store.save_message(**values)

    def album(self, trigger, first, prompt, reply_id=None, count=5):
        original = self.save(trigger, prompt, reply_to_message_id=reply_id)
        for i in range(count):
            output = self.save(first+i, "Public image result", is_bot=True, attachment_type="web_image",
                               reply_to_message_id=trigger)
            self.store.record_provenance_output(run_id=f"{trigger:032x}", chat_id=-1001,
                trigger_message_id=trigger, input_memory_id=original, route="internet_image_send",
                started_at=self.now - timedelta(minutes=2), output_memory_id=output,
                output_ordinal=i, output_part_count=count)

    def message(self, text="and now yellow ones", message_id=40, reply_id=20):
        message = FakeMessage(text, message_id=message_id)
        message.date = self.now
        message.from_user.id = 2  # Another allowed participant continues the public operation.
        if reply_id is not None:
            message.reply_to_message = FakeMessage("Public image result", message_id=reply_id)
        self.save(message_id, text, user_id=2, created_at=self.now, reply_to_message_id=reply_id)
        return message

    def test_sdk_reads_history_then_requests_once_and_stops_before_final_prose(self):
        self.album(10, 20, "Find five red flowers")
        message = self.message()
        with patch.object(main, "CONFIG", self.config), patch.object(main, "MEMORY", self.store):
            capabilities = main.primary_capabilities_for_message(message, message.text)
        model = ScriptedModel([
            ("read_chat_history", {"mode": "search", "query": "flowers"}),
            ("request_image_delivery", {"operation_text": message.text,
                "grounding": "reply_public_delivery", "subject_text": "flowers", "modifier_text": "yellow"}),
        ])
        agent = Agent(name="test", model=model, tools=capabilities.tools(),
                      tool_use_behavior=capabilities.tool_use_behavior)
        result = asyncio.run(Runner.run(agent, message.text, max_turns=6, run_config=RunConfig(tracing_disabled=True)))
        self.assertEqual("", result.final_output)
        self.assertEqual(2, result.context_wrapper.usage.requests)
        self.assertTrue(any(item.get("type") == "function_call_output" for item in model.inputs[1]))
        plan = capabilities.images.claim_plan()
        self.assertEqual("flowers yellow", plan.query)
        self.assertEqual(5, plan.target_count)
        self.assertIsNone(capabilities.images.claim_plan())
        self.assertEqual(1, capabilities.history.calls_used)

    def test_rejected_tool_proposal_returns_to_model_for_clarification(self):
        capabilities = PrimaryCapabilities(images=ImageCapabilitySession(ImageCapabilityContext("Do not send flowers", -1001)))
        model = ScriptedModel([("request_image_delivery", {"operation_text": "send flowers",
            "grounding": "current_text", "subject_text": "flowers"}), "No image delivery requested."])
        agent = Agent(name="test", model=model, tools=capabilities.tools(), tool_use_behavior=capabilities.tool_use_behavior)
        result = asyncio.run(Runner.run(agent, "Do not send flowers", run_config=RunConfig(tracing_disabled=True)))
        self.assertEqual("No image delivery requested.", result.final_output)
        self.assertIsNone(capabilities.images.claim_plan())

    def test_host_follows_multiple_verified_albums_and_rejects_foreign_reply(self):
        self.album(10, 20, "Find five red flowers")
        self.album(30, 31, "and now yellow ones", reply_id=20)
        message = self.message("and now white ones", message_id=40, reply_id=31)
        with patch.object(main, "MEMORY", self.store):
            evidence = main.verified_image_continuation(message)
            self.assertIn("red flowers", evidence.original_prompt)
            self.assertIn("yellow ones", evidence.original_prompt)
            self.assertEqual(5, evidence.delivered_count)
            message.reply_to_message.chat_id = -2002
            self.assertIsNone(main.verified_image_continuation(message))

    def test_feature_chat_and_bot_guards_and_missing_memory_cutoff(self):
        message = self.message(reply_id=None)
        with patch.object(main, "CONFIG", self.config), patch.object(main, "MEMORY", self.store):
            message.chat_id = -2002
            self.assertIsNone(main.primary_capabilities_for_message(message, message.text))
            message.chat_id = -1001
            message.from_user.is_bot = True
            self.assertIsNone(main.primary_capabilities_for_message(message, message.text))
            message.from_user.is_bot = False
            message.message_id = 9999
            self.assertIsNone(main.primary_capabilities_for_message(message, message.text).history)
        with patch.object(main, "CONFIG", replace(self.config, primary_capability_recovery_enabled=False)):
            self.assertIsNone(main.primary_capabilities_for_message(message, message.text))

    def run_host(self, route, message, agent, outcome=None, pipeline_error=None):
        policy = main.ImageRoutePolicy(route=route, response_text="old classifier refusal",
                                       guard_unconfirmed_delivery=True, suppress_lazy_image_summary=True)
        classification = main.RequestClassification(route, image_policy=policy)
        stats = main.MemoryContextCompilationStats(duplicate_items=0, budget_dropped_items=0, selected_item_ids=frozenset())
        patches = {
            "maybe_resolve_reminder_context_response": AsyncMock(return_value=False),
            "classify_request_with_intent": AsyncMock(return_value=classification),
            "route_tool_capabilities_for_message": AsyncMock(return_value=main.no_tool_route("test")),
            "send_activity_action": AsyncMock(), "maybe_prefetch_web_context": AsyncMock(return_value=None),
            "prepare_agent_memory_context": AsyncMock(return_value=("", "", stats)),
            "prepare_semantic_memory_context": AsyncMock(return_value=None),
            "run_agent_for_outbound": agent, "send_reply": AsyncMock(),
            "maybe_send_internet_image": AsyncMock(return_value=outcome or main.WebImageSendOutcome(True, 5, False, 5), side_effect=pipeline_error),
        }
        presence = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "CONFIG", self.config))
            stack.enter_context(patch.object(main, "MEMORY", self.store))
            stack.enter_context(patch.object(main, "activity_presence_for_message", return_value=presence))
            stack.enter_context(patch.object(main, "cooldown_left", return_value=0))
            for name, value in patches.items(): stack.enter_context(patch.object(main, name, value))
            asyncio.run(main.handle_prompt_generation(message, SimpleNamespace(bot=SimpleNamespace()),
                                                     message.text, allow_pending_wait=False))
        return patches

    def test_soft_classifier_routes_reach_primary_and_dispatch_existing_pipeline_once(self):
        self.album(10, 20, "Find five red flowers")
        message = self.message()
        async def primary(_provenance, _prompt, *, capability_context, **_kwargs):
            result = capability_context.images.propose(ImageDeliveryProposal(message.text,
                "reply_public_delivery", "flowers", "yellow", "plural_unspecified"))
            self.assertEqual("accepted", result.status)
            return ""
        for route in ("referenced_visual_unavailable", "image_intent_clarify", "image_source_unavailable"):
            with self.subTest(route=route):
                patches = self.run_host(route, message, AsyncMock(side_effect=primary))
                patches["run_agent_for_outbound"].assert_awaited_once()
                patches["maybe_send_internet_image"].assert_awaited_once()
                self.assertEqual(5, patches["maybe_send_internet_image"].call_args.kwargs["plan"].target_count)
                self.assertEqual("internet_image_send", patches["maybe_send_internet_image"].call_args.kwargs["outbound_provenance"].route)
                patches["send_reply"].assert_not_awaited()

    def test_ambiguous_delivery_does_not_retry_or_send_generic_success(self):
        message = self.message("Find flowers", reply_id=None)
        async def primary(_provenance, _prompt, *, capability_context, **_kwargs):
            capability_context.images.propose(ImageDeliveryProposal(message.text, "current_text", "flowers"))
            return ""
        patches = self.run_host("image_intent_clarify", message, AsyncMock(side_effect=primary),
                                main.WebImageSendOutcome(True, 0, True, 1))
        patches["maybe_send_internet_image"].assert_awaited_once()
        patches["send_reply"].assert_not_awaited()

    def test_post_claim_failure_does_not_invite_duplicate_retry(self):
        message = self.message("Find flowers", reply_id=None)
        async def primary(_provenance, _prompt, *, capability_context, **_kwargs):
            capability_context.images.propose(ImageDeliveryProposal(message.text, "current_text", "flowers"))
            return ""
        patches = self.run_host("image_intent_clarify", message, AsyncMock(side_effect=primary),
                                pipeline_error=RuntimeError("synthetic post-send failure"))
        patches["maybe_send_internet_image"].assert_awaited_once()
        patches["send_reply"].assert_not_awaited()
        self.assertEqual(1, len(message.reply_calls))
        self.assertNotIn("Спробуй ще раз", message.reply_calls[0]["text"])
        self.assertIn("Не можу підтвердити", message.reply_calls[0]["text"])

    def test_enabled_capabilities_do_not_admit_ordinary_group_chatter(self):
        message = self.message("A pleasant afternoon", reply_id=None)
        main.pending_requests.clear()
        context = SimpleNamespace(bot=SimpleNamespace(id=123456, username="test_bot", send_chat_action=AsyncMock()))
        with patch.object(main, "CONFIG", self.config), patch.object(main, "MEMORY", self.store), \
             patch.object(main, "BOT_USERNAME", main.BOT_USERNAME), patch.object(main, "BOT_ID", main.BOT_ID), \
             patch.object(main, "run_agent", AsyncMock()) as run, \
             patch.object(main, "maybe_auto_react", AsyncMock()), \
             patch.object(main, "primary_capabilities_for_message") as capabilities:
            asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))
        run.assert_not_awaited()
        capabilities.assert_not_called()


if __name__ == "__main__":
    unittest.main()
