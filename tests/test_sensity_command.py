"""Telegram selection, consent and transfer bounds for the Sensity command."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import sensity_command as command
from tests.support import FakeMessage, FakeUser


class SensityCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.remote = SimpleNamespace(file_size=4, download_as_bytearray=AsyncMock(return_value=bytearray(b"demo")))
        self.media = SimpleNamespace(file_size=4, get_file=AsyncMock(return_value=self.remote))
        self.source = FakeMessage()
        self.source.photo = [SimpleNamespace(get_file=AsyncMock()), self.media]
        self.message = FakeMessage()
        self.message.reply_to_message = self.source
        self.provider = self.enterContext(patch.object(command, "check_sensity_media",
                                                      new=AsyncMock(return_value="Оцінка Sensity.")))
        self.allowed = True

    def policy(self):
        return self.allowed

    async def run_command(self, *args, key="configured"):
        await command.handle_sensity_command(SimpleNamespace(message=self.message), SimpleNamespace(args=list(args)),
                                            api_key=key, is_allowed=self.policy)
        return self.message.last_reply

    async def test_explicit_send_reads_one_selected_rendition_and_returns_attributed_result(self):
        self.assertEqual(await self.run_command("send"), "Оцінка Sensity.")
        self.source.photo[0].get_file.assert_not_awaited()
        self.provider.assert_awaited_once_with(b"demo", "image/jpeg", "configured", is_allowed=self.policy)

    async def test_disclosure_disabled_and_missing_configuration_never_fetch(self):
        self.assertIn("зовнішній", await self.run_command(key=""))
        self.assertIn("/sensity send", await self.run_command("send", "link"))
        self.assertIn("ключ", await self.run_command("send", key=" "))
        self.allowed = False
        self.assertIn("вимкнено", await self.run_command("send"))
        self.media.get_file.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_only_owned_unprotected_direct_reply_in_same_chat_can_be_sent(self):
        changes = (
            (self.message, "reply_to_message", None), (self.source, "from_user", FakeUser(7)),
            (self.source, "from_user", None), (self.source, "chat_id", 19),
            (self.source, "forward_origin", object()), (self.message, "forward_origin", object()),
            (self.source, "sender_chat", object()), (self.message, "sender_chat", object()),
            (self.source, "has_protected_content", True),
        )
        for obj, field, value in changes:
            with self.subTest(field=field), patch.object(obj, field, value, create=True):
                self.assertIn("власне", await self.run_command("send"))
        self.media.get_file.assert_not_awaited()
        self.provider.assert_not_awaited()

    async def test_voice_video_and_media_document_use_their_media_type(self):
        self.source.photo = []
        for kind, raw, expected in (
            ("voice", "Audio/Ogg; codecs=opus", "audio/ogg"), ("video", "video/mp4", "video/mp4"),
            ("document", "image/png", "image/png"), ("video_note", None, "video/mp4"),
        ):
            with self.subTest(kind=kind), patch.object(self.source, kind, self.media, create=True):
                self.media.mime_type = raw
                self.assertEqual(await self.run_command("send"), "Оцінка Sensity.")
                self.assertEqual(self.provider.await_args.args[1], expected)
        self.source.document = self.media
        self.media.mime_type = "application/pdf"
        self.assertIn("формат", await self.run_command("send"))
        self.assertEqual(self.media.get_file.await_count, 4)

    async def test_checks_declared_remote_and_actual_size_before_external_transfer(self):
        self.media.file_size = command.SENSITY_MAX_BYTES + 1
        self.assertIn("розміром", await self.run_command("send"))
        self.media.get_file.assert_not_awaited()
        for remote_size in (None, 0, command.SENSITY_MAX_BYTES + 1):
            self.remote.file_size = remote_size
            self.media.file_size = None
            self.assertIn("розміром", await self.run_command("send"))
        self.remote.download_as_bytearray.assert_not_awaited()
        self.remote.file_size = 8
        self.assertIn("завантажити", await self.run_command("send"))
        self.provider.assert_not_awaited()

    async def test_revoke_during_telegram_download_or_provider_execution(self):
        async def revoke_transfer():
            self.allowed = False
            return bytearray(b"demo")

        self.remote.download_as_bytearray.side_effect = revoke_transfer
        self.assertIn("вимкнено", await self.run_command("send"))
        self.provider.assert_not_awaited()
        self.allowed = True
        self.remote.download_as_bytearray.side_effect = None

        async def revoke_result(*args, **kwargs):
            self.allowed = False
            return "Assessment"

        self.provider.side_effect = revoke_result
        self.assertIn("вимкнено", await self.run_command("send"))

    async def test_failed_telegram_download_and_provider_exception_are_sanitized(self):
        self.media.get_file.side_effect = RuntimeError("private detail")
        self.assertEqual(await self.run_command("send"), command._FILE_ERROR)
        self.provider.assert_not_awaited()
        self.media.get_file.side_effect = None
        self.provider.side_effect = RuntimeError("private detail")
        self.assertEqual(await self.run_command("send"), "Не вдалося отримати оцінку від Sensity.")

    async def test_parallel_send_has_no_second_download_and_cancellation_releases_slot(self):
        entered = asyncio.Event()

        async def wait_for_result(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.provider.side_effect = wait_for_result
        pending = asyncio.create_task(self.run_command("send"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIn("вже виконує", await self.run_command("send"))
            self.media.get_file.assert_awaited_once()
            self.provider.assert_awaited_once()
        finally:
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.provider.side_effect = None
        self.assertEqual(await self.run_command("send"), "Оцінка Sensity.")
