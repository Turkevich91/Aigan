"""Request snapshot, provider propagation and control-ingress regressions.

Owner: Aigan application orchestration.
"""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from chat_model_settings import ChatModelSettingsStore
from chat_model_ui import ChatModelUI, PANEL_TITLE
from memory import MemoryStore
from tests.test_standard_tools import NoTransportMCP


class ChatModelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1000.0]
        self.store = ChatModelSettingsStore(Path(self.tmp.name) / "settings.sqlite3", clock=lambda: self.now[0])
        self.config = replace(main.CONFIG, allowed_chat_ids=frozenset({-1001, -2002}),
                              openai_model="gpt-5.6-sol", model_reasoning_effort="low",
                              vision_interactive_model="gpt-6-astra", vision_interactive_reasoning_effort="medium",
                              vision_background_model="gpt-5.4-mini", vision_background_reasoning_effort="low")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main, "CONFIG", self.config))
        self.stack.enter_context(patch.object(main, "CHAT_MODEL_SETTINGS", self.store))
        self.stack.enter_context(patch.object(main, "MODEL_TELEMETRY", None))
        self.stack.enter_context(patch.object(main, "SYSTEM_LOG", None))
        self.token = main.ACTIVE_CHAT_MODEL.set(None)
        self.addCleanup(main.ACTIVE_CHAT_MODEL.reset, self.token)

    async def test_spark_paths_fail_closed_and_block_visual_inputs(self):
        self.store.apply(-1001, "qwen", "auto", 900)
        with main.chat_model_scope(-1001), patch.object(main, "SPARK_ANSWER", None), \
                patch.object(main, "OpenAI") as cloud, patch.object(main, "AsyncOpenAI") as async_cloud:
            with self.assertRaisesRegex(RuntimeError, "spark_answer_unconfigured"):
                main.run_plain_model_sync("synthetic")
            with self.assertRaisesRegex(RuntimeError, "spark_answer_unconfigured"):
                main.build_agents_run_config(model_name="spark-qwen")
            with self.assertRaisesRegex(ValueError, "spark_visual_input_unsupported"):
                main.primary_answer_settings(images=True)
            with patch.object(main, "prepare_web_image_review", new_callable=AsyncMock) as review, \
                    patch.object(main, "send_reply", new_callable=AsyncMock) as notice:
                plan = main.ImageDeliveryPlan(query="synthetic", target_count=1, requested_count=1, quantity_kind="singular")
                await main.maybe_send_internet_image(SimpleNamespace(), "synthetic", plan=plan)
                review.assert_not_awaited()
                notice.assert_awaited_once()
            cloud.assert_not_called()
            async_cloud.assert_not_called()

    def select(self, chat=-1001, model="astra", effort="high", duration=900):
        return self.store.apply(chat, model, effort, duration)

    async def test_generation_snapshot_survives_change_and_expiry_with_other_chat_isolated(self):
        self.select(duration=60)
        entered = asyncio.Event()
        proceed = asyncio.Event()
        seen = []

        async def generation(message, *args, **kwargs):
            seen.append((message.chat_id, main.primary_answer_settings()))
            if message.chat_id == -1001:
                entered.set()
                await proceed.wait()
                seen.append((message.chat_id, main.primary_answer_settings()))

        with patch.object(main, "_handle_prompt_generation", side_effect=generation), patch.object(main, "randomizer_previous_request", return_value=""):
            first = asyncio.create_task(main.handle_prompt_generation(FakeMessage(), None, "Hello", False))
            await entered.wait()
            self.now[0] += 120
            self.select(model="terra", effort="none")
            await main.handle_prompt_generation(FakeMessage(chat_id=-2002), None, "Hello", False)
            proceed.set()
            await first
        self.assertEqual([(-1001, ("gpt-6-astra", "high")),
                          (-2002, ("gpt-5.6-sol", "low")),
                          (-1001, ("gpt-6-astra", "high"))], seen)
        with main.chat_model_scope(-1001):
            self.assertEqual(("gpt-6.1-terra", "none"), main.primary_answer_settings())
        self.assertIsNone(main.ACTIVE_CHAT_MODEL.get())

    async def test_image_generation_snapshot_wraps_awaited_work_and_clears_on_failure(self):
        self.select()
        async def failed(*args, **kwargs):
            self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings(images=True))
            self.store.reset(-1001)
            await asyncio.sleep(0)
            self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings(images=True))
            raise RuntimeError("synthetic failure")
        with patch.object(main, "_handle_image_prompt_generation", side_effect=failed), patch.object(main, "randomizer_previous_request", return_value=""):
            with self.assertRaises(RuntimeError):
                await main.handle_image_prompt_generation(FakeMessage(), "Look", (), (), "key")
        self.assertIsNone(main.ACTIVE_CHAT_MODEL.get())

    async def test_to_thread_inherits_frozen_profile(self):
        self.select(model="terra", effort="none")
        with main.chat_model_scope(-1001):
            with patch.object(main, "run_plain_model_sync", side_effect=lambda *a, **k: main.primary_answer_settings()):
                self.assertEqual(("gpt-6.1-terra", "none"), await main.run_plain_model("Synthetic"))

    async def test_background_task_does_not_inherit_request_selection(self):
        self.select()
        async def read():
            return main.ACTIVE_CHAT_MODEL.get()
        with main.chat_model_scope(-1001):
            task = main.schedule_background_task(SimpleNamespace(), read())
            self.assertIsNone(await task)
            self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings())

    async def test_nested_recovery_keeps_profile_and_scheduled_chat_resolves_fresh(self):
        self.select()
        with main.chat_model_scope(-1001):
            self.store.reset(-1001)
            with main.chat_model_scope(-1001):
                self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings())
            async def scheduled(*args, **kwargs):
                return main.primary_answer_settings()
            with patch.object(main, "_run_reminder_model", side_effect=scheduled):
                self.assertEqual(("gpt-5.6-sol", "low"), await main.run_reminder_model("Synthetic", chat_id=-1001))
            self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings())

    async def test_manual_selection_skips_shadow_experiment(self):
        self.select()
        with patch.object(main, "CONFIG", replace(self.config, model_routing_mode="shadow")), main.chat_model_scope(-1001), patch.object(main, "run_model_policy_router", new_callable=AsyncMock) as router:
            result = main.schedule_model_policy_shadow(FakeMessage(), None, "Synthetic", route="normal",
                outbound_provenance=SimpleNamespace(run_id="a" * 32), tool_route_decision=None)
            self.assertFalse(result)
            router.assert_not_awaited()

    async def test_agent_text_and_image_use_same_model_settings_runconfig_and_hooks(self):
        self.select(model="terra", effort="none")
        for images in (None, ["data:image/png;base64,synthetic"]):
            with self.subTest(images=bool(images)), main.chat_model_scope(-1001), patch.object(main, "MCPServerStdio", NoTransportMCP), patch.object(main, "configured_agents_model", side_effect=lambda model_name=None: SimpleNamespace(model=model_name or self.config.openai_model)), patch.object(main.Runner, "run", new_callable=AsyncMock, return_value=SimpleNamespace(final_output="Synthetic answer", new_items=[])) as runner:
                self.assertEqual("Synthetic answer", await main.run_agent("Synthetic request", image_data_urls=images))
                agent = runner.call_args.args[0]
                kwargs = runner.call_args.kwargs
                self.assertEqual("gpt-6.1-terra", agent.model)
                self.assertEqual("none", agent.model_settings.reasoning.effort)
                self.assertEqual("gpt-6.1-terra", kwargs["run_config"].model.model)
                self.assertEqual("gpt-6.1-terra", kwargs["hooks"].intended_model)
                self.assertEqual("none", kwargs["hooks"].reasoning_effort)

    async def test_default_image_profile_and_auxiliary_background_vision_preserved(self):
        with main.chat_model_scope(-1001):
            self.assertEqual(("gpt-5.6-sol", "low"), main.primary_answer_settings())
            self.assertEqual(("gpt-6-astra", "medium"), main.vision_runtime_settings("interactive"))
        self.select(model="terra", effort="none")
        with main.chat_model_scope(-1001):
            self.assertEqual(("gpt-6.1-terra", "none"), main.vision_runtime_settings("interactive"))
            self.assertEqual(("gpt-5.4-mini", "low"), main.vision_runtime_settings("background"))
            self.assertEqual(self.config.memory_embedding_model, main.CONFIG.memory_embedding_model)
            self.assertEqual(self.config.tool_router_model, main.CONFIG.tool_router_model)

    async def test_plain_provider_and_telemetry_receive_selection(self):
        self.select()
        response = SimpleNamespace(output_text="Synthetic answer", model="gpt-6-astra", status="completed", reasoning=SimpleNamespace(effort="high"), usage=None)
        client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))
        with main.chat_model_scope(-1001), patch.object(main, "OpenAI", return_value=client), patch.object(main, "begin_model_stage", return_value=None) as begin, patch.object(main, "finish_model_stage"):
            self.assertEqual("Synthetic answer", main.run_plain_model_sync("Synthetic"))
            self.assertEqual("gpt-6-astra", client.responses.create.call_args.kwargs["model"])
            self.assertEqual({"effort": "high"}, client.responses.create.call_args.kwargs["reasoning"])
            self.assertEqual("gpt-6-astra", begin.call_args.kwargs["intended_model"])
            self.assertEqual("high", begin.call_args.kwargs["reasoning_effort"])

    async def test_character_direct_runner_uses_selection(self):
        self.select()
        session = SimpleNamespace(initial_prompt=lambda: "Synthetic", validate=lambda value: value, tools=lambda: [])
        with main.chat_model_scope(-1001), patch.object(main, "configured_agents_model", side_effect=lambda name=None: SimpleNamespace(model=name)), patch.object(main.Runner, "run", new_callable=AsyncMock, return_value=SimpleNamespace(final_output="Synthetic")) as runner:
            self.assertEqual("Synthetic", await main.run_character_evidence(session))
            self.assertEqual("gpt-6-astra", runner.call_args.args[0].model)
            self.assertEqual("high", runner.call_args.args[0].model_settings.reasoning.effort)
            self.assertEqual("gpt-6-astra", runner.call_args.kwargs["run_config"].model.model)

    async def test_character_command_entry_freezes_profile(self):
        self.select()
        async def handler(*args):
            self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings())
        with patch.object(main, "_handle_character_command", side_effect=handler):
            await main.handle_character_command(FakeMessage(), None, "")
        self.assertIsNone(main.ACTIVE_CHAT_MODEL.get())

    async def test_diagnostic_selfcheck_ignores_chat_override_and_restores_scope(self):
        self.select()
        with main.chat_model_scope(-1001), patch.object(main, "run_plain_model_sync", side_effect=lambda *a, **k: main.primary_answer_settings()):
            self.assertEqual(("gpt-5.6-sol", "low"), await main.run_plain_model_for_route("Synthetic", route_bucket="selfcheck", task_class_bucket="selfcheck"))
            self.assertEqual(("gpt-6-astra", "high"), main.primary_answer_settings())

    async def test_unavailable_store_never_resolves_to_global_default(self):
        self.select(model="luna", effort="none")
        for unavailable in (None, SimpleNamespace(resolve=Mock(side_effect=OSError("synthetic")))):
            with self.subTest(startup=unavailable is None), patch.object(main, "CHAT_MODEL_SETTINGS", unavailable):
                with self.assertRaises(main.ChatModelSettingsUnavailable):
                    with main.chat_model_scope(-1001):
                        self.fail("Unavailable policy must never admit inference")
                self.assertIsNone(main.ACTIVE_CHAT_MODEL.get())
                self.assertEqual("gpt-5.6-sol", main.CONFIG.openai_model)

    async def test_unavailable_store_stops_explicit_generation_before_routing(self):
        self.select(model="luna", effort="none")
        for unavailable in (None, SimpleNamespace(resolve=Mock(side_effect=OSError("synthetic")))):
            for kind in ("text", "image", "character"):
                with self.subTest(startup=unavailable is None, kind=kind), ExitStack() as stack:
                    stack.enter_context(patch.object(main, "CHAT_MODEL_SETTINGS", unavailable))
                    stack.enter_context(patch.object(main, "randomizer_previous_request", return_value=""))
                    text = stack.enter_context(patch.object(main, "_handle_prompt_generation", new_callable=AsyncMock))
                    image = stack.enter_context(patch.object(main, "_handle_image_prompt_generation", new_callable=AsyncMock))
                    character = stack.enter_context(patch.object(main, "_handle_character_command", new_callable=AsyncMock))
                    retained = stack.enter_context(patch.object(main, "send_reply", new_callable=AsyncMock))
                    message = FakeMessage("Synthetic admitted request")
                    if kind == "text":
                        await main.handle_prompt_generation(message, None, message.text, False)
                    elif kind == "image":
                        await main.handle_image_prompt_generation(message, message.text, (), (), "synthetic")
                    else:
                        await main.handle_character_command(message, None, "")
                    text.assert_not_awaited(); image.assert_not_awaited(); character.assert_not_awaited()
                    retained.assert_not_awaited()
                    self.assertEqual(1, len(message.reply_calls))
                    self.assertTrue(message.last_reply.startswith(PANEL_TITLE))
                    self.assertIn("Запит не запускаю", message.last_reply)
                    self.assertNotIn(self.config.openai_model, message.last_reply)
                    self.assertIsNone(main.ACTIVE_CHAT_MODEL.get())

    async def test_unavailable_store_skips_scheduled_primary_calls(self):
        with patch.object(main, "CHAT_MODEL_SETTINGS", None), patch.object(main, "_run_proactive_model", new_callable=AsyncMock) as proactive, patch.object(main, "_run_reminder_model", new_callable=AsyncMock) as reminder, patch.object(main, "_maybe_auto_react", new_callable=AsyncMock) as auto, patch.object(main, "auto_react_due", return_value=True), patch.object(main, "record_model_settings_unavailable") as event:
            self.assertIsNone(await main.run_proactive_model("Synthetic", chat_id=-1001))
            self.assertEqual((None, "model_settings_unavailable"), await main.run_reminder_model("Synthetic", chat_id=-1001))
            message = FakeMessage("Synthetic")
            await main.maybe_auto_react(message, None)
            proactive.assert_not_awaited(); reminder.assert_not_awaited(); auto.assert_not_awaited()
            self.assertEqual(3, event.call_count)
            self.assertEqual([], message.reply_calls)

    async def test_manual_proactive_unavailable_notice_is_not_skip_or_paid_retry(self):
        message = FakeMessage("/proactive_now")
        message.from_user.id = next(iter(self.config.admin_user_ids))
        with patch.object(main, "CHAT_MODEL_SETTINGS", None), patch.object(main, "build_manual_proactive_prompt", return_value="Synthetic"), patch.object(main, "provenance_for_message", return_value=None), patch.object(main, "_run_proactive_model", new_callable=AsyncMock) as run:
            await main.proactive_now_command(SimpleNamespace(effective_message=message), None)
            run.assert_not_awaited()
            self.assertIn("Запит не запускаю", message.last_reply)
            self.assertNotEqual("SKIP", message.last_reply)

    async def test_inflight_snapshot_survives_storage_outage_but_new_request_fails(self):
        self.select(model="luna", effort="none")
        with main.chat_model_scope(-1001):
            with patch.object(main, "CHAT_MODEL_SETTINGS", None):
                with main.chat_model_scope(-1001):
                    self.assertEqual(("gpt-6-luna", "none"), main.primary_answer_settings())
                with self.assertRaises(main.ChatModelSettingsUnavailable):
                    with main.chat_model_scope(-1001, inherit=False):
                        self.fail("New admission must resolve policy")

    async def test_controls_consumed_before_assembly_persistence_or_paid_work(self):
        ui = ChatModelUI(self.store, defaults=lambda: (self.config.openai_model, self.config.model_reasoning_effort),
                         is_admin=lambda actor: True, chat_allowed=lambda *args: True)
        context = SimpleNamespace(bot=SimpleNamespace(username="SyntheticBot"))
        for kind in ("authored", "forwarded", "edited", "caption", "other_bot"):
            with self.subTest(kind=kind):
                message = FakeMessage("/select_model astra-high 15m")
                if kind == "forwarded": message.forward_origin = SimpleNamespace(type="user")
                if kind == "edited": message.edit_date = datetime.now(timezone.utc)
                if kind == "caption": message.caption, message.text = message.text, ""
                if kind == "other_bot": message.text = "/select_model@AnotherBot astra-high 15m"
                before = self.store.resolve(-1001, "default", "low").revision
                with patch.object(main, "CHAT_MODEL_UI", ui), patch.object(main, "maybe_buffer_telegram_turn", new_callable=AsyncMock) as assemble, patch.object(main, "remember_message_persistently", new_callable=AsyncMock) as persist, patch.object(main, "handle_prompt", new_callable=AsyncMock) as generate:
                    await main.text_message(SimpleNamespace(effective_message=message), context)
                    assemble.assert_not_awaited(); persist.assert_not_awaited(); generate.assert_not_awaited()
                after = self.store.resolve(-1001, "default", "low").revision
                self.assertEqual(before + (kind in {"authored", "caption"}), after)

    async def test_panel_reply_source_is_excluded_but_authored_user_message_is_retained(self):
        memory = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(memory.close)
        panel = FakeMessage(PANEL_TITLE + "\nSynthetic current profile")
        panel.from_user = SimpleNamespace(is_bot=True, id=99999)
        user = FakeMessage("My ordinary authored message", message_id=202)
        user.reply_to_message = panel
        user.quote = SimpleNamespace(text="Synthetic current profile")
        with patch.object(main, "MEMORY", memory):
            self.assertIsNone(main.save_memory_message(panel))
            self.assertIsNone(main.save_memory_message(FakeMessage("/select_model astra-low 15m")))
            self.assertEqual("", main.message_content(panel))
            self.assertEqual("(none)", main.build_reference_context(user))
            item = memory.item_by_id(main.save_memory_message(user))
            self.assertIn("My ordinary authored message", item.text)
            self.assertNotIn("Synthetic current profile", item.text + item.source_text)
            self.assertIsNone(memory.message_by_message_id(-1001, panel.message_id))

    async def test_uninvoked_auto_response_does_not_resolve_settings(self):
        with patch.object(main, "auto_react_due", return_value=False), patch.object(self.store, "resolve") as resolve:
            await main.maybe_auto_react(FakeMessage("Ordinary group observation"), None)
            resolve.assert_not_called()

    async def test_panel_reply_cannot_enter_auxiliary_authorization_or_translation(self):
        panel = FakeMessage(PANEL_TITLE + " Synthetic selected model")
        panel.from_user = SimpleNamespace(is_bot=True, id=99999)
        user = FakeMessage("Find an image of a lake")
        user.reply_to_message = panel
        user.quote = SimpleNamespace(text="Synthetic selected model")
        self.assertEqual("", main.message_text(panel))
        self.assertEqual("", main.build_image_operation_authorizer_metadata(user, user.text)["reply_text_context"])
        self.assertEqual("", main.translation_source_material(user, "Translate"))
        self.assertNotIn("Synthetic selected model", main.context_signature_for_dedupe(user))

    async def test_help_discovers_controls_only_for_existing_admin(self):
        admin = next(iter(self.config.admin_user_ids))
        for actor in (admin, admin + 1):
            message = FakeMessage("/help")
            message.from_user.id = actor
            await main.help_command(SimpleNamespace(effective_message=message), None)
            self.assertEqual(actor == admin, "/select_model reset" in message.last_reply)

    async def test_private_admin_exception_preserves_existing_policy(self):
        admin = next(iter(self.config.admin_user_ids))
        self.assertTrue(main.chat_actor_allowed(12345, "private", admin))
        self.assertFalse(main.chat_actor_allowed(12345, "supergroup", admin))
        self.assertFalse(main.chat_actor_allowed(12345, "private", admin + 1))
        self.assertTrue(main.chat_actor_allowed(-1001, "supergroup", admin + 1))
        self.assertFalse(main.chat_actor_allowed(12345, "private", None))
        private = FakeMessage(chat_type="private", chat_id=12345)
        private.from_user = None
        self.assertFalse(main.should_allow_chat(private))
        private.chat_id = -1001
        self.assertTrue(main.should_allow_chat(private))


if __name__ == "__main__":
    unittest.main()
