"""Provider-free audit of frozen vector inputs and fusion acceptance scoring."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("fusion_eval", Path(__file__).parents[1]/"scripts/eval_memory_fusion.py")
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class FusionEvaluationTests(unittest.TestCase):
    def test_vector_cache_binds_exact_queries_dimensions_and_normalized_vector_bytes(self):
        query = {"query": "synthetic example"}
        vector = [1.] + [0.] * 511
        entry = {"query_sha256": evaluator.text_hash(query["query"]),
                 "provider_input_sha256": evaluator.text_hash(query["query"]),
                 "vector": vector, "vector_sha256": evaluator.digest(vector)}
        payload = {"schema_version": "fusion180-query-vectors-v1", "query_inputs_sha256": "input-hash",
                   "model": evaluator.MODEL, "dimensions": 512,
                   "normalization": evaluator.SETTINGS["query_normalization"], "entries": [entry]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"vectors.json"
            path.write_text(json.dumps(payload))
            self.assertEqual([vector], evaluator.load_vectors(path, [query], "input-hash"))
            entry["vector"][0] = .5
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "integrity_mismatch"):
                evaluator.load_vectors(path, [query], "input-hash")
            with self.assertRaisesRegex(ValueError, "binding_mismatch"):
                evaluator.load_vectors(path, [query], "different-input")

    def test_same_normalized_query_cannot_receive_two_different_vectors(self):
        queries = [{"query": "one two"}, {"query": "one\n two"}]
        entries = []
        for index, query in enumerate(queries):
            vector = [0.] * 512
            vector[index] = 1.
            entries.append({"query_sha256": evaluator.text_hash(query["query"]),
                            "provider_input_sha256": evaluator.text_hash("one two"),
                            "vector": vector, "vector_sha256": evaluator.digest(vector)})
        payload = {"schema_version": "fusion180-query-vectors-v1", "query_inputs_sha256": "input-hash",
                   "model": evaluator.MODEL, "dimensions": 512,
                   "normalization": evaluator.SETTINGS["query_normalization"], "entries": entries}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"vectors.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "different_vectors"):
                evaluator.load_vectors(path, queries, "input-hash")

    def rows_and_timings(self):
        rows, timings = [], []
        for route in evaluator.ROUTES:
            for cohort, count in (("controlled_positive", 48), ("source_derived_machine_checked", 12),
                                  ("constructed_no_answer", 12), ("isolation", 12)):
                for index in range(count):
                    row = {"family": f"{cohort}-{index//2 if cohort=='controlled_positive' else index}",
                           "cohort": cohort, "route": route, "arms": {}}
                    for policy in evaluator.POLICIES:
                        hit = float(policy != "legacy" or cohort != "controlled_positive")
                        row["arms"][policy] = {
                            "metrics": {metric: hit for metric in ("source_hit_at_1", "source_hit_at_6", "source_hit_at_12", "reciprocal_rank")},
                            "forbidden_returned": 0, "duplicate_returned": 0, "provenance_violations": 0,
                            "fallback_reason": "", "applied_policy": policy, "returned": 6,
                            "ranked_ids": [1, 2, 3, 4, 5, 6],
                            "channel_results": {"keyword": 0, "semantic": 6, "fts": 0},
                        }
                    rows.append(row)
            for policy, elapsed in (("legacy", 10.), ("rrf", 11.), ("normalized", 13.)):
                timings.extend({"route": route, "policy": policy, "local_wall_ms": elapsed} for _ in range(10))
        return rows, timings

    def test_practical_latency_gate_selects_only_candidate_meeting_every_bound(self):
        rows, timings = self.rows_and_timings()
        with patch.dict(evaluator.SETTINGS, {"bootstrap_repetitions": 30}):
            result = evaluator.summary(rows, timings)
        self.assertEqual({"rrf": True, "normalized": False}, result["candidate_gates"])
        self.assertEqual("rrf", result["selected_policy"])
        self.assertFalse(result["runtime_promotion"])
        self.assertNotIn("metrics", result["comparisons"]["constructed_no_answer"]["direct"]["arms"]["rrf"])
        self.assertEqual(2., result["performance"]["direct"]["rrf"]["p95_allowance_ms"])

    def test_unexpected_fallback_or_forbidden_source_cannot_masquerade_as_quality_gain(self):
        rows, timings = self.rows_and_timings()
        rows[0]["arms"]["rrf"].update(fallback_reason="invalid_channel", applied_policy="legacy")
        with patch.dict(evaluator.SETTINGS, {"bootstrap_repetitions": 30}):
            result = evaluator.summary(rows, timings)
        self.assertFalse(result["candidate_gates"]["rrf"])
        self.assertEqual("legacy", result["selected_policy"])
        rows[0]["arms"]["rrf"].update(fallback_reason="", applied_policy="rrf", forbidden_returned=1)
        with patch.dict(evaluator.SETTINGS, {"bootstrap_repetitions": 30}):
            self.assertFalse(evaluator.summary(rows, timings)["candidate_gates"]["rrf"])

    def test_bootstrap_counts_families_and_keeps_paired_query_wins(self):
        rows, _ = self.rows_and_timings()
        subset = [row for row in rows if row["route"] == "direct" and row["cohort"] == "controlled_positive"]
        with patch.dict(evaluator.SETTINGS, {"bootstrap_repetitions": 30}):
            interval = evaluator.paired_interval(subset, "rrf", "source_hit_at_6")
        self.assertEqual(24, interval["families"])
        self.assertEqual(48, interval["wins"])
        self.assertEqual(1., interval["lower_95"])
        self.assertEqual(0, interval["losses"])

    def test_silent_applied_policy_mismatch_cannot_pass_candidate_gate(self):
        rows, timings = self.rows_and_timings()
        rows[0]["arms"]["rrf"]["applied_policy"] = "legacy"
        with patch.dict(evaluator.SETTINGS, {"bootstrap_repetitions": 30}):
            result = evaluator.summary(rows, timings)
        self.assertFalse(result["candidate_gates"]["rrf"])
        self.assertEqual(1, result["comparisons"]["controlled_positive"]["direct"]["arms"]["rrf"]["policy_application_violations"])

    def test_numeric_protection_requires_exact_ordered_legacy_parity(self):
        rows, timings = self.rows_and_timings()
        rows[0]["arms"]["rrf"].update(applied_policy="legacy", fallback_reason="numeric_protected",
                                       ranked_ids=[2, 1, 3, 4, 5, 6])
        with patch.dict(evaluator.SETTINGS, {"bootstrap_repetitions": 30}):
            result = evaluator.summary(rows, timings)
        self.assertFalse(result["candidate_gates"]["rrf"])
        self.assertEqual(1, result["comparisons"]["controlled_positive"]["direct"]["arms"]["rrf"]["numeric_protection_violations"])


if __name__ == "__main__":
    unittest.main()
