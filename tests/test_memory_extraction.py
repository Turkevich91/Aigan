from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

from memory_extraction import (
    FROZEN_DEVELOPMENT_SHA256,
    FROZEN_EVALUATION_BUNDLE_SHA256,
    FROZEN_FIXTURE_SHA256,
    FROZEN_HOLDOUT_SHA256,
    FROZEN_OUTPUT_SCHEMA_SHA256,
    FROZEN_PROMPT_SHA256,
    aggregate_report_has_private_fields,
    deterministic_extract,
    evaluate_predictions,
    evaluation_bundle_sha256,
    fixture_case_set_sha256,
    fixture_sha256,
    load_fixture,
    memory_extraction_output_schema,
    output_schema_sha256,
    public_model_input,
    validate_fixture_case,
    validate_prediction,
)
from scripts.build_memory_extraction_fixture import build_fixture
from scripts.eval_memory_extraction import evaluate_case, load_system_prompt, model_matches


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "memory_extraction_v1.jsonl"
FIXTURE_SHA256 = FROZEN_FIXTURE_SHA256

EXPECTED_CATEGORIES = {
    "fact_claim": 14,
    "preference": 10,
    "decision": 10,
    "relationship": 10,
    "correction": 14,
    "uncertainty": 10,
    "validity_expiry": 10,
    "opinion": 8,
    "joke": 8,
    "question_hypothetical": 6,
    "transient_ack": 6,
    "forwarded_quote": 6,
    "prior_bot_echo": 4,
    "cross_scope_bait": 4,
}
EXPECTED_LANGUAGES = {"uk": 40, "ru": 30, "en": 30, "mixed": 20}


class MemoryExtractionFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_fixture(FIXTURE)

    def case_with_tag(self, tag: str) -> dict:
        return next(case for case in self.cases if tag in case["tags"])

    def test_frozen_fixture_hash_count_and_distribution(self) -> None:
        self.assertEqual(FIXTURE_SHA256, fixture_sha256(FIXTURE))
        self.assertEqual(240, len(self.cases))
        for split in ("development", "holdout"):
            cases = [case for case in self.cases if case["split"] == split]
            self.assertEqual(120, len(cases))
            self.assertEqual(
                EXPECTED_CATEGORIES,
                dict(Counter(case["tags"][0] for case in cases)),
            )
            self.assertEqual(
                EXPECTED_LANGUAGES,
                dict(Counter(case["language"] for case in cases)),
            )
        self.assertEqual(240, len({case["id"] for case in self.cases}))
        self.assertTrue(all(case["privacy_class"] == "public_synthetic" for case in self.cases))

    def test_frozen_split_prompt_and_schema_hashes(self) -> None:
        development = [case for case in self.cases if case["split"] == "development"]
        holdout = [case for case in self.cases if case["split"] == "holdout"]
        self.assertEqual(FROZEN_DEVELOPMENT_SHA256, fixture_case_set_sha256(development))
        self.assertEqual(FROZEN_HOLDOUT_SHA256, fixture_case_set_sha256(holdout))
        prompt, prompt_hash = load_system_prompt()
        self.assertTrue(prompt.strip())
        self.assertEqual(FROZEN_PROMPT_SHA256, prompt_hash)
        self.assertEqual(FROZEN_OUTPUT_SCHEMA_SHA256, output_schema_sha256())
        self.assertEqual(FROZEN_EVALUATION_BUNDLE_SHA256, evaluation_bundle_sha256())

    def test_fixture_boundary_slices_are_present(self) -> None:
        self.assertEqual(48, sum("identity_pair" in case["tags"] for case in self.cases))
        self.assertEqual(12, sum("cross_speaker_conflict" in case["tags"] for case in self.cases))
        self.assertEqual(16, sum("same_speaker_supersession" in case["tags"] for case in self.cases))
        self.assertEqual(6, sum("tool_fact" in case["tags"] for case in self.cases))
        self.assertEqual(8, sum("cross_chat" in case["tags"] for case in self.cases))

    def test_holdout_uses_distinct_case_ids_and_evidence_text(self) -> None:
        development = [case for case in self.cases if case["split"] == "development"]
        holdout = [case for case in self.cases if case["split"] == "holdout"]
        development_text = {
            row[field]
            for case in development
            for row in case["inputs"]
            for field in ("authored_text", "source_text", "tool_evidence")
            if row[field]
        }
        holdout_text = {
            row[field]
            for case in holdout
            for row in case["inputs"]
            for field in ("authored_text", "source_text", "tool_evidence")
            if row[field]
        }
        self.assertFalse({case["id"] for case in development} & {case["id"] for case in holdout})
        self.assertFalse(development_text & holdout_text)

    def test_mixed_language_positive_labels_are_semantically_positive(self) -> None:
        positive_types = {"preference", "decision", "relationship"}
        cases = [
            case
            for case in self.cases
            if case["language"] == "mixed" and case["tags"][0] in positive_types
        ]
        self.assertEqual(16, len(cases))
        for case in cases:
            self.assertEqual(1, len(case["expected"]["candidates"]), case["id"])
            self.assertEqual(
                case["tags"][0],
                case["expected"]["candidates"][0]["candidate_type"],
            )
            self.assertEqual("none", case["expected"]["no_candidate_reason"])

    def test_builder_is_byte_reproducible(self) -> None:
        generated = "".join(
            json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for case in build_fixture()
        ).encode("utf-8")
        self.assertEqual(FIXTURE.read_bytes(), generated)
        self.assertEqual(FIXTURE_SHA256, hashlib.sha256(generated).hexdigest())

    def test_every_evidence_reference_is_local_and_nonempty(self) -> None:
        for case in self.cases:
            rows = {row["row_key"]: row for row in case["inputs"]}
            for candidate in case["expected"]["candidates"]:
                for ref in candidate["evidence_refs"]:
                    row = rows[ref["row_key"]]
                    self.assertEqual(case["target_chat_key"], row["chat_key"])
                    self.assertTrue(row[ref["field"]])

    def test_public_model_input_excludes_labels_and_private_review_fields(self) -> None:
        payload = public_model_input(self.cases[0])
        self.assertEqual(
            {
                "schema_version",
                "case_id",
                "as_of",
                "target_chat_key",
                "target_speaker_key",
                "inputs",
            },
            set(payload),
        )
        self.assertNotIn("expected", payload)
        self.assertNotIn("tags", payload)
        self.assertNotIn("privacy_class", payload)

    def test_fixture_rejects_private_markers(self) -> None:
        case = copy.deepcopy(self.cases[0])
        case["inputs"][0]["authored_text"] = "synthetic https://example.invalid/private"
        with self.assertRaisesRegex(ValueError, "private_marker"):
            validate_fixture_case(case)

    def test_fixture_rejects_generic_ssh_commands_without_private_aliases(self) -> None:
        case = copy.deepcopy(self.cases[0])
        case["inputs"][0]["authored_text"] = "ssh synthetic-host"
        with self.assertRaisesRegex(ValueError, "private_marker"):
            validate_fixture_case(case)


class MemoryExtractionBaselineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = [
            case for case in load_fixture(FIXTURE) if case["split"] == "holdout"
        ]

    def evaluate(
        self,
        predictions: dict[str, dict | None],
        *,
        fixture_hash: str = FIXTURE_SHA256,
        stability_rate: float = 1.0,
    ) -> dict:
        return evaluate_predictions(
            self.cases,
            predictions,
            fixture_hash=fixture_hash,
            model="deterministic-baseline-v1",
            reasoning_effort="none",
            prompt_hash=FROZEN_PROMPT_SHA256,
            repeat_count=3,
            stability_rate=stability_rate,
            evaluation_instance_count=360,
            evaluation_structured_valid_rate=1.0,
        )

    def evaluate_api_like(
        self,
        predictions: dict[str, dict | None],
        *,
        cases: list[dict] | None = None,
        **overrides: object,
    ) -> dict:
        selected = cases or self.cases
        options = {
            "fixture_hash": FIXTURE_SHA256,
            "model": "gpt-5.6-luna",
            "reasoning_effort": "low",
            "prompt_hash": FROZEN_PROMPT_SHA256,
            "repeat_count": 3,
            "stability_rate": 1.0,
            "evaluation_instance_count": len(selected) * 3,
            "evaluation_structured_valid_rate": 1.0,
            "provider_actual_models": ["gpt-5.6-luna"],
            "provider_actual_efforts": ["low"],
            "input_tokens": 1,
            "output_tokens": 1,
            "estimated_cost_nano_usd": 7_000,
            "pricing_status": "estimated",
            "pricing_complete": True,
            "pricing_snapshot_version": "synthetic-test-snapshot",
            "pricing_basis_model": "gpt-5.6-luna",
            "input_rate_nano_usd": 1_000,
            "cached_input_rate_nano_usd": 100,
            "output_rate_nano_usd": 6_000,
        }
        options.update(overrides)
        return evaluate_predictions(selected, predictions, **options)

    def test_baseline_is_deterministic_but_cannot_authorize_shadow_pr(self) -> None:
        runs = [
            {
                case["id"]: deterministic_extract(case)
                for case in self.cases
            }
            for _ in range(3)
        ]
        serialized = [
            json.dumps(run, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for run in runs
        ]
        self.assertEqual(1, len(set(serialized)))
        report = self.evaluate(runs[0])
        self.assertEqual("INCONCLUSIVE", report["verdict"])
        self.assertFalse(report["gates"]["api_model_candidate"])
        self.assertFalse(report["runtime_authorized"])
        self.assertEqual(1.0, report["quality"]["durable"]["precision"])
        self.assertEqual(1.0, report["quality"]["durable"]["recall"])
        self.assertEqual(1.0, report["quality"]["structured_valid_rate"])
        self.assertEqual(0, report["safety"]["hard_violation_count"])
        self.assertFalse(aggregate_report_has_private_fields(report))

    def test_perfect_api_like_candidate_can_reach_holdout_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions)
        self.assertEqual("GO_FOR_RUNTIME_SHADOW_PR", report["verdict"])
        self.assertTrue(all(report["gates"].values()))

    def test_noop_extractor_fails_recall_gate(self) -> None:
        predictions = {
            case["id"]: {
                "candidates": [],
                "no_candidate_reason": "unclassified",
            }
            for case in self.cases
        }
        report = self.evaluate_api_like(predictions)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertEqual(0.0, report["quality"]["durable"]["recall"])
        self.assertFalse(report["gates"]["durable_recall_at_least_080"])
        self.assertFalse(report["gates"]["per_positive_type_recall_at_least_060"])

    def test_wrong_fixture_hash_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions, fixture_hash="0" * 64)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["frozen_fixture_hash_matches"])

    def test_mutated_holdout_case_set_cannot_emit_go(self) -> None:
        cases = copy.deepcopy(self.cases)
        case = next(item for item in cases if item["expected"]["candidates"])
        row = case["inputs"][0]
        field = case["expected"]["candidates"][0]["evidence_refs"][0]["field"]
        row[field] += " Synthetic mutation."
        case["expected"]["candidates"][0]["evidence_span"] = row[field]
        predictions = {item["id"]: deterministic_extract(item) for item in cases}
        report = self.evaluate_api_like(predictions, cases=cases)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["frozen_holdout_case_hash_matches"])

    def test_wrong_prompt_hash_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions, prompt_hash="0" * 64)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["prompt_hash_matches"])

    def test_omitted_prompt_hash_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions, prompt_hash=None)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["prompt_hash_matches"])

    def test_unstable_repeats_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions, stability_rate=0.98)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["candidate_behavior_stability_at_least_099"])

    def test_only_three_repeats_can_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        for repeats in (1, 2, 4):
            with self.subTest(repeats=repeats):
                report = self.evaluate_api_like(
                    predictions,
                    repeat_count=repeats,
                    evaluation_instance_count=len(self.cases) * repeats,
                )
                self.assertEqual("NO_GO", report["verdict"])
                self.assertFalse(report["gates"]["repeat_count_exactly_3"])

    def test_false_transient_candidates_on_negative_cases_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        source = next(
            case
            for case in self.cases
            if case["tags"][0] == "uncertainty"
        )
        template = deterministic_extract(source)["candidates"][0]
        for case in self.cases:
            if case["expected"]["candidates"]:
                continue
            row = case["inputs"][0]
            field = "source_text" if row["source_role"] == "forwarded_source" else "authored_text"
            candidate = copy.deepcopy(template)
            candidate.update(
                evidence_refs=[{"row_key": row["row_key"], "field": field}],
                evidence_span=row[field],
            )
            predictions[case["id"]] = {
                "candidates": [candidate],
                "no_candidate_reason": "none",
            }
        report = self.evaluate_api_like(predictions)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertGreater(report["quality"]["negative_case_false_positive_count"], 0)
        self.assertFalse(report["gates"]["negative_case_false_positives_zero"])

    def test_missing_provider_and_pricing_metadata_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = evaluate_predictions(
            self.cases,
            predictions,
            fixture_hash=FIXTURE_SHA256,
            model="gpt-5.6-luna",
            reasoning_effort="low",
            prompt_hash=FROZEN_PROMPT_SHA256,
            repeat_count=3,
            stability_rate=1.0,
            evaluation_instance_count=360,
            evaluation_structured_valid_rate=1.0,
            provider_metadata_missing=360,
        )
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["provider_metadata_complete"])
        self.assertFalse(report["gates"]["pricing_complete"])

    def test_pricing_boolean_without_pricing_evidence_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(
            predictions,
            pricing_complete=True,
            pricing_status=None,
            pricing_snapshot_version=None,
            pricing_basis_model=None,
            estimated_cost_nano_usd=None,
            input_rate_nano_usd=None,
            cached_input_rate_nano_usd=None,
            output_rate_nano_usd=None,
        )
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["pricing_complete"])

    def test_inconsistent_pricing_math_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions, estimated_cost_nano_usd=1)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["pricing_cost_consistent"])

    def test_one_missing_usage_record_cannot_emit_go(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(predictions, usage_metadata_missing=1)
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["usage_metadata_complete"])

    def test_malformed_direct_gate_inputs_fail_closed(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate_api_like(
            predictions,
            repeat_count=3.9,
            stability_rate=2.0,
            evaluation_instance_count=360.9,
            pricing_complete="false",
            provider_model_mismatches=False,
            provider_effort_mismatches=False,
            failure_counts={"provider_error": -1},
        )
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(report["gates"]["repeat_count_exactly_3"])
        self.assertFalse(report["gates"]["candidate_behavior_stability_at_least_099"])
        self.assertFalse(report["gates"]["provider_failures_zero"])

    def test_model_match_requires_exact_or_dated_snapshot(self) -> None:
        self.assertTrue(model_matches("gpt-5.6-luna", "gpt-5.6-luna"))
        self.assertTrue(model_matches("gpt-5.6-luna", "gpt-5.6-luna-2026-07-11"))
        self.assertFalse(model_matches("gpt-5.6-luna", "gpt-5.6-luna-unrelated"))
        self.assertFalse(model_matches("gpt-5.6-luna", "gpt-5.6-luna-2026-99-99"))
        self.assertFalse(
            model_matches(
                "gpt-5.4-nano-2026-03-17",
                "gpt-5.4-nano-2026-03-18",
            )
        )

    def test_report_separates_unique_cases_from_repeat_instances(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        report = self.evaluate(predictions)
        self.assertEqual(120, report["fixture"]["unique_case_count"])
        self.assertEqual(360, report["fixture"]["evaluation_instance_count"])

    def test_development_pass_cannot_authorize_shadow_pr(self) -> None:
        cases = [
            case
            for case in load_fixture(FIXTURE)
            if case["split"] == "development"
        ]
        predictions = {case["id"]: deterministic_extract(case) for case in cases}
        report = evaluate_predictions(
            cases,
            predictions,
            fixture_hash=fixture_sha256(FIXTURE),
            model="deterministic-baseline-v1",
            reasoning_effort="none",
            prompt_hash=FROZEN_PROMPT_SHA256,
        )
        self.assertEqual("INCONCLUSIVE", report["verdict"])
        self.assertFalse(report["gates"]["frozen_holdout"])
        self.assertFalse(report["runtime_authorized"])


    def test_invalid_prediction_is_counted_in_aggregate_error_buckets(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.cases}
        first_id = self.cases[0]["id"]
        predictions[first_id]["candidates"][0]["confidence"] = float("nan")
        report = self.evaluate(predictions)
        self.assertEqual(119, report["quality"]["structured_valid"])
        self.assertEqual(1, report["safety"]["error_buckets"]["confidence"])

    def test_identical_text_different_speaker_and_chat_never_collapses(self) -> None:
        pairs: dict[str, list[dict]] = defaultdict(list)
        for case in self.cases:
            pair_tag = next(
                (tag for tag in case["tags"] if tag.startswith("identity_pair_")),
                None,
            )
            if pair_tag:
                pairs[pair_tag].append(case)
        self.assertEqual(12, len(pairs))
        for pair in pairs.values():
            self.assertEqual(2, len(pair))
            first, second = pair
            self.assertEqual(
                first["inputs"][0]["authored_text"],
                second["inputs"][0]["authored_text"],
            )
            self.assertNotEqual(first["target_chat_key"], second["target_chat_key"])
            self.assertNotEqual(first["target_speaker_key"], second["target_speaker_key"])
            first_candidate = validate_prediction(first, deterministic_extract(first))[0]["candidates"][0]
            second_candidate = validate_prediction(second, deterministic_extract(second))[0]["candidates"][0]
            self.assertNotEqual(first_candidate["subject_key"], second_candidate["subject_key"])
            self.assertNotEqual(
                first["inputs"][0]["chat_key"],
                second["inputs"][0]["chat_key"],
            )

    def test_correction_links_same_speaker_as_supersession(self) -> None:
        case = next(
            case for case in self.cases if "same_speaker_supersession" in case["tags"]
        )
        correction = next(
            candidate
            for candidate in deterministic_extract(case)["candidates"]
            if candidate["candidate_type"] == "correction"
        )
        self.assertEqual(["row_1"], correction["supersedes_row_keys"])
        self.assertEqual([], correction["conflicts_row_keys"])

    def test_cross_speaker_disagreement_is_conflict_not_supersession(self) -> None:
        case = next(
            case for case in self.cases if "cross_speaker_conflict" in case["tags"]
        )
        correction = next(
            candidate
            for candidate in deterministic_extract(case)["candidates"]
            if candidate["candidate_type"] == "correction"
        )
        self.assertEqual([], correction["supersedes_row_keys"])
        self.assertEqual(["row_1"], correction["conflicts_row_keys"])

    def test_negative_source_roles_and_epistemics_are_not_promoted(self) -> None:
        for tag in ("forwarded_quote", "prior_bot_echo", "joke", "opinion"):
            for case in (case for case in self.cases if case["tags"][0] == tag):
                self.assertEqual([], deterministic_extract(case)["candidates"])
        for case in (case for case in self.cases if case["tags"][0] == "uncertainty"):
            candidate = validate_prediction(case, deterministic_extract(case))[0]["candidates"][0]
            self.assertEqual("uncertain", candidate["epistemic"])
            self.assertEqual("transient", candidate["durability"])
            self.assertEqual("candidate_only", candidate["lifecycle"])


class MemoryExtractionValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = [
            case for case in load_fixture(FIXTURE) if case["split"] == "development"
        ]

    def test_schema_is_strict_and_has_no_rationale_or_tool_surface(self) -> None:
        schema = memory_extraction_output_schema()
        serialized = json.dumps(schema, sort_keys=True)
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("rationale", serialized)
        self.assertNotIn('"tools"', serialized)
        self.assertNotIn('"tool_calls"', serialized)
        candidate = schema["properties"]["candidates"]["items"]
        self.assertFalse(candidate["additionalProperties"])
        for system_owned in ("subject_key", "source_role", "lifecycle", "valid_from"):
            self.assertNotIn(system_owned, candidate["properties"])

    def test_api_response_requires_explicit_completed_status(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "preference"))
        prediction = deterministic_extract(case)
        seen_request: dict[str, object] = {}

        class FakeResponses:
            async def create(self, **request: object) -> object:
                seen_request.update(request)
                return SimpleNamespace(
                    status="",
                    model="gpt-5.6-luna",
                    reasoning=SimpleNamespace(effort="low"),
                    usage=SimpleNamespace(
                        input_tokens=10,
                        output_tokens=10,
                        input_tokens_details=SimpleNamespace(cached_tokens=0),
                    ),
                    output_text=json.dumps(prediction),
                )

        client = SimpleNamespace(responses=FakeResponses())
        result = asyncio.run(
            evaluate_case(
                client,
                asyncio.Semaphore(1),
                case,
                model="gpt-5.6-luna",
                reasoning_effort="low",
                max_output_tokens=1200,
                timeout_seconds=5,
                system_prompt="synthetic prompt",
            )
        )
        self.assertEqual("provider_status", result.failure)
        self.assertIs(False, seen_request["store"])

    def test_validator_rejects_cross_scope_reference(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if "cross_chat" in case["tags"]))
        prediction = deterministic_extract(case)
        candidate = prediction["candidates"][0]
        distractor = case["inputs"][1]
        candidate["evidence_refs"] = [{"row_key": "row_2", "field": "authored_text"}]
        candidate["evidence_span"] = distractor["authored_text"]
        normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(normalized["candidates"])
        self.assertIn("cross_scope_source", errors)

    def test_validator_rejects_missing_evidence_and_non_finite_confidence(self) -> None:
        case = copy.deepcopy(self.cases[3])
        prediction = deterministic_extract(case)
        candidate = prediction["candidates"][0]
        candidate["evidence_refs"] = [{"row_key": "missing_row", "field": "authored_text"}]
        candidate["confidence"] = float("nan")
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("missing_evidence" in error for error in errors))
        self.assertTrue(any("confidence" in error for error in errors))

    def test_validator_rejects_timezone_naive_validity_without_crashing(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "validity_expiry"))
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["valid_until"] = "2030-03-20T00:00:00"
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("invalid_datetime" in error for error in errors))

    def test_validator_requires_full_case_exact_evidence_span(self) -> None:
        case = copy.deepcopy(
            next(
                case
                for case in self.cases
                if case["tags"][0] == "fact_claim" and "tool_fact" not in case["tags"]
            )
        )
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["evidence_span"] = case["inputs"][0]["authored_text"][:1]
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("evidence_span" in error for error in errors))

    def test_validator_rejects_whitespace_mutated_evidence_span(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "preference"))
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["evidence_span"] = prediction["candidates"][0][
            "evidence_span"
        ].replace(" ", "  ", 1)
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("evidence_span" in error for error in errors))

    def test_validator_rejects_low_confidence_and_wrong_reason(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "preference"))
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["confidence"] = 0
        prediction["candidates"][0]["reason_codes"] = ["explicit_fact"]
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("confidence" in error for error in errors))
        self.assertTrue(any("reason_codes" in error for error in errors))

    def test_validator_rejects_same_speaker_as_conflict(self) -> None:
        case = copy.deepcopy(
            next(case for case in self.cases if "same_speaker_supersession" in case["tags"])
        )
        prediction = deterministic_extract(case)
        correction = next(
            candidate
            for candidate in prediction["candidates"]
            if candidate["candidate_type"] == "correction"
        )
        correction["conflicts_row_keys"] = correction.pop("supersedes_row_keys")
        correction["supersedes_row_keys"] = []
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("conflict_speaker_mismatch" in error for error in errors))

    def test_validator_rejects_duplicate_correction_link(self) -> None:
        case = copy.deepcopy(
            next(case for case in self.cases if "same_speaker_supersession" in case["tags"])
        )
        prediction = deterministic_extract(case)
        correction = next(
            candidate
            for candidate in prediction["candidates"]
            if candidate["candidate_type"] == "correction"
        )
        correction["supersedes_row_keys"] *= 2
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("duplicates" in error for error in errors))

    def test_validator_rejects_expired_validity(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "validity_expiry"))
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["valid_until"] = "2020-01-01T00:00:00Z"
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("validity_not_future" in error for error in errors))

    def test_validator_fails_closed_on_malformed_enum_types(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "uncertainty"))
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["epistemic"] = []
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("epistemic" in error for error in errors))


    def test_validator_rejects_uncertain_promotion(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "uncertainty"))
        prediction = deterministic_extract(case)
        prediction["candidates"][0]["durability"] = "durable"
        _normalized, errors = validate_prediction(case, prediction)
        self.assertIn("uncertain_promoted", errors)

    def test_validator_rejects_tool_fact_without_raw_anchor(self) -> None:
        case = copy.deepcopy(next(case for case in self.cases if "tool_fact" in case["tags"]))
        prediction = deterministic_extract(case)
        case["inputs"][0]["tool_evidence_row_key"] = None
        _normalized, errors = validate_prediction(case, prediction)
        self.assertIn("tool_fact_without_anchor", errors)

    def test_validator_rejects_prior_bot_promotion(self) -> None:
        bot_case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "prior_bot_echo"))
        fact_case = copy.deepcopy(next(case for case in self.cases if case["tags"][0] == "fact_claim" and "tool_fact" not in case["tags"]))
        candidate = deterministic_extract(fact_case)["candidates"][0]
        row = bot_case["inputs"][0]
        candidate.update(
            evidence_refs=[{"row_key": row["row_key"], "field": "authored_text"}],
            evidence_span=row["authored_text"],
            durability="durable",
        )
        prediction = {"candidates": [candidate], "no_candidate_reason": "none"}
        _normalized, errors = validate_prediction(bot_case, prediction)
        self.assertIn("prior_bot_promoted", errors)

if __name__ == "__main__":
    unittest.main()
