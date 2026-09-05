"""Development-only Luna recall trial on 144 already observed synthetic cases.

Freeze is read-only except for private evidence creation. Run requires an explicit
provider flag and key. No application is imported or Telegram transport started.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recall_admission_eval import (
    BudgetRefused, EvaluationState, confusion, digest, file_hash, paired_intervals,
    private_directory, validate_fixture, write_private_json,
)
from recall_intent_model import (
    MODEL, REASONING_EFFORT, TIMEOUT_SECONDS, MAX_OUTPUT_TOKENS, classify_recall_intent,
    RecallModelResult, recall_model_metadata, recall_model_request, reservation_nano_usd,
)
from runtime_model_pricing import PRICE_SNAPSHOT_VERSION, TokenUsage, estimate_token_cost


REPETITIONS = 2
SEED = 17906
EXPECTED_CASES = 144
EXPECTED_CALLS = EXPECTED_CASES * REPETITIONS
SOURCE_FILES = (
    "recall_intent_model.py", "scripts/eval_recall_luna.py", "recall_admission_eval.py",
    "runtime_model_pricing.py", "tests/test_recall_intent_model.py", "tests/test_recall_luna_eval.py",
)
PROTOCOL = {
    "version": "recall-luna-observed-development-v1", "model": MODEL,
    "reasoning_effort": REASONING_EFFORT, "timeout_seconds": TIMEOUT_SECONDS,
    "max_output_tokens": MAX_OUTPUT_TOKENS, "max_retries": 0, "store": False,
    "provider_concurrency": 1, "cases": EXPECTED_CASES, "repetitions": REPETITIONS,
    "provider_calls": EXPECTED_CALLS, "order_seed": SEED, "shared_budget_usd": 5,
    "price_snapshot": PRICE_SNAPSHOT_VERSION,
    "reservation": "full_request_utf8_bytes_plus_2048_input_tokens_at_write_rate_and_240_output",
    "failure_policy": "one_attempt_then_previously_measured_legacy_decision",
    "bootstrap_unit": "class_stratified_concept_family_all_languages_and_repetitions",
    "bootstrap_repetitions": 10000, "acceptance": "development_only_no_promotion",
    "classifier_latency_reference_ms": 3000,
}


def source_hashes():
    return {name: file_hash(ROOT / name) for name in SOURCE_FILES}


def runtime_versions():
    return {"python": sys.version.split()[0], "openai": version("openai")}


def population(descriptors):
    """Reject fresh or substituted holdouts; this first trial uses observed v1 only."""
    if len(descriptors) != 2:
        raise ValueError("requires_two_observed_blocks")
    cases = []
    for block, expected_id in (("development", "recall-admission-179-development-v1"),
                               ("observed_holdout", "recall-admission-179-holdout-v1")):
        descriptor = descriptors[block]
        fixture_path, outcomes_path = Path(descriptor["fixture_path"]), Path(descriptor["outcomes_path"])
        if file_hash(fixture_path) != descriptor["fixture_sha256"] or file_hash(outcomes_path) != descriptor["outcomes_sha256"]:
            raise ValueError("observed_evidence_changed")
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        validate_fixture(fixture)
        if fixture["fixture_id"] != expected_id:
            raise ValueError("fresh_or_unknown_fixture_not_authorized")
        outcomes_list = [json.loads(line) for line in outcomes_path.read_text().splitlines()]
        outcomes = {row["case_id"]: row for row in outcomes_list}
        if len(outcomes) != len(outcomes_list) or set(outcomes) != {case["case_id"] for case in fixture["cases"]}:
            raise ValueError("observed_outcome_population_mismatch")
        for case in fixture["cases"]:
            prior = outcomes[case["case_id"]]
            if (prior["expected"] != case["expected"]["is_recall"] or prior["language"] != case["language"]
                    or prior["family_id"] != case["family_id"]
                    or prior["critical_negative"] != case["expected"]["critical_negative"]):
                raise ValueError("observed_label_mismatch")
            if any(not isinstance(prior[arm]["is_recall"], bool) for arm in ("baseline", "candidate")):
                raise ValueError("invalid_prior_admission")
            metadata = recall_model_metadata(case["prompt"], has_reply_text=case["context"]["has_reply_text"],
                                             has_reply_image=case["context"]["has_reply_image"])
            cases.append({"case_id": case["case_id"], "family_id": case["family_id"], "block": block,
                          "language": case["language"], "expected": case["expected"]["is_recall"],
                          "critical_negative": case["expected"]["critical_negative"], "metadata": metadata,
                          "baseline": prior["baseline"], "lexical_v1": prior["candidate"]})
    if len(cases) != EXPECTED_CASES or len({case["case_id"] for case in cases}) != EXPECTED_CASES:
        raise ValueError("trial_population_mismatch")
    return cases


def schedule(cases):
    plan = [(case, repetition) for repetition in range(REPETITIONS) for case in cases]
    random.Random(SEED).shuffle(plan)
    return plan


def plan_binding(cases):
    entries = [{"case_id": case["case_id"], "repetition": repetition,
                "request_sha256": digest(recall_model_request(case["metadata"])),
                "reservation_nano_usd": reservation_nano_usd(case["metadata"])}
               for case, repetition in schedule(cases)]
    return {"entries": entries, "sha256": digest(entries),
            "worst_total_nano_usd": sum(entry["reservation_nano_usd"] for entry in entries)}


def prepare_output(path):
    output = private_directory(path, repository=ROOT, create=True)
    if any(output.iterdir()):
        raise ValueError("trial_output_must_be_empty")
    return output


def freeze(args):
    output = prepare_output(args.output)
    descriptors = {}
    for block in ("development", "observed_holdout"):
        fixture, outcomes = getattr(args, block + "_fixture"), getattr(args, block + "_outcomes")
        descriptors[block] = {"fixture_path": str(fixture.resolve()), "fixture_sha256": file_hash(fixture),
                              "outcomes_path": str(outcomes.resolve()), "outcomes_sha256": file_hash(outcomes)}
    cases = population(descriptors)
    state = EvaluationState(private_directory(args.state, repository=ROOT))
    plan = plan_binding(cases)
    accounting = state.accounting()
    remaining = round((accounting["cap_usd"] - accounting["known_usd"] - accounting["unknown_reserved_usd"]) * 1e9)
    if plan["worst_total_nano_usd"] > remaining:
        raise BudgetRefused("full_trial_does_not_fit_remaining_shared_budget")
    frozen = {"protocol": PROTOCOL, "sources": source_hashes(), "runtime": runtime_versions(),
              "observed_evidence": descriptors, "plan": plan, "state_identity": state.identity(),
              "initial_cache_manifest": state.cache_manifest(), "initial_accounting": accounting}
    write_private_json(output / "source-freeze.json", frozen)
    return {"completion": "FROZEN", "planned_calls": len(plan["entries"]),
            "worst_trial_usd": plan["worst_total_nano_usd"] / 1e9,
            "freeze_sha256": file_hash(output / "source-freeze.json"), "accounting": accounting}


def complete_rows(rows, repetitions=REPETITIONS):
    groups = defaultdict(list)
    for row in rows:
        groups[row["case_id"]].append(row)
    return [row for group in groups.values()
            if len(group) == repetitions and {row["repetition"] for row in group} == set(range(repetitions))
            and not any(row.get("administrative_abort") for row in group) for row in group]


def _percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def _arm_metrics(rows, arm):
    selected = [row for row in rows if isinstance(row[arm]["is_recall"], bool)]
    return {"scored": len(selected), "missing": len(rows) - len(selected), **confusion(selected, arm)}


def metrics(rows):
    return {arm: _arm_metrics(rows, arm) for arm in ("baseline", "lexical_v1", "classifier", "candidate")}


def summarize(rows):
    scored = complete_rows(rows)
    groups = defaultdict(list)
    for row in scored:
        groups[row["case_id"]].append(row)
    valid_groups = [group for group in groups.values() if all(row["classifier"]["is_recall"] is not None for row in group)]
    stable_intent = sum(len({row["model"]["intent"] for row in group}) == 1 for group in valid_groups)
    stable_decision = sum(len({row["classifier"]["is_recall"] for row in group}) == 1 for group in valid_groups)
    costs = [row for row in rows if row.get("attempt_id")]
    known = sum(row["model"]["cost"]["nano_usd"] for row in costs if row["model"]["cost"]["complete"])
    unknown_reserve = sum(row["reserved_nano_usd"] for row in costs if not row["model"]["cost"]["complete"])
    report = {"completed_decisions": len(rows), "complete_paired_cases": len(groups),
              "paired_scored_decisions": len(scored), "planned_cases": EXPECTED_CASES,
              "planned_decisions": EXPECTED_CALLS, "metrics": metrics(scored),
              "per_repetition": {str(rep): metrics([row for row in scored if row["repetition"] == rep])
                                 for rep in range(REPETITIONS)},
              "per_observed_block": {block: metrics([row for row in scored if row["block"] == block])
                                     for block in ("development", "observed_holdout")},
              "by_language": {lang: metrics([row for row in scored if row["language"] == lang])
                              for lang in ("ua", "ru", "en")},
              "stability": {"complete_cases": len(groups), "all_repeats_valid_cases": len(valid_groups),
                            "stable_intent_cases": stable_intent, "stable_admission_cases": stable_decision,
                            "intent_fraction_of_all_complete_cases": stable_intent / len(groups) if groups else None},
              "provider_status_counts": dict(Counter(str(row["model"]["provider_status"]) for row in costs)),
              "classifier_status_counts": dict(Counter(row["model"]["status"] for row in costs)),
              "actual_model_counts": dict(Counter(str(row["model"]["actual_model"]) for row in costs)),
              "failure_classes": dict(Counter(row["model"]["failure_class"] for row in costs if row["model"]["failure_class"])),
              "fallback_decisions": sum(row["candidate"]["degraded"] for row in rows),
              "administrative_abort_decisions": sum(row.get("administrative_abort", False) for row in rows),
              "latency_ms": {"p50": _percentile([row["model"]["latency_ms"] for row in costs], .5),
                             "p95": _percentile([row["model"]["latency_ms"] for row in costs], .95)},
              "trial_cost": {"known_usd": known / 1e9, "unknown_reserved_usd": unknown_reserve / 1e9,
                             "accounted_upper_usd": (known + unknown_reserve) / 1e9,
                             "attempts": len(costs), "snapshot": PRICE_SNAPSHOT_VERSION},
              "promotion": "NOT_ASSESSED_OBSERVED_DEVELOPMENT_ONLY"}
    if scored:
        report["paired_family_bootstrap_vs_legacy"] = paired_intervals(scored)
        report["paired_family_bootstrap_vs_lexical_v1"] = paired_intervals(
            [{**row, "baseline": row["lexical_v1"]} for row in scored])
    return report


def result_row(case, repetition, result, *, attempt_id, reserved):
    predicted = result.is_recall
    return {key: case[key] for key in ("case_id", "family_id", "block", "language", "expected", "critical_negative")} | {
        "repetition": repetition, "baseline": case["baseline"], "lexical_v1": case["lexical_v1"],
        "classifier": {"is_recall": predicted, "degraded": predicted is None},
        "candidate": {"is_recall": predicted if predicted is not None else case["baseline"]["is_recall"],
                      "degraded": predicted is None},
        "model": asdict(result), "attempt_id": attempt_id, "reserved_nano_usd": reserved,
        "administrative_abort": result.failure_class == "ProviderIdentityMismatch"}


async def evaluate(cases, state, output, rows, *, api_key, classifier=classify_recall_intent):
    # Exclusive, owner-only file; no prompts, raw outputs, or identifiers from the provider.
    descriptor = os.open(output / "private-outcomes.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for case, repetition in schedule(cases):
            metadata = case["metadata"]
            reserved = reservation_nano_usd(metadata)
            attempt = state.reserve(reserved, phase="recall179_luna_observed_development",
                                    request_hash=digest(recall_model_request(metadata)))
            started = time.perf_counter()
            try:
                result = await classifier(metadata, api_key=api_key)
            except BaseException as exc:
                state.settle(attempt, error_class=type(exc).__name__)
                usage = TokenUsage(None)
                interrupted = RecallModelResult(None, "administrative_abort", None, None, None, usage,
                    asdict(estimate_token_cost(MODEL, [usage])), (time.perf_counter() - started) * 1000,
                    type(exc).__name__)
                row = result_row(case, repetition, interrupted, attempt_id=attempt, reserved=reserved)
                row["administrative_abort"] = True
                rows.append(row)
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                raise
            row = result_row(case, repetition, result, attempt_id=attempt, reserved=reserved)
            try:
                if result.cost["complete"]:
                    state.settle(attempt, tokens=result.usage.input_tokens + result.usage.output_tokens,
                                 cost_nano_usd=result.cost["nano_usd"], error_class=result.failure_class)
                else:
                    state.settle(attempt, error_class=result.failure_class or "UnknownUsage")
            except BaseException:
                row["administrative_abort"] = True
                rows.append(row)
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                raise
            rows.append(row)
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            if len(rows) % 24 == 0:
                print(json.dumps({"stage": "luna_development", "completed_calls": len(rows),
                                  "planned_calls": EXPECTED_CALLS}), flush=True)
            if row["administrative_abort"]:
                raise ValueError("provider_identity_mismatch_stops_trial")


def run(args):
    if not args.allow_provider or not os.environ.get("AIGAN_EVAL_API_KEY"):
        raise ValueError("explicit_provider_authorization_and_key_required")
    frozen = json.loads(args.freeze.read_text())
    if frozen["protocol"] != PROTOCOL or frozen["sources"] != source_hashes() or frozen["runtime"] != runtime_versions():
        raise ValueError("frozen_source_or_runtime_changed")
    cases = population(frozen["observed_evidence"])
    if plan_binding(cases) != frozen["plan"]:
        raise ValueError("frozen_request_population_changed")
    state = EvaluationState(private_directory(args.state, repository=ROOT))
    if state.identity() != frozen["state_identity"]:
        raise ValueError("frozen_state_identity_changed")
    cache_validation = state.verify_cache_manifest(frozen["initial_cache_manifest"], allow_additions=True)
    output = prepare_output(args.output)
    # The durable shared-state claim also rejects a copied or newly made freeze
    # for this already authorized population; a new output cannot buy a replay.
    write_private_json(state.directory / "recall-luna-observed-development-v1.claimed.json",
                       {"freeze_sha256": file_hash(args.freeze), "claimed_at": time.time()})
    rows = []
    report = {"completion": "INCOMPLETE", "freeze_sha256": file_hash(args.freeze),
              "protocol": PROTOCOL, "initial_accounting": state.accounting(),
              "state_identity": state.identity(), "cache_validation": cache_validation}
    try:
        asyncio.run(evaluate(cases, state, output, rows, api_key=os.environ["AIGAN_EVAL_API_KEY"]))
        if len(rows) != EXPECTED_CALLS:
            raise ValueError("trial_population_incomplete")
        report["completion"] = "COMPLETE"
    except BaseException as exc:
        report["error_class"] = type(exc).__name__
        raise
    finally:
        report.update(summarize(rows))
        report["accounting"] = state.accounting()
        report["limitations"] = [
            "Both blocks were already observed; this is development evidence, not fresh acceptance.",
            "Independent synthetic labels are not human gold or natural production traffic.",
            "The real Responses adapter runs in isolation, with no live recall invocation or retrieval change.",
            "Legacy fallback reuses measured decisions; it does not rerun or price embedding transport.",
        ]
        write_private_json(output / "aggregate-report.json", report)
    return {"completion": report["completion"], "metrics": report["metrics"],
            "stability": report["stability"], "latency_ms": report["latency_ms"], "trial_cost": report["trial_cost"]}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    for block in ("development", "observed_holdout"):
        for artifact in ("fixture", "outcomes"):
            prepare.add_argument("--" + block.replace("_", "-") + "-" + artifact, type=Path, required=True)
    prepare.add_argument("--state", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--freeze", type=Path, required=True)
    execute.add_argument("--state", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--allow-provider", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(freeze(args) if args.command == "freeze" else run(args), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"completion": "INCOMPLETE", "error_class": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
