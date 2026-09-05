import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import recall_intent_model as policy
from runtime_model_pricing import MODEL_TOKEN_PRICES


def response(**changes):
    values = {"model": policy.MODEL, "status": "completed", "reasoning": SimpleNamespace(effort="none"),
              "output_text": '{"intent":"prior_conversation"}',
              "usage": SimpleNamespace(input_tokens=100, output_tokens=20,
                  input_tokens_details=SimpleNamespace(cached_tokens=50, cache_write_tokens=10),
                  output_tokens_details=SimpleNamespace(reasoning_tokens=7))}
    values.update(changes)
    return SimpleNamespace(**values)


class FakeClient:
    def __init__(self, create):
        self.responses = SimpleNamespace(create=create)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class RecallIntentModelTests(unittest.IsolatedAsyncioTestCase):
    async def classify(self, *, result=None, error=None, metadata=None):
        create = AsyncMock(return_value=result or response(), side_effect=error)
        factory = Mock(return_value=FakeClient(create))
        observed = await policy.classify_recall_intent(
            metadata or policy.recall_model_metadata("What did we previously agree?"),
            api_key="synthetic-key", client_factory=factory)
        return observed, factory, create

    async def test_actual_transport_contract_is_bounded_and_provider_storage_disabled(self):
        metadata = policy.recall_model_metadata("What did we previously agree?", has_reply_text=True)
        result, factory, create = await self.classify(metadata=metadata)
        factory.assert_called_once_with(api_key="synthetic-key", timeout=8.0, max_retries=0,
                                        base_url="https://api.openai.com/v1")
        create.assert_awaited_once_with(**policy.recall_model_request(metadata))
        request = create.call_args.kwargs
        self.assertFalse(request["store"])
        self.assertEqual({"effort": "none"}, request["reasoning"])
        self.assertEqual(240, request["max_output_tokens"])
        self.assertNotIn("tools", request)
        self.assertEqual("succeeded", result.status)
        self.assertTrue(result.is_recall)

    async def test_billable_invalid_json_keeps_actual_usage_and_cost(self):
        result, _, _ = await self.classify(result=response(output_text="not json"))
        self.assertIsNone(result.intent)
        self.assertEqual("InvalidStructuredResponse", result.failure_class)
        self.assertEqual(100, result.usage.input_tokens)
        # Cached/write inputs are disjoint parts of input; reasoning is already output.
        self.assertEqual(35_500, result.cost["nano_usd"])
        self.assertTrue(result.cost["complete"])

    async def test_incomplete_response_cannot_admit_even_if_json_is_valid(self):
        result, _, _ = await self.classify(result=response(status="incomplete"))
        self.assertIsNone(result.is_recall)
        self.assertEqual("ProviderNotCompleted", result.failure_class)
        self.assertEqual(35_500, result.cost["nano_usd"])

    async def test_model_or_reasoning_mismatch_is_an_explicit_abort_signal(self):
        for change in ({"model": "gpt-5.6-terra"}, {"reasoning": SimpleNamespace(effort="low")}):
            with self.subTest(change=change):
                result, _, _ = await self.classify(result=response(**change))
                self.assertEqual("ProviderIdentityMismatch", result.failure_class)
                self.assertIsNone(result.intent)
                self.assertTrue(result.cost["complete"])

    async def test_provider_failure_is_single_attempt_without_raw_exception_text(self):
        result, _, create = await self.classify(error=TimeoutError("private provider detail"))
        self.assertEqual("TimeoutError", result.failure_class)
        self.assertIsNone(result.cost["nano_usd"])
        self.assertFalse(result.cost["complete"])
        self.assertNotIn("private provider detail", repr(result))
        self.assertEqual(1, create.await_count)

    async def test_outer_deadline_covers_the_awaited_provider_call(self):
        async def delayed(**kwargs):
            await asyncio.sleep(1)
            return response()
        factory = Mock(return_value=FakeClient(delayed))
        with patch.object(policy, "TIMEOUT_SECONDS", .005):
            result = await policy.classify_recall_intent(
                policy.recall_model_metadata("synthetic request"), api_key="synthetic-key", client_factory=factory)
        self.assertEqual("TimeoutError", result.failure_class)
        self.assertLess(result.latency_ms, 500)

    async def test_missing_usage_is_unknown_even_with_a_valid_classification(self):
        result, _, _ = await self.classify(result=response(usage=None))
        self.assertTrue(result.is_recall)
        self.assertFalse(result.cost["complete"])
        self.assertIsNone(result.cost["nano_usd"])

    def test_parser_rejects_extra_fields_duplicates_and_unknown_intents(self):
        for raw in ('{"intent":"unknown"}', '{"intent":"prior_conversation","confidence":1}',
                    '{"intent":"general_request","intent":"prior_conversation"}', '[]', 'null'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                policy.parse_recall_intent(raw)
        self.assertEqual("current_input", policy.parse_recall_intent('{"intent":"current_input"}'))

    def test_metadata_rejects_history_payload_and_non_boolean_flags(self):
        with self.assertRaisesRegex(ValueError, "unexpected_recall_metadata"):
            policy.recall_model_request({**policy.recall_model_metadata("synthetic"), "reply_text": "source"})
        for text in ("", "x" * 4001):
            with self.assertRaises(ValueError):
                policy.recall_model_metadata(text)
        with self.assertRaises(ValueError):
            policy.recall_model_metadata("synthetic", has_reply_text="true")

    def test_reservation_uses_current_write_price_and_full_output_cap(self):
        metadata = policy.recall_model_metadata("Синтетичне запитання")
        price = MODEL_TOKEN_PRICES[policy.MODEL]
        reserve = policy.reservation_nano_usd(metadata)
        self.assertEqual(200, price.input_nano_usd)
        self.assertEqual(250, price.cache_write_nano_usd)
        self.assertEqual(1200, price.output_nano_usd)
        self.assertGreater(reserve, 2048 * 250 + 240 * 1200)


if __name__ == "__main__":
    unittest.main()
