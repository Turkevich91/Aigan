from __future__ import annotations

from collections import Counter
from contextlib import redirect_stderr
from dataclasses import replace
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from context_selection_eval import (
    ContextSelectionFixtureError,
    SelectorConfig,
    candidate_snapshot_sha256,
    evaluate_context_selection_fixture,
    load_context_selection_fixture,
    paired_top1_comparison,
    score_case,
    select_b0,
    select_b1,
    select_c1,
)
from scripts.build_context_selection_fixture import build_cases


FIXTURE = Path("tests/fixtures/context_selection_contract_v1.jsonl")
MANIFEST = Path("tests/fixtures/context_selection_contract_v1_manifest.json")


class ContextSelectionContractTests(unittest.TestCase):
    def test_public_fixture_is_balanced_contract_evidence_only(self) -> None:
        cases = load_context_selection_fixture(FIXTURE)
        self.assertEqual(60, len(cases))
        self.assertEqual({"public_synthetic"}, {case.corpus_kind for case in cases})
        self.assertEqual({"contract"}, {case.split for case in cases})
        self.assertEqual(
            {
                "correction_stale_guardrail": 10,
                "explicit_reply": 10,
                "knowledge_update": 10,
                "same_question_transform": 10,
                "short_followup": 10,
                "topic_shift_distractors": 10,
            },
            Counter(case.eligibility_class for case in cases),
        )
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertFalse(manifest["efficacy_evidence"])
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), manifest["fixture_sha256"])

    def test_builder_is_deterministic(self) -> None:
        self.assertEqual(build_cases(), build_cases())

    def test_b1_keeps_equal_payloads_from_different_speakers(self) -> None:
        case = load_context_selection_fixture(FIXTURE)[0]
        selection_input = case.selection_input
        first, second, *remaining = selection_input.sources
        second = replace(second, text=first.text, speaker_key="speaker-z")
        selection_input = replace(
            selection_input,
            budget_chars=20_000,
            sources=(first, second, *remaining),
        )
        result = select_b1(selection_input, SelectorConfig(max_sources=20))
        self.assertIn(first.source_id, result.selected_source_ids)
        self.assertIn(second.source_id, result.selected_source_ids)

    def test_b1_budget_keeps_highest_priority_source(self) -> None:
        case = next(
            case
            for case in load_context_selection_fixture(FIXTURE)
            if case.eligibility_class == "explicit_reply"
        )
        expected_anchor = next(iter(case.expected.acceptable_anchor_source_ids))
        selection_input = replace(case.selection_input, budget_chars=220)
        result = select_b1(selection_input, SelectorConfig(max_sources=20))
        self.assertTrue(result.selected_source_ids)
        self.assertEqual(expected_anchor, result.selected_source_ids[0])

    def test_c1_uses_event_only_as_anchor_to_raw_sources(self) -> None:
        case = next(
            case
            for case in load_context_selection_fixture(FIXTURE)
            if case.eligibility_class == "knowledge_update"
            and case.case_id.endswith("-01")
        )
        result = select_c1(case.selection_input, SelectorConfig())
        source_ids = {source.source_id for source in case.selection_input.sources}
        event_ids = {event.event_id for event in case.selection_input.events}
        self.assertTrue(result.selected_event_ids)
        self.assertEqual(1, len(result.selected_event_ids))
        self.assertTrue(set(result.selected_source_ids).issubset(source_ids))
        self.assertFalse(set(result.selected_source_ids) & event_ids)
        self.assertEqual(
            sum(event.amortized_calls for event in case.selection_input.events),
            result.candidate_construction_calls,
        )
        self.assertEqual(
            sum(event.amortized_input_tokens for event in case.selection_input.events),
            result.candidate_construction_input_tokens,
        )

    def test_aggregate_report_never_contains_case_payloads_or_ids(self) -> None:
        cases = load_context_selection_fixture(FIXTURE)
        report = evaluate_context_selection_fixture(FIXTURE, bootstrap_samples=100)
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
        first = cases[0]
        self.assertNotIn(first.case_id, serialized)
        self.assertNotIn(first.selection_input.query, serialized)
        self.assertNotIn(first.selection_input.sources[0].source_id, serialized)
        self.assertNotIn(first.selection_input.sources[0].text, serialized)
        self.assertEqual("CONTRACT_ONLY", report["selection_signal"])
        self.assertEqual("INCONCLUSIVE", report["research_decision"])
        self.assertFalse(report["runtime_authorized"])
        self.assertFalse(report["answer_evaluation_complete"])
        provisional_cost = report["arms"]["C1"][
            "candidate_snapshot_construction_not_deduplicated"
        ]
        self.assertFalse(provisional_cost["decision_usable"])

    def test_synthetic_contract_exercises_deployed_failures(self) -> None:
        report = evaluate_context_selection_fixture(FIXTURE, bootstrap_samples=100)
        b0 = report["arms"]["B0"]["top1"]["rate"]
        b1 = report["arms"]["B1"]["top1"]["rate"]
        self.assertIsNotNone(b0)
        self.assertIsNotNone(b1)
        self.assertGreater(b1, b0)

    def test_bootstrap_is_deterministic(self) -> None:
        first = evaluate_context_selection_fixture(FIXTURE, bootstrap_samples=250)
        second = evaluate_context_selection_fixture(FIXTURE, bootstrap_samples=250)
        self.assertEqual(first["comparisons"], second["comparisons"])

    def test_point_estimate_weights_families_equally_when_sizes_differ(self) -> None:
        case = load_context_selection_fixture(FIXTURE)[0]
        template = score_case(case, select_b0(case.deployed))
        rows = [
            ("large-1", "large-family", False, True),
            ("large-2", "large-family", False, True),
            ("large-3", "large-family", False, True),
            ("large-4", "large-family", False, True),
            ("small-1", "small-family", True, False),
        ]
        left = [
            replace(
                template,
                case_id=case_id,
                case_family=family,
                eligibility_class="explicit_reply",
                top1_eligible=True,
                top1_correct=left_correct,
            )
            for case_id, family, left_correct, _right_correct in rows
        ]
        right = [
            replace(
                template,
                case_id=case_id,
                case_family=family,
                eligibility_class="explicit_reply",
                top1_eligible=True,
                top1_correct=right_correct,
            )
            for case_id, family, _left_correct, right_correct in rows
        ]
        comparison = paired_top1_comparison(
            left,
            right,
            bootstrap_samples=0,
            seed=119,
        )
        self.assertEqual("macro_equal_class_equal_family", comparison["estimand"])
        self.assertEqual(0.0, comparison["delta_right_minus_left"])

    def test_future_source_is_rejected_without_echoing_payload(self) -> None:
        case = build_cases()[0]
        case["sources"][0]["created_at"] = "2027-01-01T00:00:00Z"
        private_sentinel = "PRIVATE_SENTINEL_MUST_NOT_ECHO"
        case["sources"][0]["text"] = private_sentinel
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.jsonl"
            path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(ContextSelectionFixtureError) as raised:
                load_context_selection_fixture(path)
        self.assertNotIn(private_sentinel, str(raised.exception))

    def test_candidate_snapshot_hash_detects_tampering(self) -> None:
        case = build_cases()[0]
        private_sentinel = "TAMPERED_PRIVATE_SENTINEL"
        case["sources"][0]["text"] = private_sentinel
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tampered.jsonl"
            path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(ContextSelectionFixtureError) as raised:
                load_context_selection_fixture(path)
        self.assertIn("candidate snapshot hash mismatch", str(raised.exception))
        self.assertNotIn(private_sentinel, str(raised.exception))

    def test_event_cannot_include_source_created_after_the_event(self) -> None:
        case = build_cases()[0]
        case["events"][0]["created_at"] = "2026-01-02T00:00:00Z"
        case["snapshot"]["candidate_snapshot_sha256"] = candidate_snapshot_sha256(case)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future-event-source.jsonl"
            path.write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(ContextSelectionFixtureError) as raised:
                load_context_selection_fixture(path)
        self.assertIn("event precedes linked source", str(raised.exception))

    def test_private_development_never_emits_holdout_signal(self) -> None:
        cases = build_cases()
        for case in cases:
            case["corpus_kind"] = "private_replay"
            case["split"] = "development"
            case["snapshot"]["retrieval_snapshot_kind"] = "frozen_live_scores"
            case["snapshot"]["query_embedding_sha256"] = hashlib.sha256(
                f"embedding:{case['case_id']}".encode("utf-8")
            ).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "development.jsonl"
            path.write_text(
                "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
                encoding="utf-8",
            )
            report = evaluate_context_selection_fixture(path, bootstrap_samples=100)
        self.assertEqual("DEVELOPMENT_ONLY", report["selection_signal"])
        self.assertEqual("INCONCLUSIVE", report["research_decision"])
        self.assertFalse(report["runtime_authorized"])

    def test_private_holdout_cannot_self_authorize_from_fixture_fields(self) -> None:
        cases = build_cases()
        for case in cases:
            case["corpus_kind"] = "private_replay"
            case["split"] = "holdout"
            case["snapshot"]["retrieval_snapshot_kind"] = "frozen_live_scores"
            case["snapshot"]["query_embedding_sha256"] = hashlib.sha256(
                f"embedding:{case['case_id']}".encode("utf-8")
            ).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holdout.jsonl"
            path.write_text(
                "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
                encoding="utf-8",
            )
            with patch("context_selection_eval.select_arm", side_effect=AssertionError("holdout was evaluated")):
                report = evaluate_context_selection_fixture(path, bootstrap_samples=100)
        self.assertEqual("HOLDOUT_NOT_AUTHORIZED", report["selection_signal"])
        self.assertTrue(report["evaluation_suppressed"])
        self.assertFalse(report["holdout_authorization_present"])
        self.assertNotIn("arms", report)
        self.assertNotIn("comparisons", report)
        self.assertNotIn("top1_eligible_class_counts", report)
        self.assertEqual("INCONCLUSIVE", report["research_decision"])
        self.assertFalse(report["runtime_authorized"])

    def test_cli_bounds_unexpected_private_fixture_failures(self) -> None:
        from scripts import eval_context_selection as command

        private_sentinel = "PRIVATE_EVAL_SENTINEL"
        stderr = StringIO()
        with patch.object(
            command,
            "evaluate_context_selection_fixture",
            side_effect=ValueError(private_sentinel),
        ):
            with patch(
                "sys.argv",
                ["eval_context_selection.py", "--fixture", "ignored-private-fixture.jsonl"],
            ):
                with redirect_stderr(stderr):
                    result = command.main()
        self.assertEqual(2, result)
        self.assertEqual(
            "context-selection evaluation failed: ValueError\n",
            stderr.getvalue(),
        )
        self.assertNotIn(private_sentinel, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
