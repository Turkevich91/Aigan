"""Budget and production retry parity checks; all provider calls are synthetic."""
import asyncio
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests.support import configure_test_environment
configure_test_environment()
import main as app
from scripts import eval_auxiliary_models as evaluation
from tests.test_image_intent import frame, authorization_payload


class BudgetTests(unittest.TestCase):
    def test_unknown_usage_holds_full_reservation(self):
        budget = evaluation.Budget(.01)
        budget.reserve(8_000_000)
        budget.settle(8_000_000, None)
        with self.assertRaises(evaluation.BudgetExceeded):
            budget.reserve(2_000_001)
        self.assertEqual(budget.unknown, 8_000_000)
        self.assertEqual(budget.known, 0)

    def test_success_releases_only_unused_reservation(self):
        budget = evaluation.Budget(.01)
        budget.reserve(8_000_000)
        budget.settle(8_000_000, 1_000_000)
        budget.reserve(9_000_000)
        self.assertEqual(budget.known + budget.inflight, budget.limit)

    def test_paired_bootstrap_keeps_repeats_in_case_clusters(self):
        rows = []
        for model in ("gpt-5.4-mini", "gpt-5.6-luna"):
            for cid, baseline, candidate in (("a", False, True), ("b", True, True)):
                for repetition in range(2):
                    rows.append({"role": "image_intent", "model": model, "case_id": cid,
                                 "repeat": repetition, "semantic_pass": baseline if model == "gpt-5.4-mini" else candidate})
        result = evaluation.paired_bootstrap(rows, "image_intent", "gpt-5.4-mini", "gpt-5.6-luna", samples=100)
        self.assertEqual(result["delta"], .5)
        self.assertEqual(result["unit"], "paired_case_cluster_including_all_repeats")

    def test_bootstrap_excludes_incomplete_pairs(self):
        rows = [{"role": "image_intent", "model": model, "case_id": "a", "repeat": repeat, "semantic_pass": True}
                for model, repeat in (("gpt-5.4-mini", 0), ("gpt-5.4-mini", 1), ("gpt-5.6-luna", 0))]
        self.assertEqual(evaluation.paired_bootstrap(rows, "image_intent", "gpt-5.4-mini", "gpt-5.6-luna", samples=100), {})


class RuntimeParityTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, role, case, responses):
        calls = []
        class FakeClient:
            def __init__(self, **kwargs):
                self.responses = self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return None
            async def create(self, **request):
                calls.append(request)
                value = responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                return SimpleNamespace(model=request["model"], status="completed", output=[],
                    error=None, incomplete_details=None, reasoning=SimpleNamespace(effort="none"),
                    output_text=value if isinstance(value, str) else json.dumps(value),
                    usage=SimpleNamespace(input_tokens=100, output_tokens=40,
                        input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=0)))
        names = ("CONFIG", "AsyncOpenAI", "MODEL_TELEMETRY", "begin_model_stage", "finish_model_stage",
                 "system_event", "evaluate_image_intent", "evaluate_image_operation_authorization", "detect_memory_recall_intent")
        originals = {name: getattr(app, name) for name in names}
        budget = evaluation.Budget(4.5)
        try:
            with patch("openai.AsyncOpenAI", FakeClient):
                evaluation.install_recorder(app, budget)
            app.CONFIG = evaluation.ConfigProxy()
            app.MODEL_TELEMETRY = None
            app.begin_model_stage = lambda **kwargs: None
            app.finish_model_stage = lambda *args, **kwargs: None
            app.system_event = lambda **kwargs: None
            evaluation.install_image_tracking(app)
            row = await evaluation.evaluate(app, originals["CONFIG"], (role, "gpt-5.6-luna", 0, case), asyncio.Semaphore(2), budget)
            return row, calls, budget
        finally:
            for name, original in originals.items():
                setattr(app, name, original)

    async def test_actual_image_application_retries_then_authorizes(self):
        case = evaluation.image_eval.CASES[0]
        row, calls, budget = await self.run_case("image_intent", case, [
            frame(confidence=.3), frame(), authorization_payload(case.prompt, subject_text=case.subject)])
        self.assertNotIn("evaluation_failure", row)
        self.assertTrue(row["overall_pass"])
        self.assertFalse(row["first_attempt_semantic_pass"])
        self.assertTrue(row["semantic_pass"])
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["model"] for c in calls], ["gpt-5.6-luna", "gpt-5.6-luna", "gpt-5.6-terra"])
        self.assertTrue(all(c["store"] is False for c in calls))
        self.assertGreater(budget.known, 0)

    async def test_invalid_json_is_billed_before_runtime_fallback(self):
        case = evaluation.routing_eval.fixture_cases(evaluation.routing_eval.DEFAULT_FIXTURE)[0]
        row, calls, budget = await self.run_case("model_policy", case, ["{"])
        self.assertFalse(row["structured_valid"])
        self.assertTrue(row["fallback"])
        self.assertEqual(len(calls), 1)
        self.assertGreater(budget.known, 0)
        self.assertEqual(budget.unknown, 0)
        self.assertTrue(row["attempts"][0]["payload_invalid"])

    async def test_image_provider_failures_keep_two_unknown_reservations(self):
        row, calls, budget = await self.run_case("image_intent", evaluation.image_eval.CASES[0],
                                                [TimeoutError(), TimeoutError()])
        self.assertEqual(len(calls), 2)
        self.assertEqual(budget.known, 0)
        self.assertEqual(budget.unknown, sum(a["reserved_nano_usd"] for a in row["attempts"]))
        self.assertEqual(row["route"], "image_intent_clarify")
        self.assertFalse(row["semantic_pass"])


if __name__ == "__main__":
    unittest.main()
