from __future__ import annotations
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from embedding_dimensions_eval import Budget, source_metrics, paired_family_interval, validate_source_probe
from scripts.eval_embedding_dimensions import mini_json


class EmbeddingDimensionEvaluationTests(unittest.TestCase):
    def test_total_budget_refusal_does_not_count_undispatched_generation_call(self):
        generation, total = Budget(.2), Budget(.00001)
        before = [vars(account).copy() for account in (generation, total)]
        provider = SimpleNamespace(responses=SimpleNamespace(create=Mock()))
        with self.assertRaises(ValueError):
            mini_json(provider, "synthetic prompt", {"TARGET": "synthetic source"}, generation, total)
        self.assertEqual([vars(account) for account in (generation, total)], before)
        provider.responses.create.assert_not_called()

    def test_joint_budget_admission_counts_once_in_each_account(self):
        generation, total = Budget(.2), Budget(.5)
        Budget.reserve_all((generation, total), .1)
        for account in (generation, total):
            self.assertEqual(account.calls, 1)
            self.assertEqual(account.reserved, .1)

    def test_usage_above_reservation_is_charged_and_stops_budget(self):
        budget = Budget(.5)
        budget.reserve(.1)
        with self.assertRaises(ValueError):
            budget.settle(.1, .2)
        self.assertEqual(budget.actual, .2)
        with self.assertRaises(ValueError):
            budget.reserve(.01)

    def test_unknown_provider_calls_remain_reserved(self):
        budget = Budget(.5)
        budget.reserve(.3)
        budget.settle(.3)
        with self.assertRaises(ValueError):
            budget.reserve(.21)
        budget.reserve(.2)
        budget.settle(.2, .1)
        self.assertAlmostEqual(budget.actual + budget.unknown, .4)

    def test_source_hit_allows_prelabelled_duplicate_sources(self):
        metrics = source_metrics([9, 4, 8], [3, 4])
        self.assertEqual(metrics["source_hit_at_1"], 0)
        self.assertEqual(metrics["source_hit_at_6"], 1)
        self.assertEqual(metrics["reciprocal_rank"], .5)

    def test_bootstrap_collapses_correlated_variants(self):
        rows = [{"family": "a", "512": {"hit": 0}, "1536": {"hit": 1}}] * 10
        rows += [{"family": "b", "512": {"hit": 1}, "1536": {"hit": 0}}]
        result = paired_family_interval(rows, "hit")
        self.assertEqual(result["families"], 2)
        self.assertEqual(result["delta"], 0)

    def test_fabricated_and_leaked_answers_are_rejected(self):
        source = "Поезд отправляется в восемь часов."
        self.assertEqual(validate_source_probe({"eligible": True, "question": "Во сколько поезд?", "answer_span": "девять"}, source), "answer_not_verbatim")
        self.assertEqual(validate_source_probe({"eligible": True, "question": "Поезд в восемь часов?", "answer_span": "восемь часов"}, source), "answer_leaked")
        self.assertEqual(validate_source_probe({"eligible": True, "question": "Во сколько поезд?", "answer_span": "восемь часов"}, source), "")


if __name__ == "__main__":
    unittest.main()
