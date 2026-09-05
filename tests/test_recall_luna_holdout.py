import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from recall_admission_eval import EvaluationState, digest
from recall_intent_model import MODEL, RecallModelResult, recall_model_metadata
from runtime_model_pricing import TokenUsage, estimate_token_cost
from scripts import eval_recall_luna_holdout as evaluation


def result(intent="prior_conversation", *, failure=None, known=True, latency=100.):
    usage = TokenUsage(100, output_tokens=20) if known else TokenUsage(None)
    return RecallModelResult(intent, "failed" if failure else "succeeded", "completed" if not failure else None,
                             MODEL, "none", usage, asdict(estimate_token_cost(MODEL, [usage])), latency, failure)


def complete_evidence():
    rows, boundaries = [], []
    for rep in range(3):
        for index in range(72):
            positive = index < 36
            model = asdict(result("prior_conversation" if positive else "general_request"))
            rows.append({"case_id": f"case-{index}", "family_id": f"family-{index // 3}",
                         "language": ("ua", "ru", "en")[index % 3], "expected": positive,
                         "critical_negative": 36 <= index < 51, "repetition": rep,
                         "baseline": {"is_recall": False, "degraded": False},
                         "candidate": {"is_recall": positive, "degraded": False,
                                       "adapter_wall_ms": 110., "reason": "luna_" + model["intent"]},
                         "administrative_abort": False,
                         "attempts": [{"attempt_id": f"attempt-{rep}-{index}", "reserved_nano_usd": 100_000,
                                       "model": model, "observer_wall_ms": 101., "administrative_abort": False}]})
        boundaries.extend({"case_id": f"boundary-{index}", "repetition": rep, "passed": True}
                          for index in range(12))
    return rows, boundaries


class HoldoutScoringTests(unittest.TestCase):
    def summarize(self, rows, boundaries):
        return evaluation.summarize(rows, boundaries, bootstrap_repetitions=20)

    def test_fixed_schedule_has_exactly_three_complete_repetitions(self):
        self.assertEqual(216, len(evaluation.SCHEDULE))
        self.assertEqual(evaluation.SCHEDULE_SHA, digest(evaluation.SCHEDULE))
        for rep in range(3):
            self.assertEqual(set(range(72)), {row["case_index"] for row in evaluation.SCHEDULE if row["repetition"] == rep})

    def test_every_repetition_and_stability_pass_on_complete_evidence(self):
        rows, boundaries = complete_evidence()
        report = self.summarize(rows, boundaries)
        self.assertTrue(report["gate_pass"])
        self.assertEqual(72, report["stability"]["stable_cases"])
        for rep in report["per_repetition"].values():
            self.assertEqual(36, rep["candidate"]["tp"])
            self.assertEqual(0, rep["candidate"]["fp"])
            self.assertEqual(72, rep["classifier_attempts"])
            self.assertEqual(24, rep["paired_family_bootstrap"]["families"])

    def test_one_failed_repetition_cannot_be_rescued_by_majority(self):
        rows, boundaries = complete_evidence()
        for row in rows[72:76]:
            row["candidate"]["is_recall"] = False
        report = self.summarize(rows, boundaries)
        self.assertFalse(report["gate_pass"])
        self.assertFalse(report["per_repetition"]["1"]["gate_pass"])
        self.assertEqual(68, report["stability"]["stable_cases"])
        self.assertEqual(4, len(report["stability"]["decision_flips"]))

    def test_missing_and_duplicate_case_do_not_make_complete_population(self):
        rows, boundaries = complete_evidence()
        rows[0] = deepcopy(rows[1])
        report = self.summarize(rows, boundaries)
        self.assertFalse(report["gate_pass"])
        self.assertFalse(report["per_repetition"]["0"]["complete"])
        self.assertEqual(1, report["per_repetition"]["0"]["duplicate_cases"])

    def test_billable_invalid_output_is_degraded_even_if_legacy_fallback_is_clean(self):
        rows, boundaries = complete_evidence()
        rows[0]["attempts"][0]["model"] = asdict(result(None, failure="InvalidStructuredResponse"))
        report = self.summarize(rows, boundaries)
        self.assertFalse(report["gate_pass"])
        self.assertEqual(1, report["per_repetition"]["0"]["candidate"]["degraded"])
        self.assertEqual(1, report["per_repetition"]["0"]["invalid_provider_outcomes"])
        self.assertGreater(report["per_repetition"]["0"]["cost"]["known_usd"], 0)

    def test_unknown_failed_attempt_keeps_reserved_cost_and_latency(self):
        rows, boundaries = complete_evidence()
        rows[0]["attempts"][0]["model"] = asdict(result(None, failure="TimeoutError", known=False, latency=8000))
        for row in rows[1:5]:
            row["attempts"][0]["model"]["latency_ms"] = 3500
        report = self.summarize(rows, boundaries)
        rep = report["per_repetition"]["0"]
        self.assertFalse(rep["gate_pass"])
        self.assertEqual(3500, rep["classifier_latency_ms"]["p95"])
        self.assertEqual(.0001, rep["cost"]["unknown_reserved_usd"])

    def test_zero_call_precedence_is_reported_and_not_a_fake_latency_attempt(self):
        rows, boundaries = complete_evidence()
        rows[71]["attempts"] = []
        rows[71]["candidate"]["reason"] = "excluded_route"
        report = self.summarize(rows, boundaries)
        self.assertTrue(report["gate_pass"])
        self.assertEqual(71, report["per_repetition"]["0"]["classifier_attempts"])
        self.assertEqual(1, report["per_repetition"]["0"]["zero_call_decisions"])
        rows[71]["candidate"]["reason"] = "semantic_strong"
        self.assertFalse(self.summarize(rows, boundaries)["gate_pass"])

    def test_empty_latency_population_is_not_a_pass(self):
        rows, boundaries = complete_evidence()
        for row in rows:
            row["attempts"] = []
            row["candidate"]["reason"] = "excluded_route"
        report = self.summarize(rows, boundaries)
        self.assertFalse(report["gate_pass"])
        self.assertIsNone(report["per_repetition"]["0"]["classifier_latency_ms"]["p95"])

    def test_critical_negative_or_boundary_violation_fails(self):
        rows, boundaries = complete_evidence()
        rows[36]["candidate"]["is_recall"] = True
        self.assertFalse(self.summarize(rows, boundaries)["gate_pass"])
        rows, boundaries = complete_evidence()
        boundaries[0]["passed"] = False
        self.assertFalse(self.summarize(rows, boundaries)["gate_pass"])

    def test_aborted_attempt_is_accounted_even_without_adapter_result(self):
        rows, boundaries = complete_evidence()
        rows[0]["candidate"] = None
        rows[0]["administrative_abort"] = True
        report = self.summarize(rows, boundaries)
        rep = report["per_repetition"]["0"]
        self.assertFalse(rep["gate_pass"])
        self.assertEqual(72, rep["classifier_attempts"])
        self.assertEqual(1, rep["administrative_aborts"])


@unittest.skipUnless(os.name == "posix", "authoritative evaluation artifacts use POSIX modes")
class HoldoutBudgetAndCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_refusal_bypasses_application_exception_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = EvaluationState(Path(temporary), cap_nano_usd=100)
            provider = AsyncMock(return_value=result())
            observer = evaluation.BudgetedClassifier(state, api_key="synthetic", classifier=provider)
            fallback_called = False
            async def application():
                nonlocal fallback_called
                try:
                    return await observer(recall_model_metadata("Retrieve the shared decision."))
                except Exception:
                    fallback_called = True
            with self.assertRaises(evaluation.FatalEvaluationAbort):
                await application()
            self.assertFalse(fallback_called)
            provider.assert_not_awaited()
            self.assertEqual(0, state.accounting()["attempts"])

    async def test_invalid_billable_result_charged_before_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = EvaluationState(Path(temporary))
            provider = AsyncMock(return_value=result(None, failure="InvalidStructuredResponse"))
            observer = evaluation.BudgetedClassifier(state, api_key="trial-key", classifier=provider)
            outcome = await observer(recall_model_metadata("Retrieve the shared decision."), api_key="application-test-key")
            self.assertIsNone(outcome.is_recall)
            self.assertEqual(.000044, state.accounting()["known_usd"])
            self.assertEqual("trial-key", provider.call_args.kwargs["api_key"])
            self.assertEqual(1, len(observer.attempts))

    async def test_cancel_retains_unknown_reservation_and_prevents_another_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = EvaluationState(Path(temporary))
            provider = AsyncMock(side_effect=asyncio.CancelledError())
            observer = evaluation.BudgetedClassifier(state, api_key="synthetic", classifier=provider)
            for _ in range(2):
                with self.assertRaises(evaluation.FatalEvaluationAbort):
                    await observer(recall_model_metadata("Retrieve the shared decision."))
            provider.assert_awaited_once()
            self.assertEqual({"unknown": 1}, state.accounting()["status_counts"])
            self.assertTrue(observer.attempts[0]["administrative_abort"])
            self.assertGreater(state.accounting()["unknown_reserved_usd"], 0)

    async def test_actual_over_reservation_remains_charged_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = EvaluationState(Path(temporary))
            outcome = result()
            outcome.cost["nano_usd"] = 2_000_000_000
            observer = evaluation.BudgetedClassifier(state, api_key="synthetic", classifier=AsyncMock(return_value=outcome))
            with self.assertRaises(evaluation.FatalEvaluationAbort):
                await observer(recall_model_metadata("Retrieve the shared decision."))
            self.assertEqual(2., state.accounting()["known_usd"])
            self.assertEqual({"known": 1}, state.accounting()["status_counts"])

    def test_claim_is_exclusive_and_retained_before_a_payload_parse_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = EvaluationState(Path(temporary))
            bindings = {**state.identity(), "fixture_sha256": "a" * 64,
                        "experiment_schedule_sha256": evaluation.SCHEDULE_SHA}
            authorization = {**bindings, "schema_version": "recall179-holdout-authorization-v2",
                             "authorized": True, "scope": "one_three_repetition_holdout_experiment_no_tuning",
                             "nonce": "synthetic-nonce"}
            claim = evaluation.claim_experiment(state.directory, authorization, bindings)
            with self.assertRaises(json.JSONDecodeError):
                json.loads("invalid closed payload")
            self.assertTrue(claim.exists())
            self.assertEqual(0o600, claim.stat().st_mode & 0o777)
            with self.assertRaises(FileExistsError):
                evaluation.claim_experiment(state.directory, authorization, bindings)

    def test_changed_schedule_or_state_identity_rejected_before_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = EvaluationState(Path(temporary))
            bindings = {**state.identity(), "fixture_sha256": "b" * 64,
                        "experiment_schedule_sha256": evaluation.SCHEDULE_SHA}
            authorization = {**bindings, "schema_version": "recall179-holdout-authorization-v2", "authorized": True,
                             "scope": "one_three_repetition_holdout_experiment_no_tuning", "nonce": "synthetic"}
            authorization["experiment_schedule_sha256"] = "changed"
            with self.assertRaisesRegex(ValueError, "binding_mismatch"):
                evaluation.claim_experiment(state.directory, authorization, bindings)
            self.assertFalse(list(state.directory.glob("*.claim.json")))

    def test_source_drift_stops_before_accessing_fixture_or_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            frozen = Path(temporary) / "freeze.json"
            frozen.write_text(json.dumps({"sources": {"old": "hash"}}))
            args = SimpleNamespace(allow_provider=True, freeze=frozen)
            with patch.dict(os.environ, {"AIGAN_EVAL_API_KEY": "synthetic"}), \
                 patch.object(evaluation, "source_maps", return_value={"new": "hash"}):
                with self.assertRaisesRegex(ValueError, "source_runtime_or_schedule_changed"):
                    evaluation.run(args)


class HoldoutBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_application_configuration_binds_standard_size_rate_probes(self):
        from tests.support import configure_test_environment
        configure_test_environment()
        import main
        with patch.object(main, "CONFIG", replace(main.CONFIG, memory_recall_policy_mode="enforce")):
            bound = evaluation.configuration({"baseline": main, "candidate": main})
        self.assertEqual("enforce", bound["candidate"]["policy_mode"])
        self.assertEqual(evaluation.PROTOCOL, bound["candidate"]["classifier"]["protocol"])
        self.assertEqual({"input": .2, "output": 1.2},
                         bound["candidate"]["classifier"]["protocol"]["luna_rates_usd_per_million"])
        self.assertEqual(64, len(bound["candidate"]["classifier"]["request_semantics_sha256"]))

    async def test_uninvoked_group_retains_actual_pending_observation_seam(self):
        from tests.support import configure_test_environment
        configure_test_environment()
        import main
        case = {"case_id": "synthetic-boundary", "prompt": "We discussed the earlier choice.",
                "context": {"chat_type": "group", "invoked": False, "has_reply_text": False,
                            "has_reply_image": False, "reply_to_bot": False},
                "expected": {"invocation_eligible": False, "recall_stage_calls": 0,
                             "classifier_calls": 0, "provider_calls": 0, "telegram_deliveries": 0}}
        with patch.object(main, "BOT_ID", 999), patch.object(main, "BOT_USERNAME", "evaluation_bot"), \
             patch.object(main, "CONFIG", replace(main.CONFIG, bot_username="evaluation_bot", bot_trigger="!m")), \
             patch.object(main, "handle_pending_or_observe", wraps=main.handle_pending_or_observe) as pending:
            outcome = await evaluation.boundary_outcome(main, case, 0)
        pending.assert_awaited_once()
        self.assertTrue(outcome["passed"])
        self.assertEqual(0, outcome["observed"]["classifier_calls"])
        self.assertEqual(0, outcome["observed"]["provider_calls"])


if __name__ == "__main__":
    unittest.main()
