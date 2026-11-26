"""Photo GEOINT admission, provider selection and Telegram delivery."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from chat_model_settings import ChatModelSettingsStore
from landmark_intent import landmark_request_language


class LandmarkPhotoRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(TemporaryDirectory())
        self.store = ChatModelSettingsStore(Path(temporary) / "settings.sqlite3")
        self.photo = FakeMessage(message_id=10)
        self.photo.photo = [SimpleNamespace(file_id="test-photo", file_unique_id="test-photo", file_size=100)]
        self.reply = FakeMessage(text="Where was this photo taken? Check the visible clues", message_id=11)
        self.reply.reply_to_message = self.photo
        self.context = SimpleNamespace(bot=self.reply.bot)
        self.answer = "**A test landmark**. https://en.wikipedia.org/?curid=123"
        self.analyze = AsyncMock(return_value=self.answer)
        self.extract = AsyncMock(return_value=["data:image/jpeg;base64,test"])
        self.model = Mock(side_effect=lambda model: model)
        for name, value in {
            "CONFIG": replace(main.CONFIG, image_analysis_enabled=True, image_intent_routing_mode="off",
                              chat_inflight_guard_enabled=False, telegram_turn_assembly_enabled=False,
                              primary_capability_recovery_enabled=False),
            "CHAT_MODEL_SETTINGS": self.store, "MEMORY": None, "SYSTEM_LOG": None, "MODEL_TELEMETRY": None,
            "BOT_USERNAME": "test_bot", "BOT_ID": 123456, "should_allow_chat": Mock(return_value=True),
            "cooldown_left": Mock(return_value=0), "mark_cooldown": Mock(), "remember_observed_message": Mock(),
            "analyze_public_landmark": self.analyze, "extract_image_data_urls": self.extract,
            "configured_agents_model": self.model, "record_chat_answer": Mock(),
            "activity_presence_for_message": Mock(return_value=SimpleNamespace(start=AsyncMock(), stop=AsyncMock())),
            "remember_message_persistently": AsyncMock(), "remember_self_complaint_signal": Mock(),
            "pending_requests": {}, "send_activity_action": AsyncMock(), "maybe_resolve_reminder_context_response": AsyncMock(return_value=False),
        }.items():
            self.stack.enter_context(patch.object(main, name, value))

    def test_authored_question_forms_and_negative_controls(self):
        for question in ("GEOINT", "Що це за публічна пам’ятка на фото? Перевір видимі підказки",
                         "Определи место по фото и проверь подсказки", self.reply.text,
                         "Что это за место на фото?", "What landmark is this?"):
            with self.subTest(question=question):
                self.assertEqual("uk", landmark_request_language(question))
        self.assertEqual("en", landmark_request_language("What landmark is this? In English"))
        for other in ("Не определяй место по фото", "What does GEOINT mean?", "Find this person's house",
                      "Describe the photo", "Translate 'where was this photo taken?'", "geoint: find a person",
                      "Where was this photo taken? Also identify the people"):
            self.assertIsNone(landmark_request_language(other))
        self.photo.forward_origin = object()
        self.photo.caption = "@test_bot GEOINT"
        self.assertIsNone(main.photo_landmark_language(self.photo, "GEOINT"))
        self.assertEqual("uk", main.photo_landmark_language(self.reply, "@test_bot GEOINT"))

    async def test_attach_and_reply_reach_same_photo_with_frozen_profile(self):
        self.store.apply(self.photo.chat_id, "terra", "none", 900)
        await main.handle_image_prompt(self.photo, "Определи место по фото и проверь подсказки")
        await main.handle_prompt_generation(self.reply, self.context, self.reply.text, False, True)
        self.assertEqual([self.photo, self.photo], [call.args[0] for call in self.extract.await_args_list])
        self.assertEqual(2, self.analyze.await_count)
        for call in self.analyze.await_args_list:
            self.assertEqual("gpt-6.1-terra", call.kwargs["model"])
            self.assertEqual("none", call.kwargs["effort"])
            self.assertIsInstance(call.kwargs["hooks"], main.AiganRunHooks)
        for message in (self.photo, self.reply):
            self.assertEqual(self.answer, message.last_reply)
            self.assertNotIn("<b>", message.last_reply)

    async def test_later_photo_must_be_an_admitted_part_of_this_chat(self):
        anchor = FakeMessage(message_id=9)
        await main.handle_image_prompt_generation(anchor, "GEOINT", [self.photo], (), "reply")
        self.extract.assert_not_awaited()
        self.assertIn("Надішли", anchor.last_reply)

        await main.handle_image_prompt(anchor, "GEOINT", image_messages=[self.photo], assembled_turn=True)
        self.extract.assert_awaited_once_with(self.photo)
        self.assertEqual(self.answer, anchor.last_reply)

        other = FakeMessage(chat_id=-2002, message_id=10)
        other.photo = self.photo.photo
        await main.handle_image_prompt(anchor, "GEOINT", image_messages=[other], assembled_turn=True)
        self.assertEqual(1, self.extract.await_count)
        self.assertIn("Надішли", anchor.last_reply)

    async def test_group_ingress_requires_an_authored_invocation(self):
        self.photo.caption = "Где сделано это фото?"
        update = SimpleNamespace(effective_message=self.photo)
        with patch.object(main, "handle_pending_or_observe", AsyncMock()) as observe:
            await main.text_message(update, self.context)
        observe.assert_awaited_once()
        self.analyze.assert_not_awaited()
        self.assertFalse(self.photo.reply_calls)

        self.photo.caption = f"{main.CONFIG.bot_trigger} {self.photo.caption}"
        await main.text_message(update, self.context)
        self.analyze.assert_awaited_once()
        self.assertEqual(self.answer, self.photo.last_reply)

    async def test_two_photos_disabled_web_and_provider_failure(self):
        await main.handle_image_prompt(self.photo, "GEOINT", image_messages=[self.photo, self.reply])
        self.assertIn("одне фото", self.photo.last_reply)
        with patch.object(main, "tool_allowed", return_value=False):
            await main.handle_image_prompt(self.photo, "GEOINT")
        self.assertIn("вимкнено", self.photo.last_reply)
        self.extract.assert_not_awaited()
        self.analyze.assert_not_awaited()
        self.analyze.side_effect = RuntimeError("test")
        await main.handle_image_prompt(self.photo, "GEOINT in English")
        self.assertIn("couldn't verify", self.photo.last_reply)
        self.assertNotIn("http", self.photo.last_reply)

    async def test_checked_after_web_revocation_and_recovery_cannot_expand_the_route(self):
        with patch.object(main, "tool_allowed", side_effect=[True, False]), patch.object(
            main, "primary_capabilities_for_message", side_effect=AssertionError("unexpected recovery")
        ):
            await main.handle_image_prompt(self.photo, "GEOINT")
        self.analyze.assert_awaited_once()
        self.assertIn("вимкнено", self.photo.last_reply)
        self.assertNotIn("http", self.photo.last_reply)

    async def test_enforce_still_runs_image_authorization(self):
        with patch.object(main, "CONFIG", replace(main.CONFIG, image_intent_routing_mode="enforce")), patch.object(
            main, "evaluate_image_intent", AsyncMock(side_effect=RuntimeError("test policy reached"))
        ) as policy:
            with self.assertRaisesRegex(RuntimeError, "test policy reached"):
                await main.classify_request_with_intent(self.reply, self.reply.text)
        policy.assert_awaited_once()
        self.analyze.assert_not_awaited()
