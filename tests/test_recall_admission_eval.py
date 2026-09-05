"""Offline correctness checks for evaluation admission, evidence and accounting."""
import importlib.util
import json
import shutil
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recall_admission_eval import (
    BudgetRefused, CachedEmbeddingProvider, EvaluationState, MODEL, claim_holdout,
    confusion, digest, paired_case_outcomes, paired_intervals, provider_input, state_identity, vector_key,
)


class EvaluationStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        self.state = EvaluationState(self.path, cap_nano_usd=100_000)

    def tearDown(self):
        self.temporary.cleanup()

    def test_reservation_is_atomic_durable_and_refusal_does_not_add_attempt(self):
        attempt = self.state.reserve(90_000, phase="development", request_hash="hash")
        reopened = EvaluationState(self.path, cap_nano_usd=100_000)
        with self.assertRaises(BudgetRefused):
            reopened.reserve(20_000, phase="fusion", request_hash="other")
        self.assertEqual(1, reopened.accounting()["attempts"])
        self.assertEqual(.00009, reopened.accounting()["unknown_reserved_usd"])
        reopened.settle(attempt, tokens=100)
        reopened.reserve(90_000, phase="fusion", request_hash="other")
        self.assertEqual(2, self.state.accounting()["attempts"])

    def test_pending_attempt_blocks_parallel_dispatch_even_with_remaining_budget(self):
        self.state.reserve(100, phase="development", request_hash="one")
        with self.assertRaises(BudgetRefused):
            self.state.reserve(100, phase="fusion", request_hash="two")
        self.assertEqual(1, self.state.accounting()["attempts"])

    def test_unknown_cost_retains_reserved_amount_and_known_overrun_stops_future_spend(self):
        attempt = self.state.reserve(100, phase="development", request_hash="one")
        self.state.settle(attempt, error_class="TimeoutError")
        self.assertEqual(.0000001, self.state.accounting()["unknown_reserved_usd"])
        attempt = self.state.reserve(100, phase="development", request_hash="two")
        with self.assertRaises(BudgetRefused):
            self.state.settle(attempt, tokens=20)
        self.assertEqual(.0000004, self.state.accounting()["known_usd"])
        with self.assertRaises(BudgetRefused):
            self.state.reserve(10, phase="fusion", request_hash="three")

    def test_separate_provider_cost_keeps_one_shared_cap_and_unknown_reservations(self):
        first = self.state.reserve(90_000, phase="luna_development", request_hash="one")
        self.state.settle(first, tokens=50, cost_nano_usd=80_000)
        self.assertEqual(.00008, self.state.accounting()["known_usd"])
        with self.assertRaises(BudgetRefused):
            self.state.reserve(20_001, phase="fusion", request_hash="two")
        second = self.state.reserve(20_000, phase="luna_development", request_hash="three")
        with self.assertRaisesRegex(ValueError, "invalid_accounted_provider_cost"):
            self.state.settle(second, cost_nano_usd=100)
        self.state.settle(second, error_class="TimeoutError")
        self.assertEqual(.00002, self.state.accounting()["unknown_reserved_usd"])

    def test_existing_cap_cannot_be_silently_raised(self):
        with self.assertRaisesRegex(ValueError, "cap_mismatch"):
            EvaluationState(self.path, cap_nano_usd=200_000)

    def test_successful_embedding_cached_by_exact_normalized_input_and_reused_offline(self):
        client = Mock()
        client.embeddings.create.return_value = SimpleNamespace(
            model=MODEL, usage=SimpleNamespace(total_tokens=8),
            data=[SimpleNamespace(index=0, embedding=[2.] + [0.] * 511)])
        provider = CachedEmbeddingProvider(self.state, phase="development", client=client)
        result = provider.embed(["  sample\n text  ", "sample text"])
        self.assertEqual(result, [[1.] + [0.] * 511] * 2)
        client.embeddings.create.assert_called_once_with(model=MODEL, dimensions=512,
                                                         input=["sample text"], encoding_format="float")
        self.assertEqual(result, CachedEmbeddingProvider(self.state, phase="fusion").embed(["sample text", "sample text"]))
        self.assertEqual(1, self.state.cache_manifest()["count"])

    def test_shape_failure_still_charges_known_usage_and_does_not_cache(self):
        client = Mock()
        client.embeddings.create.return_value = SimpleNamespace(
            model=MODEL, usage=SimpleNamespace(total_tokens=8),
            data=[SimpleNamespace(index=0, embedding=[1.])])
        provider = CachedEmbeddingProvider(self.state, phase="development", client=client)
        with self.assertRaises(ValueError):
            provider.embed(["sample"])
        self.assertEqual(.00000016, self.state.accounting()["known_usd"])
        self.assertEqual(0, self.state.cache_manifest()["count"])
        with self.assertRaises(RuntimeError):
            provider.embed(["other"])
        self.assertEqual(1, client.embeddings.create.call_count)

    def test_timeout_is_accounted_unknown_and_provider_session_stops(self):
        client = Mock()
        client.embeddings.create.side_effect = TimeoutError("private detail")
        provider = CachedEmbeddingProvider(self.state, phase="development", client=client)
        with self.assertRaises(TimeoutError):
            provider.embed(["sample"])
        self.assertEqual({"unknown": 1}, self.state.accounting()["status_counts"])
        self.assertGreater(self.state.accounting()["unknown_reserved_usd"], 0)
        with self.assertRaises(RuntimeError):
            provider.embed(["other"])
        self.assertEqual(1, client.embeddings.create.call_count)

    def test_actual_provider_is_not_called_when_shared_budget_refuses(self):
        self.state.reserve(99_999, phase="fusion", request_hash="occupied")
        provider = CachedEmbeddingProvider(self.state, phase="development", client=Mock())
        with self.assertRaises(BudgetRefused):
            provider.embed(["sample"])
        provider.client.embeddings.create.assert_not_called()
        self.assertEqual(1, self.state.accounting()["attempts"])
        self.assertEqual("BudgetRefused", provider.failure)

    def test_cache_corruption_is_detected_before_use(self):
        attempt = self.state.reserve(100, phase="development", request_hash="one")
        self.state.settle(attempt, tokens=1)
        self.state.put("sample", [1.] + [0.] * 511, attempt)
        with self.state.connect() as db:
            db.execute("UPDATE vectors SET vector_json='[0]'")
        with self.assertRaisesRegex(ValueError, "integrity_failure"):
            self.state.cached("sample")
    def test_state_identity_survives_reopening(self):
        self.assertEqual(self.state.identity(), EvaluationState(self.path, cap_nano_usd=100_000).identity())

    def test_committed_cache_preserves_entries_and_allows_only_declared_additions(self):
        attempt = self.state.reserve(1000, phase="development", request_hash="one")
        self.state.settle(attempt, tokens=1)
        self.state.put("development", [1.] + [0.] * 511, attempt)
        committed = self.state.cache_manifest()
        self.state.put("fusion", [0., 1.] + [0.] * 510, attempt)
        with self.assertRaisesRegex(ValueError, "unapproved_cache_additions"):
            self.state.verify_cache_manifest(committed)
        result = self.state.verify_cache_manifest(committed, allow_additions=True)
        self.assertEqual(1, result["additional_entries"])
        with self.state.connect() as db:
            db.execute("DELETE FROM vectors WHERE key=?", (vector_key("development"),))
        with self.assertRaisesRegex(ValueError, "missing_or_changed"):
            self.state.verify_cache_manifest(committed, allow_additions=True)

    def test_valid_replacement_vector_still_violates_the_committed_cache(self):
        attempt = self.state.reserve(1000, phase="development", request_hash="one")
        self.state.settle(attempt, tokens=1)
        self.state.put("development", [1.] + [0.] * 511, attempt)
        committed = self.state.cache_manifest()
        replacement = [0., 1.] + [0.] * 510
        with self.state.connect() as db:
            db.execute("UPDATE vectors SET vector_json=?,vector_sha256=?",
                       (json.dumps(replacement), digest(replacement)))
        with self.assertRaisesRegex(ValueError, "missing_or_changed"):
            self.state.verify_cache_manifest(committed)

    def test_cache_manifest_rejects_corrupt_vector_bytes_before_claim(self):
        attempt = self.state.reserve(1000, phase="development", request_hash="one")
        self.state.settle(attempt, tokens=1)
        self.state.put("development", [1.] + [0.] * 511, attempt)
        committed = self.state.cache_manifest()
        with self.state.connect() as db:
            db.execute("UPDATE vectors SET vector_json='[0]'")
        with self.assertRaisesRegex(ValueError, "integrity_failure"):
            self.state.verify_cache_manifest(committed)


class EvaluationProtocolTests(unittest.TestCase):
    def test_provider_input_matches_runtime_clip_contract_including_long_input(self):
        self.assertEqual("one two", provider_input(" one\n two "))
        self.assertEqual("x" * 3976 + " [trimmed]", provider_input("x" * 4001))
        self.assertEqual(vector_key("one two"), vector_key("one\n two"))
        self.assertNotEqual(vector_key("one two"), vector_key("One two"))

    def test_holdout_claim_is_bound_and_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            state = EvaluationState(directory)
            bindings = {"fixture_sha256": "a" * 64, "candidate_source_sha256": "candidate", **state.identity()}
            authorization = dict(bindings, schema_version="recall179-holdout-authorization-v1",
                                 authorized=True, scope="one_holdout_run_no_tuning", nonce="nonce")
            with self.assertRaises(ValueError):
                claim_holdout(directory, authorization, dict(bindings, candidate_source_sha256="changed"))
            self.assertEqual([], list(Path(directory).glob("*.claim.json")))
            path = claim_holdout(directory, authorization, bindings)
            self.assertEqual(digest(authorization), json.loads(path.read_text())["authorization_sha256"])
            with self.assertRaises(FileExistsError):
                claim_holdout(directory, authorization, bindings)

    def test_copied_ledger_cannot_reuse_holdout_authorization_in_another_directory(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            state = EvaluationState(first)
            bindings = {"fixture_sha256": "a" * 64, **state.identity()}
            authorization = dict(bindings, schema_version="recall179-holdout-authorization-v1",
                                 authorized=True, scope="one_holdout_run_no_tuning", nonce="nonce")
            claim_holdout(first, authorization, bindings)
            shutil.copyfile(state.path, Path(second) / state.path.name)
            self.assertEqual(bindings["state_ledger_uuid"], state_identity(second)["state_ledger_uuid"])
            with self.assertRaisesRegex(ValueError, "state_binding_mismatch"):
                claim_holdout(second, authorization, bindings)
            self.assertEqual([], list(Path(second).glob("*.claim.json")))

    def test_family_bootstrap_preserves_three_language_clusters_and_class_strata(self):
        rows = []
        for family, expected in (("positive", True), ("negative", False)):
            for language in ("ua", "ru", "en"):
                rows.append({"family_id": family, "language": language, "expected": expected,
                             "critical_negative": not expected,
                             "baseline": {"is_recall": False, "degraded": False},
                             "candidate": {"is_recall": expected, "degraded": False}})
        self.assertIsNone(confusion(rows, "baseline")["precision"])
        self.assertEqual(1., confusion(rows, "candidate")["recall"])
        interval = paired_intervals(rows, repetitions=20)
        self.assertEqual(2, interval["families"])
        self.assertEqual({"low": 1., "high": 1., "defined_repetitions": 20},
                         interval["delta_candidate_minus_baseline"]["recall"])
        self.assertEqual(0, interval["delta_candidate_minus_baseline"]["precision"]["defined_repetitions"])
        self.assertEqual({"low": 0., "high": 0., "defined_repetitions": 20},
                         interval["per_arm"]["baseline"]["recall"])
        self.assertEqual({"low": 1., "high": 1., "defined_repetitions": 20},
                         interval["per_arm"]["candidate"]["precision"])
        self.assertIsNone(interval["per_arm"]["baseline"]["precision"]["low"])
        outcomes = paired_case_outcomes(rows)
        self.assertEqual(3, outcomes["positive"]["candidate_only_correct"])
        self.assertEqual(0, outcomes["positive"]["baseline_only_correct"])
        self.assertEqual(3, outcomes["negative"]["both_correct"])
        self.assertEqual(6, sum(value for key, value in outcomes["all"].items() if key != "pairs"))


class HoldoutPreflightTests(unittest.TestCase):
    def test_effective_config_failure_does_not_claim_or_parse_holdout(self):
        from scripts import eval_recall_admission as driver
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir(mode=0o700)
            state = EvaluationState(state_dir)
            payload = root / "closed.json"
            payload.write_text("not-json: closed payload must not be parsed")
            frozen = {"sources": {}, "protocol": driver.PROTOCOL,
                      "fixture_manifest_sha256": "hash", "protocol_sha256": "hash",
                      "state_identity": state.identity(), "initial_cache_manifest": state.cache_manifest(),
                      "baseline_source_path": "unused", "config": {"expected": True},
                      "fixture_path": str(payload), "fixture_sha256": "hash", "split": "holdout"}
            freeze = root / "freeze.json"
            freeze.write_text(json.dumps(frozen))
            args = SimpleNamespace(output=root / "output", state=state_dir, freeze=freeze, allow_provider=False)
            with patch.object(driver, "source_maps", return_value={}), patch.object(driver, "file_hash", return_value="hash"), \
                 patch.object(driver, "load_apps", return_value={}), patch.object(driver, "close_apps"), \
                 patch.object(driver, "config_binding", return_value={"changed": True}), \
                 patch.object(driver, "claim_holdout") as claim:
                with self.assertRaisesRegex(ValueError, "effective_config_changed"):
                    driver.run(args)
            claim.assert_not_called()
            self.assertEqual([], list(state_dir.glob("*.claim.json")))


if __name__ == "__main__":
    unittest.main()
