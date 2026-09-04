from __future__ import annotations
import unittest
from embedding_recall_eval import summarize


class EmbeddingRecallEvaluationTests(unittest.TestCase):
    def test_paired_changes_separate_improvement_regression_and_failures(self):
        rows = [
            {"id":"a","dimensions":512,"expected_recall":True,"observed_recall":False,"degraded":False},
            {"id":"a","dimensions":1536,"expected_recall":True,"observed_recall":True,"degraded":False},
            {"id":"b","dimensions":512,"expected_recall":False,"observed_recall":False,"degraded":False},
            {"id":"b","dimensions":1536,"expected_recall":False,"observed_recall":True,"degraded":False},
            {"id":"c","dimensions":512,"expected_recall":False,"observed_recall":True,"degraded":True},
            {"id":"c","dimensions":1536,"expected_recall":False,"observed_recall":False,"degraded":False},
        ]
        result = summarize(rows)
        self.assertEqual(result["paired"], {"complete_cases":2,"changed_decisions":2,
                                          "candidate_improved":1,"candidate_regressed":1})
        self.assertEqual(result["512"]["failures"],1)
        self.assertEqual(result["512"]["false_positive"],0)
        self.assertEqual(result["1536"]["false_positive"],1)


if __name__ == "__main__":
    unittest.main()
