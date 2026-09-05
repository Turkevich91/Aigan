"""Private, provider-free replay of frozen hybrid retrieval evidence.

Prepare exports only the fixed query inputs for a separately budgeted provider
adapter. Freeze/run require its saved vectors. Never points MemoryStore at the
historical archive or at a live database. Stdout contains aggregate metadata only.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from importlib.metadata import version
import json
import logging
import math
import os
from pathlib import Path
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from embedding_dimensions_eval import source_metrics

MODEL, DIMENSIONS = "text-embedding-3-small", 512
POLICIES, ROUTES = ("legacy", "rrf", "normalized"), ("direct", "memory_recall")
ARCHIVE_HASHES = {
    "index-512.sqlite3": "9b641ba861a90467864252c54215055ffcd4bdd443a6b4683157245f7cda1d23",
    "source-freeze.json": "9ff8e83ffd58925df335b8ef9ee56cc38e3acb762a8597d39c98fedde8154db7",
    "query-freeze.json": "5a3b48f2b565b10d0723f1e5c65700eaab7bbea22e639c931c9fbc8e89211401",
    "query-audit.json": "e7e071f512f13752f7185a0763deea4657bc4a29e2c1af98765fdd5fcf5a9dea",
    "reviewed-query-freeze.json": "87d6953b6f71fd77669723e0b5e0f463fb70e80c6cd6a19b2434ed8dbde74793",
}
SOURCE_FILES = ("main.py", "memory.py", "embedding_dimensions_eval.py", "scripts/eval_memory_fusion.py",
                "docs/memory-fusion-180-eval.md")
SETTINGS = {"policies": list(POLICIES), "rrf_k": 60, "channel_weights": [1, 1, 1],
            "normalized_equal_score": 1, "timed_repetitions": 3, "warmups_per_arm_route": 1,
            "bootstrap_repetitions": 10000, "bootstrap_seed": 180,
            "model": MODEL, "dimensions": DIMENSIONS, "cpu_cores": 1, "memory_gib": 2,
            "p95_allowance_ms": 2, "p95_allowance_fraction": .1, "provider_calls": 0,
            "controlled_hit6_gain_min_cases": 4, "controlled_gain_lower95_strictly_positive": True,
            "numeric_protection": "standalone_numeric_token_plus_keyword_hits_uses_legacy",
            "timing_percentile": "nearest_rank", "query_normalization": "aigan_clip_text_4000_l2_v1"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024*1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def write_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def private_dir(path, *, create=False):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)) or path.resolve().is_relative_to(ROOT):
        raise ValueError("unsafe_private_artifact_path")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("private_directory_requires_owner_only_permissions")
    return path


def provider_input(text):
    text = " ".join(text.split())
    return text if len(text) <= 4000 else text[:3976].rstrip() + " [trimmed]"


def read_archive(path):
    path = Path(path)
    if (path / "index-512.sqlite3-wal").exists():
        raise ValueError("historical_index_has_wal")
    for name, expected in ARCHIVE_HASHES.items():
        if file_hash(path / name) != expected:
            raise ValueError("historical_archive_hash_mismatch")
    source = json.loads((path / "source-freeze.json").read_text())
    labels = json.loads((path / "query-freeze.json").read_text())
    audit = json.loads((path / "query-audit.json").read_text())
    reviewed = json.loads((path / "reviewed-query-freeze.json").read_text())
    accepted = {item["family"] for item in audit["decisions"] if item["accepted"]}
    if accepted != set(reviewed["accepted_real_families"]):
        raise ValueError("historical_audit_mismatch")
    queries = [query for query in labels["queries"]
               if query["cohort"] != "source_derived_machine_checked" or query["family"] in accepted]
    if Counter(query["cohort"] for query in queries) != Counter({"controlled_positive": 48,
            "source_derived_machine_checked": 12, "constructed_no_answer": 12, "isolation": 12}):
        raise ValueError("historical_query_population_mismatch")
    if len(source["eligible_ids"]) != 4716 or len(labels["synthetic_ids"]) != 60:
        raise ValueError("historical_embedding_population_mismatch")
    return source, labels, queries


def prepare(args):
    output = private_dir(args.output, create=True)
    source, labels, queries = read_archive(args.archive)
    artifact = {"schema_version": "fusion180-query-inputs-v1", "archive_hashes": ARCHIVE_HASHES,
                "model": MODEL, "dimensions": DIMENSIONS,
                "normalization": SETTINGS["query_normalization"],
                "entries": [{"query": query["query"], "query_sha256": text_hash(query["query"]),
                             "provider_input_sha256": text_hash(provider_input(query["query"]))} for query in queries]}
    write_json(output / "query-inputs.json", artifact)
    return {"completion": "INPUTS_PREPARED", "queries": len(queries),
            "unique_provider_inputs": len({row["provider_input_sha256"] for row in artifact["entries"]}),
            "query_inputs_sha256": file_hash(output / "query-inputs.json"), "provider_calls": 0}


def load_vectors(path, queries, inputs_hash):
    payload = json.loads(Path(path).read_text())
    if payload.get("schema_version") != "fusion180-query-vectors-v1" or payload.get("query_inputs_sha256") != inputs_hash:
        raise ValueError("query_vector_binding_mismatch")
    if payload.get("model") != MODEL or payload.get("dimensions") != DIMENSIONS or payload.get("normalization") != SETTINGS["query_normalization"]:
        raise ValueError("query_vector_model_or_normalization_mismatch")
    entries = payload["entries"]
    if len(entries) != len(queries):
        raise ValueError("query_vector_count_mismatch")
    vectors = []
    seen_inputs = {}
    for query, entry in zip(queries, entries):
        if entry["query_sha256"] != text_hash(query["query"]) or entry["provider_input_sha256"] != text_hash(provider_input(query["query"])):
            raise ValueError("query_vector_input_mismatch")
        vector = entry["vector"]
        if len(vector) != DIMENSIONS or any(not math.isfinite(value) for value in vector):
            raise ValueError("query_vector_shape_mismatch")
        if abs(sum(value*value for value in vector) - 1) > 1e-6 or entry["vector_sha256"] != digest(vector):
            raise ValueError("query_vector_integrity_mismatch")
        key = entry["provider_input_sha256"]
        if key in seen_inputs and seen_inputs[key] != entry["vector_sha256"]:
            raise ValueError("same_provider_input_has_different_vectors")
        seen_inputs[key] = entry["vector_sha256"]
        vectors.append(vector)
    return vectors


def relevant_config(config):
    names = ("memory_embedding_model", "memory_embedding_dimensions", "memory_vector_enabled",
             "memory_semantic_lookback_days", "memory_semantic_top_k", "memory_recall_top_k",
             "memory_recall_intent_threshold", "memory_recall_intent_ambiguous_threshold", "bot_trigger", "bot_username")
    return {name: getattr(config, name) for name in names}


def load_runtime(config, fixed_now, temporary):
    if (ROOT / ".env").exists():
        raise ValueError("evaluation_checkout_must_not_have_dotenv")
    environment = {name: value for name, value in os.environ.items()
                   if name in {"PATH", "HOME", "LANG", "TMPDIR"}}
    environment.update({"TELEGRAM_BOT_TOKEN": "123456:evaluation-token", "OPENAI_API_KEY": "sk-test",
                        "MEMORY_ENABLED": "false", "MEMORY_DB_PATH": str(temporary / "unused.sqlite3"),
                        "REMINDERS_ENABLED": "false", "SYSTEM_LOG_ENABLED": "false", "MODEL_TELEMETRY_ENABLED": "false",
                        "MODEL_ROUTING_MODE": "off", "SOCIAL_MEMORY_ENABLED": "false", "REACTIONS_ENABLED": "false",
                        "AGENTS_TRACING_MODE": "disabled", "LOG_LEVEL": "CRITICAL", "PROACTIVE_ENABLED": "false",
                        "HEAVY_MODEL_ENABLED": "false", "GITHUB_REPORTING_ENABLED": "false"})
    logging.disable(logging.CRITICAL)
    with patch.dict(os.environ, environment, clear=True):
        import main
        import memory
    if main.MEMORY is not None:
        raise ValueError("runtime_import_isolation_failed")
    main.CONFIG = replace(main.CONFIG, **config, memory_search_fusion_policy="legacy")
    main.BOT_USERNAME = config["bot_username"]
    main.system_event = lambda *args, **kwargs: None

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

    # Patch before constructing any copied MemoryStore, including migrations.
    memory.datetime = FrozenDateTime
    return main, memory


def corpus_signature(connection, queries, cutoff):
    hasher = hashlib.sha256()
    for table in ("messages", "message_embeddings"):
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            for value in row:
                if isinstance(value, bytes):
                    hasher.update(value)
                else:
                    hasher.update(json.dumps(value, ensure_ascii=False).encode())
                hasher.update(b"\0")
    message_count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    embedding_count = connection.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0]
    if message_count != 5420 or embedding_count != 4776:
        raise ValueError("copied_corpus_population_changed")
    if connection.execute("SELECT COUNT(*) FROM message_embeddings WHERE model!=? OR dimensions!=512 OR length(embedding_blob)!=2048", (MODEL,)).fetchone()[0]:
        raise ValueError("copied_embedding_metadata_changed")
    populations = []
    for chat_id in sorted({query["chat_id"] for query in queries}):
        values = connection.execute("SELECT COUNT(*),SUM(is_bot=0 AND created_at>=?) FROM messages WHERE chat_id=?", (cutoff, chat_id)).fetchone()
        populations.append({"chat_sha256": digest(chat_id), "retained": values[0], "age_and_bot_eligible": values[1] or 0})
    return {"messages": message_count, "embeddings": embedding_count, "logical_sha256": hasher.hexdigest(),
            "filter_populations": populations}


def sources():
    return {name: file_hash(ROOT / name) for name in SOURCE_FILES}


def runtime_versions():
    return {"python": sys.version.split()[0], **{name: version(name) for name in ("openai", "openai-agents", "python-telegram-bot")}}


def resource_limits():
    cpu = Path("/sys/fs/cgroup/cpu.max").read_text().strip().split()
    memory = Path("/sys/fs/cgroup/memory.max").read_text().strip()
    if cpu[0] == "max" or memory == "max":
        raise ValueError("benchmark_resource_caps_required")
    cpu_cores, memory_bytes = int(cpu[0])/int(cpu[1]), int(memory)
    if not 0 < cpu_cores <= 1 or not 0 < memory_bytes <= 2*1024**3:
        raise ValueError("benchmark_resource_caps_exceed_protocol")
    return {"cpu_cores": cpu_cores, "memory_bytes": memory_bytes}


def freeze(args):
    output = private_dir(args.output, create=True)
    source, labels, queries = read_archive(args.archive)
    config = json.loads(args.config.read_text())
    historical_hash = hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if historical_hash != source["relevant_config_sha256"]:
        raise ValueError("historical_config_hash_mismatch")
    inputs_hash = file_hash(args.query_inputs)
    load_vectors(args.query_vectors, queries, inputs_hash)
    fixed_now = datetime.fromisoformat(source["frozen_at"])
    cutoff = (fixed_now-timedelta(days=config["memory_semantic_lookback_days"])).isoformat(timespec="seconds")
    uri = (args.archive / "index-512.sqlite3").resolve().as_uri()+"?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as original:
        signature = corpus_signature(original, queries, cutoff)
    with tempfile.TemporaryDirectory(prefix="aigan-fusion-freeze-") as tmp:
        main, memory = load_runtime(config, fixed_now, Path(tmp))
        copied = Path(tmp)/"copied.sqlite3"
        shutil.copyfile(args.archive/"index-512.sqlite3", copied)
        store = memory.MemoryStore(copied)
        try:
            if corpus_signature(store._conn, queries, cutoff) != signature or relevant_config(main.CONFIG) != config:
                raise ValueError("isolated_corpus_or_config_changed")
        finally:
            store.close()
    manifest = {"schema_version": "fusion180-source-freeze-v1", "archive_path": str(args.archive.resolve()),
                "archive_hashes": ARCHIVE_HASHES, "config": config, "config_sha256": historical_hash,
                "source_fingerprints": sources(), "settings": SETTINGS, "runtime_versions": runtime_versions(),
                "resource_limits": resource_limits(),
                "query_inputs_sha256": inputs_hash, "query_vectors_path": str(args.query_vectors.resolve()),
                "query_vectors_sha256": file_hash(args.query_vectors), "corpus_signature": signature,
                "clock": source["frozen_at"]}
    write_json(output/"source-freeze.json", manifest)
    return {"completion": "FROZEN", "queries": len(queries), "messages": signature["messages"],
            "embeddings": signature["embeddings"], "freeze_sha256": file_hash(output/"source-freeze.json"), "provider_calls": 0}


def percentile(values, fraction=.95):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)]


def paired_interval(rows, policy, metric):
    grouped = defaultdict(list)
    wins = losses = 0
    for row in rows:
        delta = row["arms"][policy]["metrics"][metric]-row["arms"]["legacy"]["metrics"][metric]
        grouped[row["family"]].append(delta)
        wins += delta > 0
        losses += delta < 0
    values = [sum(group)/len(group) for group in grouped.values()]
    rng = random.Random(SETTINGS["bootstrap_seed"])
    samples = sorted(sum(rng.choice(values) for _ in values)/len(values) for _ in range(SETTINGS["bootstrap_repetitions"]))
    return {"delta": sum(values)/len(values), "lower_95": percentile(samples, .025),
            "upper_95": percentile(samples, .975), "families": len(values), "wins": wins, "losses": losses,
            "ties": len(rows)-wins-losses}


def summary(rows, timings):
    comparisons, performance = {}, {}
    candidates_pass = {policy: True for policy in POLICIES[1:]}
    for route in ROUTES:
        performance[route] = {}
        for policy in POLICIES:
            values = [item["local_wall_ms"] for item in timings if item["route"] == route and item["policy"] == policy]
            performance[route][policy] = {"observations": len(values), "median_ms": percentile(values, .5), "p95_ms": percentile(values)}
        baseline_p95 = performance[route]["legacy"]["p95_ms"]
        allowance = max(2., .1*baseline_p95)
        for policy in POLICIES[1:]:
            delta = performance[route][policy]["p95_ms"]-baseline_p95
            performance[route][policy].update({"p95_delta_ms": delta, "p95_delta_fraction": delta/baseline_p95,
                                                "p95_allowance_ms": allowance, "latency_gate_pass": delta <= allowance})
            candidates_pass[policy] &= delta <= allowance
    for cohort in sorted({row["cohort"] for row in rows}):
        comparisons[cohort] = {}
        for route in ROUTES:
            subset = [row for row in rows if row["cohort"] == cohort and row["route"] == route]
            details = {"queries": len(subset), "arms": {}}
            positive = cohort in {"controlled_positive", "source_derived_machine_checked"}
            metrics = ["source_hit_at_1", "source_hit_at_6", "reciprocal_rank"] + (["source_hit_at_12"] if route == "memory_recall" else [])
            for policy in POLICIES:
                outcomes = [row["arms"][policy] for row in subset]
                info = {"forbidden_returned": sum(item["forbidden_returned"] for item in outcomes),
                        "duplicate_returned": sum(item["duplicate_returned"] for item in outcomes),
                        "provenance_violations": sum(item["provenance_violations"] for item in outcomes),
                        "fallback_reasons": dict(Counter(item["fallback_reason"] for item in outcomes if item["fallback_reason"])),
                        "applied_policies": dict(Counter(item["applied_policy"] for item in outcomes)),
                        "channel_nonempty_queries": {channel: sum(item["channel_results"][channel] > 0 for item in outcomes)
                                                     for channel in ("keyword", "semantic", "fts")},
                        "policy_application_violations": sum(
                            (not item["fallback_reason"] and item["applied_policy"] != policy)
                            or (item["fallback_reason"] == "numeric_protected" and item["applied_policy"] != "legacy")
                            for item in outcomes),
                        "numeric_protection_violations": sum(
                            row["arms"][policy]["fallback_reason"] == "numeric_protected"
                            and (row["arms"][policy]["applied_policy"] != "legacy"
                                 or row["arms"][policy]["ranked_ids"] != row["arms"]["legacy"]["ranked_ids"])
                            for row in subset),
                        "returned_total": sum(item["returned"] for item in outcomes)}
                if positive:
                    info["metrics"] = {metric: sum(item["metrics"][metric] for item in outcomes)/len(outcomes) for metric in metrics}
                if policy != "legacy":
                    unexpected = any(item["fallback_reason"] not in {"", "numeric_protected"} for item in outcomes)
                    valid = not (info["forbidden_returned"] or info["duplicate_returned"] or info["provenance_violations"]
                                 or info["policy_application_violations"] or info["numeric_protection_violations"] or unexpected)
                    candidates_pass[policy] &= valid
                    if positive:
                        info["paired"] = {metric: paired_interval(subset, policy, metric) for metric in metrics}
                        candidates_pass[policy] &= all(value["delta"] >= -1e-12 for value in info["paired"].values())
                        if cohort == "controlled_positive" and route == "direct":
                            gain = info["paired"]["source_hit_at_6"]
                            candidates_pass[policy] &= gain["delta"]*len(subset) >= 4-1e-12 and gain["lower_95"] > 0
                details["arms"][policy] = info
            comparisons[cohort][route] = details
    selected = next((policy for policy in POLICIES[1:] if candidates_pass[policy]), "legacy")
    return {"comparisons": comparisons, "performance": performance, "candidate_gates": candidates_pass,
            "selected_policy": selected, "runtime_promotion": False}


def run(args):
    output = private_dir(args.output, create=True)
    if any(output.iterdir()):
        raise ValueError("run_output_must_be_empty")
    frozen = json.loads(args.freeze.read_text())
    if (frozen["source_fingerprints"] != sources() or frozen["settings"] != SETTINGS
            or frozen["runtime_versions"] != runtime_versions() or frozen["resource_limits"] != resource_limits()):
        raise ValueError("frozen_source_settings_or_runtime_changed")
    archive = Path(frozen["archive_path"])
    source, labels, queries = read_archive(archive)
    vector_path = Path(frozen["query_vectors_path"])
    if file_hash(vector_path) != frozen["query_vectors_sha256"]:
        raise ValueError("query_vectors_changed")
    vectors = load_vectors(vector_path, queries, frozen["query_inputs_sha256"])
    write_json(output/"run-started.json", {"source_freeze_sha256": file_hash(args.freeze), "started_at": time.time()})
    report = {"completion": "INCOMPLETE", "source_freeze_sha256": file_hash(args.freeze),
              "settings": SETTINGS, "runtime_versions": runtime_versions(), "provider_calls": 0,
              "resource_limits": resource_limits(),
              "human_gold": False, "field_queries": False, "answer_quality_evaluated": False}
    rows, timings = {}, []
    store = None
    try:
        fixed_now = datetime.fromisoformat(frozen["clock"])
        main, memory = load_runtime(frozen["config"], fixed_now, output)
        copied = output/"copied-index.sqlite3"
        shutil.copyfile(archive/"index-512.sqlite3", copied)
        copied.chmod(0o600)
        store = memory.MemoryStore(copied)
        cutoff = (fixed_now-timedelta(days=main.CONFIG.memory_semantic_lookback_days)).isoformat(timespec="seconds")
        if corpus_signature(store._conn, queries, cutoff) != frozen["corpus_signature"]:
            raise ValueError("run_corpus_signature_changed")
        main.MEMORY = store
        config = main.CONFIG
        actual_fusion = main.fuse_memory_search_batches
        captured = []

        def capture(batches, **kwargs):
            captured[:] = batches
            return actual_fusion(batches, **kwargs)

        main.fuse_memory_search_batches = capture

        async def retrieve(index, policy, route):
            query, vector = queries[index], vectors[index]
            main.CONFIG = replace(config, memory_search_fusion_policy=policy)

            async def embedding(texts, **kwargs):
                if len(texts) != 1 or provider_input(texts[0]) != provider_input(query["query"]):
                    raise ValueError("runtime_requested_unfrozen_embedding")
                return [vector]

            main.create_embeddings = embedding
            return await main.semantic_memory_search_outcome(SimpleNamespace(chat_id=query["chat_id"]),
                        query["query"], route=route, exclude_message_id=query["exclude_message_id"])

        for route in ROUTES:
            for policy in POLICIES:
                asyncio.run(retrieve(0, policy, route))
        for repetition in range(3):
            for index, query in enumerate(queries):
                order = POLICIES[(index+repetition)%3:]+POLICIES[:(index+repetition)%3]
                for route in ROUTES:
                    row = rows.setdefault((index, route), {"case_index": index, "family": query["family"],
                                                           "cohort": query["cohort"], "route": route, "arms": {}})
                    for policy in order:
                        started = time.perf_counter()
                        outcome = asyncio.run(retrieve(index, policy, route))
                        elapsed = (time.perf_counter()-started)*1000
                        timings.append({"case_index": index, "route": route, "policy": policy,
                                        "repetition": repetition, "local_wall_ms": elapsed})
                        if not outcome.embeddings_used or outcome.embedding_error:
                            raise ValueError("actual_embedding_retrieval_path_not_used")
                        batches = [{"channel": batch.channel, "results": [{"id": result.item.id, "score": result.score,
                                   "source": result.source} for result in batch.results]} for batch in captured]
                        batch_hash = digest(batches)
                        if "batch_sha256" in row and row["batch_sha256"] != batch_hash:
                            raise ValueError("paired_retriever_inputs_changed")
                        row["batch_sha256"] = batch_hash
                        expected_sources = defaultdict(set)
                        for batch in captured:
                            for result in batch.results:
                                expected_sources[result.item.id].update(result.source.split("+"))
                        ranked = [result.item.id for result in outcome.results]
                        values = {"metrics": source_metrics(ranked, query["relevant"]), "ranked_ids": ranked,
                                  "returned": len(ranked), "forbidden_returned": len(set(query["forbidden"]) & set(ranked)),
                                  "duplicate_returned": len(ranked)-len(set(ranked)),
                                  "provenance_violations": sum(set(result.source.split("+")) != expected_sources[result.item.id] for result in outcome.results),
                                  "applied_policy": outcome.fusion_policy, "fallback_reason": outcome.fusion_fallback_reason,
                                  "channel_results": {"keyword": outcome.keyword_results, "semantic": outcome.semantic_results,
                                                      "fts": outcome.fts_results},
                                  "embedding_indexed": outcome.embedding_indexed}
                        if policy in row["arms"] and row["arms"][policy] != values:
                            raise ValueError("repeated_ranking_changed")
                        row["arms"][policy] = values
                        if repetition == 0 and policy == "legacy":
                            with (output/"private-batches.jsonl").open("a") as handle:
                                handle.write(json.dumps({"case_index": index, "route": route, "batches": batches})+"\n")
                            (output/"private-batches.jsonl").chmod(0o600)
            write_json(output/f"private-pass-{repetition+1}.json", {"rows": list(rows.values()), "timings": timings})
            print(json.dumps({"stage": "retrieval", "completed_repetitions": repetition+1, "queries": len(queries)}), flush=True)
        if corpus_signature(store._conn, queries, cutoff) != frozen["corpus_signature"]:
            raise ValueError("corpus_changed_during_read_only_retrieval")
        report.update(summary(list(rows.values()), timings))
        report["completion"] = "COMPLETE"
    except Exception as exc:
        report["error_class"] = type(exc).__name__
        raise
    finally:
        if store is not None:
            store.close()
        report["completed_rank_passes"] = len(timings)
        write_json(output/"private-outcomes.json", {"rows": list(rows.values()), "timings": timings})
        write_json(output/"aggregate-report.json", report)
    return {"completion": report["completion"], "candidate_gates": report["candidate_gates"],
            "selected_policy": report["selected_policy"], "rank_passes": len(timings), "provider_calls": 0}


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    freeze_parser = commands.add_parser("freeze")
    run_parser = commands.add_parser("run")
    for command in (prepare_parser, freeze_parser):
        command.add_argument("--archive", type=Path, required=True)
    for name in ("config", "query-inputs", "query-vectors"):
        freeze_parser.add_argument("--"+name, type=Path, required=True)
    run_parser.add_argument("--freeze", type=Path, required=True)
    for command in (prepare_parser, freeze_parser, run_parser):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = {"prepare": prepare, "freeze": freeze, "run": run}[args.command](args)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(json.dumps({"completion": "FAILED", "error_class": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
