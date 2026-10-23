"""Actual ingress keeps sticker recognition behind existing invocation gates."""
import asyncio
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram.constants import ChatType
from tests.support import FakeMessage, VALID_JPEG, configure_test_environment
configure_test_environment()
import main
from memory import MemoryStore
from telegram_turn_assembly import TurnAssembler
from tests.test_telegram_turn_assembly import AsyncioClock, ManualClock

REAL_HANDLE_PROMPT = main.handle_prompt
REAL_HANDLE_IMAGE_PROMPT = main.handle_image_prompt


class StickerAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")
        self.addCleanup(self.store.close)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tasks = []
        def create_task(coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task
        self.context = SimpleNamespace(bot=SimpleNamespace(id=123456, username="test_bot"),
                                       application=SimpleNamespace(create_task=create_task))
        self.vision = AsyncMock(return_value="Neutral unrelated photo summary")
        self.recognition = AsyncMock(return_value={})
        self.image_handler, self.text_handler = AsyncMock(), AsyncMock()
        self.enqueue = Mock()
        for name, value in {
            "MEMORY": self.store, "REACTION_MEMORY": None, "SYSTEM_LOG": None,
            "CONFIG": replace(main.CONFIG, image_analysis_enabled=True,
                memory_eager_image_summary=True, telegram_turn_assembly_enabled=True,
                telegram_turn_assembly_seconds=0.001, followup_debounce_seconds=0),
            "BOT_ID": 123456, "BOT_USERNAME": "test_bot",
            "pending_requests": {}, "telegram_turn_assembler": TurnAssembler(),
            "telegram_turn_ingress": {}, "telegram_turn_tasks": set(),
            "telegram_turn_dispatch_locks": {}, "telegram_turn_stopping": False,
            "should_allow_chat": Mock(return_value=True),
            "remember_social_observations": Mock(), "run_reaction_ingestion_hook": AsyncMock(),
            "enqueue_memory_embedding": self.enqueue, "cleanup_memory_if_due": Mock(),
            "remember_self_complaint_signal": Mock(), "remember_observed_message": Mock(),
            "system_event": Mock(), "run_vision": self.vision,
            "run_sticker_recognition": self.recognition,
            "handle_image_prompt": self.image_handler, "handle_prompt": self.text_handler,
        }.items():
            self.stack.enter_context(patch.object(main, name, value))

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def sticker(self, message_id=10, *, chat_type=ChatType.SUPERGROUP, chat_id=-1001):
        message = FakeMessage("", message_id=message_id, chat_type=chat_type, chat_id=chat_id)
        downloaded = SimpleNamespace(file_size=len(VALID_JPEG), download_as_bytearray=AsyncMock(return_value=bytearray(VALID_JPEG)))
        message.sticker = SimpleNamespace(file_id=f"synthetic-sticker-{message_id}",
            file_unique_id=f"synthetic-asset-{message_id}", is_animated=False, is_video=False,
            emoji="🙂", file_size=len(VALID_JPEG), get_file=AsyncMock(return_value=downloaded))
        return message

    def queued_image(self, attachment_type="sticker"):
        row = self.store.save_message(chat_id=-1001, message_id=1,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            text="Earlier unrelated source", content_kind="image", attachment_type=attachment_type)
        directory = self.store.media_dir / "-1001"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{row}.jpg"
        path.write_bytes(VALID_JPEG)
        self.store.update_media(row, attachment_type=attachment_type, telegram_file_id="synthetic-queued",
            telegram_unique_id="synthetic-queued-asset", local_media_path=str(path), mime_type="image/jpeg")
        if attachment_type == "sticker":
            self.store.update_sticker_metadata(row, file_id="synthetic-queued",
                file_unique_id="synthetic-queued-asset", metadata={"format": "static", "representation": "static_original"})
        return row

    async def ingest(self, message):
        await main.text_message(SimpleNamespace(effective_message=message), self.context)
        # Await only tasks created by this synthetic ingress, not any application queue.
        if self.tasks:
            await asyncio.gather(*self.tasks)

    async def passive_case(self, assembly):
        queued = self.queued_image()
        message = self.sticker()
        with patch.object(main, "CONFIG", replace(main.CONFIG, telegram_turn_assembly_enabled=assembly)):
            await self.ingest(message)
        self.recognition.assert_not_awaited()
        self.vision.assert_not_awaited()
        self.image_handler.assert_not_awaited()
        self.text_handler.assert_not_awaited()
        message.sticker.get_file.assert_not_awaited()
        self.assertEqual([], message.reply_calls)
        source = self.store.message_by_message_id(message.chat_id, message.message_id)
        self.assertEqual("sticker", source.attachment_type)
        self.assertEqual(message.sticker.file_unique_id, source.telegram_unique_id)
        self.assertEqual("", source.local_media_path)
        self.assertEqual("", self.store.item_by_id(queued).vision_summary)
        self.enqueue.assert_called_once_with(source.id)

    async def test_passive_group_sticker_cold_cache_and_eager_memory_remain_silent(self):
        await self.passive_case(True)

    async def test_legacy_passive_group_sticker_has_same_zero_recognition_boundary(self):
        await self.passive_case(False)

    async def test_unrelated_photo_background_processing_remains_available(self):
        queued = self.queued_image("photo")
        message = self.sticker()
        await self.ingest(message)
        self.vision.assert_awaited_once()
        self.assertEqual("background", self.vision.await_args.kwargs["purpose"])
        self.assertEqual("Neutral unrelated photo summary", self.store.item_by_id(queued).vision_summary)
        self.recognition.assert_not_awaited()
        message.sticker.get_file.assert_not_awaited()
        self.image_handler.assert_not_awaited()

    async def test_explicit_request_replying_to_sticker_keeps_source_and_routes_text_turn(self):
        source = self.sticker()
        message = FakeMessage("@test_bot describe this sticker", message_id=11)
        message.reply_to_message = source
        await self.ingest(message)
        self.text_handler.assert_awaited_once()
        self.image_handler.assert_not_awaited()
        self.assertIn("describe this sticker", self.text_handler.await_args.args[2])
        self.assertEqual((source,), main.referenced_image_messages(message))
        retained = self.store.message_by_message_id(-1001, source.message_id)
        current = self.store.message_by_message_id(-1001, message.message_id)
        self.assertEqual(source.sticker.file_unique_id, retained.telegram_unique_id)
        self.assertEqual("text", current.content_kind)
        self.assertEqual("", current.telegram_unique_id)
        source.sticker.get_file.assert_not_awaited()

    async def test_sticker_reply_to_bot_is_an_admitted_visual_turn(self):
        message = self.sticker()
        parent = FakeMessage("Bot question", message_id=9)
        parent.from_user.id = 123456
        parent.from_user.is_bot = True
        message.reply_to_message = parent
        await self.ingest(message)
        self.image_handler.assert_awaited_once()
        self.assertIs(message, self.image_handler.await_args.args[0])
        self.text_handler.assert_not_awaited()
        message.sticker.get_file.assert_not_awaited()

    async def test_private_sticker_is_an_admitted_visual_turn(self):
        message = self.sticker(chat_type=ChatType.PRIVATE)
        await self.ingest(message)
        self.image_handler.assert_awaited_once()
        self.assertIs(message, self.image_handler.await_args.args[0])
        self.assertEqual(main.DEFAULT_CONTEXT_PROMPT, self.image_handler.await_args.args[1])
        self.text_handler.assert_not_awaited()

    async def test_correlated_pending_invocation_accepts_sticker_without_second_turn(self):
        request = FakeMessage("@test_bot explain the next attachment", message_id=9)
        self.assertIsNotNone(main.store_pending_request(request, "Explain this sticker", "context"))
        message = self.sticker()
        await self.ingest(message)
        self.image_handler.assert_awaited_once()
        self.assertIs(message, self.image_handler.await_args.args[0])
        self.assertEqual("Explain this sticker", self.image_handler.await_args.args[1])
        self.assertEqual({}, main.pending_requests)
        self.text_handler.assert_not_awaited()

    async def test_other_participant_sticker_cannot_claim_pending_invocation(self):
        request = FakeMessage("@test_bot explain the next attachment", message_id=9)
        self.assertIsNotNone(main.store_pending_request(request, "Explain this sticker", "context"))
        message = self.sticker()
        message.from_user.id += 1
        await self.ingest(message)
        self.assertTrue(main.has_pending_request(request))
        self.image_handler.assert_not_awaited()
        self.text_handler.assert_not_awaited()
        self.recognition.assert_not_awaited()
        message.sticker.get_file.assert_not_awaited()

    def real_generation(self, mode, *, deny=False, capabilities=True):
        """Keep ingress, classification policy, handlers, cache and loader real."""
        primary = AsyncMock(return_value="Synthetic visual answer")
        sender = AsyncMock(return_value=main.TextDeliveryResult((), False, 1, ()))
        decision = main.ImageIntentDecision(
            intent="referenced_visual_analysis", target="none", source_scope="reference",
            subject_grounding="reference_only", subject_text=None, quantity_kind="none",
            quantity_value=0, language="en", execution="requested_now", confidence=0.99,
            reason_codes=("analysis_request",))
        authorization = main.ImageOperationAuthorization(
            verdict="deny" if deny else "allow", operation="referenced_visual_analysis",
            execution="negated" if deny else "explicit_now", source="reference", destination="none",
            operation_text="do not analyze" if deny else "describe this sticker",
            subject_grounding="reference_only", subject_text=None, private_or_internal=False,
            external_source_or_destination=False, reference_dependent=True,
            meta_or_classifier_test=False, negated=deny, confidence=0.99,
            reason_codes=("negated_operation",) if deny else ("referenced_visual",))
        stats = main.MemoryContextCompilationStats(duplicate_items=0, budget_dropped_items=0,
                                                   selected_item_ids=frozenset())
        for name, value in {
            "CONFIG": replace(main.CONFIG, image_intent_routing_mode=mode,
                primary_capability_recovery_enabled=capabilities, memory_vector_enabled=False,
                chat_inflight_guard_enabled=False),
            "handle_prompt": REAL_HANDLE_PROMPT, "handle_image_prompt": REAL_HANDLE_IMAGE_PROMPT,
            "histories": defaultdict(list), "passive_contexts": defaultdict(list),
            "cooldown_left": Mock(return_value=0), "mark_cooldown": Mock(),
            "activity_presence_for_message": Mock(return_value=SimpleNamespace(start=AsyncMock(), stop=AsyncMock())),
            "send_activity_action": AsyncMock(), "send_reply": sender,
            "evaluate_image_intent": AsyncMock(return_value=decision),
            "evaluate_image_operation_authorization": AsyncMock(return_value=authorization),
            "detect_memory_recall_intent": AsyncMock(return_value=main.MemoryRecallIntent(False, reason="synthetic")),
            "prepare_memory_context": AsyncMock(return_value="(none)"),
            "prepare_agent_memory_context": AsyncMock(return_value=("(none)", "(none)", stats)),
            "prepare_semantic_memory_context": AsyncMock(return_value=None),
            "maybe_prefetch_web_context": AsyncMock(return_value=None),
            "run_agent_for_outbound": primary,
        }.items():
            self.stack.enter_context(patch.object(main, name, value))
        self.recognition.reset_mock()
        self.recognition.return_value = {"summary": "Synthetic sticker description",
            "visible_text": "Привіт", "text_status": "readable", "model": "synthetic-model"}
        return primary, sender

    async def test_actual_reply_handler_recognizes_cold_sticker_in_off_and_shadow_modes(self):
        for index, (mode, capabilities) in enumerate((
                ("off", False), ("off", True), ("shadow", False), ("shadow", True))):
            with self.subTest(mode=mode, capabilities=capabilities):
                primary, sender = self.real_generation(mode, capabilities=capabilities)
                source = self.sticker(100 + index * 2)
                message = FakeMessage("@test_bot describe this sticker", message_id=source.message_id + 1)
                message.reply_to_message = source
                await self.ingest(message)
                self.recognition.assert_awaited_once()
                source.sticker.get_file.assert_awaited_once()
                primary.assert_awaited_once()
                payload = primary.await_args.args[1]
                self.assertIn("Synthetic sticker description", payload)
                self.assertIn("Привіт", payload)
                self.assertIn("🙂", payload)
                self.assertIsNone(primary.await_args.kwargs.get("image_data_urls"))
                sender.assert_awaited_once()
                self.assertEqual("vision", sender.await_args.kwargs["route"])
                current = self.store.message_by_message_id(-1001, message.message_id)
                retained = self.store.message_by_message_id(-1001, source.message_id)
                self.assertEqual("", current.local_media_path)
                self.assertTrue(retained.local_media_path)
                self.assertEqual(source.sticker.file_unique_id, retained.telegram_unique_id)
                self.vision.assert_not_awaited()

    async def test_actual_translation_reply_reads_cold_sticker_before_primary(self):
        for index, mode in enumerate(("off", "shadow", "enforce")):
            with self.subTest(mode=mode):
                primary, sender = self.real_generation(mode)
                source = self.sticker(200 + index * 2)
                message = FakeMessage("@test_bot translate this into English", message_id=source.message_id + 1)
                message.reply_to_message = source
                await self.ingest(message)
                self.recognition.assert_awaited_once()
                source.sticker.get_file.assert_awaited_once()
                primary.assert_awaited_once()
                self.assertIn("translate this into English", primary.await_args.args[1])
                self.assertIn("Привіт", primary.await_args.args[1])
                self.assertEqual("vision", sender.await_args.kwargs["route"])

    async def test_actual_enforce_positive_analysis_still_recognizes_sticker(self):
        primary, sender = self.real_generation("enforce")
        source = self.sticker()
        message = FakeMessage("@test_bot describe this sticker", message_id=11)
        message.reply_to_message = source
        await self.ingest(message)
        self.recognition.assert_awaited_once()
        source.sticker.get_file.assert_awaited_once()
        primary.assert_awaited_once()
        self.assertIn("Synthetic sticker description", primary.await_args.args[1])
        self.assertEqual("vision", sender.await_args.kwargs["route"])

    async def test_actual_enforce_negative_operation_cannot_enter_sticker_fallback(self):
        primary, sender = self.real_generation("enforce", deny=True)
        source = self.sticker()
        message = FakeMessage("@test_bot do not analyze the sticker; acknowledge only", message_id=11)
        message.reply_to_message = source
        await self.ingest(message)
        self.recognition.assert_not_awaited()
        source.sticker.get_file.assert_not_awaited()
        self.vision.assert_not_awaited()
        self.assertTrue(primary.await_count or sender.await_count)
        for call in primary.await_args_list:
            self.assertNotIn("Synthetic sticker description", call.args[1])
            self.assertIsNone(call.kwargs.get("image_data_urls"))
        for call in sender.await_args_list:
            self.assertNotEqual("vision", call.kwargs.get("route"))

    async def test_actual_assembled_sticker_parts_both_reach_recognition_once(self):
        primary, sender = self.real_generation("off")
        clock = ManualClock()
        first = self.sticker(10, chat_type=ChatType.PRIVATE)
        second = self.sticker(11, chat_type=ChatType.PRIVATE)
        with patch.object(main, "CONFIG", replace(main.CONFIG, telegram_turn_assembly_seconds=2)), \
                patch.object(main, "time", clock), patch.object(main, "asyncio", AsyncioClock(clock)):
            await main.text_message(SimpleNamespace(effective_message=first), self.context)
            await asyncio.sleep(0)
            await main.text_message(SimpleNamespace(effective_message=second), self.context)
            await asyncio.sleep(0)
            primary.assert_not_awaited()
            clock.advance(2)
            await asyncio.gather(*self.tasks)
        self.assertEqual(2, self.recognition.await_count)
        first.sticker.get_file.assert_awaited_once()
        second.sticker.get_file.assert_awaited_once()
        primary.assert_awaited_once()
        self.assertEqual(2, primary.await_args.args[1].count("Synthetic sticker description"))
        self.assertIsNone(primary.await_args.kwargs.get("image_data_urls"))
        sender.assert_awaited_once()
        self.assertEqual(0, main.telegram_turn_assembler.outstanding_count)
        for source in (first, second):
            row = self.store.message_by_message_id(-1001, source.message_id)
            self.assertEqual(source.sticker.file_unique_id, row.telegram_unique_id)
            self.assertTrue(row.local_media_path)


if __name__ == "__main__":
    unittest.main()
