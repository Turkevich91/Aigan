"""Actual admission adapter: rollout, bounded transport, and invocation isolation."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests.support import FakeMessage, configure_test_environment

configure_test_environment()
import main
from recall_intent_model import MODEL, RecallModelResult
from runtime_model_pricing import TokenUsage


def model_result(intent='prior_conversation', failure=None):
    return RecallModelResult(intent, 'failed' if failure else 'succeeded',
                             'completed', MODEL, 'none', TokenUsage(10, output_tokens=4),
                             {}, 12., failure)


class RecallRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = main.CONFIG
        self.message = FakeMessage('What amount did we agree on earlier?', chat_type='private')
        self.memory = patch.object(main, 'MEMORY', object())
        self.memory.start()
        self.addCleanup(self.memory.stop)

    def test_off_keeps_exact_legacy_and_never_calls_classifier(self):
        legacy = main.MemoryRecallIntent(False, confidence=.2, query='amount', reason='baseline')
        with patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='off')), \
             patch.object(main, 'detect_legacy_memory_recall_intent', AsyncMock(return_value=legacy)), \
             patch.object(main, 'classify_recall_intent', AsyncMock()) as classifier:
            self.assertIs(asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text)), legacy)
            classifier.assert_not_awaited()

    def test_shadow_observes_candidate_and_preserves_exact_legacy(self):
        legacy = main.MemoryRecallIntent(False, confidence=.2, query='amount', reason='baseline')
        with patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='shadow')), \
             patch.object(main, 'detect_legacy_memory_recall_intent', AsyncMock(return_value=legacy)) as baseline, \
             patch.object(main, 'classify_recall_intent', AsyncMock(return_value=model_result())) as classifier, \
             patch.object(main, 'system_event') as event:
            self.assertIs(asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text)), legacy)
            baseline.assert_awaited_once()
            classifier.assert_awaited_once()
        details = event.call_args.kwargs['details']
        self.assertTrue(details['candidate_recall'])
        self.assertFalse(details['applied_recall'])
        self.assertTrue(details['differs'])
        self.assertNotIn(self.message.text, repr(details))

    def test_enforce_uses_one_classifier_and_no_legacy_embedding_call(self):
        with patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='enforce')), \
             patch.object(main, 'memory_recall_embedding_confidence', AsyncMock()) as embed, \
             patch.object(main, 'classify_recall_intent', AsyncMock(return_value=model_result())) as classifier, \
             patch.object(main, 'system_event') as event:
            result = asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text))
        embed.assert_not_awaited()
        classifier.assert_awaited_once()
        self.assertTrue(result.is_recall)
        self.assertEqual('luna_prior_conversation', result.reason)
        self.assertEqual(0., result.confidence)
        self.assertTrue(result.query)
        self.assertIsNone(event.call_args.kwargs['details']['differs'])

    def test_negative_candidate_does_not_fall_back_to_legacy_positive(self):
        with patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='enforce')), \
             patch.object(main, 'detect_legacy_memory_recall_intent', AsyncMock()) as baseline, \
             patch.object(main, 'classify_recall_intent', AsyncMock(return_value=model_result('new_memory'))), \
             patch.object(main, 'system_event'):
            result = asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text))
        self.assertFalse(result.is_recall)
        baseline.assert_not_awaited()

    def test_candidate_failure_and_invalid_result_keep_legacy_without_retry(self):
        legacy = main.MemoryRecallIntent(True, confidence=.8, query='amount', reason='semantic_strong')
        for mode in ('shadow', 'enforce'):
            for transport in (AsyncMock(side_effect=RuntimeError('private detail')),
                              AsyncMock(return_value=model_result(None, 'InvalidStructuredResponse'))):
                with self.subTest(mode=mode), \
                     patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode=mode)), \
                     patch.object(main, 'detect_legacy_memory_recall_intent', AsyncMock(return_value=legacy)) as baseline, \
                     patch.object(main, 'classify_recall_intent', transport), \
                     patch.object(main, 'system_event') as event:
                    self.assertIs(asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text)), legacy)
                    baseline.assert_awaited_once()
                    transport.assert_awaited_once()
                    self.assertEqual('candidate_failed', event.call_args.kwargs['details']['fallback_reason'])
                    self.assertNotIn('private detail', repr(event.call_args))

    def test_excluded_route_disabled_memory_empty_and_overlong_avoid_provider(self):
        cases = [(None, self.message.text), (object(), ''),
                 (object(), 'Translate this into English: bonjour'), (object(), 'x' * 4001)]
        for memory, text in cases:
            with self.subTest(length=len(text)), \
                 patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='enforce')), \
                 patch.object(main, 'MEMORY', memory), \
                 patch.object(main, 'detect_legacy_memory_recall_intent', AsyncMock(return_value=main.MemoryRecallIntent(False))), \
                 patch.object(main, 'classify_recall_intent', AsyncMock()) as classifier, \
                 patch.object(main, 'system_event'):
                self.assertFalse(asyncio.run(main.detect_memory_recall_intent(self.message, text)).is_recall)
                classifier.assert_not_awaited()

    def test_double_provider_failure_preserves_legacy_degraded_fallback(self):
        with patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='enforce')), \
             patch.object(main, 'memory_recall_embedding_confidence', AsyncMock(side_effect=RuntimeError('unavailable'))) as embed, \
             patch.object(main, 'classify_recall_intent', AsyncMock(return_value=model_result(None, 'TimeoutError'))) as classifier, \
             patch.object(main, 'system_event'):
            result = asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text))
        embed.assert_awaited_once()
        classifier.assert_awaited_once()
        self.assertEqual(main.memory_recall_fallback_intent(self.message.text, reason='embedding_failed'), result)
        self.assertTrue(result.degraded)

    def test_cancellation_propagates_and_finishes_telemetry_without_fallback(self):
        stage = object()
        with patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode='enforce')), \
             patch.object(main, 'detect_legacy_memory_recall_intent', AsyncMock()) as baseline, \
             patch.object(main, 'classify_recall_intent', AsyncMock(side_effect=asyncio.CancelledError)), \
             patch.object(main, 'begin_model_stage', return_value=stage), \
             patch.object(main, 'finish_model_stage') as finish:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(main.detect_memory_recall_intent(self.message, self.message.text))
        baseline.assert_not_awaited()
        self.assertEqual('cancelled', finish.call_args.kwargs['status'])

    def test_transport_receives_only_current_instruction_and_presence_flags_and_accounts_usage(self):
        self.message.reply_to_message = SimpleNamespace(text='PRIVATE REPLY', caption=None)
        self.message.quote = SimpleNamespace(text='PRIVATE QUOTE')
        with patch.object(main, 'classify_recall_intent', AsyncMock(return_value=model_result())) as classifier, \
             patch.object(main, 'begin_model_stage', return_value=object()), \
             patch.object(main, 'finish_model_stage') as finish:
            asyncio.run(main.run_recall_intent_classifier(self.message, self.message.text))
        metadata = classifier.call_args.args[0]
        self.assertEqual({'trusted_text', 'has_reply_text', 'has_reply_image'}, set(metadata))
        self.assertTrue(metadata['has_reply_text'])
        self.assertNotIn('PRIVATE', repr(metadata))
        self.assertEqual(10, finish.call_args.kwargs['usage']['input_tokens'])
        self.assertEqual(MODEL, finish.call_args.kwargs['actual_model'])

    def test_ordinary_group_message_never_reaches_candidate_or_model(self):
        message = FakeMessage('Do you remember our previous choice?', message_id=99111)
        context = SimpleNamespace(bot=SimpleNamespace(id=999, username='evaluation_bot'))
        for mode in ('off', 'shadow', 'enforce'):
            with self.subTest(mode=mode), \
                 patch.object(main, 'CONFIG', replace(self.config, memory_recall_policy_mode=mode)), \
                 patch.object(main, 'BOT_ID', main.BOT_ID), \
                 patch.object(main, 'BOT_USERNAME', main.BOT_USERNAME), \
                 patch.object(main, 'remember_message_persistently', AsyncMock()), \
                 patch.object(main, 'handle_pending_or_observe', AsyncMock(return_value=False)), \
                 patch.object(main, 'handle_prompt', AsyncMock()) as handle, \
                 patch.object(main, 'classify_recall_intent', AsyncMock()) as classifier, \
                 patch.object(main, 'create_embeddings', AsyncMock()) as embed:
                asyncio.run(main.text_message(SimpleNamespace(effective_message=message), context))
                handle.assert_not_awaited()
                classifier.assert_not_awaited()
                embed.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
