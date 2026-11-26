from __future__ import annotations

from dataclasses import replace
import html
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from chat_model_settings import ChatModelSettingsStore, MODEL_CATALOG
from chat_model_ui import (
    CALLBACK_PREFIX, PANEL_TITLE, ChatModelUI,
    is_settings_control_message, is_settings_panel_message, settings_control_for_message,
)


def user(user_id=42, *, bot=False):
    return SimpleNamespace(id=user_id, is_bot=bot)


def message(text="/model", *, user_id=42, chat_id=-1001, message_id=10, **extra):
    value = SimpleNamespace(
        text=text, caption=None, from_user=user(user_id), chat_id=chat_id,
        chat=SimpleNamespace(id=chat_id, type="supergroup"), message_id=message_id,
        sender_chat=None, edit_date=None, forward_origin=None, reply_markup=None,
        reply_text=AsyncMock(), delete=AsyncMock(),
    )
    for key, item in extra.items():
        setattr(value, key, item)
    return value


class ControlRecognitionTests(unittest.TestCase):
    def test_authored_text_caption_and_suffix(self):
        for name in ("model", "settings", "select_model"):
            for caption in (False, True):
                msg = message("" if caption else f"/{name}@test_bot astra-low 15m")
                if caption:
                    msg.caption = f"/{name}@test_bot astra-low 15m"
                result = settings_control_for_message(msg, "test_bot")
                self.assertTrue(result.eligible)
                self.assertEqual(caption, result.caption)
                self.assertEqual("astra-low 15m", result.arguments)

    def test_control_is_excluded_but_ineligible_when_untrusted_or_edited(self):
        changes = [
            {"forward_origin": object()}, {"forward_from": user(43)},
            {"is_automatic_forward": True}, {"via_bot": user(99, bot=True)},
            {"sender_chat": object()}, {"edit_date": object()},
            {"from_user": None}, {"from_user": user(99, bot=True)},
            {"text": "/model@another_bot"},
        ]
        for values in changes:
            with self.subTest(values=tuple(values)):
                msg = message(**values)
                self.assertTrue(is_settings_control_message(msg, "test_bot"))
                self.assertFalse(settings_control_for_message(msg, "test_bot").eligible)

    def test_nonleading_and_other_commands_remain_ordinary_content(self):
        for text in ("Explain /model", "/models", "/model-extra", "/select_modelx", "> /model", "/ai hello"):
            self.assertFalse(is_settings_control_message(message(text)))

    def test_panel_source_detection_requires_bot_authorship(self):
        msg = message(PANEL_TITLE)
        self.assertFalse(is_settings_panel_message(msg))
        msg.from_user = user(99, bot=True)
        self.assertTrue(is_settings_panel_message(msg))
        msg.text = "different"
        msg.reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("x", callback_data="cm:token:0:x:-")]])
        self.assertTrue(is_settings_panel_message(msg))
        msg.reply_markup = None
        self.assertFalse(is_settings_panel_message(msg))


class ChatModelUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.store = ChatModelSettingsStore(Path(self.temp.name) / "settings.sqlite3", clock=lambda: self.now[0])
        self.admins = {42, 43}
        self.allowed = {-1001, -1002}
        self.ui = ChatModelUI(
            self.store, defaults=lambda: ("gpt-6.1-terra", "low"),
            is_admin=lambda actor: actor in self.admins,
            is_owner=lambda actor: actor == 42 and actor in self.admins,
            chat_allowed=lambda chat, kind, actor: chat in self.allowed,
            clock=lambda: self.now[0], max_panels=2,
        )
        self.context = SimpleNamespace(bot=SimpleNamespace(username="test_bot", get_chat_member=AsyncMock(
            return_value=SimpleNamespace(status="member"),
        )))
        self.next_id = 100
        self.sent = []
        self.panel = None

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def profile(self, chat_id=-1001):
        return self.store.resolve(chat_id, "gpt-6.1-terra", "low")

    async def command(self, text="/model", **kwargs):
        msg = message(text, **kwargs)

        async def reply(text, **options):
            self.next_id += 1
            sent = message(html.unescape(re.sub(r"<[^>]+>", "", text)),
                           chat_id=msg.chat_id, message_id=self.next_id)
            sent.from_user = user(99, bot=True)
            sent.reply_markup = options.get("reply_markup")
            self.sent.append((text, options, sent))
            if sent.reply_markup is not None:
                self.panel = sent
            return sent

        msg.reply_text.side_effect = reply
        await self.ui.handle_command(SimpleNamespace(effective_message=msg), self.context)
        return msg

    def button(self, text):
        for row in self.panel.reply_markup.inline_keyboard:
            for button in row:
                if button.text.lstrip("✓ ") == text:
                    return button.callback_data
        self.fail(f"Missing button: {text}")

    async def click(self, label=None, *, data=None, actor=42, panel=None, edit_error=False):
        target = panel or self.panel
        query = SimpleNamespace(
            message=target, from_user=user(actor), data=data or self.button(label),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )

        async def edit(text, **options):
            if edit_error:
                raise TelegramError("synthetic_transport_failure")
            target.text = html.unescape(re.sub(r"<[^>]+>", "", text))
            target.reply_markup = options.get("reply_markup")

        query.edit_message_text.side_effect = edit
        await self.ui.handle_callback(SimpleNamespace(callback_query=query), self.context)
        return query

    async def test_compact_menu_two_taps_save_and_one_tap_undo_to_inherited(self):
        self.ui.get_catalog = AsyncMock(side_effect=AssertionError("unexpected catalog request"))
        inherited = "Довжина відповіді · Як у бота (типово)"
        await self.command("/settings", user_id=43)
        self.assertIn(inherited, self.panel.text)
        self.assertTrue(is_settings_panel_message(self.panel))
        await self.click(inherited, actor=43)
        await self.click("Докладно", actor=43)
        self.assertEqual("detailed", self.store.reply_length(-1001).value)
        self.assertIsNone(self.store.reply_length(-1002).value)
        self.assertIn("Довжина відповіді · Докладно", self.panel.text)
        undo = self.button("Повернути · Довжина відповіді — Як у бота (типово)")
        await self.click(data=undo, actor=43)
        self.assertIsNone(self.store.reply_length(-1001).value)
        duplicate = await self.click(data=undo, actor=43)
        duplicate.edit_message_text.assert_not_awaited()
        self.assertIn(inherited, self.panel.text)
        self.ui.get_catalog.assert_not_awaited()

    async def test_reset_one_field_and_stale_undo_keeps_other_admin_choice(self):
        self.store.set_reply_length(-1001, "short", role="admin", expected_revision=0)
        await self.command("/settings")
        await self.click("Довжина відповіді · Стисло")
        await self.click("Повернути типову довжину")
        current = self.store.reply_length(-1001)
        self.assertIsNone(current.value)
        undo = self.button("Повернути · Довжина відповіді — Стисло")
        self.store.set_reply_length(-1001, "normal", role="admin", expected_revision=current.revision)
        await self.click(data=undo)
        self.assertEqual("normal", self.store.reply_length(-1001).value)
        self.assertIn("Довжина відповіді · Звичайно", self.panel.text)
        self.assertFalse(any("Повернути ·" in b.text for row in self.panel.reply_markup.inline_keyboard for b in row))

    async def test_compact_subpanels_return_to_their_own_parent(self):
        await self.command("/settings")
        await self.click("Моделі медіаінструментів")
        await self.click("Назад")
        await self.click("Модель")
        await self.click("Моделі медіаінструментів")
        await self.click("Назад")
        self.assertIn("1/3", self.panel.text)
        await self.click("Назад")
        self.assertIn("Довжина відповіді", self.panel.text)

    async def test_tool_page_toggle_persists_after_model_reset_and_in_one_chat(self):
        from chat_tool_policy import CHAT_TOOLS
        web = next(t for t in CHAT_TOOLS if t.key == "web")
        await self.command("/settings")
        await self.click("Інструменти")
        await self.click("✅ " + web.label)
        self.assertIn("web", self.store.tools.resolve(-1001).disabled)
        self.assertFalse(self.store.tools.resolve(-1002).disabled)
        await self.click("Назад")
        await self.click("Модель")
        await self.choose()
        await self.click("Застосувати")
        await self.click("Скинути до типових")
        await self.click("Інструменти")
        await self.click("🚫 " + web.label)
        self.assertFalse(self.store.tools.resolve(-1001).disabled)

    async def test_local_photo_choice_rechecks_availability_and_stays_in_its_chat_and_tool(self):
        qualified = Mock(return_value=True)
        self.ui.spark_media_available = qualified
        await self.command()
        await self.click("Моделі медіаінструментів")
        await self.click("Аудіо YouTube")
        qualified.assert_not_called()
        self.assertNotIn("Spark / Qwen · локально", [b.text for row in self.panel.reply_markup.inline_keyboard for b in row])
        await self.click("Назад")
        await self.click("Фото для пам’яті")
        stale = self.button("Spark / Qwen · локально")
        qualified.return_value = False
        await self.click(data=stale)
        self.assertEqual((None, 0), self.store.media_selection(-1001, "photo"))
        qualified.return_value = True
        await self.click("Оновити")
        await self.click("Spark / Qwen · локально")
        self.assertEqual(("spark-qwen", 1), self.store.media_selection(-1001, "photo"))
        self.assertEqual((None, 0), self.store.media_selection(-2002, "photo"))
        self.assertEqual((None, 0), self.store.media_selection(-1001, "video"))
        await self.click("Назад")
        await self.click("Відео Telegram")
        self.assertIn("OpenAI", self.panel.text)
        await self.click("Spark / Qwen · локально")
        self.assertEqual(("spark-qwen", 1), self.store.media_selection(-1001, "video"))
        await self.click("Назад")
        await self.click("Відео за посиланням")
        self.assertIn("gpt-6.1-terra", self.panel.text)
        self.assertIn("вебчитання", self.panel.text)
        await self.click("Spark / Qwen · локально")
        self.assertEqual(("spark-qwen", 1), self.store.media_selection(-1001, "public_video"))

    async def test_media_menu_independent_persistent_models_and_ownership(self):
        self.ui.tool_available = lambda key: True
        await self.command()
        before = self.profile()
        await self.click("Моделі медіаінструментів")
        for label, key, model in (("Аудіо YouTube", "youtube", "gpt-4o-mini-transcribe"),
                                  ("Фото для пам’яті", "photo", "gpt-6-luna"),
                                  ("Відео Telegram", "video", "gpt-6.1-terra"),
                                  ("Відео TikTok", "tiktok", "gpt-6-luna"),
                                  ("Відео за посиланням", "public_video", "gpt-6.1-sol")):
            await self.click(label)
            wrong_actor = await self.click(model, actor=45)
            self.assertTrue(wrong_actor.answer.await_args.kwargs["show_alert"])
            self.assertEqual((None, 0), self.store.media_selection(-1001, key))
            await self.click(model)
            self.assertEqual((model, 1), self.store.media_selection(-1001, key))
            self.assertEqual((None, 0), self.store.media_selection(-2002, key))
            await self.click("Назад")
        await self.click("Аудіо YouTube")
        stale = self.button("gpt-transcribe")
        self.store.set_media_model(-1001, "youtube", "gpt-4o-transcribe", role="admin", expected_revision=1,
                                   available=("gpt-4o-transcribe",))
        self.assertTrue((await self.click(data=stale)).answer.await_args.kwargs["show_alert"])
        await self.click("Оновити")
        await self.click("Типова модель інструмента")
        self.assertIsNone(self.store.media_selection(-1001, "youtube")[0])
        self.assertEqual(before, self.profile())
        self.assertEqual("gpt-6-luna", self.store.media_selection(-1001, "photo")[0])
        self.assertEqual("gpt-6.1-terra", self.store.media_selection(-1001, "video")[0])
        self.assertEqual("gpt-6-luna", self.store.media_selection(-1001, "tiktok")[0])

    async def test_public_video_panel_shows_rollout_state_and_keeps_selection(self):
        self.ui.tool_available = lambda key: key != "public_video"
        await self.command()
        await self.click("Моделі медіаінструментів")
        await self.click("Відео за посиланням")
        self.assertIn("на сервері вимкнено", self.panel.text)
        await self.click("gpt-6-luna")
        self.assertEqual(("gpt-6-luna", 1), self.store.media_selection(-1001, "public_video"))
        self.ui.tool_available = lambda key: True
        await self.click("Назад")
        await self.click("Відео за посиланням")
        self.assertNotIn("на сервері вимкнено", self.panel.text)

    async def test_tiktok_model_can_be_saved_offline_and_tool_toggle_obeys_deployment(self):
        self.ui.tool_available = lambda key: key != "tiktok"
        await self.command()
        await self.click("Моделі медіаінструментів")
        await self.click("Відео TikTok")
        self.assertIn("gpt-6.1-terra", self.panel.text)
        self.assertIn("на сервері вимкнено", self.panel.text)
        self.assertIn("залежить від мережі TikTok", self.panel.text)
        self.assertIn("надішли його файлом у Telegram", self.panel.text)
        await self.click("gpt-6-luna")
        self.assertEqual(("gpt-6-luna", 1), self.store.media_selection(-1001, "tiktok"))
        self.assertEqual((None, 0), self.store.media_selection(-1001, "video"))
        await self.click("Назад")
        await self.click("Відео TikTok")
        self.assertIn("gpt-6-luna", self.panel.text)
        await self.click("Типова модель інструмента")
        self.assertEqual((None, 2), self.store.media_selection(-1001, "tiktok"))
        await self.click("Назад")
        await self.click("Назад")
        self.ui.tool_available = lambda key: True
        await self.click("Інструменти")
        toggle = self.button("✅ Аналіз відео TikTok")
        before = self.store.tools.resolve(-1001)
        self.ui.tool_available = lambda key: key != "tiktok"
        self.assertTrue((await self.click(data=toggle)).answer.await_args.kwargs["show_alert"])
        self.assertEqual(before, self.store.tools.resolve(-1001))
        await self.click("Оновити")
        self.assertFalse(any("TikTok" in b.text for row in self.panel.reply_markup.inline_keyboard for b in row))

    async def test_member_media_menu_filters_persistent_ceiling_and_endpoint(self):
        await self.command(user_id=55)
        await self.click("Моделі медіаінструментів", actor=55)
        await self.click("Відео Telegram", actor=55)
        labels = [b.text for row in self.panel.reply_markup.inline_keyboard for b in row]
        self.assertFalse(any("astra" in name or "transcribe" in name or "Qwen" in name for name in labels))
        await self.click("gpt-6-luna", actor=55)
        self.assertEqual("gpt-6-luna", self.store.media_selection(-1001, "video")[0])

    async def test_malformed_numeric_callback_does_not_change_tool_permissions(self):
        from chat_tool_policy import CHAT_TOOLS
        await self.command()
        await self.click("Інструменти")
        data = self.button("✅ " + CHAT_TOOLS[0].label)
        for field in (2, 4):
            parts = data.split(":")
            parts[field] = "²"
            bad = await self.click(data=":".join(parts))
            self.assertTrue(bad.answer.await_args.kwargs["show_alert"])
        self.assertFalse(self.store.tools.resolve(-1001).disabled)

    async def test_tool_buttons_refuse_other_actors_and_stale_revisions(self):
        from chat_tool_policy import CHAT_TOOLS
        web, other = CHAT_TOOLS[:2]
        await self.command()
        await self.click("Інструменти")
        action = self.button("✅ " + web.label)
        await self.click(data=action, actor=43)
        self.assertFalse(self.store.tools.resolve(-1001).disabled)
        policy = self.store.tools.resolve(-1001)
        self.store.tools.set_enabled(-1001, other.key, False, expected_revision=policy.revision)
        stale = await self.click(data=action)
        self.assertTrue(stale.answer.await_args.kwargs["show_alert"])
        self.assertEqual({other.key}, self.store.tools.resolve(-1001).disabled)
        await self.click("Оновити")
        await self.click("✅ " + web.label)
        self.assertEqual({web.key, other.key}, self.store.tools.resolve(-1001).disabled)
        await self.click("Відновити дозволи інструментів")
        self.assertFalse(self.store.tools.resolve(-1001).disabled)

    async def choose(self, model="gpt-6-astra", effort="high", duration="1 год"):
        await self.click(model)
        await self.click(effort)
        await self.click(duration)

    async def test_alias_and_shortcut_follow_discovery_but_visible_buttons_keep_their_model(self):
        astra = replace(MODEL_CATALOG[0], model="gpt-6.2-astra")
        sol = replace(MODEL_CATALOG[1], model="gpt-6.2-sol")
        current = (astra, sol, MODEL_CATALOG[-1])
        self.ui.get_catalog = AsyncMock(return_value=current)
        await self.command("/select_model sol-mid persistent", user_id=77)
        self.assertEqual(sol.model, self.profile().model)
        await self.command("/settings", user_id=77)
        await self.click("Модель", actor=77)
        await self.click("Astra/low · 15 хв", actor=77)
        await self.click("Застосувати", actor=77)
        self.assertEqual(astra.model, self.profile().model)
        await self.command()
        sol_token = self.button(sol.model)
        next_astra = replace(astra, model="gpt-6.3-astra")
        self.ui.get_catalog.return_value = (next_astra, *current)
        await self.click(data=sol_token)
        await self.click("medium")
        await self.click("Постійно")
        await self.click("Застосувати")
        self.assertEqual(sol.model, self.profile().model)
        self.assertTrue(self.button(next_astra.model))
        labels = [b.text.lstrip("✓ ") for row in self.panel.reply_markup.inline_keyboard for b in row]
        self.assertNotIn(astra.model, labels)
        await self.command(f"/select_model {astra.model} low 15m")
        self.assertEqual(astra.model, self.profile().model)

    async def test_withdrawn_profile_cannot_be_applied_and_refresh_keeps_status_and_reset(self):
        self.ui.get_catalog = AsyncMock(return_value=MODEL_CATALOG)
        await self.command()
        await self.choose()
        self.ui.get_catalog.return_value = (MODEL_CATALOG[-1],)
        denied = await self.click("Застосувати")
        self.assertTrue(denied.answer.await_args.kwargs["show_alert"])
        self.assertFalse(self.profile().overridden)
        await self.click("Оновити")
        self.assertIn("Зараз немає доступних моделей", self.panel.text)
        self.assertTrue(self.button("Скинути до типових"))
        self.assertTrue(self.button("Spark / Qwen · недоступний"))
        await self.command(user_id=77)
        labels = [b.text for row in self.panel.reply_markup.inline_keyboard for b in row]
        self.assertNotIn("Astra/low · 15 хв", labels)

    async def test_member_leaving_during_discovery_cannot_apply_or_use_command(self):
        await self.command(user_id=77)
        await self.click("Astra/low · 15 хв", actor=77)

        async def discover():
            self.context.bot.get_chat_member.return_value = SimpleNamespace(status="left")
            return MODEL_CATALOG

        self.ui.get_catalog = discover
        await self.click("Застосувати", actor=77)
        self.assertFalse(self.profile().overridden)
        self.context.bot.get_chat_member.return_value = SimpleNamespace(status="member")
        await self.command("/select_model sol-mid persistent", user_id=77)
        self.assertFalse(self.profile().overridden)

    async def test_spark_requires_configuration_and_skips_effort_step(self):
        await self.command("/select_model qwen 15m")
        self.assertFalse(self.profile().overridden)
        await self.command()
        query = await self.click("Spark / Qwen · недоступний")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.ui.spark_available = lambda: True
        await self.command()
        await self.click("Spark / Qwen · локально, безплатно")
        await self.click("Назад")
        await self.click("Spark / Qwen · локально, безплатно")
        await self.click("15 хв")
        await self.click("Застосувати")
        self.assertEqual(("spark-qwen", "auto"), (self.profile().model, self.profile().reasoning_effort))

    async def test_select_command_defaults_to_fifteen_minutes_and_deletes_after_success(self):
        msg = await self.command("/select_model astra-low")
        current = self.profile()
        self.assertEqual(("gpt-6-astra", "low", 1900.0),
                         (current.model, current.reasoning_effort, current.expires_at))
        msg.delete.assert_awaited_once()
        self.assertIn("Застосовано", self.sent[-1][0])
        self.assertFalse(msg.reply_text.await_args.kwargs["do_quote"])
        self.assertFalse(self.profile(-1002).overridden)

    async def test_persistent_and_both_reset_aliases(self):
        await self.command("/select_model terra-high persistent")
        self.assertIsNone(self.profile().expires_at)
        for reset in ("default", "reset"):
            await self.command("/select_model astra-low 15m")
            await self.command(f"/select_model {reset}")
            self.assertFalse(self.profile().overridden)

    async def test_invalid_selection_does_not_mutate_or_echo_payload(self):
        await self.command("/select_model secret-unknown-model low 15m")
        self.assertFalse(self.profile().overridden)
        self.assertNotIn("secret-unknown-model", self.sent[-1][0])

    async def test_close_deletes_panel_without_a_confirmation_or_reply_quote(self):
        msg = await self.command()
        self.assertFalse(msg.reply_text.await_args.kwargs["do_quote"])
        msg.delete.assert_awaited_once()
        token = self.button("Закрити")
        await self.click(data=token, actor=43)
        self.panel.delete.assert_not_awaited()
        query = await self.click(data=token)
        self.panel.delete.assert_awaited_once()
        query.edit_message_text.assert_not_awaited()
        query.answer.assert_awaited_once()
        stale = await self.click(data=token)
        self.assertTrue(stale.answer.await_args.kwargs["show_alert"])
        self.panel.delete.assert_awaited_once()
        self.assertEqual(1, len(self.sent))

    async def test_failed_close_can_be_retried_without_posting_a_message(self):
        await self.command()
        self.panel.delete.side_effect = [TelegramError("synthetic_failure"), True]
        query = await self.click("Закрити")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query.edit_message_text.assert_not_awaited()
        await self.click("Закрити")
        self.assertEqual(2, self.panel.delete.await_count)
        self.assertEqual(1, len(self.sent))

    async def test_panel_draft_does_not_apply_until_confirmation(self):
        await self.command()
        self.assertIn("включно з усіма темами", self.sent[-1][0])
        await self.choose()
        self.assertFalse(self.profile().overridden)
        await self.click("Застосувати")
        current = self.profile()
        self.assertEqual(("gpt-6-astra", "high", 4600.0),
                         (current.model, current.reasoning_effort, current.expires_at))
        self.assertIn("Збережено", self.panel.text)
        self.assertFalse(self.profile(-1002).overridden)

    async def test_reasoning_options_are_model_specific(self):
        await self.command()
        await self.click("gpt-6-astra")
        labels = [button.text for row in self.panel.reply_markup.inline_keyboard for button in row]
        self.assertNotIn("none", labels)
        await self.click("Назад")
        await self.click("gpt-6.1-terra")
        labels = [button.text for row in self.panel.reply_markup.inline_keyboard for button in row]
        self.assertIn("none", labels)

    async def test_panel_can_choose_until_reset_and_reset(self):
        await self.command()
        await self.choose(duration="Постійно")
        await self.click("Застосувати")
        self.assertTrue(self.profile().overridden)
        self.assertIsNone(self.profile().expires_at)
        await self.click("Скинути до типових")
        self.assertFalse(self.profile().overridden)

    async def test_disallowed_command_cannot_open_or_read_store(self):
        self.ui.get_catalog = AsyncMock(side_effect=AssertionError("must not discover"))
        with patch.object(self.store, "resolve", side_effect=AssertionError("must not read")):
            for actor in (42, 77):
                await self.command(chat_id=-999, user_id=actor)
            self.assertFalse(self.sent)

    async def test_member_persistent_choice_survives_boost_and_other_chats_are_unchanged(self):
        await self.command("/select_model luna-low persistent")
        await self.command(user_id=77)
        for item in ("gpt-6.1-sol", "medium", "Постійно", "Застосувати"):
            await self.click(item, actor=77)
        self.assertEqual(("gpt-6.1-sol", "medium", None),
                         (self.profile().model, self.profile().reasoning_effort, self.profile().expires_at))
        await self.click("Astra/low · 15 хв", actor=77)
        await self.click("Застосувати", actor=77)
        self.assertEqual(("gpt-6-astra", self.now[0] + 900), (self.profile().model, self.profile().expires_at))
        self.now[0] += 900
        self.assertEqual(("gpt-6.1-sol", "medium", None),
                         (self.profile().model, self.profile().reasoning_effort, self.profile().expires_at))
        self.assertFalse(self.profile(-1002).overridden)
        await self.command("/select_model reset", user_id=77)
        self.assertEqual("gpt-6-luna", self.profile().model)

    async def test_member_duplicate_command_reports_actual_unchanged_deadline(self):
        await self.command("/select_model astra-low 15m", user_id=77)
        first = self.sent[-1][0]
        self.now[0] += 60
        await self.command("/select_model astra-low 15m", user_id=77)
        self.assertIn("00:31:40 UTC", first)
        self.assertEqual(first, self.sent[-1][0])

    async def test_owner_ceiling_survives_member_downgrade_and_rejects_higher_effort(self):
        await self.command("/select_model astra-high persistent")
        for effort in ("medium", "low", "high"):
            await self.command(f"/select_model astra-{effort} persistent", user_id=77)
            self.assertEqual((effort, None), (self.profile().reasoning_effort, self.profile().expires_at))
        revision = self.profile().revision
        await self.command("/select_model astra-xhigh 1m", user_id=77)
        self.assertEqual(revision, self.profile().revision)

    async def test_member_cannot_view_toggle_or_reset_tools_through_callbacks(self):
        disabled = self.store.tools.set_enabled(-1001, "web", False, expected_revision=0)
        await self.command("/settings", user_id=77)
        buttons = [b for row in self.panel.reply_markup.inline_keyboard for b in row]
        self.assertNotIn("Інструменти", [b.text for b in buttons])
        self.assertFalse(any("Довжина відповіді" in b.text for b in buttons))
        self.assertIn("Довжина відповіді", self.panel.text)
        prefix = self.button("Модель").rsplit(":", 2)[0]
        for ending in ("t:-", "v:0", "z:-", "l:-", "j:-", "y:-", "n:0", "r:-"):
            query = await self.click(data=f"{prefix}:{ending}", actor=77)
            self.assertTrue(query.answer.await_args.kwargs["show_alert"])
            query.edit_message_text.assert_not_awaited()
        await self.click("Модель", actor=77)
        await self.click("Повернути базовий профіль", actor=77)
        self.assertIsNone(self.store.reply_length(-1001).value)
        self.assertEqual(disabled, self.store.tools.resolve(-1001))

    async def test_member_cannot_forge_reasoning_or_duration_or_promote_timed_profile(self):
        await self.command(user_id=77)
        await self.click("gpt-6-astra", actor=77)
        low = self.button("low")
        self.assertFalse(any(b.text == "medium" for row in self.panel.reply_markup.inline_keyboard for b in row))
        rejected = await self.click(data=low.rsplit(":", 1)[0] + ":1", actor=77)
        rejected.edit_message_text.assert_not_awaited()
        await self.click(data=low, actor=77)
        duration = self.button("15 хв")
        for forged in (1, 2, 3):
            rejected = await self.click(data=duration.rsplit(":", 1)[0] + f":{forged}", actor=77)
            rejected.edit_message_text.assert_not_awaited()
        for command in ("astra-low persistent", "astra-low 16m", "astra-medium 1m"):
            await self.command("/select_model " + command, user_id=77)
        self.assertFalse(self.profile().overridden)

    async def test_group_callback_checks_current_membership_and_fails_closed(self):
        await self.command(user_id=77)
        action = self.button("gpt-6.1-sol")
        lookup = self.context.bot.get_chat_member
        for result in (SimpleNamespace(status="left"), SimpleNamespace(status="kicked"),
                       SimpleNamespace(status="restricted", is_member=False), TelegramError("unavailable")):
            lookup.side_effect = [result]
            query = await self.click(data=action, actor=77)
            self.assertTrue(query.answer.await_args.kwargs["show_alert"])
            query.edit_message_text.assert_not_awaited()
        lookup.side_effect = [SimpleNamespace(status="restricted", is_member=True)]
        query = await self.click(data=action, actor=77)
        query.edit_message_text.assert_awaited_once()
        lookup.assert_awaited_with(-1001, 77)

    async def test_queued_group_commands_cannot_mutate_after_member_leaves(self):
        lookup = self.context.bot.get_chat_member
        await self.command(user_id=77)
        lookup.assert_not_awaited()
        await self.command("/select_model luna-high persistent", user_id=77)
        saved = self.profile()
        lookup.return_value = SimpleNamespace(status="left")
        for change in ("sol-medium persistent", "astra-low 15m", "reset"):
            await self.command("/select_model " + change, user_id=77)
            self.assertEqual(saved, self.profile())
        lookup.side_effect = TelegramError("temporarily unavailable")
        await self.command("/select_model reset", user_id=77)
        self.assertEqual(saved, self.profile())

    async def test_group_selection_rechecks_role_and_chat_after_membership_lookup(self):
        for revoke in (lambda: self.allowed.remove(-1001), lambda: self.admins.add(77)):
            async def lookup(chat_id, user_id):
                revoke()
                return SimpleNamespace(status="member")
            self.context.bot.get_chat_member.side_effect = lookup
            await self.command("/select_model sol-medium persistent", user_id=77)
            self.assertFalse(self.profile().overridden)
            self.allowed.add(-1001)

    async def test_forwarded_edited_anonymous_and_otherbot_controls_are_silent(self):
        for extra in ({"forward_origin": object()}, {"edit_date": object()},
                      {"from_user": None}, {"sender_chat": object()}):
            msg = message("/select_model astra-low 15m", **extra)
            await self.ui.handle_command(SimpleNamespace(effective_message=msg), self.context)
            msg.reply_text.assert_not_awaited()
        await self.command("/select_model@other_bot astra-low 15m")
        self.assertFalse(self.profile().overridden)
        self.assertFalse(self.sent)

    async def test_caption_command_is_handled_without_processing_media(self):
        msg = await self.command("", caption="/select_model astra-low 15m", photo=[object()])
        self.assertTrue(self.profile().overridden)
        msg.delete.assert_awaited_once()

    async def test_delete_failure_does_not_undo_saved_settings(self):
        msg = message("/select_model astra-low 15m")
        msg.delete.side_effect = TelegramError("missing_delete_permission")
        await self.ui.handle_command(SimpleNamespace(effective_message=msg), self.context)
        self.assertTrue(self.profile().overridden)
        self.assertIn("Застосовано", msg.reply_text.await_args.args[0])

    async def test_callback_uses_query_actor_not_bot_panel_author(self):
        await self.command()
        query = await self.click("gpt-6-astra")
        self.assertTrue(query.answer.await_count)
        self.assertIn("2/3", self.panel.text)

    async def test_other_admin_nonadmin_wrong_chat_and_wrong_message_cannot_reuse_panel(self):
        await self.command()
        token = self.button("gpt-6-astra")
        for actor in (43, 77):
            query = await self.click(data=token, actor=actor)
            query.edit_message_text.assert_not_awaited()
        copied = message("panel", chat_id=-1002, message_id=self.panel.message_id)
        copied.from_user = user(99, bot=True)
        query = await self.click(data=token, panel=copied)
        query.edit_message_text.assert_not_awaited()
        copied.chat_id = -1001
        copied.message_id += 1
        query = await self.click(data=token, panel=copied)
        query.edit_message_text.assert_not_awaited()
        self.assertFalse(self.profile().overridden)

    async def test_revoked_admin_cannot_use_existing_panel(self):
        await self.command()
        self.admins.remove(42)
        query = await self.click("gpt-6-astra")
        query.edit_message_text.assert_not_awaited()
        self.assertFalse(self.profile().overridden)

    async def test_old_step_callback_and_duplicate_apply_do_not_mutate(self):
        await self.command()
        old = self.button("gpt-6-astra")
        await self.click(data=old)
        query = await self.click(data=old)
        query.edit_message_text.assert_not_awaited()
        await self.click("high")
        await self.click("15 хв")
        apply_token = self.button("Застосувати")
        await self.click(data=apply_token)
        revision = self.profile().revision
        await self.click(data=apply_token)
        self.assertEqual(revision, self.profile().revision)

    async def test_unsupported_forged_actions_do_not_mutate(self):
        await self.command()
        token = self.button("gpt-6-astra")
        prefix = token.rsplit(":", 2)[0]
        for ending in ("m:-1", "m:999", "m:astra", "a:-", "unknown:-", "r:1"):
            query = await self.click(data=f"{prefix}:{ending}")
            query.edit_message_text.assert_not_awaited()
        self.assertFalse(self.profile().overridden)

    async def test_command_change_makes_older_panel_stale_then_refresh_rebinds(self):
        await self.command()
        await self.choose()
        old_panel = self.panel
        apply_token = self.button("Застосувати")
        await self.command("/select_model terra-medium 15m", user_id=43)
        query = await self.click(data=apply_token, panel=old_panel)
        query.edit_message_text.assert_not_awaited()
        self.assertEqual("gpt-6.1-terra", self.profile().model)
        await self.click("Оновити")
        await self.click("Скинути до типових")
        self.assertFalse(self.profile().overridden)

    async def test_ttl_restart_and_bounded_panel_sessions(self):
        await self.command()
        old = self.panel
        token = self.button("gpt-6-astra")
        self.now[0] += 901
        query = await self.click(data=token)
        query.edit_message_text.assert_not_awaited()
        self.ui._panels.clear()  # Equivalent to a new process with no panel sessions.
        query = await self.click(data=token, panel=old)
        query.edit_message_text.assert_not_awaited()
        await self.command()
        first, first_token = self.panel, self.button("gpt-6-astra")
        await self.command()
        await self.command()
        self.assertLessEqual(len(self.ui._panels), 2)
        query = await self.click(data=first_token, panel=first)
        query.edit_message_text.assert_not_awaited()

    async def test_failed_transport_after_apply_does_not_replay_mutation(self):
        await self.command()
        await self.choose()
        token = self.button("Застосувати")
        await self.click(data=token, edit_error=True)
        revision = self.profile().revision
        self.assertTrue(self.profile().overridden)
        query = await self.click(data=token)
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(revision, self.profile().revision)

    async def test_storage_failure_is_sanitized_and_acknowledged(self):
        with patch.object(self.store, "apply", side_effect=sqlite3.OperationalError("sensitive_database_location")):
            await self.command("/select_model astra-low")
        self.assertFalse(self.profile().overridden)
        self.assertNotIn("sensitive_database_location", self.sent[-1][0])
        await self.command()
        await self.choose()
        with patch.object(self.store, "apply", side_effect=sqlite3.OperationalError("sensitive_database_location")):
            query = await self.click("Застосувати")
        query.answer.assert_awaited()
        self.assertNotIn("sensitive_database_location", str(query.answer.await_args))
        query.edit_message_text.assert_not_awaited()
        self.assertFalse(self.profile().overridden)

    async def test_unavailable_store_preserves_command_access_without_model_call(self):
        self.ui.store = None
        await self.command()
        self.assertIn("недоступна", self.sent[-1][0])
        self.assertIsNone(self.panel)

    async def test_all_callback_data_is_short_and_has_no_model_ids(self):
        await self.command()
        for row in self.panel.reply_markup.inline_keyboard:
            for button in row:
                self.assertTrue(button.callback_data.startswith(CALLBACK_PREFIX))
                self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64)
                for capability in MODEL_CATALOG:
                    self.assertNotIn(capability.model, button.callback_data)

    async def test_status_uses_absolute_utc_deadline_and_timestamp(self):
        await self.command("/select_model astra-low 15m")
        await self.command()
        text = self.sent[-1][0]
        self.assertIn("1970-01-01 00:31 UTC", text)
        self.assertIn("Стан на 1970-01-01 00:16:40 UTC", text)
        self.assertNotIn("Залишилось", text)

    async def test_resolver_failure_is_sanitized_for_command_and_callback(self):
        await self.command()
        with patch.object(self.store, "resolve", side_effect=sqlite3.OperationalError("private_backend_detail")):
            query = await self.click("gpt-6-astra")
            await self.command()
        query.answer.assert_awaited()
        query.edit_message_text.assert_not_awaited()
        self.assertNotIn("private_backend_detail", str(query.answer.await_args))
        self.assertNotIn("private_backend_detail", self.sent[-1][0])

    async def test_unauthorized_callback_cannot_read_settings_or_panel(self):
        await self.command()
        token = self.button("gpt-6-astra")
        with patch.object(self.store, "resolve", side_effect=AssertionError("unexpected read")):
            query = await self.click(data=token, actor=77)
        query.edit_message_text.assert_not_awaited()
        self.assertNotIn("gpt", str(query.answer.await_args))

    async def test_unexpected_programming_error_is_not_swallowed(self):
        await self.command()
        await self.choose()
        with patch.object(self.store, "apply", side_effect=AssertionError("test invariant")):
            with self.assertRaises(AssertionError):
                await self.click("Застосувати")


if __name__ == "__main__":
    unittest.main()
