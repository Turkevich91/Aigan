from __future__ import annotations

import asyncio
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
import memory_extraction_v2 as contract_v2
from memory_extraction_v2 import (
    API_ARMS,
    BASELINE_MODEL,
    EVALUATOR_VERSION,
    HOLDOUT_CLAIM_NAMESPACE,
    MANIFEST_VERSION,
    EXPECTED_CASES_PER_SPLIT,
    HOLDOUT_CLAIM_SCOPE,
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
    PROMPT_VERSION,
    aggregate_report_has_private_fields,
    canonical_to_v2,
    deterministic_extract,
    deterministic_baseline_sha256,
    evaluate_predictions,
    evaluation_bundle_sha256,
    fixture_case_set_sha256,
    holdout_claim_key_sha256,
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
import scripts.select_memory_extraction_v2_candidate as select_v2
import scripts.eval_memory_extraction_v2 as eval_v2
from scripts.eval_memory_extraction_v2 import (
    CallResult,
    canonical_holdout_claim_path,
    first_repeat_predictions,
    reserve_holdout_once,
)
from scripts.build_memory_extraction_fixture_v2 import build_split, write_fixture


ROOT = Path(__file__).resolve().parents[1]
DEV_FIXTURE = ROOT / "tests" / "fixtures" / "memory_extraction_v2_development.jsonl"
HOLDOUT_FIXTURE = ROOT / "tests" / "fixtures" / "memory_extraction_v2_holdout.jsonl"
MANIFEST = ROOT / "tests" / "fixtures" / "memory_extraction_v2_manifest.json"
PROMPT = ROOT / "prompts" / "memory_extraction_eval_v7.md"
SUPERSEDED_PROMPT_V6 = ROOT / "prompts" / "memory_extraction_eval_v6.md"
SUPERSEDED_PROMPT_V5 = ROOT / "prompts" / "memory_extraction_eval_v5.md"


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

    def test_superseded_prompts_remain_immutable_and_inactive(self) -> None:
        self.assertEqual(
            "51ed7624b663001de77a0e219bde71e229d19b8cf953a479e5ebd07840e2af59",
            fixture_sha256(SUPERSEDED_PROMPT_V5),
        )
        self.assertEqual(
            "f063b311df0ba62abd610ac58b62410db25fb1a6d1b3c423a800e9ae13d3aa63",
            fixture_sha256(SUPERSEDED_PROMPT_V6),
        )
        self.assertNotEqual(SUPERSEDED_PROMPT_V5, PROMPT)
        self.assertNotEqual(SUPERSEDED_PROMPT_V6, PROMPT)

    def test_prompt_v7_closes_canonical_date_and_correction_direction_gaps(self) -> None:
        prompt = PROMPT.read_text(encoding="utf-8")
        for clause in (
            "YYYY-MM-DDT00:00:00Z",
            "canonical transport",
            "Every non-correction candidate must leave both link arrays empty.",
            "Never link the replied-to older row forward to the correction.",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, prompt)

    def test_development_validity_dates_use_canonical_transport_encoding(self) -> None:
        cases = [
            case
            for case in self.development
            if case["tags"][0] == "validity_expiry"
        ]
        self.assertEqual(12, len(cases))
        for case in cases:
            with self.subTest(case_id=case["id"]):
                candidate = case["expected"]["candidates"][0]
                valid_until = candidate["valid_until"]
                self.assertEqual(20, len(valid_until))
                self.assertTrue(valid_until.endswith("T00:00:00Z"))
                self.assertIn(valid_until[:10], candidate["evidence_span"])

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
        self.assertEqual(MANIFEST_VERSION, manifest["manifest_version"])
        self.assertEqual(FROZEN_DEVELOPMENT_FILE_SHA256, manifest["development"]["file_sha256"])
        self.assertEqual(FROZEN_HOLDOUT_FILE_SHA256, manifest["holdout"]["file_sha256"])
        self.assertEqual(
            FROZEN_HOLDOUT_CASE_SHA256,
            manifest["holdout"]["case_set_sha256"],
        )
        self.assertEqual(
            {
                "caller_selectable_path": False,
                "ephemeral_environment_allowed": False,
                "key_sha256": holdout_claim_key_sha256(),
                "namespace": HOLDOUT_CLAIM_NAMESPACE,
                "scope": HOLDOUT_CLAIM_SCOPE,
            }, manifest["holdout"]["claim"])
        self.assertEqual(FROZEN_PROMPT_SHA256, manifest["prompt"]["sha256"])
        self.assertEqual(PROMPT_VERSION, manifest["prompt"]["version"])
        self.assertEqual(
            "prompts/memory_extraction_eval_v7.md",
            manifest["prompt"]["file"],
        )
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

    def test_prompt_uses_exact_candidate_type_enum_tokens(self) -> None:
        prompt = PROMPT.read_text(encoding="utf-8")
        for candidate_type in v1.CANDIDATE_TYPES:
            with self.subTest(candidate_type=candidate_type):
                self.assertIn(f"`{candidate_type}`", prompt)
        self.assertNotIn("fact claim", prompt)
        self.assertNotIn("validity/expiry candidates", prompt)

    def test_parent_directory_fsync_is_fail_closed_on_posix(self) -> None:
        path = Path("external") / "receipt.json"
        expected_flags = (
            eval_v2.os.O_RDONLY
            | getattr(eval_v2.os, "O_DIRECTORY", 0)
            | getattr(eval_v2.os, "O_CLOEXEC", 0)
            | getattr(eval_v2.os, "O_NOFOLLOW", 0)
        )
        with (
            patch.object(eval_v2.os, "name", "posix"),
            patch.object(eval_v2.os, "open", return_value=17) as open_directory,
            patch.object(eval_v2.os, "fsync") as fsync,
            patch.object(eval_v2.os, "close") as close,
        ):
            eval_v2._fsync_parent_directory(path)
        open_directory.assert_called_once_with(path.parent, expected_flags)
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

    def test_evaluate_case_propagates_cooperative_cancellation(self) -> None:
        class CancelledResponses:
            async def create(self, **_request: object) -> object:
                raise asyncio.CancelledError

        class CancelledClient:
            responses = CancelledResponses()

        async def run() -> None:
            await eval_v2.evaluate_case(
                CancelledClient(),
                asyncio.Semaphore(1),
                self.development[0],
                model="gpt-5.6-luna",
                reasoning_effort="none",
                max_output_tokens=1200,
                timeout_seconds=45,
                system_prompt="synthetic test prompt",
            )

        self.assertFalse(issubclass(asyncio.CancelledError, Exception))
        self.assertTrue(issubclass(asyncio.CancelledError, BaseException))
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())

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

    def test_validator_requires_exact_canonical_valid_until(self) -> None:
        case = next(
            case
            for case in self.development
            if case["tags"][0] == "validity_expiry" and case["language"] == "en"
        )
        canonical_prediction = deterministic_extract(case)
        _normalized, canonical_errors = validate_prediction(case, canonical_prediction)
        self.assertEqual([], canonical_errors)
        canonical = canonical_prediction["result"]["candidates"][0]["valid_until"]
        date = canonical[:10]
        for noncanonical in (
            date,
            f"{date}T00:00:00+00:00",
            f"{date}T00:00:00.000Z",
        ):
            with self.subTest(valid_until=noncanonical):
                prediction = copy.deepcopy(canonical_prediction)
                prediction["result"]["candidates"][0]["valid_until"] = noncanonical
                _normalized, errors = validate_prediction(case, prediction)
                self.assertTrue(
                    any("normalized_valid_until" in error for error in errors)
                )

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

    def test_holdout_receipt_is_canonical_atomic_and_survives_first_claim(self) -> None:
        attestation = build_selection_attestation(
            self._passing_matrix_reports(),
            source_commit="a" * 40,
        )
        selected = attestation["selected"]
        attestation_hash = selection_attestation_sha256(attestation)
        with tempfile.TemporaryDirectory() as directory:
            receipt_path = Path(directory) / "canonical-holdout-receipt.json"
            os.chmod(Path(directory), 0o700)

            def attempt() -> dict | None:
                try:
                    return reserve_holdout_once(
                        attestation_sha256=attestation_hash,
                        selected=selected,
                        source_commit="a" * 40,
                    )
                except ValueError:
                    return None

            with patch.object(
                eval_v2,
                "canonical_holdout_claim_path",
                return_value=receipt_path,
            ), patch.object(
                eval_v2,
                "_validate_durable_holdout_environment",
                return_value=None,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _index: attempt(), range(2)))
                self.assertEqual(1, sum(result is not None for result in results))
                self.assertTrue(receipt_path.exists())
                self.assertEqual(0o600, receipt_path.stat().st_mode & 0o777)
                with self.assertRaisesRegex(ValueError, "exclusive_artifact_exists"):
                    reserve_holdout_once(
                        attestation_sha256=attestation_hash,
                        selected=selected,
                        source_commit="a" * 40,
                    )

    @unittest.skipUnless(os.name == "posix", "canonical holdout is POSIX-only")
    def test_canonical_holdout_claim_ignores_home_cwd_and_manifest(self) -> None:
        original = canonical_holdout_claim_path()
        with tempfile.TemporaryDirectory() as alternate_home:
            with (
                patch.dict(os.environ, {"HOME": alternate_home}),
                patch.object(contract_v2, "FROZEN_MANIFEST_SHA256", "f" * 64),
            ):
                alternate = canonical_holdout_claim_path()
            self.assertEqual(original, alternate)
            self.assertEqual(
                eval_v2.HOLDOUT_CLAIM_DIRECTORY_NAME,
                original.parent.name,
            )
            self.assertEqual(f"{holdout_claim_key_sha256()}.json", original.name)
            previous_cwd = Path.cwd()
            try:
                os.chdir(alternate_home)
                self.assertEqual(original, canonical_holdout_claim_path())
            finally:
                os.chdir(previous_cwd)

    @unittest.skipUnless(os.name == "posix", "canonical holdout is POSIX-only")
    def test_private_claim_directory_is_durable_owner_only_and_not_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            with patch.object(eval_v2, "_fsync_parent_directory") as fsync_parent:
                eval_v2._ensure_private_claim_directory(private)
            fsync_parent.assert_called_once_with(private)
            self.assertEqual(0o700, private.stat().st_mode & 0o777)

            os.chmod(private, 0o750)
            with self.assertRaisesRegex(ValueError, "claim_directory_not_private"):
                eval_v2._ensure_private_claim_directory(private)

            os.chmod(private, 0o700)
            with patch.object(
                eval_v2.os,
                "geteuid",
                return_value=private.stat().st_uid + 1,
            ), self.assertRaisesRegex(ValueError, "claim_directory_wrong_owner"):
                eval_v2._ensure_private_claim_directory(private)

            private.rmdir()
            target = root / "target"
            target.mkdir(mode=0o700)
            private.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "claim_directory_invalid"):
                eval_v2._ensure_private_claim_directory(private)

    def test_holdout_claim_identity_constants_are_permanent(self) -> None:
        self.assertEqual("memory_extraction_v2_holdout", HOLDOUT_CLAIM_NAMESPACE)
        self.assertEqual(
            "frozen_holdout_content_per_effective_posix_user",
            HOLDOUT_CLAIM_SCOPE,
        )
        self.assertEqual(
            ".aigan-memory-extraction-holdout-claims",
            eval_v2.HOLDOUT_CLAIM_DIRECTORY_NAME,
        )

    @unittest.skipUnless(os.name == "posix", "canonical holdout is POSIX-only")
    def test_claim_directory_parent_sync_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            with (
                patch.object(
                    eval_v2,
                    "_fsync_parent_directory",
                    side_effect=OSError("sync failed"),
                ),
                self.assertRaisesRegex(ValueError, "claim_directory_sync_failed"),
            ):
                eval_v2._ensure_private_claim_directory(private)

    @unittest.skipUnless(os.name == "posix", "canonical holdout is POSIX-only")
    def test_claim_key_changes_only_with_frozen_holdout_content(self) -> None:
        original = holdout_claim_key_sha256()
        with patch.object(contract_v2, "FROZEN_MANIFEST_SHA256", "e" * 64):
            self.assertEqual(original, holdout_claim_key_sha256())
        with patch.object(contract_v2, "FROZEN_HOLDOUT_FILE_SHA256", "d" * 64):
            self.assertNotEqual(original, holdout_claim_key_sha256())

    @unittest.skipUnless(os.name == "posix", "canonical holdout is POSIX-only")
    def test_ci_holdout_environment_fails_closed(self) -> None:
        with (
            patch.dict(os.environ, {"CI": "true"}),
            self.assertRaisesRegex(ValueError, "ephemeral_ci_forbidden"),
        ):
            eval_v2._validate_durable_holdout_environment()

    def test_holdout_receipt_path_is_not_caller_selectable(self) -> None:
        argv = [
            "eval_memory_extraction_v2.py",
            "--holdout-receipt",
            str(Path("alternate") / "receipt.json"),
        ]
        errors = io.StringIO()
        with (
            patch.object(eval_v2, "run_api") as run_api,
            patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(errors),
            self.assertRaises(SystemExit),
        ):
            eval_v2.main()
        self.assertIn("unrecognized arguments: --holdout-receipt", errors.getvalue())
        run_api.assert_not_called()

    def test_eval_cli_read_failures_are_clean_and_pre_provider(self) -> None:
        api_argv = ["eval_memory_extraction_v2.py", "--mode", "api", "--limit", "48"]
        scenarios = (
            ("fixture_sha256", PermissionError(13, "Permission denied")),
            ("load_system_prompt", UnicodeDecodeError("utf-8", b"x", 0, 1, "bad")),
        )
        for target, failure in scenarios:
            with self.subTest(target=target):
                errors = io.StringIO()
                with (
                    patch.object(eval_v2, target, side_effect=failure),
                    patch.object(eval_v2, "run_api") as run_api,
                    patch.object(eval_v2, "current_clean_source_commit", return_value="a" * 40),
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test"}),
                    patch.object(sys, "argv", api_argv),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit),
                ):
                    eval_v2.main()
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertIn("evaluation inputs", errors.getvalue())
                run_api.assert_not_called()

    def test_api_preflight_artifact_read_failures_are_clean(self) -> None:
        scenarios = (
            ("manifest_sha256", PermissionError(13, "Permission denied")),
            (
                "evaluation_bundle_sha256",
                UnicodeDecodeError("utf-8", b"x", 0, 1, "bad"),
            ),
        )
        argv = ["eval_memory_extraction_v2.py", "--mode", "api", "--limit", "48"]
        for target, failure in scenarios:
            with self.subTest(target=target):
                errors = io.StringIO()
                with (
                    patch.object(eval_v2, target, side_effect=failure),
                    patch.object(eval_v2, "run_api") as run_api,
                    patch.object(
                        eval_v2,
                        "current_clean_source_commit",
                        return_value="a" * 40,
                    ),
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test"}),
                    patch.object(sys, "argv", argv),
                    contextlib.redirect_stderr(errors),
                    self.assertRaises(SystemExit),
                ):
                    eval_v2.main()
                self.assertNotIn("Traceback", errors.getvalue())
                self.assertIn("frozen evaluation artifacts", errors.getvalue())
                run_api.assert_not_called()

    def test_eval_cli_invalid_fixture_is_clean_and_pre_provider(self) -> None:
        errors = io.StringIO()
        private_marker = "private-user-fixture-id"
        with (
            patch.object(eval_v2, "load_fixture", side_effect=ValueError(private_marker)),
            patch.object(eval_v2, "run_api") as run_api,
            patch.object(eval_v2, "current_clean_source_commit", return_value="a" * 40),
            patch.dict(os.environ, {"OPENAI_API_KEY": "test"}),
            patch.object(
                sys,
                "argv",
                ["eval_memory_extraction_v2.py", "--mode", "api", "--limit", "48"],
            ),
            contextlib.redirect_stderr(errors),
            self.assertRaises(SystemExit),
        ):
            eval_v2.main()
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertIn("invalid evaluation fixture", errors.getvalue())
        self.assertNotIn(private_marker, errors.getvalue())
        run_api.assert_not_called()

    def test_undecodable_holdout_attestation_is_pre_provider_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attestation_path = root / "attestation.json"
            attestation_path.write_bytes(b"\xff")
            result_path = root / "holdout-result.json"
            argv = [
                "eval_memory_extraction_v2.py",
                "--fixture",
                str(HOLDOUT_FIXTURE),
                "--mode",
                "api",
                "--model",
                "gpt-5.6-luna",
                "--reasoning-effort",
                "low",
                "--repeats",
                "3",
                "--require-gates",
                "--acknowledge-holdout-manifest-sha256",
                FROZEN_MANIFEST_SHA256,
                "--development-attestation",
                str(attestation_path),
                "--acknowledge-development-attestation-sha256",
                "0" * 64,
                "--holdout-result",
                str(result_path),
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
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertIn("selection:attestation_read", errors.getvalue())
            run_api.assert_not_called()

    def test_selection_cli_output_oserror_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            report_path.write_text("{}\n", encoding="utf-8")
            output_path = root / "attestation.json"
            errors = io.StringIO()
            argv = [
                "select_memory_extraction_v2_candidate.py",
                str(report_path),
                "--output",
                str(output_path),
            ]
            with (
                patch.object(
                    select_v2,
                    "current_clean_source_commit",
                    return_value="a" * 40,
                ),
                patch.object(
                    select_v2,
                    "build_selection_attestation",
                    return_value={"schema_version": "test"},
                ),
                patch.object(
                    select_v2.os,
                    "open",
                    side_effect=PermissionError(13, "Permission denied"),
                ),
                patch.object(sys, "argv", argv),
                contextlib.redirect_stderr(errors),
                self.assertRaises(SystemExit),
            ):
                select_v2.main()
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertIn(
                "cannot create attestation output: Permission denied",
                errors.getvalue(),
            )

    def test_selection_cli_undecodable_report_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            report_path.write_bytes(b"\xff")
            output_path = root / "attestation.json"
            errors = io.StringIO()
            argv = [
                "select_memory_extraction_v2_candidate.py",
                str(report_path),
                "--output",
                str(output_path),
            ]
            with (
                patch.object(sys, "argv", argv),
                contextlib.redirect_stderr(errors),
                self.assertRaises(SystemExit),
            ):
                select_v2.main()
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertIn("cannot decode aggregate report", errors.getvalue())

    def test_holdout_artifact_oserror_becomes_bounded_value_error(self) -> None:
        with (
            patch.object(
                eval_v2.os,
                "open",
                side_effect=PermissionError(13, "Permission denied"),
            ),
            self.assertRaisesRegex(ValueError, "holdout:artifact_write_failed"),
        ):
            eval_v2._write_exclusive_private_json(
                Path("external") / "claim.json",
                {"status": "claimed_before_api"},
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
        self.assertEqual({"validation_provider_failure"}, bucket_keys)
        self.assertTrue(all(key.startswith("validation_") for key in bucket_keys))
        self.assertFalse(any("evidence_span" in key for key in bucket_keys))
        serialized_buckets = json.dumps(
            report["safety"]["error_buckets"],
            sort_keys=True,
        )
        private_markers = {"wrong", positive["id"]}
        private_markers.update(row["row_key"] for row in positive["inputs"])
        for marker in private_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, serialized_buckets)

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
