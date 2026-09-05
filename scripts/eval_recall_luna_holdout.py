"""One custodially released, integrated recall holdout experiment.

Freeze never opens the holdout. Run consumes its durable authorization before
parsing it; provider calls require an explicit flag and the isolated trial key.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import random
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recall_admission_eval import (
    BASELINE_SHA, BudgetRefused, CachedEmbeddingProvider, EvaluationState,
    confusion, digest, file_hash, paired_case_outcomes, paired_intervals,
    private_directory, state_identity, validate_fixture, write_private_json,
)
from recall_intent_model import (
    MODEL, REASONING_EFFORT, TIMEOUT_SECONDS, MAX_OUTPUT_TOKENS,
    classify_recall_intent, recall_model_metadata, recall_model_request, reservation_nano_usd,
)
from runtime_model_pricing import PRICE_SNAPSHOT_VERSION, TokenUsage, estimate_token_cost
from scripts.eval_recall_admission import (
    BASELINE_MAIN_HASH, close_apps, config_binding, load_apps, runtime_versions,
    synthetic_message,
)

MANIFEST_FILE = "tests/fixtures/memory_recall_179_v2_manifest.json"
PROTOCOL_FILE = "docs/recall-admission-179-v2-eval.md"
EVALUATOR_FILES = (
    "scripts/eval_recall_luna_holdout.py", "scripts/eval_recall_admission.py",
    "recall_admission_eval.py", "tests/support.py", "tests/test_recall_luna_holdout.py",
)
CANDIDATE_FILES = ("main.py", "memory_recall.py", "recall_intent_model.py", "runtime_model_pricing.py")
REPETITIONS, CASES, SEED = 3, 72, 179
SCHEDULE = [
    {"repetition": repetition, "case_index": index}
    for repetition in range(REPETITIONS)
    for index in random.Random(SEED + repetition).sample(range(CASES), CASES)
]
SCHEDULE_SHA = digest(SCHEDULE)
PROTOCOL = {
    "version": "recall-luna-integrated-holdout-v2", "baseline_sha": BASELINE_SHA,
    "repetitions": REPETITIONS, "cases_per_repetition": CASES,
    "schedule_sha256": SCHEDULE_SHA, "model": MODEL, "reasoning_effort": REASONING_EFFORT,
    "timeout_seconds": TIMEOUT_SECONDS, "max_output_tokens": MAX_OUTPUT_TOKENS,
    "max_retries": 0, "store": False, "provider_concurrency": 1,
    "price_snapshot": PRICE_SNAPSHOT_VERSION,
    "luna_rates_usd_per_million": {"input": .2, "output": 1.2},
    "bootstrap_repetitions": 10000, "bootstrap_seed": 179,
    "candidate_mode": "enforce", "baseline_repetitions": 1,
    "boundary_scope": "actual_text_message_admission_downstream_generation_spy",
}


class FatalEvaluationAbort(BaseException):
    """Administrative failures must bypass the application's ordinary fallback."""


def source_maps():
    common = sorted({str(path.relative_to(ROOT)).replace("\\", "/")
                     for pattern in ("*.py", "mcp_servers/**/*.py") for path in ROOT.glob(pattern)}
                    - set(CANDIDATE_FILES) - set(EVALUATOR_FILES))
    return {"candidate": {name: file_hash(ROOT / name) for name in CANDIDATE_FILES},
            "evaluator": {name: file_hash(ROOT / name) for name in EVALUATOR_FILES},
            "common": {name: file_hash(ROOT / name) for name in common}}


def candidate_source_digest(maps):
    return digest({**maps["common"], **maps["candidate"]})


def configuration(apps):
    config = config_binding(apps)
    candidate = apps["candidate"]
    if (candidate.CONFIG.memory_recall_policy_mode != "enforce"
            or not callable(getattr(candidate, "run_recall_intent_classifier", None))
            or getattr(candidate, "RECALL_INTENT_MODEL", None) != MODEL):
        raise ValueError("integrated_luna_enforce_adapter_required")
    config["candidate"]["classifier"] = {
        "request_semantics_sha256": digest(recall_model_request({
            "trusted_text": "synthetic freeze binding", "has_reply_text": False, "has_reply_image": False})),
        "protocol": PROTOCOL,
    }
    # Bind the current rate implementation, not the unrelated historical catalog.
    # Probe ordinary-size requests: a million-token input intentionally invokes
    # the separate long-context surcharge and is not the base-rate contract.
    for usage, expected in ((TokenUsage(1_000, output_tokens=0), 200_000),
                            (TokenUsage(0, output_tokens=1_000), 1_200_000)):
        if estimate_token_cost(MODEL, [usage]).nano_usd != expected:
            raise ValueError("frozen_luna_rate_mismatch")
    return config


def prepare_output(path):
    output = private_directory(path, repository=ROOT, create=True)
    if any(output.iterdir()):
        raise ValueError("output_must_be_empty")
    return output


def freeze(args):
    output = prepare_output(args.output)
    state = EvaluationState(private_directory(args.state, repository=ROOT))
    manifest = json.loads((ROOT / MANIFEST_FILE).read_text())
    if manifest["protocol"]["sha256"] != file_hash(ROOT / PROTOCOL_FILE):
        raise ValueError("protocol_manifest_mismatch")
    development = json.loads(args.development_report.read_text())
    if (development.get("completion") != "COMPLETE"
            or development.get("completed_decisions") != 288
            or development.get("promotion") != "NOT_ASSESSED_OBSERVED_DEVELOPMENT_ONLY"):
        raise ValueError("complete_observed_development_required")
    cache = json.loads(args.development_cache_manifest.read_text())
    state.verify_cache_manifest(cache, allow_additions=True)
    with tempfile.TemporaryDirectory(prefix="recall-v2-freeze-") as temporary:
        apps = load_apps(args.baseline_source, Path(temporary))
        try:
            config = configuration(apps)
        finally:
            close_apps(apps)
    frozen = {
        "schema_version": "recall-luna-integrated-freeze-v2", "protocol": PROTOCOL,
        "protocol_sha256": file_hash(ROOT / PROTOCOL_FILE),
        "fixture_manifest_sha256": file_hash(ROOT / MANIFEST_FILE),
        "fixture_sha256": manifest["holdout"]["sha256"],
        "baseline_source_path": str(args.baseline_source.resolve()),
        "baseline_main_sha256": BASELINE_MAIN_HASH, "sources": source_maps(),
        "config": config, "config_sha256": digest(config), "runtime": runtime_versions(),
        "state_identity": state.identity(), "initial_cache_manifest": state.cache_manifest(),
        "initial_accounting": state.accounting(), "schedule": SCHEDULE,
        "development_report_path": str(args.development_report.resolve()),
        "development_report_sha256": file_hash(args.development_report),
        "development_cache_manifest_path": str(args.development_cache_manifest.resolve()),
        "development_cache_manifest_sha256": file_hash(args.development_cache_manifest),
    }
    write_private_json(output / "source-freeze.json", frozen)
    return {"completion": "FROZEN", "freeze_sha256": file_hash(output / "source-freeze.json"),
            "authorization_bindings": authorization_bindings(frozen)}


def authorization_bindings(frozen):
    return {**frozen["state_identity"],
            **{key: frozen[key] for key in (
                "fixture_sha256", "fixture_manifest_sha256", "protocol_sha256",
                "development_report_sha256", "development_cache_manifest_sha256")},
            "evaluator_source_sha256": digest(frozen["sources"]["evaluator"]),
            "candidate_source_sha256": candidate_source_digest(frozen["sources"]),
            "candidate_config_sha256": digest(frozen["config"]["candidate"]),
            "experiment_schedule_sha256": SCHEDULE_SHA}


def claim_experiment(directory, authorization, bindings):
    expected = {"schema_version": "recall179-holdout-authorization-v2", "authorized": True,
                "scope": "one_three_repetition_holdout_experiment_no_tuning"}
    if any(authorization.get(key) != value for key, value in expected.items()):
        raise ValueError("invalid_v2_authorization")
    if (not isinstance(authorization.get("nonce"), str) or not authorization["nonce"]
            or any(authorization.get(key) != value for key, value in bindings.items())):
        raise ValueError("authorization_binding_mismatch")
    if state_identity(directory) != {key: bindings[key] for key in
                                     ("state_directory_sha256", "state_ledger_uuid")}:
        raise ValueError("authorization_state_mismatch")
    claim = Path(directory) / ("holdout-" + bindings["fixture_sha256"] + ".claim.json")
    write_private_json(claim, {"authorization_sha256": digest(authorization), "bindings": bindings,
                               "nonce": authorization["nonce"], "claimed_at": time.time()})
    return claim


def append_private(path, row):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class BudgetedClassifier:
    def __init__(self, state, *, api_key, classifier=classify_recall_intent):
        self.state, self.api_key, self.classifier = state, api_key, classifier
        self.attempts = []
        self.fatal_error = None

    async def __call__(self, metadata, **kwargs):
        if self.fatal_error:
            raise FatalEvaluationAbort(self.fatal_error)
        reserved = reservation_nano_usd(metadata)
        try:
            token = self.state.reserve(reserved, phase="recall179_luna_holdout_v2",
                                       request_hash=digest(recall_model_request(metadata)))
        except BaseException as exc:
            self.fatal_error = type(exc).__name__
            raise FatalEvaluationAbort(self.fatal_error) from None
        event = {"attempt_id": token, "reserved_nano_usd": reserved,
                 "request_sha256": digest(recall_model_request(metadata)), "model": None,
                 "administrative_abort": False}
        self.attempts.append(event)
        started, settled = time.perf_counter(), False
        try:
            result = await self.classifier(metadata, api_key=self.api_key)
            event["model"] = asdict(result)
            # Mark before settling: a reservation overrun has already been charged.
            settled = True
            if result.cost["complete"]:
                self.state.settle(token, tokens=result.usage.input_tokens + result.usage.output_tokens,
                                  cost_nano_usd=result.cost["nano_usd"], error_class=result.failure_class)
            else:
                self.state.settle(token, error_class=result.failure_class or "UnknownUsage")
            if result.failure_class == "ProviderIdentityMismatch":
                raise ValueError("provider_identity_mismatch")
            return result
        except BaseException as exc:
            event["administrative_abort"] = True
            event["error_class"] = type(exc).__name__
            self.fatal_error = type(exc).__name__
            if not settled:
                self.state.settle(token, error_class=type(exc).__name__)
            raise FatalEvaluationAbort(self.fatal_error) from None
        finally:
            event["observer_wall_ms"] = (time.perf_counter() - started) * 1000


async def boundary_outcome(app, case, repetition):
    message = synthetic_message(case, boundary=True)
    context = SimpleNamespace(bot=SimpleNamespace(id=999, username="evaluation_bot"))
    handle, detect, embed, classifier = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    with ExitStack() as stack:
        for name, replacement in {
            "remember_message_persistently": AsyncMock(), "remember_observed_message": Mock(),
            "remember_self_complaint_signal": Mock(), "handle_prompt": handle,
            "detect_memory_recall_intent": detect, "create_embeddings": embed,
            "classify_recall_intent": classifier,
        }.items():
            stack.enter_context(patch.object(app, name, replacement))
        await app.text_message(SimpleNamespace(effective_message=message), context)
    observed = {"invocation_eligible": bool(handle.await_count), "recall_stage_calls": detect.await_count,
                "classifier_calls": classifier.await_count,
                "provider_calls": embed.await_count + classifier.await_count,
                "telegram_deliveries": len(message.reply_calls) + len(message.photo_calls)
                                       + len(message.media_group_calls)}
    expected = case["expected"]
    unknown = set(expected) - set(observed)
    passed = not unknown and all(value is None or observed[key] == value for key, value in expected.items())
    return {"case_id": case["case_id"], "repetition": repetition, "passed": passed,
            "observed": observed, "scope": PROTOCOL["boundary_scope"]}


def admission(result, wall_ms):
    if not isinstance(result.is_recall, bool):
        raise ValueError("non_boolean_adapter_decision")
    return {"is_recall": result.is_recall, "reason": result.reason, "degraded": result.degraded,
            "confidence": result.confidence, "adapter_wall_ms": wall_ms}


def request_plan(fixture):
    requests = []
    for entry in SCHEDULE:
        case = fixture["cases"][entry["case_index"]]
        metadata = recall_model_metadata(case["prompt"], has_reply_text=case["context"]["has_reply_text"],
                                         has_reply_image=case["context"]["has_reply_image"])
        requests.append({**entry, "case_id": case["case_id"],
                         "request_sha256": digest(recall_model_request(metadata)),
                         "reserved_nano_usd": reservation_nano_usd(metadata)})
    return requests


async def evaluate(apps, fixture, provider, observer, output, rows, boundaries):
    async def embeddings(texts, **kwargs):
        return provider.embed(texts)
    for app in apps.values():
        app.create_embeddings = embeddings
    apps["candidate"].classify_recall_intent = observer
    baselines = {}
    for case in fixture["cases"]:
        started = time.perf_counter()
        result = await apps["baseline"].detect_memory_recall_intent(synthetic_message(case), case["prompt"])
        baselines[case["case_id"]] = admission(result, (time.perf_counter() - started) * 1000)
        if provider.failure or result.degraded:
            raise FatalEvaluationAbort("baseline_degraded")
    write_private_json(output / "private-baseline.json", baselines)
    for entry in SCHEDULE:
        case, repetition = fixture["cases"][entry["case_index"]], entry["repetition"]
        row = {"case_id": case["case_id"], "family_id": case["family_id"], "language": case["language"],
               "expected": case["expected"]["is_recall"], "critical_negative": case["expected"]["critical_negative"],
               "repetition": repetition, "baseline": baselines[case["case_id"]],
               "candidate": None, "administrative_abort": False, "attempts": []}
        offset, started = len(observer.attempts), time.perf_counter()
        try:
            result = await apps["candidate"].detect_memory_recall_intent(synthetic_message(case), case["prompt"])
            if observer.fatal_error or provider.failure:
                raise FatalEvaluationAbort(observer.fatal_error or provider.failure)
            row["candidate"] = admission(result, (time.perf_counter() - started) * 1000)
        except BaseException as exc:
            row["administrative_abort"] = True
            row["error_class"] = type(exc).__name__
            raise
        finally:
            row["attempts"] = observer.attempts[offset:]
            rows.append(row)
            append_private(output / "private-outcomes.jsonl", row)
        if len(rows) % CASES == 0:
            for case in fixture["boundary_cases"]:
                boundaries.append(await boundary_outcome(apps["candidate"], case, repetition))
            print(json.dumps({"stage": "recall_v2_holdout", "completed_decisions": len(rows),
                              "planned_decisions": CASES * REPETITIONS}), flush=True)


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def valid_rows(rows):
    return [row for row in rows if row.get("candidate") is not None and not row.get("administrative_abort")]


def summarize(rows, boundaries, *, bootstrap_repetitions=10000):
    reports = {}
    for repetition in range(REPETITIONS):
        selected = [row for row in rows if row["repetition"] == repetition]
        scored = valid_rows(selected)
        attempts = [attempt for row in selected for attempt in row["attempts"]]
        models = [attempt["model"] for attempt in attempts if attempt.get("model")]
        paired = []
        for row in scored:
            degraded = row["candidate"]["degraded"] or any(
                attempt.get("model") is None or attempt["model"]["status"] != "succeeded"
                or attempt["model"]["intent"] is None for attempt in row["attempts"])
            paired.append({**row, "candidate": {**row["candidate"], "degraded": degraded}})
        baseline, candidate = confusion(paired, "baseline"), confusion(paired, "candidate")
        latencies = [attempt["model"]["latency_ms"] if attempt.get("model") else attempt["observer_wall_ms"]
                     for attempt in attempts]
        boundary = [item for item in boundaries if item["repetition"] == repetition]
        invalid = sum(attempt.get("model") is None or attempt["model"]["status"] != "succeeded"
                      or attempt["model"]["intent"] is None for attempt in attempts)
        p95 = percentile(latencies, .95)
        complete = len(paired) == CASES and len({row["case_id"] for row in paired}) == CASES
        aborts = sum(row.get("administrative_abort", False) for row in selected)
        counts = Counter(row["case_id"] for row in selected)
        known = sum(model["cost"]["nano_usd"] for model in models if model["cost"]["complete"])
        unknown = sum(attempt["reserved_nano_usd"] for attempt in attempts
                      if not attempt.get("model") or not attempt["model"]["cost"]["complete"])
        report = {
            "complete": complete, "scored_cases": len(paired), "baseline": baseline, "candidate": candidate,
            "duplicate_cases": sum(count > 1 for count in counts.values()),
            "administrative_aborts": aborts, "invalid_provider_outcomes": invalid,
            "classifier_attempts": len(attempts), "zero_call_decisions": sum(not row["attempts"] for row in selected),
            "multiple_call_decisions": sum(len(row["attempts"]) > 1 for row in selected),
            "unexplained_zero_call_decisions": sum(not row["attempts"] and row["candidate"]["reason"] != "excluded_route"
                                                    for row in scored),
            "classifier_latency_ms": {"p50": percentile(latencies, .5), "p95": p95},
            "adapter_latency_ms": {"p95": percentile([row["candidate"]["adapter_wall_ms"] for row in scored], .95)},
            "boundary_cases": len(boundary), "boundary_violations": sum(not row["passed"] for row in boundary),
            "provider_status_counts": dict(Counter(str(model["provider_status"]) for model in models)),
            "actual_model_counts": dict(Counter(str(model["actual_model"]) for model in models)),
            "failure_classes": dict(Counter(model["failure_class"] for model in models if model["failure_class"])),
            "cost": {"known_usd": known / 1e9, "unknown_reserved_usd": unknown / 1e9},
            "by_language": {language: {arm: confusion([row for row in paired if row["language"] == language], arm)
                                       for arm in ("baseline", "candidate")} for language in ("ua", "ru", "en")},
            "paired_case_outcomes": paired_case_outcomes(paired),
        }
        if paired:
            report["paired_family_bootstrap"] = paired_intervals(paired, repetitions=bootstrap_repetitions, seed=SEED)
        report["gate_pass"] = bool(
            complete and len(selected) == CASES and len(boundary) == 12
            and not report["boundary_violations"] and not aborts and not invalid
            and not report["multiple_call_decisions"] and not report["unexplained_zero_call_decisions"]
            and not baseline["degraded"] and not candidate["degraded"]
            and candidate["tp"] >= 33 and candidate["tp"] - baseline["tp"] >= 8
            and candidate["fp"] <= 1 and candidate["critical_fp"] == 0 and p95 is not None and p95 <= 3000)
        reports[str(repetition)] = report
    groups = defaultdict(list)
    for row in rows:
        groups[row["case_id"]].append(row)
    complete_groups = {key: group for key, group in groups.items()
                       if len(group) == REPETITIONS and {row["repetition"] for row in group} == set(range(REPETITIONS))
                       and len(valid_rows(group)) == REPETITIONS}
    stable = sum(len({row["candidate"]["is_recall"] for row in group}) == 1 for group in complete_groups.values())
    flips = [{"case_id": key, "decisions": [row["candidate"]["is_recall"] for row in sorted(group, key=lambda row: row["repetition"])]}
             for key, group in complete_groups.items() if len({row["candidate"]["is_recall"] for row in group}) > 1]
    return {"completed_decisions": len(rows), "per_repetition": reports,
            "stability": {"complete_cases": len(complete_groups), "stable_cases": stable,
                          "required_stable_cases": 69, "denominator": CASES, "decision_flips": flips},
            "gate_pass": len(rows) == CASES * REPETITIONS and stable >= 69
                         and all(report["gate_pass"] for report in reports.values())}


def run(args):
    if not args.allow_provider or not os.environ.get("AIGAN_EVAL_API_KEY"):
        raise ValueError("explicit_provider_flag_and_trial_key_required")
    frozen = json.loads(args.freeze.read_text())
    if (frozen["sources"] != source_maps() or frozen["protocol"] != PROTOCOL
            or frozen["runtime"] != runtime_versions() or frozen["schedule"] != SCHEDULE):
        raise ValueError("source_runtime_or_schedule_changed")
    if (frozen["fixture_manifest_sha256"] != file_hash(ROOT / MANIFEST_FILE)
            or frozen["protocol_sha256"] != file_hash(ROOT / PROTOCOL_FILE)):
        raise ValueError("custodial_protocol_changed")
    state = EvaluationState(private_directory(args.state, repository=ROOT))
    if state.identity() != frozen["state_identity"]:
        raise ValueError("state_identity_changed")
    cache_checks = {"freeze": state.verify_cache_manifest(frozen["initial_cache_manifest"], allow_additions=True)}
    for artifact in ("development_report", "development_cache_manifest"):
        if file_hash(Path(frozen[artifact + "_path"])) != frozen[artifact + "_sha256"]:
            raise ValueError("development_evidence_changed")
    cache_checks["development"] = state.verify_cache_manifest(
        json.loads(Path(frozen["development_cache_manifest_path"]).read_text()), allow_additions=True)
    output = prepare_output(args.output)
    with tempfile.TemporaryDirectory(prefix="recall-v2-preflight-") as temporary:
        apps = load_apps(Path(frozen["baseline_source_path"]), Path(temporary))
        try:
            if configuration(apps) != frozen["config"]:
                raise ValueError("effective_config_changed")
        finally:
            close_apps(apps)
    if file_hash(args.fixture) != frozen["fixture_sha256"]:
        raise ValueError("fixture_changed")
    claim_experiment(state.directory, json.loads(args.authorization.read_text()), authorization_bindings(frozen))
    # No holdout JSON was parsed before the exclusive, retained claim above.
    report = {"completion": "INCOMPLETE", "freeze_sha256": file_hash(args.freeze), "protocol": PROTOCOL,
              "fixture_sha256": frozen["fixture_sha256"], "state_identity": state.identity(),
              "cache_validation": cache_checks, "initial_accounting": state.accounting(),
              "human_gold": False, "label_authority": "independent_agent_synthetic"}
    rows, boundaries, client = [], [], None
    try:
        fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        validate_fixture(fixture)
        if fixture["fixture_id"] != "recall-admission-179-holdout-v2" or fixture["split"] != "holdout":
            raise ValueError("wrong_holdout_version")
        plan = request_plan(fixture)
        remaining = state.accounting()
        # Full candidate requests plus a conservative finite embedding prefetch.
        embedding_reserve = (sum(len(case["prompt"].encode("utf-8")) for case in fixture["cases"])
                             + 64_000) * 20
        maximum = sum(entry["reserved_nano_usd"] for entry in plan) + embedding_reserve
        available = round((remaining["cap_usd"] - remaining["known_usd"] - remaining["unknown_reserved_usd"]) * 1e9)
        if maximum > available:
            raise BudgetRefused("complete_experiment_does_not_fit_remaining_budget")
        write_private_json(output / "private-request-plan.json", {"entries": plan, "sha256": digest(plan),
                                                                 "maximum_reserved_nano_usd": maximum})
        write_private_json(output / "run-started.json", {"freeze_sha256": file_hash(args.freeze),
                                                       "started_at": time.time(), "schedule_sha256": SCHEDULE_SHA})
        from openai import OpenAI
        key = os.environ["AIGAN_EVAL_API_KEY"]
        client = OpenAI(api_key=key, max_retries=0, timeout=45, base_url="https://api.openai.com/v1")
        provider = CachedEmbeddingProvider(state, phase="recall179_holdout_v2_baseline", client=client)
        observer = BudgetedClassifier(state, api_key=key)
        with tempfile.TemporaryDirectory(prefix="recall-v2-run-") as temporary:
            apps = load_apps(Path(frozen["baseline_source_path"]), Path(temporary))
            try:
                if configuration(apps) != frozen["config"]:
                    raise ValueError("effective_config_changed")
                provider.embed([case["prompt"] for case in fixture["cases"]]
                               + list(apps["baseline"].MEMORY_RECALL_ARCHETYPES))
                provider.client = None
                asyncio.run(evaluate(apps, fixture, provider, observer, output, rows, boundaries))
            finally:
                close_apps(apps)
        report.update({"completion": "COMPLETE", "cache_hits": provider.cache_hits,
                       "cache_misses": provider.cache_misses})
    except BaseException as exc:
        report["error_class"] = type(exc).__name__
        raise
    finally:
        if client is not None:
            client.close()
        report.update(summarize(rows, boundaries))
        report["accounting"] = state.accounting()
        report["limitations"] = [
            "Independent synthetic labels are not human gold or natural field traffic.",
            "The actual admission adapter runs; retrieval and answer quality are not measured.",
            "Baseline uses one frozen embedding replay; three repetitions are paired observations.",
            "Invocation boundaries retain actual admission with downstream generation mocked.",
            "Classifier timings include each attempted transport, including slow or failed calls.",
        ]
        write_private_json(output / "cache-manifest.json", state.cache_manifest())
        write_private_json(output / "private-boundaries.json", boundaries)
        write_private_json(output / "aggregate-report.json", report)
    return {"completion": report["completion"], "gate_pass": report["gate_pass"],
            "per_repetition": report["per_repetition"], "stability": report["stability"],
            "accounting": report["accounting"]}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare, execute = commands.add_parser("freeze"), commands.add_parser("run")
    for name in ("baseline-source", "development-report", "development-cache-manifest"):
        prepare.add_argument("--" + name, type=Path, required=True)
    for name in ("freeze", "fixture", "authorization"):
        execute.add_argument("--" + name, type=Path, required=True)
    execute.add_argument("--allow-provider", action="store_true")
    for command in (prepare, execute):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(freeze(args) if args.command == "freeze" else run(args), sort_keys=True))
    except BaseException as exc:
        print(json.dumps({"completion": "INCOMPLETE", "error_class": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
