"""A real authorized transcript tool can yield one Telegram generation safely."""
import asyncio
from collections import defaultdict, deque
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agents.mcp import MCPServerStdio
from mcp.types import CallToolResult, TextContent
from tests.support import FakeMessage, configure_test_environment

configure_test_environment()
import main
from agent_capabilities import ChatToolMCPServer
from telegram_turn_assembly import TurnAssembler
from tests.test_telegram_turn_assembly import ManualClock, AsyncioClock


class YoutubeConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, assembled, read_tool, mutation=False, guard=True):
        clock = ManualClock()
        clock.advance(5000)
        started, unblock = asyncio.Event(), asyncio.Event()
        count = []
        reply_order = []
        first = FakeMessage("Explain the geological sample.", chat_type="private", message_id=171)
        second = FakeMessage("Explain how a thermometer works.", chat_type="private", message_id=172)
        for msg in (first, second):
            msg.from_user.id = 1234

        async def enter(server):
            return server

        async def transport(server, tool, args, meta=None):
            self.assertEqual((server.name, tool), ("youtube_transcript", "get_youtube_transcript"))
            count.append(tool)
            started.set()
            await unblock.wait()
            return CallToolResult(content=[TextContent(type="text", text="Synthetic transcript.")])

        async def runner(agent, *args, **kwargs):
            trigger = main.ACTIVE_OUTBOUND_PROVENANCE.get().trigger_message_id
            if trigger == first.message_id:
                if read_tool:
                    server = next(s for s in agent.mcp_servers if s.name == "youtube_transcript")
                    await server.call_tool("get_youtube_transcript", {"video": "synthetic"})
                else:
                    started.set()
                    await unblock.wait()
            return SimpleNamespace(final_output=f"Answer to {trigger}.", new_items=())

        async def log_reply(msg, text, **kwargs):
            # The post-SDK continuation owns chat serialization again.
            if guard:
                self.assertTrue(main.chat_generation_lock(msg.chat_id).locked())
            reply_order.append(msg.message_id)
            await msg.reply_text(text)
            return SimpleNamespace(delivered_chunks=(text,))

        async def settle():
            for _ in range(40):
                await asyncio.sleep(0)

        ctx = SimpleNamespace(bot=SimpleNamespace(id=99, username="test_bot", send_chat_action=AsyncMock()),
                              application=SimpleNamespace(create_task=asyncio.create_task))
        async def ingress(msg):
            await main.text_message(SimpleNamespace(effective_message=msg), ctx)

        with ExitStack() as stack:
            cooldown = bool(read_tool and not mutation)
            opts = dict(telegram_turn_assembly_enabled=assembled, telegram_turn_assembly_seconds=2,
                chat_inflight_guard_enabled=guard, user_cooldown_seconds=20 if cooldown else 0,
                chat_cooldown_seconds=5 if cooldown else 0)
            for name, value in {
                "CONFIG": replace(main.CONFIG, **opts), "time": clock, "asyncio": AsyncioClock(clock),
                "telegram_turn_stopping": False, "telegram_turn_assembler": TurnAssembler(), "telegram_turn_ingress": {},
                "telegram_turn_tasks": set(), "telegram_turn_dispatch_locks": {}, "chat_generation_locks": {},
                "pending_requests": {}, "last_user_call": {}, "last_chat_call": {}, "MEMORY": None,
                "recent_chat_answers": defaultdict(deque), "detached_transcripts": {}, "BOT_USERNAME": "test_bot",
                "remember_message_persistently": AsyncMock(), "remember_self_complaint_signal": MagicMock(),
                "classify_request_with_intent": AsyncMock(return_value=("normal", None)), "send_reply": log_reply,
            }.items():
                stack.enter_context(patch.object(main, name, value))
            stack.enter_context(patch.object(main.Runner, "run", side_effect=runner))
            stack.enter_context(patch.object(MCPServerStdio, "__aenter__", enter))
            stack.enter_context(patch.object(MCPServerStdio, "__aexit__", AsyncMock(return_value=False)))
            stack.enter_context(patch.object(MCPServerStdio, "call_tool", transport))
            if mutation:
                rctx = SimpleNamespace(intent="reminder_create", allowed_toolsets=("reminder_crud",))
                stack.enter_context(patch.object(main, "reminder_tool_context_for_message", return_value=rctx))
            one = asyncio.create_task(ingress(first))
            await settle()
            clock.advance(5002)
            await asyncio.wait_for(started.wait(), 3)
            if cooldown:
                elsewhere = FakeMessage(chat_type="private", chat_id=-1002)
                elsewhere.from_user.id = first.from_user.id
                self.assertGreater(main.cooldown_left(elsewhere), 0)
            async def next_update():
                await one   # Telegram's serial update consumer.
                await ingress(second)
            two = asyncio.create_task(next_update())
            draining = None
            try:
                await settle()
                clock.advance(5004)
                await settle()
                expected = [172] if read_tool and not mutation else []
                self.assertEqual(reply_order, expected)
                if expected:
                    self.assertIn((first.chat_id, first.message_id), main.detached_transcripts)
                    await ingress(first)  # replay cannot issue another read or another answer
                    await settle()
                    self.assertEqual(count, ["get_youtube_transcript"])
                    if not assembled:
                        third = FakeMessage("Explain lunar phases.", chat_type="private", message_id=173)
                        third.from_user.id = first.from_user.id
                        await ingress(third)
                        self.assertIn("Зачекай", third.reply_calls[0]["text"])
                        self.assertEqual(reply_order, [172])
                    draining = asyncio.create_task(main.shutdown_telegram_turns())
                    await settle()
                    self.assertFalse(draining.done())
            finally:
                unblock.set()
                await settle()
                clock.advance(5010)
                await asyncio.gather(one, two)
                await settle()
                clock.advance(5020)
                await main.shutdown_telegram_turns()
                if draining:
                    await draining
            self.assertEqual(reply_order, [172, 171] if read_tool and not mutation else [171, 172])
            self.assertFalse(main.detached_transcripts)
            self.assertFalse(main.chat_generation_lock(first.chat_id).locked())

    async def test_transcript_read_frees_serial_input_and_assembled_dispatch(self):
        for mode in (False, True):
            with self.subTest(assembled=mode):
                await self.exercise(assembled=mode, read_tool=True)

    async def test_one_cooldown_handoff_when_generation_lock_is_disabled(self):
        await self.exercise(assembled=False, read_tool=True, guard=False)

    async def test_plain_requests_retain_generation_order_in_both_modes(self):
        for mode in (False, True):
            with self.subTest(assembled=mode):
                await self.exercise(assembled=mode, read_tool=False)

    async def test_mutation_capable_sdk_run_keeps_its_slot_through_transcript_read(self):
        await self.exercise(assembled=True, read_tool=True, mutation=True)

    async def test_revoked_tool_cannot_release_turn_or_reach_transport(self):
        server = ChatToolMCPServer(name="youtube_transcript", params={"command": "unused"})
        signal = server.on_transcript_read = MagicMock(return_value=True)
        with patch("agent_capabilities.tool_allowed", return_value=False), \
             patch.object(MCPServerStdio, "call_tool", AsyncMock()) as call:
            self.assertTrue((await server.call_tool("get_youtube_transcript", {})).isError)
        signal.assert_not_called()
        call.assert_not_awaited()

    async def test_interrupted_resume_restores_lock_before_propagating_timeout(self):
        msg = FakeMessage()
        lock = asyncio.Lock()
        with patch.object(main, "detached_transcripts", {}):
            window = main.TranscriptReadWindow(msg, "Synthetic request", lock)
            await window.acquire()
            self.assertTrue(window.release())
            await lock.acquire()  # adjacent normal turn
            restoring = asyncio.create_task(window.resume())
            await asyncio.sleep(0)
            restoring.cancel()
            await asyncio.sleep(0)
            self.assertFalse(restoring.done())
            lock.release()
            with self.assertRaises(asyncio.CancelledError):
                await restoring
            self.assertTrue(window.owns_lock)
            window.unlock()
