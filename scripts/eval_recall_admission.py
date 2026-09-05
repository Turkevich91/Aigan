"""Isolated paired recall admission evaluation. No Telegram application is started.

Freeze binds code/config/fixture before requests. Run is offline unless explicitly
enabled. Provider credentials are read only from AIGAN_EVAL_API_KEY and are never
passed to the imported application. Holdout requires a separate custodian claim.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from importlib.metadata import version
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recall_admission_eval import (
    BASELINE_SHA, MODEL, DIMENSIONS, PROTOCOL, CachedEmbeddingProvider, EvaluationState,
    claim_holdout, digest, file_hash, private_directory, summarize, validate_fixture,
    write_private_json,
)

BASELINE_MAIN_HASH = "8117c14b80df560a8df00acc4e4289945e1e17889738e764cb2ba8ff7a7c652a"
FIXTURE_MANIFEST = "tests/fixtures/memory_recall_179_manifest.json"
PROTOCOL_FILE = "docs/recall-admission-179-eval.md"
EVALUATOR_FILES = ("recall_admission_eval.py", "scripts/eval_recall_admission.py", "tests/support.py")
CANDIDATE_FILES = ("main.py", "memory_recall.py")


def source_maps():
    # Bind local dependencies without copying their payload into evidence.
    common = sorted({str(path.relative_to(ROOT)).replace("\\", "/")
                     for pattern in ("*.py", "mcp_servers/**/*.py") for path in ROOT.glob(pattern)}
                    - set(CANDIDATE_FILES) - set(EVALUATOR_FILES))
    return {"candidate": {name: file_hash(ROOT / name) for name in CANDIDATE_FILES},
            "evaluator": {name: file_hash(ROOT / name) for name in EVALUATOR_FILES},
            "common": {name: file_hash(ROOT / name) for name in common}}


def candidate_source_digest(maps):
    return digest({**maps["common"], **maps["candidate"]})


def runtime_versions():
    return {"python": sys.version.split()[0],
            **{package: version(package) for package in ("openai", "openai-agents", "python-telegram-bot")}}


def isolated_environment(temporary):
    values = {key: value for key, value in os.environ.items()
              if key in {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SYSTEMROOT"}}
    values.update({"TELEGRAM_BOT_TOKEN": "123456:evaluation-token", "OPENAI_API_KEY": "sk-test",
                   "MEMORY_ENABLED": "false", "MEMORY_DB_PATH": str(temporary / "unused.sqlite3"),
                   "REMINDERS_ENABLED": "false", "SYSTEM_LOG_ENABLED": "false",
                   "MODEL_TELEMETRY_ENABLED": "false", "MODEL_ROUTING_MODE": "off",
                   "SOCIAL_MEMORY_ENABLED": "false", "REACTIONS_ENABLED": "false",
                   "AGENTS_TRACING_MODE": "disabled", "LOG_LEVEL": "CRITICAL",
                   "MEMORY_VECTOR_ENABLED": "true", "MEMORY_EMBEDDING_MODEL": MODEL,
                   "MEMORY_EMBEDDING_DIMENSIONS": str(DIMENSIONS), "MEMORY_VECTOR_BACKFILL_ON_START": "false",
                   "MEMORY_RECALL_INTENT_THRESHOLD": "0.62", "MEMORY_RECALL_INTENT_AMBIGUOUS_THRESHOLD": "0.48",
                   "MEMORY_RECALL_POLICY_MODE": "enforce", "PROACTIVE_ENABLED": "false",
                   "AUTO_REACT_ENABLED": "false", "HEAVY_MODEL_ENABLED": "false",
                   "GITHUB_REPORTING_ENABLED": "false", "BOT_USERNAME": "evaluation_bot",
                   "BOT_TRIGGER": "!m", "ALLOWED_CHAT_IDS": "-1001", "BOT_TIMEZONE": "UTC"})
    return values


def load_apps(baseline_source, temporary):
    if (ROOT / ".env").exists():
        raise ValueError("evaluation_checkout_must_not_have_dotenv")
    if file_hash(baseline_source) != BASELINE_MAIN_HASH:
        raise ValueError("baseline_source_hash_mismatch")
    logging.disable(logging.CRITICAL)
    apps = {}
    with patch.dict(os.environ, isolated_environment(temporary), clear=True):
        for arm, path in (("baseline", baseline_source), ("candidate", ROOT / "main.py")):
            name = "aigan_recall_eval_" + arm
            module = ModuleType(name)
            module.__file__ = str(ROOT / "main.py")
            module.__package__ = ""
            sys.modules[name] = module
            exec(compile(path.read_text(encoding="utf-8"), str(ROOT / "main.py"), "exec"), module.__dict__)
            # No constructor has touched a runtime database; create a disposable store.
            if any(getattr(module, key, None) is not None for key in
                   ("MEMORY", "REMINDERS", "SYSTEM_LOG", "MODEL_TELEMETRY", "SOCIAL_MEMORY", "REACTION_MEMORY")):
                raise ValueError("import_time_store_isolation_failed")
            module.MEMORY = module.MemoryStore(str(temporary / (arm + ".sqlite3")), 30)
            module.BOT_ID, module.BOT_USERNAME = 999, "evaluation_bot"
            module.recall_intent_embedding_cache.clear()
            module.system_event = lambda *args, **kwargs: None
            apps[arm] = module
    return apps


def config_binding(apps):
    fields = ("memory_embedding_model", "memory_embedding_dimensions", "memory_vector_enabled",
              "memory_recall_intent_threshold", "memory_recall_intent_ambiguous_threshold",
              "bot_trigger", "bot_username")
    return {arm: {"config": {name: getattr(app.CONFIG, name) for name in fields},
                  "runtime_versions": runtime_versions(),
                  "policy_mode": getattr(app.CONFIG, "memory_recall_policy_mode", "legacy"),
                  "archetypes": list(app.MEMORY_RECALL_ARCHETYPES)} for arm, app in apps.items()}


def close_apps(apps):
    for app in apps.values():
        app.MEMORY.close()


def freeze(args):
    output = private_directory(args.output, repository=ROOT, create=True)
    state = EvaluationState(private_directory(args.state, repository=ROOT, create=True))
    manifest = json.loads((ROOT / FIXTURE_MANIFEST).read_text())
    fixture_sha = file_hash(args.fixture)
    split = args.split
    if fixture_sha != manifest[split]["sha256"]:
        raise ValueError("fixture_hash_does_not_match_custodian_manifest")
    with tempfile.TemporaryDirectory(prefix="aigan-recall-freeze-") as temp:
        apps = load_apps(args.baseline_source, Path(temp))
        try:
            config = config_binding(apps)
        finally:
            close_apps(apps)
    frozen = {"schema_version": "recall-admission-source-freeze-v1", "split": split,
              "fixture_path": str(args.fixture.resolve()), "fixture_sha256": fixture_sha,
              "baseline_source_path": str(args.baseline_source.resolve()), "baseline_sha": BASELINE_SHA,
              "baseline_main_sha256": BASELINE_MAIN_HASH, "sources": source_maps(),
              "config": config, "config_sha256": digest(config), "protocol": PROTOCOL,
              "protocol_sha256": file_hash(ROOT / PROTOCOL_FILE),
              "fixture_manifest_sha256": file_hash(ROOT / FIXTURE_MANIFEST),
              "state_identity": state.identity(),
              "initial_cache_manifest": state.cache_manifest(), "initial_accounting": state.accounting()}
    write_private_json(output / "source-freeze.json", frozen)
    return {"completion": "FROZEN", "split": split, "freeze_sha256": file_hash(output / "source-freeze.json"),
            "candidate_source_sha256": candidate_source_digest(frozen["sources"]),
            "evaluator_source_sha256": digest(frozen["sources"]["evaluator"]),
            "candidate_config_sha256": digest(config["candidate"])}


def synthetic_message(case, *, boundary=False):
    from tests.support import FakeMessage
    ctx = case["context"]
    prompt = case["prompt"]
    if boundary and ctx["invoked"]:
        prompt = "!m " + prompt
    message = FakeMessage(prompt, chat_type=ctx["chat_type"])
    if ctx["has_reply_text"] or ctx.get("reply_to_bot"):
        reply = FakeMessage(ctx["reply_text"], chat_type=ctx["chat_type"], message_id=99)
        if ctx.get("reply_to_bot"):
            reply.from_user.id, reply.from_user.is_bot = 999, True
        message.reply_to_message = reply
    if ctx["has_reply_image"]:
        raise ValueError("image_fixture_not_in_registered_scope")
    return message


async def boundary_outcome(app, case, arm):
    message = synthetic_message(case, boundary=True)
    context = SimpleNamespace(bot=SimpleNamespace(id=999, username="evaluation_bot"))
    handle, detect, embed = AsyncMock(), AsyncMock(), AsyncMock()
    # The actual handler retains the invocation branch. Replace only persistence,
    # background observation and downstream generation; no policy gate is mocked.
    with ExitStack() as stack:
        for name, replacement in {
            "remember_message_persistently": AsyncMock(), "remember_observed_message": Mock(),
            "remember_self_complaint_signal": Mock(), "handle_prompt": handle,
            "detect_memory_recall_intent": detect, "create_embeddings": embed,
        }.items():
            stack.enter_context(patch.object(app, name, replacement))
        await app.text_message(SimpleNamespace(effective_message=message), context)
    expected = case["expected"]
    observed = {"invocation_eligible": bool(handle.await_count), "recall_stage_calls": detect.await_count,
                "telegram_deliveries": len(message.reply_calls) + len(message.photo_calls) + len(message.media_group_calls)}
    passed = all(value is None or observed[key] == value for key, value in expected.items()) and not embed.await_count
    return {"case_id": case["case_id"], "arm": arm, "passed": passed, "observed": observed,
            "scope": "invocation_admission_only_downstream_spy"}


async def evaluate(apps, fixture, provider, rows, boundaries, output):
    async def embeddings(texts, **kwargs):
        return provider.embed(texts)
    for app in apps.values():
        app.create_embeddings = embeddings
    for index, case in enumerate(fixture["cases"]):
        row = {"case_id": case["case_id"], "family_id": case["family_id"], "language": case["language"],
               "expected": case["expected"]["is_recall"], "critical_negative": case["expected"]["critical_negative"]}
        for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
            started = time.perf_counter()
            result = await apps[arm].detect_memory_recall_intent(synthetic_message(case), case["prompt"])
            row[arm] = {"is_recall": result.is_recall, "confidence": result.confidence,
                        "reason": result.reason, "degraded": result.degraded,
                        "local_wall_ms_including_cache_or_api": (time.perf_counter() - started) * 1000}
            if provider.failure or result.degraded:
                raise RuntimeError("embedding_or_detector_degraded_study_incomplete")
        rows.append(row)
        with (output / "private-outcomes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        (output / "private-outcomes.jsonl").chmod(0o600)
    for case in fixture["boundary_cases"]:
        for arm, app in apps.items():
            boundaries.append(await boundary_outcome(app, case, arm))


def run(args):
    output = private_directory(args.output, repository=ROOT, create=True)
    if any(output.iterdir()):
        raise ValueError("run_output_must_be_empty")
    state = EvaluationState(private_directory(args.state, repository=ROOT, create=True))
    frozen = json.loads(args.freeze.read_text())
    if frozen["sources"] != source_maps() or frozen["protocol"] != PROTOCOL:
        raise ValueError("measured_sources_or_protocol_changed")
    if frozen["fixture_manifest_sha256"] != file_hash(ROOT / FIXTURE_MANIFEST) or frozen["protocol_sha256"] != file_hash(ROOT / PROTOCOL_FILE):
        raise ValueError("custodian_protocol_changed")
    if frozen.get("state_identity") != state.identity():
        raise ValueError("shared_state_identity_changed")
    cache_validation = {"source_freeze": state.verify_cache_manifest(
        frozen["initial_cache_manifest"], allow_additions=True)}
    # Reject runtime/configuration drift before consuming or parsing a holdout.
    with tempfile.TemporaryDirectory(prefix="aigan-recall-preflight-") as temp:
        apps = load_apps(Path(frozen["baseline_source_path"]), Path(temp))
        try:
            if config_binding(apps) != frozen["config"]:
                raise ValueError("effective_config_changed")
        finally:
            close_apps(apps)
    if args.allow_provider and not os.environ.get("AIGAN_EVAL_API_KEY"):
        raise ValueError("explicit_evaluation_api_key_missing")
    fixture_path = Path(frozen["fixture_path"])
    if file_hash(fixture_path) != frozen["fixture_sha256"]:
        raise ValueError("fixture_changed")
    if frozen["split"] == "holdout":
        if not args.authorization or not args.development_report or not args.development_cache_manifest:
            raise ValueError("holdout_requires_custodian_authorization_and_development_artifacts")
        cache_validation["development"] = state.verify_cache_manifest(
            json.loads(args.development_cache_manifest.read_text()), allow_additions=True)
        bindings = {**state.identity(), "fixture_sha256": frozen["fixture_sha256"],
                    "fixture_manifest_sha256": frozen["fixture_manifest_sha256"],
                    "protocol_sha256": frozen["protocol_sha256"],
                    "evaluator_source_sha256": digest(frozen["sources"]["evaluator"]),
                    "candidate_source_sha256": candidate_source_digest(frozen["sources"]),
                    "candidate_config_sha256": digest(frozen["config"]["candidate"]),
                    "development_report_sha256": file_hash(args.development_report),
                    "development_cache_manifest_sha256": file_hash(args.development_cache_manifest)}
        claim_holdout(state.directory, json.loads(args.authorization.read_text()), bindings)
    # The holdout payload is first parsed only after an exclusive authorization claim.
    try:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        validate_fixture(fixture)
    except Exception as exc:
        write_private_json(output / "aggregate-report.json", {
            "completion": "INCOMPLETE", "error_class": type(exc).__name__,
            "freeze_sha256": file_hash(args.freeze), "accounting": state.accounting()})
        raise
    write_private_json(output / "run-started.json", {"freeze_sha256": file_hash(args.freeze),
                                                    "allow_provider": args.allow_provider,
                                                    "started_at": time.time()})
    report = {"completion": "INCOMPLETE", "split": frozen["split"], "human_gold": False,
              "label_authority": fixture["label_authority"], "freeze_sha256": file_hash(args.freeze),
              "fixture_sha256": frozen["fixture_sha256"], "protocol": PROTOCOL,
              "runtime_versions": runtime_versions(), "state_identity": state.identity(),
              "cache_validation": cache_validation,
              "initial_accounting": state.accounting()}
    rows, boundaries = [], []
    client = None
    try:
        if args.allow_provider:
            key = os.environ.get("AIGAN_EVAL_API_KEY")
            if not key:
                raise ValueError("explicit_evaluation_api_key_missing")
            from openai import OpenAI
            client = OpenAI(api_key=key, max_retries=0, timeout=45)
        provider = CachedEmbeddingProvider(state, phase="recall179_" + frozen["split"], client=client)
        with tempfile.TemporaryDirectory(prefix="aigan-recall-run-") as temp:
            apps = load_apps(Path(frozen["baseline_source_path"]), Path(temp))
            try:
                if config_binding(apps) != frozen["config"]:
                    raise ValueError("effective_config_changed")
                # Fetch the frozen finite input population once. Replay then
                # measures both actual detectors from identical cached vectors.
                provider.embed([case["prompt"] for case in fixture["cases"]]
                               + list(apps["baseline"].MEMORY_RECALL_ARCHETYPES)
                               + list(apps["candidate"].MEMORY_RECALL_ARCHETYPES))
                provider.client = None
                asyncio.run(evaluate(apps, fixture, provider, rows, boundaries, output))
            finally:
                close_apps(apps)
        report.update(summarize(rows, boundaries))
        report.update({"completion": "COMPLETE", "cache_hits": provider.cache_hits,
                       "cache_misses": provider.cache_misses,
                       "limitations": ["Synthetic independent-agent labels are not human gold or natural field queries.",
                                       "Boundary checks cover actual invocation admission with a downstream spy, not answer delivery.",
                                       "Shared cache and API trial overrides do not measure production latency or reliability.",
                                       "Recall admission alone does not measure retrieval or final-answer quality."]})
    except BaseException as exc:
        report.update({"error_class": type(exc).__name__, "completed_pairs": len(rows)})
        raise
    finally:
        if client is not None:
            client.close()
        report["accounting"] = state.accounting()
        report["cache_manifest"] = state.cache_manifest()
        write_private_json(output / "cache-manifest.json", report["cache_manifest"])
        write_private_json(output / "private-boundaries.json", boundaries)
        write_private_json(output / "aggregate-report.json", report)
    return {"completion": report["completion"], "split": frozen["split"], "cases": report["cases"],
            "baseline": report["baseline"], "candidate": report["candidate"],
            "boundary_violations": report["boundary_violations"], "gate_pass": report["gate_pass"],
            "accounting": report["accounting"]}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("freeze")
    prepare.add_argument("--fixture", type=Path, required=True)
    prepare.add_argument("--split", choices=("development", "holdout"), required=True)
    prepare.add_argument("--baseline-source", type=Path, required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--freeze", type=Path, required=True)
    execute.add_argument("--allow-provider", action="store_true")
    execute.add_argument("--authorization", type=Path)
    execute.add_argument("--development-report", type=Path)
    execute.add_argument("--development-cache-manifest", type=Path)
    for command in (prepare, execute):
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(freeze(args) if args.command == "freeze" else run(args), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"completion": "FAILED", "error_class": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
