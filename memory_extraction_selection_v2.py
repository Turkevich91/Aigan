from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from memory_extraction_v2 import (
    API_ARMS,
    FROZEN_DETERMINISTIC_BASELINE_SHA256,
    FROZEN_DEVELOPMENT_CASE_SHA256,
    FROZEN_DEVELOPMENT_FILE_SHA256,
    FROZEN_EVALUATION_BUNDLE_SHA256,
    FROZEN_MANIFEST_SHA256,
    FROZEN_OUTPUT_SCHEMA_SHA256,
    FROZEN_PRICING_SNAPSHOT_SHA256,
    FROZEN_PROMPT_SHA256,
    FROZEN_RUN_MATRIX_SHA256,
    FROZEN_SCREEN_CASE_SHA256,
    aggregate_report_has_private_fields,
)


SELECTION_ATTESTATION_VERSION = "memory_extraction_selection_v1"
SELECTION_RULE = "least_measured_cost_then_p95"


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _arm(report: Mapping[str, Any]) -> tuple[str, str]:
    run = report.get("run")
    _require(isinstance(run, Mapping), "selection:report_run")
    return str(run.get("model", "")), str(run.get("reasoning_effort", ""))


def _assert_common_report(
    report: Mapping[str, Any],
    *,
    source_commit: str,
) -> None:
    _require(not aggregate_report_has_private_fields(report), "selection:private_report")
    _require(report.get("runtime_authorized") is False, "selection:runtime_authorized")
    versions = report.get("versions")
    fixture = report.get("fixture")
    run = report.get("run")
    provider = report.get("provider")
    gates = report.get("gates")
    _require(isinstance(versions, Mapping), "selection:versions")
    _require(isinstance(fixture, Mapping), "selection:fixture")
    _require(isinstance(run, Mapping), "selection:run")
    _require(isinstance(provider, Mapping), "selection:provider")
    _require(isinstance(gates, Mapping) and gates, "selection:gates")
    expected_versions = {
        "output_schema_sha256": FROZEN_OUTPUT_SCHEMA_SHA256,
        "deterministic_baseline_sha256": FROZEN_DETERMINISTIC_BASELINE_SHA256,
        "prompt_sha256": FROZEN_PROMPT_SHA256,
        "evaluation_bundle_sha256": FROZEN_EVALUATION_BUNDLE_SHA256,
        "run_matrix_sha256": FROZEN_RUN_MATRIX_SHA256,
        "manifest_sha256": FROZEN_MANIFEST_SHA256,
        "pricing_snapshot_sha256": FROZEN_PRICING_SNAPSHOT_SHA256,
        "source_commit": source_commit,
    }
    for field, expected in expected_versions.items():
        _require(versions.get(field) == expected, f"selection:version:{field}")
    _require(fixture.get("split") == "development", "selection:development_split")
    _require(
        fixture.get("sha256") == FROZEN_DEVELOPMENT_FILE_SHA256,
        "selection:development_file_hash",
    )
    _require(run.get("concurrency") == 6, "selection:concurrency")
    _require(run.get("timeout_seconds") == 45, "selection:timeout")
    _require(run.get("max_output_tokens") == 1200, "selection:max_output_tokens")
    _require(run.get("store") is False, "selection:store")
    arm = _arm(report)
    _require(arm in API_ARMS, "selection:arm")
    actual_models = provider.get("actual_models")
    actual_efforts = provider.get("actual_efforts")
    _require(
        isinstance(actual_models, list),
        "selection:actual_models",
    )
    _require(
        isinstance(actual_efforts, list),
        "selection:actual_efforts",
    )
    _require(
        all(isinstance(value, bool) for value in gates.values()),
        "selection:gate_types",
    )


def _assert_passing_provider_snapshot(report: Mapping[str, Any]) -> None:
    arm = _arm(report)
    provider = report["provider"]
    _require(
        len(provider["actual_models"]) == 1,
        "selection:actual_model_snapshot",
    )
    _require(
        provider["actual_efforts"] == [arm[1]],
        "selection:actual_effort",
    )


def _assert_screen_report(report: Mapping[str, Any], *, source_commit: str) -> None:
    _assert_common_report(report, source_commit=source_commit)
    fixture = report["fixture"]
    run = report["run"]
    gates = report["gates"]
    _require(report.get("phase") == "screen", "selection:screen_phase")
    _require(fixture.get("unique_case_count") == 48, "selection:screen_count")
    _require(
        fixture.get("case_set_sha256") == FROZEN_SCREEN_CASE_SHA256,
        "selection:screen_hash",
    )
    _require(fixture.get("evaluation_instance_count") == 48, "selection:screen_instances")
    _require(run.get("repeat_count") == 1, "selection:screen_repeats")
    passed = all(gates.values())
    if passed:
        _assert_passing_provider_snapshot(report)
    expected_verdict = "PASS_FOR_LOCKED_DEVELOPMENT" if passed else "SCREEN_FAIL"
    _require(report.get("verdict") == expected_verdict, "selection:screen_verdict")


def _assert_development_report(
    report: Mapping[str, Any],
    *,
    source_commit: str,
) -> None:
    _assert_common_report(report, source_commit=source_commit)
    fixture = report["fixture"]
    run = report["run"]
    gates = report["gates"]
    _require(
        report.get("phase") == "locked_development",
        "selection:development_phase",
    )
    _require(fixture.get("unique_case_count") == 160, "selection:development_count")
    _require(
        fixture.get("case_set_sha256") == FROZEN_DEVELOPMENT_CASE_SHA256,
        "selection:development_hash",
    )
    _require(
        fixture.get("evaluation_instance_count") == 480,
        "selection:development_instances",
    )
    _require(run.get("repeat_count") == 3, "selection:development_repeats")
    passed = all(gates.values())
    if passed:
        _assert_passing_provider_snapshot(report)
    expected_verdict = "GO_FOR_HOLDOUT_CANDIDATE" if passed else "NO_GO"
    _require(report.get("verdict") == expected_verdict, "selection:development_verdict")


def build_selection_attestation(
    reports: Sequence[Mapping[str, Any]],
    *,
    source_commit: str,
) -> dict[str, Any]:
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", source_commit)), "selection:source_commit")
    screen_by_arm: dict[tuple[str, str], Mapping[str, Any]] = {}
    development_by_arm: dict[tuple[str, str], Mapping[str, Any]] = {}
    for report in reports:
        _require(isinstance(report, Mapping), "selection:report_object")
        phase = report.get("phase")
        arm = _arm(report)
        if phase == "screen":
            _require(arm not in screen_by_arm, "selection:duplicate_screen_arm")
            _assert_screen_report(report, source_commit=source_commit)
            screen_by_arm[arm] = report
        elif phase == "locked_development":
            _require(arm not in development_by_arm, "selection:duplicate_development_arm")
            _assert_development_report(report, source_commit=source_commit)
            development_by_arm[arm] = report
        else:
            raise ValueError("selection:report_phase")
    _require(set(screen_by_arm) == set(API_ARMS), "selection:screen_matrix_incomplete")
    screen_pass_arms = {
        arm
        for arm, report in screen_by_arm.items()
        if report["verdict"] == "PASS_FOR_LOCKED_DEVELOPMENT"
    }
    _require(
        set(development_by_arm) == screen_pass_arms,
        "selection:development_matrix_incomplete",
    )
    passing = [
        (arm, development_by_arm[arm])
        for arm in API_ARMS
        if arm in development_by_arm
        and development_by_arm[arm]["verdict"] == "GO_FOR_HOLDOUT_CANDIDATE"
    ]
    selected: dict[str, Any] | None = None
    verdict = "NO_GO"
    if passing:
        ranked: list[tuple[tuple[int, int, int], tuple[str, str], Mapping[str, Any]]] = []
        for arm, report in passing:
            cost = report["usage"].get("estimated_cost_nano_usd")
            latency = report["latency_ms"].get("p95")
            _require(isinstance(cost, int) and cost > 0, "selection:measured_cost")
            _require(isinstance(latency, int) and latency >= 0, "selection:p95")
            ranked.append(((cost, latency, API_ARMS.index(arm)), arm, report))
        _rank, arm, report = min(ranked, key=lambda item: item[0])
        selected = {
            "model": arm[0],
            "reasoning_effort": arm[1],
            "actual_models": copy.deepcopy(report["provider"]["actual_models"]),
            "actual_efforts": copy.deepcopy(report["provider"]["actual_efforts"]),
            "estimated_cost_nano_usd": report["usage"]["estimated_cost_nano_usd"],
            "latency_p95_ms": report["latency_ms"]["p95"],
        }
        verdict = "CANDIDATE_SELECTED"
    return {
        "schema_version": SELECTION_ATTESTATION_VERSION,
        "manifest_sha256": FROZEN_MANIFEST_SHA256,
        "run_matrix_sha256": FROZEN_RUN_MATRIX_SHA256,
        "source_commit": source_commit,
        "selection_rule": SELECTION_RULE,
        "screen_reports": [copy.deepcopy(screen_by_arm[arm]) for arm in API_ARMS],
        "development_reports": [
            copy.deepcopy(development_by_arm[arm])
            for arm in API_ARMS
            if arm in development_by_arm
        ],
        "verdict": verdict,
        "selected": selected,
    }


def validate_selection_attestation(attestation: Mapping[str, Any]) -> dict[str, Any]:
    _require(isinstance(attestation, Mapping), "selection:attestation_object")
    _require(
        set(attestation)
        == {
            "schema_version",
            "manifest_sha256",
            "run_matrix_sha256",
            "source_commit",
            "selection_rule",
            "screen_reports",
            "development_reports",
            "verdict",
            "selected",
        },
        "selection:attestation_keys",
    )
    reports = list(attestation.get("screen_reports", [])) + list(
        attestation.get("development_reports", [])
    )
    rebuilt = build_selection_attestation(
        reports,
        source_commit=str(attestation.get("source_commit", "")),
    )
    _require(dict(attestation) == rebuilt, "selection:attestation_recomputed")
    return rebuilt


def selection_attestation_sha256(attestation: Mapping[str, Any]) -> str:
    validated = validate_selection_attestation(attestation)
    encoded = json.dumps(
        validated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_selection_attestation(path: Path | str) -> tuple[dict[str, Any], str]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("selection:attestation_read") from exc
    validated = validate_selection_attestation(payload)
    return validated, selection_attestation_sha256(validated)


def load_screen_admission(
    path: Path | str,
    *,
    source_commit: str,
    expected_arm: tuple[str, str],
) -> tuple[dict[str, Any], str]:
    report_path = Path(path)
    try:
        raw = report_path.read_bytes()
        report = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("selection:screen_report_read") from exc
    _require(isinstance(report, Mapping), "selection:screen_report_object")
    _assert_screen_report(report, source_commit=source_commit)
    _require(
        report.get("verdict") == "PASS_FOR_LOCKED_DEVELOPMENT",
        "selection:screen_not_admitted",
    )
    _require(_arm(report) == expected_arm, "selection:screen_arm_mismatch")
    return copy.deepcopy(dict(report)), hashlib.sha256(raw).hexdigest()


def current_clean_source_commit(root: Path | str) -> str:
    repository = Path(root)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    _require(not status.stdout.strip(), "selection:source_not_clean")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "selection:source_commit")
    return commit
