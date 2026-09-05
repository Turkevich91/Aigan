"""Shared bounded session and real SDK branch reads, with synthetic originals only."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from chat_history import ChatHistorySession, HistoryLimits
from history_citations import HistoryCitationSession
from memory import MemoryStore


class BranchSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temporary.name) / 'memory.sqlite3')
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.store.close)
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.sequence = 0

    def add(self, text='Synthetic original', **kwargs):
        self.sequence += 1
        values = dict(chat_id=-100123, message_id=self.sequence * 10, user_id=1,
                      created_at=self.start + timedelta(minutes=self.sequence),
                      sender_label='Synthetic participant', text=text)
        values.update(kwargs)
        return self.store.save_message(**values)

    def session(self, **kwargs):
        cutoff = self.add('Current request')
        return ChatHistorySession(self.store, chat_id=-100123, cutoff_memory_id=cutoff,
            cutoff_created_at=(self.start + timedelta(minutes=self.sequence)).isoformat(), **kwargs)

    def expose(self, session, anchor):
        result = json.loads(session.read(mode='around', anchor_id=anchor, limit=1))
        self.assertEqual([anchor], [row['id'] for row in result['messages']])
        return result['messages'][0]

    def test_unexposed_foreign_and_missing_anchors_fail_before_branch_query(self):
        anchor = self.add()
        foreign = self.add('Foreign', chat_id=-100456)
        session = self.session()
        with patch.object(self.store, 'bounded_conversation_branch_rows') as select:
            for identity in (anchor, foreign, 999):
                self.assertEqual('anchor_unavailable', json.loads(session.read_branch(anchor_id=identity))['status'])
            select.assert_not_called()
        self.assertFalse(session.exposed_ids)
        self.assertIsNone(session.last_coverage)

    def test_edges_forwarded_attribution_and_validated_citations(self):
        parent = self.add('Original proposal')
        anchor = self.add('Agreed', reply_to_message_id=10)
        self.add('Unrelated adjacent conversation')
        child = self.add('Forwarded qualification', reply_to_message_id=20,
                         forward_origin='Synthetic forwarded origin')
        session = self.session()
        original = self.expose(session, anchor)
        result = json.loads(session.read_branch(anchor_id=anchor))
        self.assertEqual([parent, anchor, child], [row['id'] for row in result['messages']])
        self.assertEqual('connected_reply_branch', result['coverage']['selection'])
        self.assertIsNone(result['next_cursor'])
        self.assertFalse(result['coverage']['pagination_supported'])
        self.assertEqual(3, result['coverage']['displayed_unique_count'])
        self.assertTrue(result['messages'][-1]['is_forwarded'])
        self.assertEqual(original['citation_ref'], result['messages'][1]['citation_ref'])
        self.assertTrue(all(session.resolve_citation_ref(row['citation_ref']) for row in result['messages']))
        registry = HistoryCitationSession(self.store, chat_id=-100123, chat_type='supergroup',
            cutoff_memory_id=5, cutoff_created_at=(self.start + timedelta(minutes=5)).isoformat(), history=session)
        rendered = registry.render(result['messages'][-1]['citation_ref'])
        self.assertIn('https://t.me/c/123/40', rendered)
        self.assertIn('матеріал у чаті', rendered)
        self.assertNotIn('Synthetic forwarded origin', rendered)
        self.assertNotIn('evidence_digest', json.dumps(result))
        self.assertNotIn('drop_priority', json.dumps(result))

    def test_lexical_and_branch_share_four_calls_and_exact_char_accounting(self):
        anchor = self.add('uniqueanchor')
        self.add(reply_to_message_id=10)
        session = self.session()
        outputs = [session.read(mode='search', query='uniqueanchor')]
        outputs.extend(session.read_branch(anchor_id=anchor) for _ in range(3))
        with patch.object(self.store, 'bounded_conversation_branch_rows') as select:
            outputs.append(session.read_branch(anchor_id=anchor))
            select.assert_not_called()
        self.assertEqual('budget_exhausted', json.loads(outputs[-1])['status'])
        self.assertEqual(4, session.calls_used)
        self.assertEqual(sum(map(len, outputs)), session.chars_used)
        self.assertLessEqual(session.chars_used, 30000)

    def test_concurrent_readers_reserve_before_await_and_publish_unique_ids(self):
        anchor = self.add()
        for _ in range(18):
            self.add('x' * 1200, reply_to_message_id=10)
        session = self.session()
        initial = self.expose(session, anchor)
        before = session.chars_used
        async def run():
            return await asyncio.gather(*(session.aread_branch(anchor_id=anchor, limit=20) for _ in range(9)))
        outputs = asyncio.run(run())
        accepted = [json.loads(value) for value in outputs if value and json.loads(value)['status'] == 'ok']
        self.assertLessEqual(len(accepted), 3)
        self.assertEqual(4, session.calls_used)
        self.assertEqual(before + sum(map(len, outputs)), session.chars_used)
        self.assertLessEqual(session.chars_used, 30000)
        self.assertTrue(all(len(output) <= 12000 for output in outputs))
        union = {initial['id']} | {row['id'] for result in accepted for row in result['messages']}
        self.assertEqual(union, session.exposed_ids)
        for result in accepted:
            self.assertLessEqual(len(result['messages']), 20)
            self.assertTrue(all(len(json.dumps(row, ensure_ascii=False, separators=(',', ':'))) <= 1000
                                for row in result['messages']))
            self.assertEqual({str(row['id']) for row in result['messages']}, set(result['coverage']['branch']['relations']))

    def test_cancelled_branch_worker_never_exposes_unseen_children(self):
        anchor = self.add()
        child = self.add(reply_to_message_id=10)
        session = self.session()
        self.expose(session, anchor)
        initial_chars = session.chars_used
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        actual = self.store.bounded_conversation_branch_rows
        def blocked(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Synthetic worker was not released')
            try:
                return actual(**kwargs)
            finally:
                finished.set()
        async def run():
            task = asyncio.create_task(session.aread_branch(anchor_id=anchor))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 5))
        with patch.object(self.store, 'bounded_conversation_branch_rows', side_effect=blocked):
            try:
                asyncio.run(run())
            finally:
                release.set()
        self.assertEqual({anchor}, session.exposed_ids)
        self.assertIsNone(session.validated_exposed_item(child))
        self.assertEqual(initial_chars, session.chars_used)
        self.assertEqual(2, session.calls_used)
        self.assertIsNone(session.last_coverage)
        self.assertEqual('ok', json.loads(session.read_branch(anchor_id=anchor))['status'])

    def test_edit_between_authorization_and_locked_selection_fails_closed(self):
        anchor = self.add('old content')
        self.add(reply_to_message_id=10)
        session = self.session()
        old = self.expose(session, anchor)
        actual = self.store.bounded_conversation_branch_rows
        def edit_then_read(**kwargs):
            self.store.save_message(chat_id=-100123, message_id=10, user_id=1,
                created_at=self.start + timedelta(minutes=1), sender_label='Synthetic participant', text='edited content')
            return actual(**kwargs)
        with patch.object(self.store, 'bounded_conversation_branch_rows', side_effect=edit_then_read):
            result = json.loads(session.read_branch(anchor_id=anchor))
        self.assertEqual('anchor_unavailable', result['status'])
        self.assertEqual([], result['messages'])
        self.assertIsNone(session.resolve_citation_ref(old['citation_ref']))

    def test_fixed_character_identity_cannot_traverse_quoted_bot_or_other_author_bridge(self):
        parent = self.add('OTHER', user_id=2)
        anchor = self.add('Own original', reply_to_message_id=10, source_text='QUOTED')
        self.add('BOT', is_bot=True, reply_to_message_id=20)
        self.add('FORWARD', forward_origin='Synthetic source', reply_to_message_id=20)
        self.add('OTHER CHILD', user_id=2, reply_to_message_id=20)
        self.add('Target behind other author', reply_to_message_id=50)
        child = self.add('Own qualification', reply_to_message_id=20)
        session = self.session(target_user_id=1)
        self.expose(session, anchor)
        result = json.loads(session.read_branch(anchor_id=anchor))
        self.assertEqual([anchor, child], [row['id'] for row in result['messages']])
        self.assertTrue(result['coverage']['authored_only'])
        self.assertTrue(result['coverage']['branch']['limits']['filtered_nodes'])
        self.assertTrue(all(row['source_text'] == '' and row['user_id'] == 1 for row in result['messages']))
        self.assertNotIn('QUOTED', json.dumps(result))
        self.assertEqual('invalid_filter', json.loads(session.read_branch(anchor_id=anchor, participant_id=2))['status'])

    def test_budget_drops_weakest_reply_before_ancestor_and_prunes_relations(self):
        parent = self.add('p' * 900)
        anchor = self.add('a' * 900, reply_to_message_id=10)
        child = self.add('c' * 900, reply_to_message_id=20)
        session = self.session(limits=HistoryLimits(max_response_chars=3600))
        self.expose(session, anchor)
        output = session.read_branch(anchor_id=anchor)
        result = json.loads(output)
        ids = [row['id'] for row in result['messages']]
        self.assertIn(anchor, ids)
        self.assertNotIn(child, ids)
        self.assertIn(parent, ids)
        self.assertLessEqual(len(output), 3600)
        self.assertTrue(result['coverage']['branch']['partial'])
        self.assertTrue(result['coverage']['branch']['limits']['character_cap'])
        self.assertEqual(set(map(str, ids)), set(result['coverage']['branch']['relations']))
        self.assertEqual(1, result['coverage']['omitted_due_to_response_budget'])
        self.assertEqual(set(ids), session.exposed_ids)
        self.assertIsNone(session.validated_exposed_item(child))

    def test_neighbor_opt_in_never_claims_reply_relationship(self):
        before = self.add()
        anchor = self.add()
        after = self.add()
        session = self.session()
        self.expose(session, anchor)
        result = json.loads(session.read_branch(anchor_id=anchor, include_neighbors=True))
        relations = result['coverage']['branch']['relations']
        self.assertEqual('neighbor', relations[str(before)]['relation'])
        self.assertEqual('neighbor', relations[str(after)]['relation'])
        self.assertFalse(result['coverage']['branch']['complete_topic'])

    def test_partial_branch_footer_survives_bounded_render(self):
        anchor = self.add('Retained reply', reply_to_message_id=999)
        session = self.session()
        self.expose(session, anchor)
        result = json.loads(session.read_branch(anchor_id=anchor))
        self.assertTrue(result['coverage']['branch']['limits']['missing_parent'])
        registry = HistoryCitationSession(self.store, chat_id=-100123, chat_type='supergroup',
            cutoff_memory_id=2, cutoff_created_at=(self.start + timedelta(minutes=2)).isoformat(), history=session)
        rendered = registry.render('x' * 5000 + result['messages'][0]['citation_ref'], max_chars=600)
        self.assertLessEqual(len(rendered), 600)
        self.assertIn('Остання гілка розмови часткова', rendered)
        self.assertIn('https://t.me/c/123/10', rendered)

    def test_metadata_budget_failure_never_exposes_new_branch_evidence(self):
        anchor = self.add('short')
        child = self.add('child', reply_to_message_id=10)
        session = self.session(limits=HistoryLimits(max_response_chars=1400))
        self.expose(session, anchor)
        result = json.loads(session.read_branch(anchor_id=anchor))
        self.assertEqual('response_budget_exhausted', result['status'])
        self.assertEqual([], result['messages'])
        self.assertEqual({anchor}, session.exposed_ids)
        self.assertIsNone(session.validated_exposed_item(child))
        self.assertIsNone(session.last_coverage)

    def test_date_bound_excluding_anchor_does_not_broaden(self):
        anchor = self.add()
        session = self.session()
        self.expose(session, anchor)
        result = json.loads(session.read_branch(anchor_id=anchor, after='2026-01-02'))
        self.assertEqual('anchor_unavailable', result['status'])
        self.assertIsNone(session.last_coverage)

    def test_branch_does_not_invalidate_lexical_cursor(self):
        for _ in range(4):
            self.add('sample')
        session = self.session()
        first = json.loads(session.read(mode='search', query='sample', limit=1))
        anchor = first['messages'][0]['id']
        session.read_branch(anchor_id=anchor)
        second = json.loads(session.read(cursor=first['next_cursor']))
        self.assertNotEqual(anchor, second['messages'][0]['id'])
        self.assertEqual('search', second['mode'])
        self.assertNotIn('branch', second['coverage'])

    def test_actual_sdk_finds_anchor_reads_branch_and_cites_new_evidence(self):
        from tests.test_agent_capabilities import ScriptedModel
        from agent_capabilities import PrimaryCapabilities
        from agents import Agent, Runner, RunConfig
        parent = self.add('The original proposal')
        anchor = self.add('uniqueanchor', reply_to_message_id=10)
        child = self.add('The later qualification', reply_to_message_id=20)
        session = self.session()
        class RefModel(ScriptedModel):
            async def get_response(self, system_instructions, input, *args, **kwargs):
                if self.steps == ['USE_BRANCH_REFERENCE']:
                    payloads = [json.loads(item['output']) for item in input
                                if isinstance(item, dict) and item.get('type') == 'function_call_output']
                    branch = next(value for value in payloads if value.get('mode') == 'branch')
                    self.steps = ['The later qualification ' + next(row['citation_ref'] for row in branch['messages'] if row['id'] == child)]
                return await super().get_response(system_instructions, input, *args, **kwargs)
        capabilities = PrimaryCapabilities(history=session)
        model = RefModel([('read_chat_history', {'mode': 'search', 'query': 'uniqueanchor'}),
                          ('read_conversation_branch', {'anchor_id': anchor}), 'USE_BRANCH_REFERENCE'])
        agent = Agent(name='Synthetic branch reader', model=model, tools=capabilities.tools())
        result = asyncio.run(Runner.run(agent, 'Inspect the context', max_turns=4,
                                      run_config=RunConfig(tracing_disabled=True)))
        registry = HistoryCitationSession(self.store, chat_id=-100123, chat_type='supergroup',
            cutoff_memory_id=4, cutoff_created_at=(self.start + timedelta(minutes=4)).isoformat(), history=session)
        rendered = registry.render(str(result.final_output))
        self.assertIn('https://t.me/c/123/30', rendered)
        self.assertEqual(2, session.calls_used)
        self.assertEqual({parent, anchor, child}, session.exposed_ids)
        self.assertEqual([], PrimaryCapabilities().tools())


if __name__ == '__main__':
    unittest.main()
