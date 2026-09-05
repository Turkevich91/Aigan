"""Synthetic prepared delivery faults and real grounded command integration."""
import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment
configure_test_environment()
import main
from character_evidence import CharacterReport
import character_delivery as cd
from memory import MemoryStore
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut


class DeliveryStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.now = 1000.0
        self.store = cd.CharacterDeliveryStore(self.path, clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.scope = cd.CharacterDeliveryScope(-1001, 0, 101, 101)
        self.next_id = 0

    def admit(self, *, scope=None, store=None, command_id=None):
        self.next_id += 1
        return (store or self.store).admit(scope or self.scope, command_id or self.next_id, command_time=self.now)

    def prepare(self, chunks=("frozen first", "frozen second")):
        claim = self.admit()
        return claim, self.store.prepare(claim, chunks)

    def snapshot(self, identity):
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return dict(c.execute("SELECT * FROM character_delivery_responses WHERE id=?", (identity,)).fetchone())

    async def deliver(self, claim, prepared, send, **kwargs):
        return await cd.deliver_prepared_character(self.store, claim, prepared, send, lambda: True, **kwargs)

    async def test_write_ahead_and_immediate_real_ack_confirmation(self):
        claim, prepared = self.prepare()
        async def send(text):
            row = self.snapshot(prepared.id)
            index = prepared.chunks.index(text)
            self.assertIn(index, json.loads(row['attempted_json']))
            self.assertIsNone(json.loads(row['confirmed_json'])[index])
            if index:
                self.assertEqual(700, json.loads(row['confirmed_json'])[0])
            return SimpleNamespace(message_id=700 + index)
        await self.deliver(claim, prepared, send)
        row = self.snapshot(prepared.id)
        self.assertEqual(1, row['complete'])
        self.assertEqual([700, 701], json.loads(row['confirmed_json']))
        self.assertEqual(list(prepared.chunks), json.loads(row['chunks_json']))
        with self.assertRaises(cd.CharacterDeliveryError):
            self.store.prepare(claim, ("replacement",))

    async def test_partial_failure_duplicate_replay_and_new_command_only_remainder(self):
        claim, prepared = self.prepare(("one", "two", "three"))
        sent = AsyncMock(side_effect=[SimpleNamespace(message_id=10), NetworkError('synthetic')])
        with self.assertRaises(NetworkError):
            await self.deliver(claim, prepared, sent)
        self.assertEqual(2, sent.await_count)
        self.store.release(claim)
        self.assertEqual('duplicate', self.admit(command_id=claim.command_id).status)
        recovery = self.admit()
        self.assertEqual(prepared.id, recovery.prepared.id)
        sent = AsyncMock(side_effect=[SimpleNamespace(message_id=20), SimpleNamespace(message_id=21), SimpleNamespace(message_id=22)])
        await self.deliver(recovery, recovery.prepared, sent)
        self.assertEqual([cd.RECOVERY_NOTICE, 'two', 'three'], [c.args[0] for c in sent.await_args_list])
        self.assertEqual([10, 21, 22], json.loads(self.snapshot(prepared.id)['confirmed_json']))

    async def test_ack_then_database_failure_is_ambiguous(self):
        claim, prepared = self.prepare(("paid",))
        sent = AsyncMock(return_value=SimpleNamespace(message_id=81))
        with patch.object(self.store, 'confirm', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.deliver(claim, prepared, sent)
        self.store.release(claim)
        recovery = self.admit()
        self.assertEqual((None,), recovery.prepared.confirmed)
        self.assertEqual((0,), recovery.prepared.attempted)
        sent = AsyncMock(return_value=SimpleNamespace(message_id=82))
        await self.deliver(recovery, recovery.prepared, sent)
        self.assertEqual(cd.RECOVERY_NOTICE, sent.await_args_list[0].args[0])

    async def test_cancellation_preserves_unknown_marker(self):
        claim, prepared = self.prepare(("paid",))
        entered = asyncio.Event()
        async def send(text):
            entered.set()
            await asyncio.Future()
        task = asyncio.create_task(self.deliver(claim, prepared, send))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.store.release(claim)
        recovery = self.admit()
        self.assertEqual((0,), recovery.prepared.attempted)
        self.assertEqual((None,), recovery.prepared.confirmed)

    async def test_failure_before_attempt_marker_sends_nothing(self):
        claim, prepared = self.prepare(("paid",))
        sent = AsyncMock()
        with patch.object(self.store, 'attempted', side_effect=sqlite3.OperationalError('synthetic')):
            with self.assertRaises(sqlite3.OperationalError):
                await self.deliver(claim, prepared, sent)
        sent.assert_not_awaited()
        self.store.release(claim)
        recovery = self.admit()
        sent = AsyncMock(return_value=SimpleNamespace(message_id=30))
        await self.deliver(recovery, recovery.prepared, sent)
        self.assertEqual(cd.CONFIRMED_FAILURE_RECOVERY_NOTICE, sent.await_args_list[0].args[0])

    async def test_known_telegram_rejection_does_not_invent_uncertainty(self):
        for error in (BadRequest('synthetic'), Forbidden('synthetic'), RetryAfter(1)):
            with self.subTest(type=type(error).__name__):
                claim, prepared = self.prepare(("paid",))
                with self.assertRaises(type(error)):
                    await self.deliver(claim, prepared, AsyncMock(side_effect=error),
                        definite_rejections=(BadRequest, Forbidden, RetryAfter))
                self.store.release(claim)
                recovery = self.admit()
                sent = AsyncMock(return_value=SimpleNamespace(message_id=41))
                await self.deliver(recovery, recovery.prepared, sent)
                self.assertEqual(cd.CONFIRMED_FAILURE_RECOVERY_NOTICE, sent.await_args_list[0].args[0])
                self.store.release(recovery)

    async def test_later_definite_rejection_cannot_erase_earlier_unknown(self):
        claim, prepared = self.prepare(("paid",))
        with self.assertRaises(NetworkError):
            await self.deliver(claim, prepared, AsyncMock(side_effect=NetworkError('synthetic')))
        self.store.release(claim)
        recovery = self.admit()
        sent = AsyncMock(side_effect=[SimpleNamespace(message_id=50), Forbidden('synthetic')])
        with self.assertRaises(Forbidden):
            await self.deliver(recovery, recovery.prepared, sent, definite_rejections=(Forbidden,))
        self.store.release(recovery)
        again = self.admit()
        sent = AsyncMock(return_value=SimpleNamespace(message_id=51))
        await self.deliver(again, again.prepared, sent)
        self.assertEqual(cd.RECOVERY_NOTICE, sent.await_args_list[0].args[0])

    async def test_confirmed_prefix_only_does_not_warn_duplicate_remainder(self):
        claim, prepared = self.prepare()
        self.store.attempted(claim, prepared.id, 0)
        self.store.confirm(claim, prepared.id, 0, 61)
        self.store.release(claim)
        recovery = self.admit()
        sent = AsyncMock(return_value=SimpleNamespace(message_id=62))
        await self.deliver(recovery, recovery.prepared, sent)
        self.assertEqual([cd.CONFIRMED_FAILURE_RECOVERY_NOTICE, 'frozen second'], [c.args[0] for c in sent.await_args_list])

    async def test_invalid_ack_never_marks_confirmation(self):
        for ack in (None, True, 0, '7'):
            claim, prepared = self.prepare(("paid",))
            with self.assertRaises(ValueError):
                await self.deliver(claim, prepared, AsyncMock(return_value=SimpleNamespace(message_id=ack)))
            self.assertEqual([None], json.loads(self.snapshot(prepared.id)['confirmed_json']))
            self.store.release(claim)
            # Finish this fixture so the next subcase gets its own prepared row.
            again = self.admit()
            await self.deliver(again, again.prepared, AsyncMock(return_value=SimpleNamespace(message_id=7)))
            self.store.release(again)

    async def test_send_renews_owned_lease_before_network_wait(self):
        claim, prepared = self.prepare(("paid",))
        other = cd.CharacterDeliveryStore(self.path, clock=lambda: self.now)
        self.addCleanup(other.close)
        self.now += cd.LEASE_SECONDS - 1
        entered, done = asyncio.Event(), asyncio.Event()
        async def send(text):
            entered.set()
            await done.wait()
            return SimpleNamespace(message_id=91)
        task = asyncio.create_task(self.deliver(claim, prepared, send))
        await entered.wait()
        self.now += 2
        self.assertEqual('busy', self.admit(store=other).status)
        done.set()
        await task

    async def test_recovery_notice_is_also_fenced(self):
        claim, prepared = self.prepare(("paid",))
        self.store.release(claim)
        recovery = self.admit()
        self.now += cd.LEASE_SECONDS - 1
        other = cd.CharacterDeliveryStore(self.path, clock=lambda: self.now)
        self.addCleanup(other.close)
        calls = []
        async def send(text):
            calls.append(text)
            if len(calls) == 1:
                self.now += 2
                self.assertEqual('busy', self.admit(store=other).status)
            return SimpleNamespace(message_id=101)
        await self.deliver(recovery, recovery.prepared, send)

    def test_restart_lease_cas_and_immutable_recovery(self):
        claim, prepared = self.prepare(("paid **literal** <b>example</b>",))
        other = cd.CharacterDeliveryStore(self.path, clock=lambda: self.now)
        self.addCleanup(other.close)
        self.assertEqual('busy', self.admit(store=other).status)
        self.now += cd.LEASE_SECONDS + 1
        recovery = self.admit(store=other)
        self.assertEqual(prepared.chunks, recovery.prepared.chunks)
        self.store.release(claim)
        self.assertEqual('busy', self.admit(store=self.store).status)
        with self.assertRaises(cd.CharacterDeliveryError):
            self.store.attempted(claim, prepared.id, 0)

    def test_identity_topic_chat_scope_and_same_update_dedupe(self):
        claim, prepared = self.prepare()
        self.store.release(claim)
        for scope in (cd.CharacterDeliveryScope(-2002,0,101,101), cd.CharacterDeliveryScope(-1001,4,101,101),
                      cd.CharacterDeliveryScope(-1001,0,202,101), cd.CharacterDeliveryScope(-1001,0,101,202)):
            other = self.admit(scope=scope)
            self.assertEqual('new', other.status)
            self.assertIsNone(other.prepared)
            self.store.release(other)
        changed_scope = cd.CharacterDeliveryScope(-1001, 9, 202, 202)
        self.assertEqual('duplicate', self.admit(scope=changed_scope,command_id=claim.command_id).status)

    def test_expiry_not_extended_by_attempt_and_old_update_cannot_regenerate(self):
        claim, prepared = self.prepare(("paid",))
        expires = prepared.expires_at
        self.store.attempted(claim, prepared.id, 0)
        self.store.release(claim)
        self.now += 100
        recovery = self.admit()
        self.store.attempted(recovery, prepared.id, 0)
        self.assertEqual(expires, self.snapshot(prepared.id)['expires_at'])
        self.store.release(recovery)
        self.now = expires + 1
        self.assertEqual('expired_command', self.store.admit(self.scope,claim.command_id,command_time=1000).status)
        new = self.admit()
        self.assertEqual('new',new.status)
        self.assertEqual(0,self.store._conn.execute('SELECT count(*) FROM character_delivery_responses').fetchone()[0])

    def test_capacity_and_pending_response_are_preserved(self):
        with patch.object(cd,'MAX_RESPONSES',1):
            claim, prepared = self.prepare(("paid",))
            self.store.release(claim)
            rejected = self.admit(scope=cd.CharacterDeliveryScope(-1001,0,202,202))
            self.assertEqual('capacity',rejected.status)
            self.assertEqual('duplicate',self.admit(scope=rejected.scope,command_id=rejected.command_id).status)
            self.assertEqual('paid',json.loads(self.snapshot(prepared.id)['chunks_json'])[0])
            self.assertEqual('recovery',self.admit().status)

    def test_inflight_generation_reserves_capacity_before_model(self):
        with patch.object(cd,'MAX_RESPONSES',1):
            first=self.admit()
            second=self.admit(scope=cd.CharacterDeliveryScope(-1001,0,202,202))
            self.assertEqual('new',first.status)
            self.assertEqual('capacity',second.status)

    def test_prepare_survives_brief_external_writer_contention(self):
        import threading
        claim = self.admit()
        locked, release = threading.Event(), threading.Event()
        failures = []
        def hold_writer():
            try:
                with sqlite3.connect(self.path) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    locked.set()
                    release.wait(5)
            except Exception as exc:
                failures.append(type(exc).__name__)
                locked.set()
        worker = threading.Thread(target=hold_writer)
        worker.start()
        timer = None
        try:
            self.assertTrue(locked.wait(5))
            self.assertEqual([], failures)
            timer = threading.Timer(1.2, release.set)
            timer.start()
            prepared = self.store.prepare(claim, ('already generated literal text',))
            self.assertEqual(['already generated literal text'], json.loads(self.snapshot(prepared.id)['chunks_json']))
            self.assertEqual(1, self.store._conn.execute('SELECT count(*) FROM character_delivery_responses').fetchone()[0])
        finally:
            release.set()
            if timer is not None:
                timer.cancel()
                timer.join(5)
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failures)

    def test_chunk_caps_validate_before_persisting(self):
        claim=self.admit()
        for chunks in ((), ('a',)*9, ('x'*4097,), ('😀'*2049,), ('x'*4001,)*4):
            with self.subTest(size=len(chunks)),self.assertRaises(cd.CharacterDeliveryError):
                self.store.prepare(claim,chunks)
        self.assertEqual(0,self.store._conn.execute('SELECT count(*) FROM character_delivery_responses').fetchone()[0])

    def test_full_command_capacity_fails_before_claim(self):
        with patch.object(cd, 'MAX_COMMANDS', 1):
            self.admit()
            with self.assertRaisesRegex(cd.CharacterDeliveryError, 'command_capacity'):
                self.admit(scope=cd.CharacterDeliveryScope(-1001, 0, 202, 202))
        self.assertEqual(1, self.store._conn.execute('SELECT count(*) FROM character_delivery_leases').fetchone()[0])

    async def test_outer_timeout_keeps_write_ahead_ambiguity(self):
        claim, prepared = self.prepare(('paid',))
        async def blocked(text):
            await asyncio.Future()
        with patch.object(cd, 'SEND_TIMEOUT_SECONDS', 0.01):
            with self.assertRaises(TimeoutError):
                await self.deliver(claim, prepared, blocked)
        self.store.release(claim)
        recovery = self.admit()
        self.assertEqual((0,), recovery.prepared.attempted)
        self.assertEqual((None,), recovery.prepared.confirmed)

    async def test_permission_revoked_before_recovery_sends_nothing(self):
        claim,prepared=self.prepare()
        self.store.release(claim)
        recovery=self.admit()
        sent=AsyncMock()
        with self.assertRaises(cd.CharacterDeliveryError):
            await cd.deliver_prepared_character(self.store,recovery,recovery.prepared,sent,lambda:False)
        sent.assert_not_awaited()


class DeliveryCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.memory=MemoryStore(Path(self.tmp.name)/'memory.sqlite3')
        self.addCleanup(self.memory.close)
        self.config=replace(main.CONFIG,character_evidence_enabled=True)
        self.now=datetime.now(timezone.utc)
        for i in range(4):
            self.memory.save_message(chat_id=-1001,message_id=i+1,user_id=101,username='target',sender_label='Synthetic',
                text=f'Перевірмо факти {i}',created_at=self.now-timedelta(days=5-i))
        self.model=AsyncMock(return_value=CharacterReport(facets=[],abstention='ambiguous'))
        self.events=[]
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        for target,value in [('CONFIG',self.config),('MEMORY',self.memory),('run_character_evidence',self.model),
                             ('maybe_send_chat_action',AsyncMock())]:
            self.stack.enter_context(patch.object(main,target,value))
        self.stack.enter_context(patch.object(main,'system_event',side_effect=lambda **kwargs:self.events.append(kwargs)))

    def message(self, identity=900, *, user_id=101):
        msg=FakeMessage('/character me',message_id=identity)
        msg.from_user.id=user_id
        msg.from_user.username='self'
        return msg

    async def run_command(self,msg,args='me'):
        await main.handle_character_command(msg,SimpleNamespace(),args)

    async def test_actual_memory_path_prepared_before_send_and_restart_recovery_no_model(self):
        chunks=('PAID **original** <b>one</b>','PAID second')
        self.stack.enter_context(patch.object(main,'split_text_chunks',return_value=list(chunks)))
        self.stack.enter_context(patch.object(main.CharacterEvidenceSession,'render',return_value='\n\n'.join(chunks)))
        first=self.message()
        deliveries=[]
        async def partial(text,**kwargs):
            with sqlite3.connect(self.memory.db_path) as c:
                row=c.execute('SELECT chunks_json,attempted_json,confirmed_json FROM character_delivery_responses').fetchone()
            if text==chunks[0]:
                self.assertEqual(list(chunks),json.loads(row[0]))
                self.assertIn(0,json.loads(row[1]))
                deliveries.append(text)
                return SimpleNamespace(message_id=1234)
            if text==chunks[1]:
                self.assertEqual(1234,json.loads(row[2])[0])
                raise NetworkError('synthetic')
            return SimpleNamespace(message_id=999)
        first.reply_text=partial
        await self.run_command(first)
        self.model.assert_awaited_once()
        replay=self.message()
        await self.run_command(replay)
        self.assertEqual([],replay.reply_calls)
        self.assertEqual(1,self.model.await_count)
        # Every handler opens a fresh connection; recovery must use persisted state.
        second=self.message(901)
        await self.run_command(second)
        self.assertEqual(1,self.model.await_count)
        self.assertEqual([cd.RECOVERY_NOTICE,chunks[1]],[r['text'] for r in second.reply_calls])
        self.assertTrue(all(r['parse_mode'] is None for r in second.reply_calls))
        self.assertFalse(any('PAID' in item.text for item in self.memory.latest(-1001,100)))
        self.assertNotIn('PAID',json.dumps(self.events))
        kinds=[row['event_type'] for row in self.events]
        self.assertIn('prepared',kinds);self.assertIn('delivery_failed',kinds);self.assertIn('delivery_recovered',kinds)
        ids=[row['details']['prepared_id'] for row in self.events if row['event_type'] in {'prepared','delivery_recovered'}]
        self.assertEqual(ids[0],ids[1])

    async def test_successful_new_command_regenerates_but_duplicate_does_not(self):
        first=self.message()
        await self.run_command(first)
        await self.run_command(self.message())
        await self.run_command(self.message(901))
        self.assertEqual(2,self.model.await_count)
        self.assertEqual(1,len(first.reply_calls))

    async def test_parallel_same_scope_generates_only_once(self):
        entered,finish=asyncio.Event(),asyncio.Event()
        async def model(session):
            entered.set();await finish.wait()
            return CharacterReport(facets=[],abstention='ambiguous')
        self.model.side_effect=model
        first=self.message()
        task=asyncio.create_task(self.run_command(first))
        await entered.wait()
        second=self.message(901)
        await self.run_command(second)
        self.assertEqual(1,self.model.await_count)
        self.assertIn('уже готую',second.reply_calls[0]['text'])
        finish.set();await task
        await self.run_command(second)
        self.assertEqual(1,self.model.await_count)

    async def test_store_failure_before_claim_never_spends(self):
        with patch.object(main,'CharacterDeliveryStore',side_effect=sqlite3.OperationalError('synthetic')):
            await self.run_command(self.message())
        self.model.assert_not_awaited()

    async def test_permission_or_username_identity_change_cannot_recover(self):
        admin=next(iter(self.config.admin_user_ids))
        first=self.message(user_id=admin)
        first.reply_text=AsyncMock(side_effect=NetworkError('synthetic'))
        await self.run_command(first,'@target')
        self.assertEqual(1,self.model.await_count)
        self.config=replace(self.config,admin_user_ids=frozenset())
        with patch.object(main,'CONFIG',self.config):
            denied=self.message(901,user_id=admin)
            await self.run_command(denied,'@target')
            self.assertEqual(1,len(denied.reply_calls))
            self.assertIn('лише адмін',denied.reply_calls[0]['text'])
        self.memory.save_message(chat_id=-1001,message_id=99,user_id=202,username='target',sender_label='Other',text='Different author',created_at=self.now)
        ambiguous=self.message(902,user_id=admin)
        await self.run_command(ambiguous,'@target')
        self.assertIn('однозначно',ambiguous.reply_calls[0]['text'])
        self.assertEqual(1,self.model.await_count)

    async def test_cancelled_transport_preserves_cache_and_resets_context(self):
        entered=asyncio.Event()
        first=self.message()
        async def send(*args,**kwargs):
            entered.set();await asyncio.Future()
        first.reply_text=send
        seen=[]
        async def model(session):
            seen.append(main.current_model_run_id())
            return CharacterReport(facets=[],abstention='ambiguous')
        self.model.side_effect=model
        token=main.ACTIVE_MODEL_RUN_ID.set('a'*32)
        try:
            task=asyncio.create_task(self.run_command(first))
            await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertEqual('a'*32,main.ACTIVE_MODEL_RUN_ID.get())
            self.assertEqual(['a'*32],seen)
            prepared_event=next(e for e in self.events if e['event_type']=='prepared')
            self.assertEqual(seen[0],prepared_event['details']['run_id'])
            second=self.message(901)
            await self.run_command(second)
            self.assertEqual(1,self.model.await_count)
            self.assertEqual(cd.RECOVERY_NOTICE,second.reply_calls[0]['text'])
            self.assertIn('delivery_cancelled',[e['event_type'] for e in self.events])
        finally:
            main.ACTIVE_MODEL_RUN_ID.reset(token)

    async def test_first_body_and_failure_notice_timed_out_then_saved_reply_recovers(self):
        first = self.message()
        first.reply_text = AsyncMock(side_effect=TimedOut('synthetic'))
        await self.run_command(first)
        self.assertEqual(2, first.reply_text.await_count)
        self.model.assert_awaited_once()
        with sqlite3.connect(self.memory.db_path) as connection:
            row = connection.execute('SELECT chunks_json,attempted_json,confirmed_json FROM character_delivery_responses').fetchone()
        chunks = json.loads(row[0])
        self.assertEqual(1, len(chunks))
        self.assertEqual([0], json.loads(row[1]))
        self.assertEqual([None], json.loads(row[2]))
        second = self.message(901)
        await self.run_command(second)
        self.assertEqual([cd.RECOVERY_NOTICE, chunks[0]], [r['text'] for r in second.reply_calls])
        self.model.assert_awaited_once()

    async def test_expiry_during_recovery_does_not_promise_saved_text(self):
        first = self.message()
        first.reply_text = AsyncMock(side_effect=NetworkError('synthetic'))
        await self.run_command(first)
        self.assertEqual(1, self.model.await_count)
        second = self.message(901)
        sent = []
        async def expire_after_notice(text, **kwargs):
            sent.append(text)
            if text == cd.RECOVERY_NOTICE:
                with sqlite3.connect(self.memory.db_path) as connection:
                    connection.execute('UPDATE character_delivery_responses SET expires_at=0')
            return SimpleNamespace(message_id=10001)
        second.reply_text = expire_after_notice
        await self.run_command(second)
        self.assertEqual(1, self.model.await_count)
        self.assertEqual(2, len(sent))
        self.assertIn('Термін збереження', sent[-1])
        self.assertNotIn('надішлю збережений текст', sent[-1])
        await self.run_command(self.message(902))
        self.assertEqual(2, self.model.await_count)

    async def test_expiry_during_network_timeout_does_not_promise_saved_text(self):
        clock = [cd.time.time()]
        factory = lambda path: cd.CharacterDeliveryStore(path, clock=lambda: clock[0])
        with patch.object(main, 'CharacterDeliveryStore', side_effect=factory):
            first = self.message()
            first.reply_text = AsyncMock(side_effect=NetworkError('synthetic'))
            await self.run_command(first)
            second = self.message(901)
            sent = []
            async def timeout_after_expiry(text, **kwargs):
                sent.append(text)
                if len(sent) == 2:
                    clock[0] += cd.TTL_SECONDS + 1
                    raise NetworkError('synthetic')
                return SimpleNamespace(message_id=10001)
            second.reply_text = timeout_after_expiry
            await self.run_command(second)
        self.assertEqual(1, self.model.await_count)
        self.assertEqual(3, len(sent))
        self.assertIn('Термін збереження', sent[-1])
        self.assertNotIn('надішлю збережений текст', sent[-1])

    async def test_expired_telegram_update_does_not_start_model(self):
        message=self.message();message.date=self.now-timedelta(days=2)
        await self.run_command(message)
        self.model.assert_not_awaited()
        self.assertEqual([],message.reply_calls)

if __name__ == '__main__':
    unittest.main()
