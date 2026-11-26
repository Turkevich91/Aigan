"""Check Maltego CSV content and delivery to the invoking Telegram topic."""
import csv
from datetime import datetime, timezone
from io import StringIO
import unittest
from unittest.mock import AsyncMock

from telegram import Bot, Chat, Message

from maltego_export import (
    MAX_CELL_CHARS, MAX_INPUT_CHARS, MAX_ROWS, build_maltego_csv, handle_maltego_export,
)


def read_csv(data):
    return list(csv.reader(StringIO(data.decode("utf-8"), newline=""), strict=True))


class MaltegoCSVTests(unittest.TestCase):
    def test_unicode_quotes_commas_blank_lines_and_order_round_trip(self):
        source = '  Музей "Кит", зал А | видає | Літопис 🌊  \r\n\nЛітопис 🌊|описує|Музей "Кит", зал А'
        self.assertEqual(read_csv(build_maltego_csv(source)), [
            ["source", "relationship", "target"],
            ['Музей "Кит", зал А', "видає", "Літопис 🌊"],
            ["Літопис 🌊", "описує", 'Музей "Кит", зал А'],
        ])

    def test_malformed_data_and_limits_fail_without_truncating_the_graph(self):
        self.assertEqual(len(read_csv(build_maltego_csv("x|r|y\n" * MAX_ROWS))), MAX_ROWS + 1)
        self.assertEqual(read_csv(build_maltego_csv("x" * MAX_CELL_CHARS + "|r|y"))[1][0], "x" * MAX_CELL_CHARS)
        for data in ("", "x|r", "x||y", "x|r|y|z", "x|r|y\x00z",
                     "x|r|y\n" * (MAX_ROWS + 1), "x" * (MAX_CELL_CHARS + 1) + "|r|y",
                     " " * MAX_INPUT_CHARS + "x|r|y"):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError):
                build_maltego_csv(data)

    def test_formula_prefixes_in_any_column_are_rejected_without_echo(self):
        for prefix in ("=", "\t＋", "-", " @", "＝", "－", "＠", "\ufeff="):
            for col in range(3):
                cells = ["s", "r", "t"]
                cells[col] = prefix + "sensitive_text"
                with self.subTest(prefix=prefix, col=col), self.assertRaises(ValueError) as caught:
                    build_maltego_csv("s|r|t\n" + "|".join(cells))
                self.assertIn("рядку 2", str(caught.exception))
                self.assertNotIn("sensitive_text", str(caught.exception))


class MaltegoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.chat = Chat(id=-1001, type=Chat.SUPERGROUP)
        self.bot = AsyncMock(spec=Bot)

    def message(self, text, reply=None):
        message = Message(
            message_id=11, date=datetime.now(timezone.utc), chat=self.chat, text=text,
            reply_to_message=reply, is_topic_message=True, message_thread_id=8,
        )
        message.set_bot(self.bot)
        return message

    async def test_attachment_contains_only_command_data_and_replies_in_the_same_topic(self):
        message = self.message("/maltego@ExampleBot\n<А>|пише|Б", self.message("Private | old | row"))
        await handle_maltego_export(message)
        self.bot.send_document.assert_awaited_once()
        sent = self.bot.send_document.await_args.kwargs
        self.assertEqual(read_csv(sent["document"].getvalue())[1:], [["<А>", "пише", "Б"]])
        self.assertEqual(sent["filename"], "maltego-relationships.csv")
        self.assertIn("Map Columns to Links", sent["caption"])
        self.assertIsNone(sent["parse_mode"])
        self.assertEqual((sent["chat_id"], sent["message_thread_id"], sent["reply_parameters"].message_id),
                         (self.chat.id, 8, message.message_id))

    async def test_empty_command_or_invalid_row_returns_only_usage_to_the_same_topic(self):
        for text in ("/maltego", "/maltego A | r | B\n<malformed>"):
            await handle_maltego_export(self.message(text, self.message("Private | old | row")))
            sent = self.bot.send_message.await_args.kwargs
            self.assertIn("/maltego", sent["text"])
            self.assertNotIn("<malformed>", sent["text"])
            self.assertEqual(sent["message_thread_id"], 8)
            self.assertIsNone(sent["parse_mode"])
        self.bot.send_document.assert_not_awaited()
