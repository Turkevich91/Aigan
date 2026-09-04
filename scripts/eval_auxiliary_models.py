"""Bounded development comparison through Aigan's actual runtime adapters.

Run only in an isolated container with ephemeral data, using the deployed image
and the matching source checkout. This never starts the Telegram application.
The existing fixtures are development evidence, never an untouched holdout.
Only aggregate results and synthetic case identifiers are written; no prompts,
provider response text, credentials, or user data are included in artifacts.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import hashlib
import inspect
import json
import logging
import os
import random
import stat
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime_model_pricing import TokenUsage, estimate_token_cost
from scripts import eval_image_intent as image_eval
from scripts import eval_model_routing as routing_eval

SEED = 1760904
CONFIG_CONTEXT: contextvars.ContextVar[Any] = contextvars.ContextVar("eval_config")
JOB_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar("eval_job")
FUNCTIONS = (
    "run_model_policy_router", "evaluate_model_policy_shadow",
    "run_image_intent_router", "evaluate_image_intent",
    "run_image_operation_authorizer", "evaluate_image_operation_authorization",
    "classify_request_with_intent", "build_image_intent_router_metadata",
    "build_image_operation_authorizer_metadata",
)
SETTINGS = {
    "model_router_reasoning_effort": "none",
    "model_router_max_output_tokens": 240,
    "model_router_timeout_seconds": 8.0,
    "model_router_confidence_threshold": 0.75,
    "image_intent_routing_mode": "enforce",
    "image_intent_router_reasoning_effort": "none",
    "image_intent_router_max_output_tokens": 240,
    "image_intent_router_timeout_seconds": 8.0,
    "image_intent_router_confidence_threshold": 0.70,
    "image_operation_authorizer_model": "gpt-5.6-terra",
    "image_operation_authorizer_reasoning_effort": "none",
    "image_operation_authorizer_max_output_tokens": 300,
    "image_operation_authorizer_timeout_seconds": 8.0,
    "image_operation_authorizer_confidence_threshold": 0.80,
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def prepare_private_output(path: Path) -> Path:
    """Require an empty owner-only directory in the documented POSIX container."""
    if os.name != "posix":
        raise ValueError("private_output_requires_posix_container")
    if path.is_symlink():
        raise ValueError("output_directory_must_not_be_symlink")
    output = path.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("output_must_be_outside_repository")
    try:
        output.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    details = output.lstat()
    if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o700):
        raise ValueError("output_directory_must_be_owned_and_mode_0700")
    if any(output.iterdir()):
        raise ValueError("output_directory_must_be_empty")
    return output


def private_text_file(path: Path):
    """Create a new mode-0600 file without following or replacing existing paths."""
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        details = os.fstat(directory_fd)
        if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
            raise ValueError("output_directory_must_be_owned_and_mode_0700")
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Hold the entire conservative reservation when usage is unknown."""
    def __init__(self, limit_usd: float):
        self.limit = round(limit_usd * 1_000_000_000)
        self.known = 0
        self.unknown = 0
        self.inflight = 0
        self.total_reserved = 0
        self.stopped = False

    def reserve(self, amount: int) -> None:
        if self.stopped or self.known + self.unknown + self.inflight + amount > self.limit:
            self.stopped = True
            raise BudgetExceeded("budget_reservation_refused")
        self.inflight += amount
        self.total_reserved += amount

    def settle(self, reservation: int, actual: int | None) -> None:
        self.inflight -= reservation
        if actual is None:
            self.unknown += reservation
        else:
            self.known += actual
            if actual > reservation:
                self.stopped = True

    def summary(self) -> dict:
        return {
            "limit_usd": self.limit / 1e9,
            "known_estimated_usd": self.known / 1e9,
            "unknown_usage_reserved_usd": self.unknown / 1e9,
            "inflight_reserved_usd": self.inflight / 1e9,
            "accounted_upper_bound_usd": (self.known + self.unknown + self.inflight) / 1e9,
            "sum_of_per_attempt_reservations_usd": self.total_reserved / 1e9,
            "stopped": self.stopped,
        }


def reservation_for(request: dict) -> int:
    # Byte fallback BPE cannot need more tokens than UTF-8 bytes. Include the
    # complete serialized request plus a generous allowance for wire framing.
    input_upper_bound = len(json.dumps(request, ensure_ascii=False).encode()) + 2048
    value = estimate_token_cost(request["model"], [TokenUsage(
        input_tokens=input_upper_bound, cached_input_tokens=0, cache_write_tokens=input_upper_bound,
        output_tokens=int(request["max_output_tokens"]),
    )]).nano_usd
    if value is None:
        raise ValueError("unpriced_requested_model")
    return value


class ConfigProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(CONFIG_CONTEXT.get(), name)


def initialize_runtime(api_key: str):
    # Discard inherited application settings before importing main. In particular
    # no real Telegram token, database location, or telemetry setting survives.
    system_env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "HOME", "TMP", "TEMP", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "LANG", "LC_ALL", "PYTHONPATH",
    }}
    os.environ.clear()
    os.environ.update(system_env)
    from tests.support import configure_test_environment
    configure_test_environment()
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["SYSTEM_LOG_ENABLED"] = "false"
    os.environ["MODEL_TELEMETRY_ENABLED"] = "false"
    os.environ["MEMORY_VECTOR_ENABLED"] = "false"
    os.environ["MEMORY_VECTOR_BACKFILL_ON_START"] = "false"
    import main as app
    logging.disable(logging.CRITICAL)
    app.MODEL_TELEMETRY = None
    app.begin_model_stage = lambda **kwargs: None
    app.finish_model_stage = lambda *args, **kwargs: None
    app.system_event = lambda **kwargs: None
    return app


def install_recorder(app, budget: Budget):
    from openai import AsyncOpenAI

    class Client:
        def __init__(self, **kwargs):
            assert kwargs.get("max_retries") == 0
            assert kwargs.get("timeout") == 8.0
            self.client = AsyncOpenAI(**kwargs)
            self.responses = self

        async def __aenter__(self):
            await self.client.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)

        async def create(self, **request):
            request["store"] = False
            assert request["reasoning"] == {"effort": "none"}
            assert "tools" not in request
            job = JOB_CONTEXT.get()
            stage = request["text"]["format"]["name"]
            reservation = reservation_for(request)
            try:
                budget.reserve(reservation)
            except BudgetExceeded:
                job["administrative_abort"] = "budget_reservation_refused"
                raise
            row = {"stage": stage, "model": request["model"],
                   "reserved_nano_usd": reservation, "request_sha256": digest(request),
                   "known_cost_nano_usd": None, "failure_class": ""}
            job["attempts"].append(row)
            started = time.monotonic()
            cost = None
            try:
                response = await self.client.responses.create(**request)
                row["provider_status"] = str(response.status or "")
                row["provider_model"] = str(response.model or "")
                row["provider_effort"] = str(getattr(response.reasoning, "effort", "") or "")
                row["model_matches"] = routing_eval.provider_model_matches(request["model"], response.model)
                row["effort_matches"] = row["provider_effort"] == "none"
                usage = response.usage
                if usage is not None:
                    details = usage.input_tokens_details
                    cost = routing_eval.usage_cost(request["model"], response)
                    row["usage"] = {"input_tokens": usage.input_tokens,
                                    "cached_tokens": getattr(details, "cached_tokens", 0),
                                    "cache_write_tokens": getattr(details, "cache_write_tokens", 0),
                                    "output_tokens": usage.output_tokens}
                    row["known_cost_nano_usd"] = cost
                if not row["model_matches"] or not row["effort_matches"]:
                    budget.stopped = True
                    job["administrative_abort"] = "provider_identity_guard"
                try:
                    payload = json.loads(response.output_text)
                    if stage == "model_policy_v1":
                        job["raw_class"] = payload.get("task_class", "unclassified")
                        job["raw_confidence"] = payload.get("confidence", 0)
                    elif stage == "image_intent_v1":
                        decision = app.normalize_image_intent_decision(
                            payload, trusted_prompt=job["case"].prompt,
                            confidence_threshold=0.70)
                        checks = image_eval.strict_semantic_checks(job["case"], decision)
                        row["semantic_checks"] = checks
                        row["semantic_pass"] = all(checks.values())
                        row["normalized_valid"] = decision.outcome not in {"invalid", "failed"}
                        row["intent"] = decision.intent
                except (ValueError, TypeError, KeyError, AttributeError):
                    row["payload_invalid"] = True
                return response
            except BaseException as exc:
                row["failure_class"] = type(exc).__name__
                raise
            finally:
                row["latency_ms"] = round((time.monotonic() - started) * 1000)
                budget.settle(reservation, cost)

    app.AsyncOpenAI = Client


def image_message(case):
    from tests.support import FakeMessage
    flags = dict(case.flags)
    message = FakeMessage(case.prompt, chat_type="private")
    if flags.get("has_reply"):
        reply = FakeMessage(str(flags.get("reply_text_context", "")), chat_type="private")
        if flags.get("has_reply_image"):
            reply.photo = [SimpleNamespace(file_id="synthetic-image", file_unique_id="synthetic-image",
                                            width=32, height=32, file_size=100)]
        if flags.get("has_reply_visual_media"):
            reply.video = SimpleNamespace(file_id="synthetic-video", file_unique_id="synthetic-video",
                                          width=32, height=32, file_size=100, mime_type="video/mp4")
        message.reply_to_message = reply
    return message


def install_image_tracking(app):
    original_intent = app.evaluate_image_intent
    original_authorization = app.evaluate_image_operation_authorization

    async def intent(*args, **kwargs):
        result = await original_intent(*args, **kwargs)
        JOB_CONTEXT.get()["decision"] = result
        return result

    async def authorization(*args, **kwargs):
        result = await original_authorization(*args, **kwargs)
        JOB_CONTEXT.get()["authorization"] = result
        return result

    async def no_memory_lookup(*args, **kwargs):
        # The comparison owns the image routing stage, not semantic memory I/O.
        return app.MemoryRecallIntent(False, reason="evaluation_isolation")

    app.evaluate_image_intent = intent
    app.evaluate_image_operation_authorization = authorization
    app.detect_memory_recall_intent = no_memory_lookup


async def evaluate(app, baseline, job, semaphore, budget):
    role, model, repetition, case = job
    async with semaphore:
        if budget.stopped:
            return {"role": role, "model": model, "repeat": repetition,
                    "case_id": case["id"] if role == "model_policy" else case.id,
                    "skipped_budget": True, "attempts": []}
        settings = dict(SETTINGS)
        settings["model_router_model" if role == "model_policy" else "image_intent_router_model"] = model
        config_token = CONFIG_CONTEXT.set(replace(baseline, **settings))
        context = {"case": case, "attempts": []}
        job_token = JOB_CONTEXT.set(context)
        started = time.monotonic()
        row = {"role": role, "model": model, "repeat": repetition,
               "case_id": case["id"] if role == "model_policy" else case.id}
        try:
            if role == "model_policy":
                metadata = routing_eval.router_metadata(case)
                decision = await app.evaluate_model_policy_shadow(
                    metadata, run_id="synthetic-evaluation", route_bucket=metadata["request_route"],
                    assignment_key="synthetic-evaluation", assignment_scope="single_turn")
                expected = case["expected"]
                row.update({"expected_class": expected["task_class"],
                    "observed_class": context.get("raw_class", "unclassified"),
                    "selected_tier": decision.selected_tier, "minimum_tier": expected["minimum_tier"],
                    "structured_valid": decision.outcome not in {"invalid", "failed"},
                    "fallback": decision.degraded, "fallback_reason": decision.fallback_reason,
                    "confidence": context.get("raw_confidence", 0),
                    "unsafe_downgrade": routing_eval.TIER_RANK[decision.selected_tier] < routing_eval.TIER_RANK[expected["minimum_tier"]]})
            else:
                result = await app.classify_request_with_intent(image_message(case), case.prompt)
                decision = context.get("decision")
                authorization = context.get("authorization")
                policy = result.image_policy
                actual_count = policy.plan.target_count if policy and policy.plan else 0
                semantic = image_eval.strict_semantic_checks(case, decision) if decision else {"classifier_called": False}
                gated = case.route in {"internet_image_send", "referenced_visual_analysis"}
                checks = {
                    "route": result.route == case.route,
                    "owner": image_eval.OWNER_BY_ROUTE.get(result.route) == case.owner,
                    "plan_count": actual_count == case.count,
                    "plan_presence": bool(policy and policy.plan) == (case.route == "internet_image_send"),
                    "no_unexpected_public_delivery": not (result.route == "internet_image_send" and case.route != "internet_image_send"),
                    "authorizer_called": case.authorizer_called is None or (authorization is not None) == case.authorizer_called,
                    "normalized_authorizer": authorization is None or (authorization.outcome == "succeeded" and not authorization.degraded),
                    "two_key_gate": not gated or (authorization is not None and authorization.outcome == "succeeded" and not authorization.degraded),
                    "plan_uses_grounded_subject": (bool(policy and policy.plan and decision and policy.plan.query == decision.subject_text)
                        and (case.subject is None or image_eval.image_subject_spans_agree(policy.plan.query, case.subject))) if case.route == "internet_image_send" else True,
                }
                first = next((a for a in context["attempts"] if a["stage"] == "image_intent_v1"), {})
                row.update({"semantic_checks": semantic, "semantic_pass": all(semantic.values()),
                    "first_attempt_semantic_pass": bool(first.get("semantic_pass")),
                    "end_to_end_checks": checks, "end_to_end_pass": all(checks.values()),
                    "overall_pass": all(semantic.values()) and all(checks.values()),
                    "route": result.route, "expected_route": case.route,
                    "intent": decision.intent if decision else "not_called",
                    "structured_valid": bool(decision and decision.outcome not in {"invalid", "failed"}),
                    "classifier_fallback": decision.fallback_reason if decision else "not_called",
                    "safety_recovery": not all(semantic.values()) and all(checks.values()) and authorization is not None})
        except Exception as exc:
            row["evaluation_failure"] = type(exc).__name__
        finally:
            row["latency_ms"] = round((time.monotonic() - started) * 1000)
            row["attempts"] = context["attempts"]
            if context.get("administrative_abort"):
                row["administrative_abort"] = context["administrative_abort"]
            classifier_stage = "model_policy_v1" if role == "model_policy" else "image_intent_v1"
            classifier_attempts = [a for a in context["attempts"] if a["stage"] == classifier_stage]
            last = classifier_attempts[-1] if classifier_attempts else {}
            row["strict_completion_valid"] = bool(row.get("structured_valid") and
                last.get("provider_status") == "completed" and not last.get("payload_invalid")
                and not last.get("failure_class"))
            JOB_CONTEXT.reset(job_token)
            CONFIG_CONTEXT.reset(config_token)
        return row


def routing_result(row):
    attempts = row["attempts"]
    attempt = attempts[0] if attempts else {}
    return routing_eval.EvalResult(
        case_id=row["case_id"], expected_class=row.get("expected_class", "unclassified"),
        observed_class=row.get("observed_class", "unclassified"),
        selected_tier=row.get("selected_tier", "premium"), minimum_tier=row.get("minimum_tier", "premium"),
        structured_valid=row.get("structured_valid", False), fallback=row.get("fallback", True),
        unsafe_downgrade=row.get("unsafe_downgrade", False),
        actual_model_mismatch=not attempt.get("model_matches", True), actual_model=attempt.get("provider_model", ""),
        actual_effort_mismatch=not attempt.get("effort_matches", True), actual_effort=attempt.get("provider_effort", ""),
        confidence=row.get("confidence", 0), fallback_reason=row.get("fallback_reason", "evaluation_failure"),
        latency_ms=row["latency_ms"], estimated_cost_nano_usd=attempt.get("known_cost_nano_usd"),
        failure_class=attempt.get("failure_class", row.get("evaluation_failure", "")))


def paired_bootstrap(rows, role, baseline, candidate, *, samples=2000):
    groups = {model: defaultdict(list) for model in (baseline, candidate)}
    for row in rows:
        if row["role"] == role and not row.get("skipped_budget") and not row.get("administrative_abort"):
            groups[row["model"]][row["case_id"]].append(row)
    repeats = 3 if role == "model_policy" else 2
    ids = sorted(cid for cid in set(groups[baseline]) & set(groups[candidate])
                 if all(len(groups[m][cid]) == repeats and
                        {r["repeat"] for r in groups[m][cid]} == set(range(repeats))
                        for m in (baseline, candidate)))
    def score(sample_rows):
        if role == "model_policy":
            return routing_eval.macro_f1([routing_result(r) for r in sample_rows])
        return statistics.fmean(bool(r.get("semantic_pass")) for r in sample_rows)
    if not ids:
        return {}
    def difference(selected):
        return score([r for cid in selected for r in groups[candidate][cid]]) - score([r for cid in selected for r in groups[baseline][cid]])
    rng = random.Random(SEED)
    differences = sorted(difference(rng.choices(ids, k=len(ids))) for _ in range(samples))
    return {"metric": "macro_f1" if role == "model_policy" else "final_classifier_semantic_rate",
            "unit": "paired_case_cluster_including_all_repeats", "complete_paired_cases": len(ids), "samples": samples,
            "delta": difference(ids), "ci95": [differences[int(samples * .025)], differences[int(samples * .975)]]}


def summarize(rows, manifest, budget):
    reports = []
    for role, models, repeats in (("model_policy", ("gpt-5.4-nano", "gpt-5.6-luna"), 3),
                                  ("image_intent", ("gpt-5.4-mini", "gpt-5.6-luna"), 2)):
        for model in models:
            family_rows = [r for r in rows if r["role"] == role and r["model"] == model]
            selected = [r for r in family_rows if
                        not r.get("skipped_budget") and not r.get("administrative_abort")]
            # Cost attribution includes every dispatched attempt, even when a
            # later administrative abort excludes that decision from scoring.
            attempts = [a for r in family_rows for a in r["attempts"]]
            if role == "model_policy":
                report = routing_eval.aggregate([routing_result(r) for r in selected],
                    fixture_hash=manifest["fixtures"]["model_policy"], model=model,
                    effort="none", repeats=repeats, threshold=.75)
            else:
                count = len(selected)
                safety_failures = sum(not r.get("end_to_end_checks", {}).get("no_unexpected_public_delivery", False) for r in selected)
                valid = sum(r.get("structured_valid", False) for r in selected)
                by_case = defaultdict(list)
                for row in selected:
                    by_case[row["case_id"]].append(row)
                report = {"decisions": count, "structured_valid": valid,
                    "structured_valid_rate": valid / count if count else 0,
                    "first_attempt_semantic_passed": sum(r.get("first_attempt_semantic_pass", False) for r in selected),
                    "semantic_passed": sum(r.get("semantic_pass", False) for r in selected),
                    "end_to_end_passed": sum(r.get("end_to_end_pass", False) for r in selected),
                    "overall_passed": sum(r.get("overall_pass", False) for r in selected),
                    "unexpected_public_deliveries": safety_failures,
                    "semantic_failed_ids": sorted({r["case_id"] for r in selected if not r.get("semantic_pass")}),
                    "end_to_end_failed_ids": sorted({r["case_id"] for r in selected if not r.get("end_to_end_pass")}),
                    "stable_cases": sum(len({(r.get("intent"), r.get("route")) for r in rr}) == 1 for rr in by_case.values()),
                    "latency_ms": {"p50": routing_eval.percentile([r["latency_ms"] for r in selected], .5),
                                   "p95": routing_eval.percentile([r["latency_ms"] for r in selected], .95)},
                    "safety_recoveries": sum(r.get("safety_recovery", False) for r in selected)}
            report.update({"role": role, "candidate_model": model, "provider_attempts": len(attempts),
                "strict_completion_valid": sum(r.get("strict_completion_valid", False) for r in selected),
                "provider_statuses": dict(Counter(a.get("provider_status", "not_received") for a in attempts)),
                "completed_provider_attempts": sum(a.get("provider_status") == "completed" for a in attempts),
                "provider_failures": dict(Counter(a["failure_class"] for a in attempts if a["failure_class"])),
                "unknown_usage_attempts": sum(a["known_cost_nano_usd"] is None for a in attempts),
                "known_pipeline_cost_usd": sum(a["known_cost_nano_usd"] or 0 for a in attempts) / 1e9,
                "unknown_pipeline_reserve_usd": sum(a["reserved_nano_usd"] for a in attempts if a["known_cost_nano_usd"] is None) / 1e9,
                "attempt_stages": dict(Counter(a["stage"] for a in attempts)),
                "provider_model_mismatches": sum(not a.get("model_matches", True) for a in attempts),
                "provider_effort_mismatches": sum(not a.get("effort_matches", True) for a in attempts),
                "promotion_verdict": "NOT_EVALUATED_DEVELOPMENT_CORPUS_ONLY"})
            report["per_repeat"] = []
            for repetition in range(repeats):
                block = [r for r in selected if r["repeat"] == repetition]
                report["per_repeat"].append({"repeat": repetition, "decisions": len(block),
                    "macro_f1": routing_eval.macro_f1([routing_result(r) for r in block]) if role == "model_policy" else None,
                    "semantic_passed": sum(r.get("semantic_pass", False) for r in block) if role == "image_intent" else None,
                    "end_to_end_passed": sum(r.get("end_to_end_pass", False) for r in block) if role == "image_intent" else None,
                    "strict_completion_valid": sum(r.get("strict_completion_valid", False) for r in block)})
            reports.append(report)
    complete = len(rows) == 940 and not any(r.get("skipped_budget") or r.get("administrative_abort")
                                          or r.get("evaluation_failure") for r in rows)
    return {"purpose": "development_comparison_no_promotion", "completion": "COMPLETE" if complete else "INCOMPLETE",
        "manifest_sha256": digest(manifest),
        "reports": reports, "budget": budget.summary(),
        "skipped_budget": sum(bool(r.get("skipped_budget")) for r in rows),
        "administrative_aborts": dict(Counter(r["administrative_abort"] for r in rows if r.get("administrative_abort"))),
        "paired_uncertainty": {
            "model_policy": paired_bootstrap(rows, "model_policy", "gpt-5.4-nano", "gpt-5.6-luna"),
            "image_intent": paired_bootstrap(rows, "image_intent", "gpt-5.4-mini", "gpt-5.6-luna")},
        "unassessed_roles": ["tool_router", "background_vision", "image_candidate_review", "memory_extraction"]}


def build_manifest(app, jobs, limit):
    return {"schema": "auxiliary_development_eval_v1", "seed": SEED, "concurrency": 2,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "provider_max_retries": 0, "store": False, "tracing": "disabled",
        "budget_usd": limit, "settings": SETTINGS,
        "runtime_functions_sha256": {name: hashlib.sha256(inspect.getsource(getattr(app, name)).encode()).hexdigest() for name in FUNCTIONS},
        "source_files_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "main.py", ROOT / "model_routing.py", ROOT / "image_intent.py", ROOT / "runtime_model_pricing.py",
                      ROOT / "scripts" / "eval_auxiliary_models.py", ROOT / "scripts" / "eval_model_routing.py",
                      ROOT / "scripts" / "eval_image_intent.py", ROOT / "tests" / "support.py")},
        "fixtures": {"model_policy": hashlib.sha256(routing_eval.DEFAULT_FIXTURE.read_bytes()).hexdigest(),
                     "image_intent": image_eval.stable_hash(image_eval.CASES)},
        "prompts": {"model_policy": digest(routing_eval.MODEL_POLICY_ROUTER_SYSTEM_PROMPT),
                    "image_intent": digest(image_eval.IMAGE_INTENT_ROUTER_SYSTEM_PROMPT),
                    "image_authorizer": digest(image_eval.IMAGE_OPERATION_AUTHORIZER_SYSTEM_PROMPT)},
        "schemas": {"model_policy": digest(app.model_routing_schema()),
                    "image_intent": digest(app.image_intent_schema()),
                    "image_authorizer": digest(app.image_operation_authorizer_schema())},
        "jobs": [[role, model, rep, c["id"] if role == "model_policy" else c.id] for role, model, rep, c in jobs],
        "isolation": "synthetic_message_objects_ephemeral_databases_no_telegram_no_memory_lookup",
        "scope": "actual_runtime_adapters_application_retries_and_image_policy; existing_synthetic_metadata_for_model_policy"}


async def run(args):
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not args.plan_only and not api_key:
        raise ValueError("OPENAI_API_KEY_required")
    output = prepare_private_output(args.output)
    app = initialize_runtime(api_key or "sk-test")
    image_eval.validate_cases(image_eval.CASES)
    jobs = [("model_policy", m, rep, c) for c in routing_eval.fixture_cases(routing_eval.DEFAULT_FIXTURE)
            for rep in range(3) for m in ("gpt-5.4-nano", "gpt-5.6-luna")]
    jobs += [("image_intent", m, rep, c) for c in image_eval.CASES
             for rep in range(2) for m in ("gpt-5.4-mini", "gpt-5.6-luna")]
    random.Random(SEED).shuffle(jobs)
    manifest = build_manifest(app, jobs, args.budget_usd)
    with private_text_file(output / "manifest.json") as stream:
        stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if args.plan_only:
        print(json.dumps({"plan_only": True, "jobs": len(jobs), "manifest_sha256": digest(manifest)}))
        return
    budget = Budget(args.budget_usd)
    baseline = app.CONFIG
    app.CONFIG = ConfigProxy()
    install_recorder(app, budget)
    install_image_tracking(app)
    semaphore = asyncio.Semaphore(2)
    rows = []
    with private_text_file(output / "decisions.jsonl") as stream:
        tasks = [asyncio.create_task(evaluate(app, baseline, job, semaphore, budget)) for job in jobs]
        for completed in asyncio.as_completed(tasks):
            row = await completed
            rows.append(row)
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
            if len(rows) % 25 == 0:
                print(json.dumps({"completed": len(rows), "total": len(jobs), "budget": budget.summary()}), flush=True)
    summary = summarize(rows, manifest, budget)
    with private_text_file(output / "summary.json") as stream:
        stream.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--budget-usd", type=float, default=4.50)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if not 0 < args.budget_usd <= 4.50:
        parser.error("budget must be greater than zero and at most USD 4.50")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
