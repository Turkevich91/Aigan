from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import memory_extraction as v1
from memory_extraction_v2 import (
    API_ARMS,
    BASELINE_MODEL,
    EVALUATOR_VERSION,
    EXPECTED_CASES_PER_SPLIT,
    FROZEN_DETERMINISTIC_BASELINE_SHA256,
    FROZEN_DEVELOPMENT_CASE_SHA256,
    FROZEN_DEVELOPMENT_FILE_SHA256,
    FROZEN_EVALUATION_BUNDLE_SHA256,
    FROZEN_HOLDOUT_CASE_SHA256,
    FROZEN_HOLDOUT_FILE_SHA256,
    FROZEN_MANIFEST_SHA256,
    FROZEN_OUTPUT_SCHEMA_SHA256,
    FROZEN_PRICING_SNAPSHOT_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_RUN_MATRIX_SHA256,
    FROZEN_SCREEN_CASE_SHA256,
    PRICING_SNAPSHOT,
    aggregate_report_has_private_fields,
    canonical_to_v2,
    deterministic_extract,
    deterministic_baseline_sha256,
    evaluate_predictions,
    evaluation_bundle_sha256,
    fixture_case_set_sha256,
    fixture_sha256,
    load_fixture,
    manifest_sha256,
    memory_extraction_output_schema,
    output_schema_sha256,
    pricing_snapshot_sha256,
    public_model_input,
    repeat_jobs,
    run_matrix_sha256,
    select_screen_cases,
    validate_prediction,
)
from model_pricing import PRICE_SNAPSHOT_VERSION, TokenUsage, estimate_token_cost, token_price_for_model
from memory_extraction_selection_v2 import (
    build_selection_attestation,
    load_screen_admission,
    selection_attestation_sha256,
    validate_selection_attestation,
)
import scripts.eval_memory_extraction_v2 as eval_v2
from scripts.eval_memory_extraction_v2 import (
    CallResult,
    first_repeat_predictions,
    reserve_holdout_once,
)
from scripts.build_memory_extraction_fixture_v2 import build_split, write_fixture


ROOT = Path(__file__).resolve().parents[1]
DEV_FIXTURE = ROOT / "tests" / "fixtures" / "memory_extraction_v2_development.jsonl"
HOLDOUT_FIXTURE = ROOT / "tests" / "fixtures" / "memory_extraction_v2_holdout.jsonl"
MANIFEST = ROOT / "tests" / "fixtures" / "memory_extraction_v2_manifest.json"
PROMPT = ROOT / "prompts" / "memory_extraction_eval_v5.md"


class MemoryExtractionV2FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.development = load_fixture(DEV_FIXTURE)

    def test_frozen_hashes_and_versions_match(self) -> None:
        self.assertEqual(FROZEN_DEVELOPMENT_FILE_SHA256, fixture_sha256(DEV_FIXTURE))
        self.assertEqual(FROZEN_HOLDOUT_FILE_SHA256, fixture_sha256(HOLDOUT_FIXTURE))
        self.assertEqual(
            FROZEN_DEVELOPMENT_CASE_SHA256,
            fixture_case_set_sha256(self.development),
        )
        self.assertEqual(FROZEN_PROMPT_SHA256, fixture_sha256(PROMPT))
        self.assertEqual(FROZEN_OUTPUT_SCHEMA_SHA256, output_schema_sha256())
        self.assertEqual(
            FROZEN_DETERMINISTIC_BASELINE_SHA256,
            deterministic_baseline_sha256(),
        )
        self.assertEqual(FROZEN_PRICING_SNAPSHOT_SHA256, pricing_snapshot_sha256())
        self.assertEqual(FROZEN_RUN_MATRIX_SHA256, run_matrix_sha256())
        self.assertEqual(FROZEN_EVALUATION_BUNDLE_SHA256, evaluation_bundle_sha256())
        self.assertEqual(FROZEN_MANIFEST_SHA256, manifest_sha256(MANIFEST))

    def test_v1_bundle_remains_byte_stable(self) -> None:
        v1_fixture = ROOT / "tests" / "fixtures" / "memory_extraction_v1.jsonl"
        v1_prompt = ROOT / "prompts" / "memory_extraction_eval_v4.md"
        self.assertEqual(v1.FROZEN_FIXTURE_SHA256, v1.fixture_sha256(v1_fixture))
        self.assertEqual(v1.FROZEN_PROMPT_SHA256, v1.fixture_sha256(v1_prompt))
        self.assertEqual(v1.FROZEN_OUTPUT_SCHEMA_SHA256, v1.output_schema_sha256())
        self.assertEqual(v1.FROZEN_EVALUATION_BUNDLE_SHA256, v1.evaluation_bundle_sha256())

    def test_distribution_matches_preregistration(self) -> None:
        expected_reasons = set(v1.NO_CANDIDATE_REASONS) - {"none"}
        cases = self.development
        self.assertEqual(EXPECTED_CASES_PER_SPLIT, len(cases))
        self.assertEqual({"development"}, {case["split"] for case in cases})
        self.assertEqual(
            Counter({"uk": 40, "ru": 40, "en": 40, "mixed": 40}),
            Counter(case["language"] for case in cases),
        )
        self.assertEqual(112, sum(bool(case["expected"]["candidates"]) for case in cases))
        self.assertEqual(48, sum(not case["expected"]["candidates"] for case in cases))
        self.assertEqual(24, sum("exact_whitespace" in case["tags"] for case in cases))
        self.assertEqual(24, sum("multi_prior_reply_anchor" in case["tags"] for case in cases))
        self.assertEqual(
            expected_reasons,
            {
                case["expected"]["no_candidate_reason"]
                for case in cases
                if not case["expected"]["candidates"]
            },
        )

    def test_builder_is_byte_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "memory_extraction_v2_development.jsonl"
            write_fixture(output, build_split("development"))
            self.assertEqual(DEV_FIXTURE.read_bytes(), output.read_bytes())

    def test_screen_subset_is_frozen_and_stratified(self) -> None:
        selected = select_screen_cases(self.development)
        self.assertEqual(48, len(selected))
        self.assertEqual(FROZEN_SCREEN_CASE_SHA256, fixture_case_set_sha256(selected))
        self.assertEqual(
            Counter({"uk": 12, "ru": 12, "en": 12, "mixed": 12}),
            Counter(case["language"] for case in selected),
        )
        self.assertEqual(34, sum(bool(case["expected"]["candidates"]) for case in selected))
        self.assertEqual(14, sum(not case["expected"]["candidates"] for case in selected))
        self.assertEqual(
            set(v1.CANDIDATE_TYPES),
            {case["tags"][0] for case in selected if case["expected"]["candidates"]},
        )
        self.assertEqual(
            set(v1.NO_CANDIDATE_REASONS) - {"none"},
            {
                case["expected"]["no_candidate_reason"]
                for case in selected
                if not case["expected"]["candidates"]
            },
        )

    def test_manifest_matches_frozen_contract(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(FROZEN_DEVELOPMENT_FILE_SHA256, manifest["development"]["file_sha256"])
        self.assertEqual(FROZEN_HOLDOUT_FILE_SHA256, manifest["holdout"]["file_sha256"])
        self.assertEqual(
            FROZEN_HOLDOUT_CASE_SHA256,
            manifest["holdout"]["case_set_sha256"],
        )
        self.assertEqual(FROZEN_PROMPT_SHA256, manifest["prompt"]["sha256"])
        self.assertEqual(FROZEN_OUTPUT_SCHEMA_SHA256, manifest["output_schema"]["sha256"])
        self.assertEqual(FROZEN_EVALUATION_BUNDLE_SHA256, manifest["evaluation_bundle"]["sha256"])
        self.assertEqual(EVALUATOR_VERSION, manifest["evaluation_bundle"]["version"])
        self.assertEqual(
            FROZEN_DETERMINISTIC_BASELINE_SHA256,
            manifest["deterministic_baseline"]["sha256"],
        )
        self.assertEqual(FROZEN_RUN_MATRIX_SHA256, manifest["run_matrix"]["sha256"])
        self.assertEqual(FROZEN_SCREEN_CASE_SHA256, manifest["run_matrix"]["screen_case_set_sha256"])
        self.assertEqual(
            [case["id"] for case in select_screen_cases(self.development)],
            manifest["run_matrix"]["screen_case_ids"],
        )
        self.assertEqual([list(item) for item in API_ARMS], manifest["run_matrix"]["api_arms"])
        self.assertEqual(FROZEN_PRICING_SNAPSHOT_SHA256, manifest["pricing_snapshot"]["sha256"])
        self.assertEqual(PRICING_SNAPSHOT, manifest["pricing_snapshot"]["snapshot"])

    def test_shared_pricing_matches_frozen_snapshot(self) -> None:
        self.assertEqual(PRICING_SNAPSHOT["version"], PRICE_SNAPSHOT_VERSION)
        for model, expected in PRICING_SNAPSHOT["models"].items():
            with self.subTest(model=model):
                price = token_price_for_model(model)
                self.assertIsNotNone(price)
                self.assertEqual(expected["input_rate_nano_usd"], price.input_nano_usd)
                self.assertEqual(
                    expected["cached_input_rate_nano_usd"],
                    price.cached_input_nano_usd,
                )
                self.assertEqual(expected["output_rate_nano_usd"], price.output_nano_usd)
                self.assertEqual(
                    expected["cache_write_rate_nano_usd"],
                    price.cache_write_nano_usd,
                )


class MemoryExtractionV2ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.development = load_fixture(DEV_FIXTURE)

    def test_schema_uses_one_nested_discriminated_anyof(self) -> None:
        schema = memory_extraction_output_schema()
        self.assertEqual("object", schema["type"])
        self.assertEqual(["result"], schema["required"])
        self.assertNotIn("anyOf", schema)
        branches = schema["properties"]["result"]["anyOf"]
        self.assertEqual(2, len(branches))
        self.assertEqual(
            {"candidates", "no_candidate"},
            {branch["properties"]["kind"]["enum"][0] for branch in branches},
        )
        for branch in branches:
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["properties"]), set(branch["required"]))
        serialized = json.dumps(schema, sort_keys=True)
        for unsupported in ('"if"', '"then"', '"else"', '"allOf"', '"not"'):
            self.assertNotIn(unsupported, serialized)

    def test_response_usage_accepts_plural_and_singular_input_details(self) -> None:
        for detail_field in ("input_tokens_details", "input_token_details"):
            with self.subTest(detail_field=detail_field):
                response = {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        detail_field: {
                            "cached_tokens": 30,
                            "cache_write_tokens": 10,
                        },
                    }
                }
                self.assertEqual((100, 30, 10, 20), eval_v2.response_usage_v2(response))

    def test_parent_directory_fsync_is_fail_closed_on_posix(self) -> None:
        path = Path("external") / "receipt.json"
        with (
            patch.object(eval_v2.os, "name", "posix"),
            patch.object(eval_v2.os, "open", return_value=17) as open_directory,
            patch.object(eval_v2.os, "fsync") as fsync,
            patch.object(eval_v2.os, "close") as close,
        ):
            eval_v2._fsync_parent_directory(path)
        open_directory.assert_called_once_with(
            path.parent,
            eval_v2.os.O_RDONLY | getattr(eval_v2.os, "O_DIRECTORY", 0),
        )
        fsync.assert_called_once_with(17)
        close.assert_called_once_with(17)

        with (
            patch.object(eval_v2.os, "name", "posix"),
            patch.object(eval_v2.os, "open", return_value=23),
            patch.object(eval_v2.os, "fsync", side_effect=OSError("sync failed")),
            patch.object(eval_v2.os, "close") as close_after_failure,
            self.assertRaises(OSError),
        ):
            eval_v2._fsync_parent_directory(path)
        close_after_failure.assert_called_once_with(23)

    def test_validator_rejects_ambiguous_or_empty_branches(self) -> None:
        case = self.development[0]
        invalid = (
            {},
            {"result": {"kind": "candidates", "candidates": []}},
            {"result": {"kind": "no_candidate", "no_candidate_reason": "none"}},
            {
                "result": {
                    "kind": "candidates",
                    "candidates": [],
                    "no_candidate_reason": "opinion",
                }
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                _normalized, errors = validate_prediction(case, payload)
                self.assertTrue(errors)

    def test_public_input_excludes_labels_and_private_control_fields(self) -> None:
        payload = public_model_input(self.development[0])
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

    def test_baseline_preserves_exact_whitespace(self) -> None:
        case = next(
            case
            for case in self.development
            if "exact_whitespace" in case["tags"] and case["tags"][0] == "preference" and case["language"] == "en"
        )
        prediction = deterministic_extract(case)
        normalized, errors = validate_prediction(case, prediction)
        self.assertEqual([], errors)
        field = normalized["candidates"][0]["evidence_refs"][0]["field"]
        self.assertEqual(case["inputs"][0][field], normalized["candidates"][0]["evidence_span"])

    def test_baseline_correction_links_only_reply_anchor(self) -> None:
        for tag, link_field in (
            ("same_speaker_supersession", "supersedes_row_keys"),
            ("cross_speaker_conflict", "conflicts_row_keys"),
        ):
            case = next(
                case
                for case in self.development
                if tag in case["tags"] and case["language"] == "en"
            )
            prediction = deterministic_extract(case)
            normalized, errors = validate_prediction(case, prediction)
            self.assertEqual([], errors)
            correction = next(
                item
                for item in normalized["candidates"]
                if item["candidate_type"] == "correction"
            )
            self.assertEqual(["dev_row_1"], correction[link_field])
            other = "conflicts_row_keys" if link_field == "supersedes_row_keys" else "supersedes_row_keys"
            self.assertEqual([], correction[other])

    def test_baseline_is_structurally_valid_for_every_development_case(self) -> None:
        failures = {}
        for case in self.development:
            _normalized, errors = validate_prediction(case, deterministic_extract(case))
            if errors:
                failures[case["id"]] = errors
        self.assertEqual({}, failures)

    def test_validator_rejects_values_that_v1_would_normalize(self) -> None:
        case = next(
            case
            for case in self.development
            if case["tags"][0] == "validity_expiry" and case["language"] == "en"
        )
        prediction = deterministic_extract(case)
        prediction["result"]["candidates"][0]["valid_until"] = "2032-04-11"
        _normalized, errors = validate_prediction(case, prediction)
        self.assertTrue(any("normalized_valid_until" in error for error in errors))

    def test_repeats_keep_model_input_identical(self) -> None:
        jobs = repeat_jobs([self.development[0]], 3)
        payloads = [
            json.dumps(public_model_input(case), sort_keys=True)
            for _original_id, case in jobs
        ]
        self.assertEqual(1, len(set(payloads)))

    def test_quality_uses_first_repeat_of_each_distinct_case(self) -> None:
        jobs = repeat_jobs(self.development[:2], 3)
        results = [
            CallResult(
                {"marker": index},
                "gpt-5.6-luna",
                "none",
                1,
                0,
                0,
                1,
                1,
                "",
            )
            for index in range(6)
        ]
        selected = first_repeat_predictions(jobs, results)
        self.assertEqual(
            {
                self.development[0]["id"]: {"marker": 0},
                self.development[1]["id"]: {"marker": 3},
            },
            selected,
        )

    def _api_like_report(
        self,
        cases: list[dict],
        *,
        fixture_path: Path,
        fixture_hash: str | None = None,
        model: str = "gpt-5.6-luna",
        effort: str = "none",
        repeat_count: int = 3,
        predictions: dict[str, dict] | None = None,
        provider_actual_models: list[str] | None = None,
        provider_actual_efforts: list[str] | None = None,
        provider_metadata_missing: int = 0,
    ) -> dict:
        if predictions is None:
            predictions = {
                case["id"]: canonical_to_v2(case["expected"])
                for case in cases
            }
        usage = TokenUsage(
            input_tokens=160_000,
            cached_input_tokens=40_000,
            cache_write_tokens=0,
            output_tokens=16_000,
        )
        cost = estimate_token_cost(model, [usage])
        sol = estimate_token_cost("gpt-5.6-sol", [usage])
        return evaluate_predictions(
            cases,
            predictions,
            fixture_hash=fixture_hash or fixture_sha256(fixture_path),
            model=model,
            reasoning_effort=effort,
            prompt_hash=FROZEN_PROMPT_SHA256,
            repeat_count=repeat_count,
            stability_rate=1.0,
            full_contract_stability_rate=1.0,
            evaluation_instance_count=len(cases) * repeat_count,
            evaluation_structured_valid_rate=1.0,
            sol_counterfactual_nano_usd=sol.nano_usd,
            provider_model_mismatches=0,
            provider_effort_mismatches=0,
            provider_metadata_missing=provider_metadata_missing,
            provider_actual_models=(
                provider_actual_models
                if provider_actual_models is not None
                else [model]
            ),
            provider_actual_efforts=(
                provider_actual_efforts
                if provider_actual_efforts is not None
                else [effort]
            ),
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            estimated_cost_nano_usd=cost.nano_usd,
            pricing_status=cost.status,
            pricing_complete=cost.complete,
            usage_metadata_missing=0,
            pricing_snapshot_version=cost.snapshot_version,
            pricing_basis_model=cost.basis_model,
            input_rate_nano_usd=cost.input_rate_nano_usd,
            cached_input_rate_nano_usd=cost.cached_input_rate_nano_usd,
            cache_write_rate_nano_usd=cost.cache_write_rate_nano_usd,
            output_rate_nano_usd=cost.output_rate_nano_usd,
            concurrency=6,
            timeout_seconds=45,
            max_output_tokens=1200,
            store=False,
            latency_ms=[100] * (len(cases) * repeat_count),
            failure_counts={},
            repeat_error_counts={},
            attested_actual_models=[model],
            source_commit="a" * 40,
        )

    def test_per_call_provider_metadata_is_canonicalized_fail_closed(self) -> None:
        screen = select_screen_cases(self.development)
        repeated = self._api_like_report(
            screen,
            fixture_path=DEV_FIXTURE,
            repeat_count=1,
            provider_actual_models=["gpt-5.6-luna"] * len(screen),
            provider_actual_efforts=["none"] * len(screen),
        )
        self.assertEqual(["gpt-5.6-luna"], repeated["provider"]["actual_models"])
        self.assertEqual(["none"], repeated["provider"]["actual_efforts"])
        self.assertEqual("PASS_FOR_LOCKED_DEVELOPMENT", repeated["verdict"])

        mixed = self._api_like_report(
            screen,
            fixture_path=DEV_FIXTURE,
            repeat_count=1,
            provider_actual_models=["gpt-5.6-luna"] * (len(screen) - 1)
            + ["gpt-5.6-luna-2026-07-10"],
            provider_actual_efforts=["none"] * len(screen),
        )
        self.assertEqual("SCREEN_FAIL", mixed["verdict"])
        self.assertFalse(mixed["gates"]["single_actual_model_snapshot"])

        missing = self._api_like_report(
            screen,
            fixture_path=DEV_FIXTURE,
            repeat_count=1,
            provider_actual_models=["gpt-5.6-luna"] * (len(screen) - 1) + [""],
            provider_actual_efforts=["none"] * (len(screen) - 1) + [""],
            provider_metadata_missing=1,
        )
        self.assertEqual("SCREEN_FAIL", missing["verdict"])
        self.assertFalse(missing["gates"]["provider_metadata_complete"])

    def test_perfect_development_report_can_only_authorize_holdout(self) -> None:
        report = self._api_like_report(self.development, fixture_path=DEV_FIXTURE)
        self.assertEqual("GO_FOR_HOLDOUT_CANDIDATE", report["verdict"])
        self.assertFalse(report["runtime_authorized"])
        self.assertTrue(all(report["gates"].values()))
        self.assertFalse(aggregate_report_has_private_fields(report))

    def test_perfect_holdout_report_can_only_authorize_shadow_pr(self) -> None:
        synthetic_holdout = copy.deepcopy(self.development)
        for case in synthetic_holdout:
            case["split"] = "holdout"
        with patch(
            "memory_extraction_v2.fixture_case_set_sha256",
            return_value=FROZEN_HOLDOUT_CASE_SHA256,
        ):
            report = self._api_like_report(
                synthetic_holdout,
                fixture_path=HOLDOUT_FIXTURE,
                fixture_hash=FROZEN_HOLDOUT_FILE_SHA256,
            )
        self.assertEqual("GO_FOR_RUNTIME_SHADOW_PR", report["verdict"])
        self.assertFalse(report["runtime_authorized"])
        self.assertTrue(all(report["gates"].values()))

    def test_perfect_screen_only_admits_locked_development(self) -> None:
        screen = select_screen_cases(self.development)
        report = self._api_like_report(
            screen,
            fixture_path=DEV_FIXTURE,
            repeat_count=1,
        )
        self.assertEqual("screen", report["phase"])
        self.assertEqual("PASS_FOR_LOCKED_DEVELOPMENT", report["verdict"])
        self.assertFalse(report["runtime_authorized"])
        self.assertTrue(all(report["gates"].values()))

    def test_screen_admission_binds_exact_passing_arm_and_file(self) -> None:
        screen = select_screen_cases(self.development)
        report = self._api_like_report(
            screen,
            fixture_path=DEV_FIXTURE,
            repeat_count=1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "screen.json"
            path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
            loaded, report_hash = load_screen_admission(
                path,
                source_commit="a" * 40,
                expected_arm=("gpt-5.6-luna", "none"),
            )
            self.assertEqual(report, loaded)
            self.assertRegex(report_hash, r"^[0-9a-f]{64}$")
            with self.assertRaisesRegex(ValueError, "screen_arm_mismatch"):
                load_screen_admission(
                    path,
                    source_commit="a" * 40,
                    expected_arm=("gpt-5.6-luna", "low"),
                )

    def _passing_matrix_reports(self) -> list[dict]:
        screen = select_screen_cases(self.development)
        reports = []
        for model, effort in API_ARMS:
            reports.append(
                self._api_like_report(
                    screen,
                    fixture_path=DEV_FIXTURE,
                    model=model,
                    effort=effort,
                    repeat_count=1,
                )
            )
            reports.append(
                self._api_like_report(
                    self.development,
                    fixture_path=DEV_FIXTURE,
                    model=model,
                    effort=effort,
                )
            )
        return reports

    def test_matrix_attestation_recomputes_cheapest_passing_selection(self) -> None:
        attestation = build_selection_attestation(
            self._passing_matrix_reports(),
            source_commit="a" * 40,
        )
        self.assertEqual("CANDIDATE_SELECTED", attestation["verdict"])
        self.assertEqual("gpt-5.6-luna", attestation["selected"]["model"])
        self.assertEqual("none", attestation["selected"]["reasoning_effort"])
        self.assertEqual(attestation, validate_selection_attestation(attestation))
        self.assertRegex(selection_attestation_sha256(attestation), r"^[0-9a-f]{64}$")

    def test_matrix_attestation_rejects_missing_or_tampered_evidence(self) -> None:
        reports = self._passing_matrix_reports()
        with self.assertRaisesRegex(ValueError, "screen_matrix_incomplete"):
            build_selection_attestation(reports[2:], source_commit="a" * 40)
        attestation = build_selection_attestation(reports, source_commit="a" * 40)
        tampered = copy.deepcopy(attestation)
        tampered["selected"]["model"] = "gpt-5.6-terra"
        with self.assertRaisesRegex(ValueError, "attestation_recomputed"):
            validate_selection_attestation(tampered)

    def test_all_screen_failures_produce_matrix_no_go(self) -> None:
        screen = select_screen_cases(self.development)
        reports = []
        for model, effort in API_ARMS:
            report = self._api_like_report(
                screen,
                fixture_path=DEV_FIXTURE,
                model=model,
                effort=effort,
                repeat_count=1,
            )
            report["gates"]["provider_failures_zero"] = False
            report["verdict"] = "SCREEN_FAIL"
            report["provider"]["actual_models"] = []
            report["provider"]["actual_efforts"] = []
            reports.append(report)
        attestation = build_selection_attestation(
            reports,
            source_commit="a" * 40,
        )
        self.assertEqual("NO_GO", attestation["verdict"])
        self.assertIsNone(attestation["selected"])

    def test_holdout_receipt_is_atomic_and_survives_first_claim(self) -> None:
        attestation = build_selection_attestation(
            self._passing_matrix_reports(),
            source_commit="a" * 40,
        )
        selected = attestation["selected"]
        attestation_hash = selection_attestation_sha256(attestation)
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "holdout-receipt.json"

            def attempt() -> dict | None:
                try:
                    return reserve_holdout_once(
                        receipt_path,
                        attestation_sha256=attestation_hash,
                        selected=selected,
                        source_commit="a" * 40,
                    )
                except ValueError:
                    return None

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: attempt(), range(2)))
            self.assertEqual(1, sum(result is not None for result in results))
            self.assertTrue(receipt_path.exists())
            with self.assertRaisesRegex(ValueError, "exclusive_artifact_exists"):
                reserve_holdout_once(
                    receipt_path,
                    attestation_sha256=attestation_hash,
                    selected=selected,
                    source_commit="a" * 40,
                )

    def test_api_preflight_failure_never_calls_provider(self) -> None:
        argv = [
            "eval_memory_extraction_v2.py",
            "--fixture",
            str(DEV_FIXTURE),
            "--mode",
            "api",
            "--model",
            "gpt-5.6-luna",
            "--reasoning-effort",
            "none",
            "--limit",
            "48",
        ]
        with (
            patch.object(eval_v2, "current_clean_source_commit", return_value="a" * 40),
            patch.object(eval_v2, "manifest_sha256", return_value="0" * 64),
            patch.object(eval_v2, "run_api") as run_api,
            patch.dict(os.environ, {"OPENAI_API_KEY": "test"}),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            eval_v2.main()
        run_api.assert_not_called()

    def test_locked_development_requires_passing_screen_before_provider(self) -> None:
        argv = [
            "eval_memory_extraction_v2.py",
            "--fixture",
            str(DEV_FIXTURE),
            "--mode",
            "api",
            "--model",
            "gpt-5.6-luna",
            "--reasoning-effort",
            "none",
            "--repeats",
            "3",
            "--require-gates",
        ]
        errors = io.StringIO()
        with (
            patch.object(eval_v2, "current_clean_source_commit", return_value="a" * 40),
            patch.object(eval_v2, "run_api") as run_api,
            patch.dict(os.environ, {"OPENAI_API_KEY": "test"}),
            patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(errors),
            self.assertRaises(SystemExit),
        ):
            eval_v2.main()
        self.assertIn("passing screen report", errors.getvalue())
        run_api.assert_not_called()

    def test_wrong_hash_and_partial_screen_cannot_emit_go(self) -> None:
        wrong = self._api_like_report(
            self.development,
            fixture_path=DEV_FIXTURE,
            fixture_hash="0" * 64,
        )
        self.assertEqual("NO_GO", wrong["verdict"])
        screen = self._api_like_report(
            self.development[:48],
            fixture_path=DEV_FIXTURE,
            repeat_count=1,
        )
        self.assertEqual("SCREEN_FAIL", screen["verdict"])
        self.assertFalse(screen["gates"]["frozen_screen_case_hash_matches"])

    def test_one_missing_transient_candidate_respects_exact_set_099_gate(self) -> None:
        predictions = {
            case["id"]: canonical_to_v2(case["expected"])
            for case in self.development
        }
        transient_case = next(
            case for case in self.development if case["tags"][0] == "uncertainty"
        )
        predictions[transient_case["id"]] = {
            "result": {
                "kind": "no_candidate",
                "no_candidate_reason": "unclassified",
            }
        }
        report = self._api_like_report(
            self.development,
            fixture_path=DEV_FIXTURE,
            predictions=predictions,
        )
        self.assertGreaterEqual(report["quality"]["exact_candidate_set_rate"], 0.99)
        self.assertEqual("GO_FOR_HOLDOUT_CANDIDATE", report["verdict"])
        self.assertTrue(all(report["gates"].values()))

    def test_validation_failures_remain_privacy_safe_aggregate_reports(self) -> None:
        predictions = {
            case["id"]: canonical_to_v2(case["expected"])
            for case in self.development
        }
        positive = next(case for case in self.development if case["expected"]["candidates"])
        predictions[positive["id"]]["result"]["candidates"][0]["evidence_span"] = "wrong"
        report = self._api_like_report(
            self.development,
            fixture_path=DEV_FIXTURE,
            predictions=predictions,
        )
        self.assertEqual("NO_GO", report["verdict"])
        self.assertFalse(aggregate_report_has_private_fields(report))
        bucket_keys = set(report["safety"]["error_buckets"])
        self.assertNotIn("evidence_span", bucket_keys)
        self.assertTrue(any(key.startswith("validation_") for key in bucket_keys))

    def test_deterministic_baseline_never_authorizes(self) -> None:
        predictions = {case["id"]: deterministic_extract(case) for case in self.development}
        report = evaluate_predictions(
            self.development,
            predictions,
            fixture_hash=fixture_sha256(DEV_FIXTURE),
            model=BASELINE_MODEL,
            reasoning_effort="none",
            prompt_hash=FROZEN_PROMPT_SHA256,
            repeat_count=3,
            stability_rate=1.0,
            full_contract_stability_rate=1.0,
            evaluation_instance_count=len(self.development) * 3,
            evaluation_structured_valid_rate=1.0,
            sol_counterfactual_nano_usd=0,
            provider_model_mismatches=0,
            provider_effort_mismatches=0,
            provider_metadata_missing=0,
            provider_actual_models=[],
            provider_actual_efforts=[],
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=0,
            estimated_cost_nano_usd=0,
            pricing_status="not_applicable",
            pricing_complete=True,
            usage_metadata_missing=0,
            pricing_snapshot_version=None,
            pricing_basis_model=None,
            input_rate_nano_usd=None,
            cached_input_rate_nano_usd=None,
            output_rate_nano_usd=None,
            concurrency=0,
            timeout_seconds=0,
            max_output_tokens=0,
            store=False,
            latency_ms=[],
            failure_counts={},
            repeat_error_counts={},
        )
        self.assertEqual("INCONCLUSIVE", report["verdict"])
        self.assertFalse(report["runtime_authorized"])


if __name__ == "__main__":
    unittest.main()
