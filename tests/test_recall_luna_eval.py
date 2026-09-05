import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from recall_admission_eval import BudgetRefused, EvaluationState
from recall_intent_model import MODEL, RecallModelResult, recall_model_metadata
from runtime_model_pricing import TokenUsage, estimate_token_cost
from scripts import eval_recall_luna as evaluation


def case(identity="synthetic-case", expected=True):
    return {"case_id": identity, "family_id": identity + "-family", "block": "development", "language": "en",
            "expected": expected, "critical_negative": not expected,
            "metadata": recall_model_metadata("Retrieve the earlier shared decision."),
            "baseline": {"is_recall": False, "degraded": False},
            "lexical_v1": {"is_recall": False, "degraded": False}}


def result(*, intent="prior_conversation", failure=None, known=True):
    usage = TokenUsage(100, cached_input_tokens=0, cache_write_tokens=0, output_tokens=20) if known else TokenUsage(None)
    return RecallModelResult(intent, "failed" if failure else "succeeded", "completed" if not failure else None,
                             MODEL, "none", usage, asdict(estimate_token_cost(MODEL, [usage])), 25., failure)


class RecallLunaEvidenceTests(unittest.TestCase):
    def test_schedule_contains_exactly_two_repetitions_and_is_reproducible(self):
        cases = [case("one"), case("two")]
        first = [(row["case_id"], rep) for row, rep in evaluation.schedule(cases)]
        second = [(row["case_id"], rep) for row, rep in evaluation.schedule(cases)]
        self.assertEqual(first, second)
        self.assertEqual({("one", 0), ("one", 1), ("two", 0), ("two", 1)}, set(first))

    def test_partial_duplicate_or_administratively_aborted_pairs_are_not_scored(self):
        rows = []
        for cid, reps in (("full", (0, 1)), ("partial", (0,)), ("duplicate", (0, 0))):
            for rep in reps:
                rows.append(evaluation.result_row(case(cid), rep, result(), attempt_id="synthetic", reserved=100_000))
        rows.append(evaluation.result_row(case("aborted"), 0, result(), attempt_id="synthetic", reserved=100_000))
        aborted = evaluation.result_row(case("aborted"), 1, result(), attempt_id="synthetic", reserved=100_000)
        aborted["administrative_abort"] = True
        rows.append(aborted)
        self.assertEqual({"full"}, {row["case_id"] for row in evaluation.complete_rows(rows)})

    def test_unknown_classifier_is_distinct_from_legacy_fallback(self):
        row = evaluation.result_row(case(expected=False), 0, result(intent=None, failure="TimeoutError", known=False),
                                    attempt_id="synthetic", reserved=123_000)
        counts = evaluation.metrics([row])
        self.assertEqual(0, counts["classifier"]["scored"])
        self.assertEqual(1, counts["classifier"]["missing"])
        self.assertEqual(1, counts["candidate"]["tn"])
        self.assertEqual(1, counts["candidate"]["degraded"])

    def test_aggregate_cost_keeps_unscored_administrative_attempts(self):
        row = evaluation.result_row(case(), 0, result(), attempt_id="synthetic", reserved=100_000)
        row["administrative_abort"] = True
        summary = evaluation.summarize([row])
        self.assertEqual(0, summary["paired_scored_decisions"])
        self.assertEqual(1, summary["trial_cost"]["attempts"])
        self.assertEqual(44_000 / 1e9, summary["trial_cost"]["known_usd"])

    def test_unknown_attempt_retains_reservation_in_report(self):
        row = evaluation.result_row(case(), 0, result(intent=None, failure="TimeoutError", known=False),
                                    attempt_id="synthetic", reserved=123_000)
        self.assertEqual(123_000 / 1e9, evaluation.summarize([row])["trial_cost"]["unknown_reserved_usd"])


@unittest.skipUnless(os.name == "posix", "authoritative private artifacts use POSIX modes")
class RecallLunaBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_refusal_prevents_provider_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = EvaluationState(directory, cap_nano_usd=100)
            output = evaluation.prepare_output(directory / "results")
            classifier = AsyncMock(return_value=result())
            with self.assertRaises(BudgetRefused):
                await evaluation.evaluate([case()], state, output, [], api_key="synthetic", classifier=classifier)
            classifier.assert_not_awaited()
            self.assertEqual(0, state.accounting()["attempts"])

    async def test_invalid_billable_result_is_settled_before_fallback_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = EvaluationState(directory)
            output = evaluation.prepare_output(directory / "results")
            classifier = AsyncMock(return_value=result(intent=None, failure="InvalidStructuredResponse"))
            rows = []
            await evaluation.evaluate([case()], state, output, rows, api_key="synthetic", classifier=classifier)
            self.assertEqual(2, classifier.await_count)
            self.assertEqual(88_000 / 1e9, state.accounting()["known_usd"])
            self.assertEqual(2, len(rows))
            self.assertTrue(all(row["candidate"]["degraded"] for row in rows))
            self.assertEqual(0o600, (output / "private-outcomes.jsonl").stat().st_mode & 0o777)
            self.assertEqual(0o700, output.stat().st_mode & 0o777)

    async def test_interrupted_attempt_settles_unknown_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = EvaluationState(directory)
            output = evaluation.prepare_output(directory / "results")
            classifier = AsyncMock(side_effect=asyncio.CancelledError())
            rows = []
            with self.assertRaises(asyncio.CancelledError):
                await evaluation.evaluate([case()], state, output, rows, api_key="synthetic", classifier=classifier)
            self.assertEqual({"unknown": 1}, state.accounting()["status_counts"])
            self.assertEqual(1, classifier.await_count)
            summary = evaluation.summarize(rows)
            self.assertEqual(1, summary["administrative_abort_decisions"])
            self.assertEqual(1, summary["trial_cost"]["attempts"])
            self.assertEqual(state.accounting()["unknown_reserved_usd"], summary["trial_cost"]["unknown_reserved_usd"])

    def test_existing_output_and_symlink_are_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = evaluation.prepare_output(directory / "results")
            (output / "existing").write_text("keep")
            with self.assertRaisesRegex(ValueError, "must_be_empty"):
                evaluation.prepare_output(output)
            alias = directory / "alias"
            alias.symlink_to(output, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                evaluation.prepare_output(alias)
            self.assertEqual("keep", (output / "existing").read_text())


if __name__ == "__main__":
    unittest.main()
