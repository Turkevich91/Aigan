"""Source media and neutral descriptions stay separate from interactive replies."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

from tests.support import FakeMessage, VALID_JPEG, configure_test_environment

configure_test_environment()
import main
from memory import MemoryStore


def photo(file_id="source-photo"):
    file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(VALID_JPEG)))
    return SimpleNamespace(file_id=file_id, file_unique_id=file_id + "-unique", get_file=AsyncMock(return_value=file))


class MediaMemoryOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3", retention_days=30)
        self.stack = ExitStack()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.stack.close)
        self.vision = AsyncMock(return_value="A separate interactive explanation.")
        self.enqueue = Mock()
        for name, value in {
            "MEMORY": self.store,
            "REACTION_MEMORY": None,
            "SYSTEM_LOG": None,
            "CONFIG": replace(main.CONFIG, memory_eager_image_summary=False),
            "should_allow_chat": Mock(return_value=True),
            "remember_social_observations": Mock(),
            "run_reaction_ingestion_hook": AsyncMock(),
            "enqueue_memory_embedding": self.enqueue,
            "cleanup_memory_if_due": Mock(),
            "run_vision": self.vision,
            "activity_presence_for_message": Mock(return_value=SimpleNamespace(start=AsyncMock(), stop=AsyncMock())),
            "cooldown_left": Mock(return_value=0),
            "mark_cooldown": Mock(),
            "remember_observed_message": Mock(),
            "record_chat_answer": Mock(),
        }.items():
            self.stack.enter_context(patch.object(main, name, value))

    def image_message(self, message_id=10, *, chat_id=-1001, caption="Source caption"):
        message = FakeMessage("", message_id=message_id, chat_id=chat_id)
        message.caption = caption
        message.photo = [photo(f"photo-{message_id}")]
        return message

    def item(self, message):
        return self.store.message_by_message_id(message.chat_id, message.message_id)

    def fts(self, query):
        return self.store.fts_search(chat_id=-1001, query=query, lookback_days=30, limit=10)

    async def test_text_reply_caches_source_without_becoming_an_image(self):
        source = self.image_message()
        reply = FakeMessage("Explain its price", message_id=11)
        reply.reply_to_message = source
        current_id = await main.remember_message_persistently(reply)
        current, original = self.item(reply), self.item(source)
        self.assertEqual(current_id, current.id)
        self.assertEqual(("text", "", "", ""), (current.content_kind, current.attachment_type, current.local_media_path, current.vision_summary))
        self.assertEqual(source.message_id, current.reply_to_message_id)
        self.assertEqual("image", original.content_kind)
        self.assertEqual("Source caption", original.text)
        self.assertEqual(VALID_JPEG, Path(original.local_media_path).read_bytes())
        self.assertEqual(f"{original.id}.jpg", Path(original.local_media_path).name)
        self.assertEqual([call(original.id), call(current_id)], self.enqueue.call_args_list)
        self.vision.assert_not_awaited()
        self.assertEqual((source,), main.referenced_image_messages(reply))
        self.assertTrue(await main.extract_image_data_urls(source))

    async def test_existing_source_is_not_resaved_or_reattributed_on_repeat_reply(self):
        source = self.image_message()
        await main.remember_message_persistently(source)
        original_id = self.item(source).id
        self.store.update_vision_summary(original_id, "Neutral amber lighthouse.")
        self.enqueue.reset_mock()
        # Telegram may carry abbreviated context; never replace stored authorship.
        source.caption = "Abbreviated reply preview"
        source.from_user.full_name = "Changed display label"
        reply = FakeMessage("What does this imply?", message_id=11)
        reply.reply_to_message = source
        await main.remember_message_persistently(reply)
        await main.remember_message_persistently(reply)
        original = self.item(source)
        self.assertEqual(original_id, original.id)
        self.assertEqual("Source caption", original.text)
        self.assertNotIn("Changed", original.sender_label)
        self.assertEqual("Neutral amber lighthouse.", original.vision_summary)
        self.assertEqual(2, len(self.store.latest(-1001, 10)))
        self.assertEqual([call(self.item(reply).id), call(self.item(reply).id)], self.enqueue.call_args_list)
        self.assertEqual([original_id], [result.item.id for result in self.fts("amber lighthouse")])

    async def test_sticker_reply_keeps_its_attachment_type(self):
        reply = FakeMessage("", message_id=11)
        reply.sticker = SimpleNamespace(emoji="🙂", set_name="fixture", file_id="own-sticker")
        reply.reply_to_message = self.image_message()
        await main.remember_message_persistently(reply)
        current = self.item(reply)
        self.assertEqual(("attachment", "sticker", ""), (current.content_kind, current.attachment_type, current.local_media_path))
        self.assertEqual("photo", self.item(reply.reply_to_message).attachment_type)

    async def test_own_photo_in_reply_remains_the_current_rows_media(self):
        original = self.image_message()
        reply = self.image_message(11, caption="Own image")
        reply.reply_to_message = original
        await main.remember_message_persistently(reply)
        current = self.item(reply)
        self.assertEqual("photo-11", current.telegram_file_id)
        self.assertEqual("Own image", current.text)
        self.assertEqual(original.message_id, current.reply_to_message_id)
        self.assertIsNone(self.item(original))
        original.photo[0].get_file.assert_not_awaited()

    async def test_image_document_reply_uses_source_row(self):
        source = FakeMessage("", message_id=10)
        document = photo("image-document")
        document.mime_type = "image/png"
        source.document = document
        reply = FakeMessage("Explain document", message_id=11)
        reply.reply_to_message = source
        await main.remember_message_persistently(reply)
        self.assertEqual("document", self.item(source).attachment_type)
        self.assertEqual(".png", Path(self.item(source).local_media_path).suffix)
        self.assertEqual("text", self.item(reply).content_kind)

    async def test_external_reply_never_assigns_source_media_to_current_row(self):
        for same_chat in (False, True):
            with self.subTest(same_chat=same_chat):
                source = self.image_message(10 + int(same_chat), chat_id=-1001 if same_chat else -2002)
                external = SimpleNamespace(photo=source.photo, chat=SimpleNamespace(id=source.chat_id), message_id=source.message_id)
                reply = FakeMessage("Explain external image", message_id=20 + int(same_chat))
                reply.external_reply = external
                await main.remember_message_persistently(reply)
                current = self.item(reply)
                self.assertEqual(("text", "", ""), (current.content_kind, current.attachment_type, current.local_media_path))
                self.assertIsNone(self.item(source))
                source.photo[0].get_file.assert_not_awaited()
                self.assertTrue(main.referenced_external_visual_available(reply))

    async def test_cross_chat_and_invalid_source_ids_are_not_persisted(self):
        cases = [(-2002, 10), (-1001, None), (-1001, 0), (-1001, True), (-1001, 20)]
        for chat_id, source_id in cases:
            with self.subTest(chat_id=chat_id, source_id=source_id):
                source = self.image_message(source_id, chat_id=chat_id)
                reply = FakeMessage("Current text", message_id=20)
                reply.reply_to_message = source
                await main.remember_message_persistently(reply)
                self.assertEqual("text", self.item(reply).content_kind)
                self.assertEqual("", self.item(reply).local_media_path)
                source.photo[0].get_file.assert_not_awaited()
        self.assertEqual(1, len(self.store.latest(-1001, 10)))

    async def test_source_download_failure_keeps_reply_persisted_as_text(self):
        source = self.image_message()
        source.photo[0].get_file.side_effect = RuntimeError("synthetic unavailable photo")
        reply = FakeMessage("Explain source", message_id=11)
        reply.reply_to_message = source
        await main.remember_message_persistently(reply)
        self.assertEqual("text", self.item(reply).content_kind)
        self.assertEqual("", self.item(reply).local_media_path)
        self.assertEqual("", self.item(source).local_media_path)

    async def test_new_bot_source_keeps_bot_authorship(self):
        source = self.image_message()
        source.from_user.is_bot = True
        reply = FakeMessage("Explain source", message_id=11)
        reply.reply_to_message = source
        await main.remember_message_persistently(reply)
        self.assertTrue(self.item(source).is_bot)
        self.assertFalse(self.item(reply).is_bot)

    async def test_background_summary_is_written_once_to_source_not_reply(self):
        source = self.image_message()
        reply = FakeMessage("Explain source", message_id=11)
        reply.reply_to_message = source
        await main.remember_message_persistently(reply)
        self.vision.return_value = "Neutral amber lighthouse."
        await main.ensure_recent_image_summaries(reply.chat_id)
        await main.ensure_recent_image_summaries(reply.chat_id)
        self.vision.assert_awaited_once()
        self.assertEqual("background", self.vision.await_args.kwargs["purpose"])
        self.assertEqual("Neutral amber lighthouse.", self.item(source).vision_summary)
        self.assertEqual("", self.item(reply).vision_summary)

    async def test_eager_summary_enqueues_new_source_once_alongside_current_reply(self):
        source = self.image_message()
        reply = FakeMessage("Explain source", message_id=11)
        reply.reply_to_message = source
        self.vision.return_value = "Neutral amber lighthouse."
        with patch.object(main, "CONFIG", replace(main.CONFIG, memory_eager_image_summary=True)):
            await main.remember_message_persistently(reply)
        self.assertEqual([call(self.item(source).id), call(self.item(reply).id)], self.enqueue.call_args_list)
        self.vision.assert_awaited_once()
        self.assertEqual("background", self.vision.await_args.kwargs["purpose"])
        self.assertEqual("Neutral amber lighthouse.", self.item(source).vision_summary)

    async def test_album_interactive_answer_is_only_stored_as_bot_output(self):
        first, second = self.image_message(), self.image_message(11)
        for source, summary in ((first, "Neutral amber lighthouse."), (second, "Neutral cobalt sailboat.")):
            await main.remember_message_persistently(source)
            self.store.update_vision_summary(self.item(source).id, summary)
        reply = FakeMessage("Compare the economic impact", message_id=12)
        reply.reply_to_message = first
        await main.remember_message_persistently(reply)
        self.vision.return_value = "Economic consequence markerzeta."
        await main.handle_image_prompt_generation(reply, reply.text, (first, second), (), "synthetic-album")
        self.vision.assert_awaited_once()
        self.assertEqual(2, len(self.vision.await_args.args[1]))
        self.assertEqual("Neutral amber lighthouse.", self.item(first).vision_summary)
        self.assertEqual("Neutral cobalt sailboat.", self.item(second).vision_summary)
        self.assertEqual("", self.item(reply).vision_summary)
        self.assertEqual(1, len(reply.reply_calls))
        stored_outputs = [item for item in self.store.latest(-1001, 10) if item.is_bot]
        self.assertEqual([self.vision.return_value], [item.text for item in stored_outputs])
        self.assertEqual([self.item(first).id], [result.item.id for result in self.fts("amber lighthouse")])
        self.assertEqual([], self.fts("markerzeta"))


if __name__ == "__main__":
    unittest.main()
