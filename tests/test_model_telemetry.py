import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from model_pricing import TokenUsage, estimate_token_cost
from model_routing import normalize_model_routing_decision, opaque_episode_key
from model_telemetry import (
    ModelTelemetryStore,
    extract_usage_metrics,
    format_datetime,
    normalize_tracing_mode,
    utc_now,
)


def response_usage(
    *,
    input_tokens: int = 1000,
    cached_tokens: int = 100,
    cache_write_tokens: int = 100,
    output_tokens: int = 50,
    reasoning_tokens: int = 20,
) -> SimpleNamespace:
    return SimpleNamespace(
        requests=1,
        input_tokens=input_tokens,
        input_tokens_details=SimpleNamespace(
            cached_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        output_tokens=output_tokens,
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        total_tokens=input_tokens + output_tokens,
        request_usage_entries=[],
    )


class ModelPricingTests(unittest.TestCase):
    def test_sol_price_counts_cached_and_cache_write_without_double_counting_reasoning(self) -> None:
        estimate = estimate_token_cost(
            "gpt-5.6-sol",
            [
                TokenUsage(
                    input_tokens=1000,
                    cached_input_tokens=100,
                    cache_write_tokens=100,
                    output_tokens=50,
                )
            ],
        )

        self.assertTrue(estimate.complete)
        self.assertEqual("estimated", estimate.status)
        self.assertEqual(6_175_000, estimate.nano_usd)

    def test_unknown_model_and_missing_usage_never_become_zero_cost(self) -> None:
        unknown = estimate_token_cost(
            "private-model-name",
            [TokenUsage(input_tokens=100, output_tokens=10)],
        )
        missing = estimate_token_cost("gpt-5.6-sol", [])

        self.assertIsNone(unknown.nano_usd)
        self.assertEqual("missing_price", unknown.status)
        self.assertFalse(unknown.complete)
        self.assertIsNone(missing.nano_usd)
        self.assertEqual("missing_usage", missing.status)

    def test_unavailable_bare_alias_is_not_assumed_to_be_sol(self) -> None:
        estimate = estimate_token_cost(
            "gpt-5.6",
            [TokenUsage(input_tokens=100, output_tokens=10)],
        )

        self.assertEqual("missing_price", estimate.status)
        self.assertIsNone(estimate.nano_usd)

    def test_partial_usage_keeps_known_cost_components(self) -> None:
        input_only = estimate_token_cost(
            "gpt-5.6-sol",
            [TokenUsage(input_tokens=100, output_tokens=None)],
        )
        output_only = estimate_token_cost(
            "gpt-5.6-sol",
            [TokenUsage(input_tokens=None, output_tokens=10)],
        )

        self.assertEqual("partial", input_only.status)
        self.assertEqual(500_000, input_only.nano_usd)
        self.assertFalse(input_only.complete)
        self.assertEqual("partial", output_only.status)
        self.assertEqual(300_000, output_only.nano_usd)
        self.assertFalse(output_only.complete)

    def test_long_context_surcharge_is_applied_per_request(self) -> None:
        estimate = estimate_token_cost(
            "gpt-5.6-sol",
            [
                TokenUsage(input_tokens=300_000, output_tokens=10),
                TokenUsage(input_tokens=100, output_tokens=10),
            ],
        )

        expected = (300_000 * 5_000 * 2) + (10 * 30_000 * 3 // 2)
        expected += (100 * 5_000) + (10 * 30_000)
        self.assertEqual(expected, estimate.nano_usd)


class UsageExtractionTests(unittest.TestCase):
    def test_real_agents_default_usage_is_missing_not_zero_cost(self) -> None:
        try:
            from agents.usage import Usage
        except ImportError:
            self.skipTest("OpenAI Agents SDK is not installed")

        metrics = extract_usage_metrics(Usage(), endpoint="agents")

        self.assertEqual("missing", metrics.status)
        self.assertEqual((), metrics.request_entries)
        self.assertIsNone(metrics.input_tokens)
        self.assertIsNone(metrics.output_tokens)

    def test_agents_usage_keeps_per_request_entries(self) -> None:
        entry = response_usage(input_tokens=20, cached_tokens=2, cache_write_tokens=3, output_tokens=5)
        usage = response_usage(input_tokens=20, cached_tokens=2, cache_write_tokens=3, output_tokens=5)
        usage.requests = 1
        usage.request_usage_entries = [entry]

        metrics = extract_usage_metrics(usage, endpoint="agents")

        self.assertEqual("reported", metrics.status)
        self.assertEqual(20, metrics.input_tokens)
        self.assertEqual(2, metrics.cached_input_tokens)
        self.assertEqual(3, metrics.cache_write_tokens)
        self.assertEqual(5, metrics.output_tokens)
        self.assertEqual(1, len(metrics.request_entries))

    def test_embedding_usage_leaves_non_applicable_output_nullable(self) -> None:
        metrics = extract_usage_metrics(
            SimpleNamespace(prompt_tokens=40, total_tokens=40),
            endpoint="embeddings",
        )

        self.assertEqual(40, metrics.input_tokens)
        self.assertIsNone(metrics.output_tokens)
        self.assertEqual(0, metrics.request_entries[0].output_tokens)

    def test_duration_usage_is_not_misclassified_as_tokens(self) -> None:
        metrics = extract_usage_metrics(
            SimpleNamespace(type="duration", seconds=1.25),
            endpoint="transcription",
        )

        self.assertEqual("duration", metrics.unit)
        self.assertEqual(1250, metrics.audio_duration_ms)
        self.assertEqual((), metrics.request_entries)


class ModelTelemetryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "telemetry.sqlite3"
        self.store = ModelTelemetryStore(self.db_path, retention_days=30)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except sqlite3.Error:
            pass
        self.temp.cleanup()

    def test_tracing_policy_is_explicit_and_fails_closed_on_invalid_value(self) -> None:
        self.assertEqual("disabled", normalize_tracing_mode(None))
        self.assertEqual("metadata_only", normalize_tracing_mode("metadata-only"))
        self.assertEqual("sensitive", normalize_tracing_mode("sensitive"))
        with self.assertRaisesRegex(ValueError, "AGENTS_TRACING_MODE"):
            normalize_tracing_mode("provider-default")

    def test_additive_migration_preserves_existing_tables_and_rows(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute("CREATE TABLE IF NOT EXISTS existing_memory(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO existing_memory(value) VALUES ('preserve-me')")
        connection.commit()
        connection.close()

        self.store = ModelTelemetryStore(self.db_path)
        check = sqlite3.connect(self.db_path)
        try:
            value = check.execute("SELECT value FROM existing_memory").fetchone()[0]
            tables = {
                row[0]
                for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            check.close()

        self.assertEqual("preserve-me", value)
        self.assertIn("model_telemetry_runs", tables)
        self.assertIn("model_telemetry_stages", tables)
        self.assertIn("model_routing_decisions", tables)

    def test_ordered_migration_upgrades_v1_to_current_schema(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "ALTER TABLE model_telemetry_stages DROP COLUMN actual_reasoning_effort"
        )
        connection.execute(
            "UPDATE model_telemetry_meta SET value = '1' WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()

        self.store = ModelTelemetryStore(self.db_path)
        columns = {
            str(row[1])
            for row in self.store._conn.execute(
                "PRAGMA table_info(model_telemetry_stages)"
            )
        }
        routing_columns = {
            str(row[1])
            for row in self.store._conn.execute(
                "PRAGMA table_info(model_routing_decisions)"
            )
        }
        version = self.store._conn.execute(
            "SELECT value FROM model_telemetry_meta WHERE key = 'schema_version'"
        ).fetchone()[0]

        self.assertIn("actual_reasoning_effort", columns)
        self.assertIn("route_bucket", columns)
        self.assertIn("task_class_bucket", columns)
        self.assertIn("router_prompt_version", routing_columns)
        self.assertEqual("5", version)

    def test_v3_migration_adds_routing_ledger_without_touching_stage_rows(self) -> None:
        handle = self.store.begin_stage(
            run_id="4" * 32,
            route_bucket="normal",
            task_class_bucket="agent",
            stage_kind="final_answer",
            intended_model="gpt-5.6-sol",
            endpoint="agents",
        )
        self.store.finish_stage(handle, status="succeeded", usage=response_usage())
        self.store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute("DROP TABLE model_routing_decisions")
        connection.execute(
            "UPDATE model_telemetry_meta SET value = '3' WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()

        self.store = ModelTelemetryStore(self.db_path)

        self.assertEqual(1, len(self.store.latest_stages(10)))
        self.assertEqual([], self.store.latest_routing_decisions(10))
        self.assertEqual(
            "5",
            self.store._conn.execute(
                "SELECT value FROM model_telemetry_meta WHERE key = 'schema_version'"
            ).fetchone()[0],
        )

    def test_newer_schema_version_is_rejected_without_being_downgraded(self) -> None:
        self.store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE model_telemetry_meta SET value = '99' WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(RuntimeError, "newer"):
            ModelTelemetryStore(self.db_path)
        connection = sqlite3.connect(self.db_path)
        try:
            version = connection.execute(
                "SELECT value FROM model_telemetry_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual("99", version)
        self.store = ModelTelemetryStore(Path(self.temp.name) / "replacement.sqlite3")

    def test_v3_migration_backfills_stage_route_and_task_from_run(self) -> None:
        handle = self.store.begin_stage(
            run_id="2" * 32,
            route_bucket="normal",
            task_class_bucket="agent",
            stage_kind="agent_turn",
            intended_model="gpt-5.6-sol",
            endpoint="agents",
        )
        self.assertIsNotNone(handle)
        self.store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute("DROP INDEX idx_model_telemetry_stage_route")
        connection.execute("DROP INDEX idx_model_telemetry_stage_task")
        connection.execute(
            "ALTER TABLE model_telemetry_stages DROP COLUMN route_bucket"
        )
        connection.execute(
            "ALTER TABLE model_telemetry_stages DROP COLUMN task_class_bucket"
        )
        connection.execute(
            "UPDATE model_telemetry_meta SET value = '2' WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()

        self.store = ModelTelemetryStore(self.db_path)
        row = self.store.latest_stages(1)[0]

        self.assertEqual("normal", row.route_bucket)
        self.assertEqual("agent", row.task_class_bucket)

    def test_successful_stage_records_usage_cost_and_provider_actual_model(self) -> None:
        handle = self.store.begin_stage(
            run_id="a" * 32,
            route_bucket="normal",
            policy_version="primary_sol_low_v1",
            stage_kind="final_answer",
            intended_model="gpt-5.6-sol",
            reasoning_effort="low",
            endpoint="responses",
        )
        completed = self.store.finish_stage(
            handle,
            status="succeeded",
            usage=response_usage(),
            actual_model="gpt-5.6-sol",
            actual_model_source="provider_response",
            actual_reasoning_effort="low",
        )
        row = self.store.latest_stages(1)[0]

        self.assertTrue(completed)
        self.assertEqual("succeeded", row.status)
        self.assertEqual("provider_response", row.actual_model_source)
        self.assertEqual("low", row.actual_reasoning_effort)
        self.assertEqual(1000, row.input_tokens)
        self.assertEqual(20, row.reasoning_tokens)
        self.assertEqual(6_175_000, row.estimated_cost_nano_usd)
        self.assertEqual("estimated", row.cost_status)

    def test_terminal_stage_kind_can_reclassify_an_agent_turn(self) -> None:
        handle = self.store.begin_stage(
            run_id="9" * 32,
            route_bucket="normal",
            stage_kind="agent_turn",
            intended_model="gpt-5.6-sol",
            endpoint="agents",
        )

        self.store.finish_stage(
            handle,
            status="succeeded",
            usage=response_usage(),
            stage_kind="agent_tool_turn",
        )

        self.assertEqual("agent_tool_turn", self.store.latest_stages(1)[0].stage_kind)

    def test_finish_rolls_back_and_connection_remains_usable_after_write_failure(self) -> None:
        handle = self.store.begin_stage(
            run_id="4" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        self.store._conn.execute(
            """
            CREATE TRIGGER fail_model_telemetry_run_update
            BEFORE UPDATE ON model_telemetry_runs
            BEGIN
                SELECT RAISE(ABORT, 'forced failure');
            END
            """
        )
        self.store._conn.commit()

        self.assertFalse(
            self.store.finish_stage(handle, status="succeeded", usage=response_usage())
        )
        self.assertEqual("pending", self.store.latest_stages(1)[0].status)

        self.store._conn.execute("DROP TRIGGER fail_model_telemetry_run_update")
        self.store._conn.commit()
        self.assertTrue(
            self.store.finish_stage(handle, status="succeeded", usage=response_usage())
        )
        self.assertEqual("succeeded", self.store.latest_stages(1)[0].status)

    def test_failure_stores_only_failure_class_not_exception_text(self) -> None:
        private_marker = "PRIVATE-URL-and-user-marker"
        handle = self.store.begin_stage(
            run_id="b" * 32,
            route_bucket="normal",
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        self.store.finish_stage(
            handle,
            status="failed",
            failure_class=RuntimeError(private_marker),
        )
        row = self.store.latest_stages(1)[0]
        persisted_values = repr(
            [
                tuple(row)
                for row in self.store._conn.execute(
                    "SELECT * FROM model_telemetry_runs JOIN model_telemetry_stages USING(run_id)"
                ).fetchall()
            ]
        )
        self.store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = b"".join(
            path.read_bytes()
            for path in (
                self.db_path,
                Path(f"{self.db_path}-wal"),
            )
            if path.exists()
        )

        self.assertEqual("runtimeerror", row.failure_class)
        self.assertIsNone(row.input_tokens)
        self.assertIsNone(row.estimated_cost_nano_usd)
        self.assertNotIn(private_marker, persisted_values)
        self.assertNotIn(private_marker.encode(), raw)

    def test_user_derived_bucket_values_are_rejected_at_the_sink(self) -> None:
        private_marker = "private_marker"
        handle = self.store.begin_stage(
            run_id="5" * 32,
            route_bucket=private_marker,
            task_class_bucket=private_marker,
            policy_version=private_marker,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            reasoning_effort=private_marker,
            endpoint=private_marker,
        )
        self.store.finish_stage(
            handle,
            status="succeeded",
            usage=response_usage(),
            actual_model_source=private_marker,
            actual_reasoning_effort=private_marker,
            fallback_reason=private_marker,
        )

        persisted_values = repr(
            [
                tuple(row)
                for row in self.store._conn.execute(
                    "SELECT * FROM model_telemetry_runs JOIN model_telemetry_stages USING(run_id)"
                ).fetchall()
            ]
        )
        self.assertNotIn(private_marker, persisted_values)

    def test_unknown_price_is_explicit(self) -> None:
        handle = self.store.begin_stage(
            run_id="c" * 32,
            route_bucket="normal",
            stage_kind="plain",
            intended_model="unknown-model",
            endpoint="responses",
        )
        self.store.finish_stage(handle, status="succeeded", usage=response_usage())
        row = self.store.latest_stages(1)[0]

        self.assertEqual("missing_price", row.cost_status)
        self.assertIsNone(row.estimated_cost_nano_usd)

    def test_duration_billing_unit_remains_unpriced(self) -> None:
        handle = self.store.begin_stage(
            run_id="d" * 32,
            route_bucket="youtube_audio_fallback",
            stage_kind="transcription",
            intended_model="gpt-4o-mini-transcribe",
            endpoint="transcription",
        )
        self.store.finish_stage(
            handle,
            status="succeeded",
            usage=SimpleNamespace(type="duration", seconds=2.5),
        )
        row = self.store.latest_stages(1)[0]

        self.assertEqual(2500, row.audio_duration_ms)
        self.assertEqual("unsupported_unit", row.cost_status)
        self.assertIsNone(row.estimated_cost_nano_usd)

    def test_multi_stage_run_uses_unique_ordinals_and_aggregates(self) -> None:
        run_id = "e" * 32
        first = self.store.begin_stage(
            run_id=run_id,
            route_bucket="pre_route",
            task_class_bucket="router",
            stage_kind="router",
            intended_model="gpt-5.4-nano",
            endpoint="responses",
        )
        second = self.store.begin_stage(
            run_id=run_id,
            route_bucket="normal",
            task_class_bucket="agent",
            stage_kind="final_answer",
            intended_model="gpt-5.6-sol",
            reasoning_effort="low",
            endpoint="agents",
        )
        self.store.finish_stage(
            first,
            status="succeeded",
            usage=response_usage(
                input_tokens=10,
                cached_tokens=0,
                cache_write_tokens=0,
                output_tokens=2,
            ),
            actual_model="gpt-5.4-nano",
            actual_model_source="provider_response",
        )
        self.store.finish_stage(
            second,
            status="succeeded",
            usage=response_usage(
                input_tokens=20,
                cached_tokens=0,
                cache_write_tokens=0,
                output_tokens=3,
            ),
            actual_model_source="unavailable_sdk",
        )

        rows = sorted(self.store.latest_stages(10), key=lambda item: item.ordinal)
        summary = self.store.aggregate_since(3600)

        self.assertEqual([0, 1], [item.ordinal for item in rows])
        self.assertEqual(["pre_route", "normal"], [item.route_bucket for item in rows])
        self.assertEqual(["router", "agent"], [item.task_class_bucket for item in rows])
        self.assertTrue(all(item.latency_ms is not None for item in rows))
        self.assertEqual("low", rows[1].reasoning_effort)
        self.assertEqual("provider_response", rows[0].actual_model_source)
        self.assertEqual("unavailable_sdk", rows[1].actual_model_source)
        self.assertEqual(2, summary["stage_count"])
        self.assertEqual({"pre_route": 1, "normal": 1}, summary["route_counts"])
        self.assertEqual({"router": 1, "agent": 1}, summary["task_class_counts"])
        self.assertEqual(30, summary["token_totals"]["input_tokens"])
        self.assertEqual(5, summary["token_totals"]["output_tokens"])
        self.assertTrue(summary["cost_complete"])
        run_task = self.store._conn.execute(
            "SELECT task_class_bucket FROM model_telemetry_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        self.assertEqual("agent", run_task)

    def test_shadow_routing_decision_is_payload_free_and_aggregated(self) -> None:
        run_id = "5" * 32
        stage = self.store.begin_stage(
            run_id=run_id,
            route_bucket="normal",
            task_class_bucket="model_policy_router",
            policy_version="shadow_tier_router_v1",
            stage_kind="model_policy_router",
            intended_model="gpt-5.4-nano",
            reasoning_effort="none",
            endpoint="responses",
        )
        self.store.finish_stage(
            stage,
            status="succeeded",
            usage=response_usage(input_tokens=20, output_tokens=5),
            actual_model="gpt-5.4-nano",
            actual_model_source="provider_response",
            actual_reasoning_effort="none",
        )
        decision = normalize_model_routing_decision(
            {
                "task_class": "simple_utility",
                "complexity": "low",
                "freshness": "not_required",
                "risk": "low",
                "ambiguity": "low",
                "selected_tier": "economy",
                "reasoning_effort": "none",
                "confidence": 0.96,
                "reason_codes": ["bounded_extraction"],
                "fallback_chain": ["economy", "balanced", "premium"],
            },
            confidence_threshold=0.75,
            route_bucket="normal",
        )
        marker = "private-routing-payload-marker"
        recorded = self.store.record_routing_decision(
            run_id=run_id,
            route_bucket="normal",
            policy_version="shadow_tier_router_v1",
            router_schema_version="model_policy_v1",
            router_prompt_version="model_policy_prompt_v1",
            mode="shadow",
            decision=decision,
            selected_model="gpt-5.4-nano",
            applied_tier="premium",
            applied_model="gpt-5.6-sol",
            applied_reasoning_effort="low",
            assignment_key=opaque_episode_key("private-test-secret", marker),
            assignment_scope="single_turn",
            canary_eligible=True,
            eligibility_reason="eligible_exact_utility",
        )

        row = self.store.latest_routing_decisions(1)[0]
        summary = self.store.aggregate_since(3600)["routing"]
        query_plan = " ".join(
            str(item[3])
            for item in self.store._conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM model_routing_decisions
                ORDER BY created_at DESC, decision_id DESC
                LIMIT 1
                """
            ).fetchall()
        )

        self.assertTrue(recorded)
        self.assertEqual("simple_utility", row.task_class)
        self.assertEqual("model_policy_prompt_v1", row.router_prompt_version)
        self.assertEqual("economy", row.selected_tier)
        self.assertEqual("premium", row.applied_tier)
        self.assertEqual("gpt-5.6-sol", row.applied_model)
        self.assertTrue(row.canary_eligible)
        self.assertEqual(1, summary["decision_count"])
        self.assertEqual({"economy": 1}, summary["selected_tier_counts"])
        self.assertEqual(1, summary["canary_eligible_count"])
        self.assertIn("idx_model_routing_decision_created", query_plan)
        self.store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        persisted = self.db_path.read_bytes()
        wal_path = Path(f"{self.db_path}-wal")
        if wal_path.exists():
            persisted += wal_path.read_bytes()
        self.assertNotIn(marker.encode(), persisted)

    def test_routing_decision_is_at_most_once_per_run(self) -> None:
        run_id = "b" * 32
        stage = self.store.begin_stage(
            run_id=run_id,
            route_bucket="normal",
            task_class_bucket="model_policy_router",
            policy_version="shadow_tier_router_v1",
            stage_kind="model_policy_router",
            intended_model="gpt-5.4-nano",
            endpoint="responses",
        )
        self.store.finish_stage(stage, status="succeeded", usage=response_usage())
        decision = normalize_model_routing_decision(
            {
                "task_class": "simple_utility",
                "complexity": "low",
                "freshness": "not_required",
                "risk": "low",
                "ambiguity": "low",
                "selected_tier": "economy",
                "reasoning_effort": "none",
                "confidence": 0.99,
                "reason_codes": ["low_complexity"],
                "fallback_chain": ["economy", "balanced", "premium"],
            },
            confidence_threshold=0.75,
        )
        values = dict(
            run_id=run_id,
            route_bucket="normal",
            policy_version="shadow_tier_router_v1",
            router_schema_version="model_policy_v1",
            router_prompt_version="model_policy_prompt_v1",
            mode="shadow",
            decision=decision,
            selected_model="gpt-5.4-nano",
            applied_tier="premium",
            applied_model="gpt-5.6-sol",
            applied_reasoning_effort="low",
            assignment_key=opaque_episode_key("secret", "episode"),
            assignment_scope="single_turn",
            canary_eligible=True,
            eligibility_reason="eligible_exact_utility",
        )

        self.assertTrue(self.store.record_routing_decision(**values))
        self.assertFalse(self.store.record_routing_decision(**values))
        self.assertEqual(1, len(self.store.latest_routing_decisions(10)))

    def test_recovery_marks_pending_stage_abandoned(self) -> None:
        self.store.begin_stage(
            run_id="f" * 32,
            stage_kind="agent_turn",
            intended_model="gpt-5.6-sol",
            endpoint="agents",
        )
        old_started_at = format_datetime(utc_now() - timedelta(seconds=1))
        self.store._conn.execute(
            "UPDATE model_telemetry_stages SET started_at = ? WHERE run_id = ?",
            (old_started_at, "f" * 32),
        )
        self.store._conn.commit()

        recovered = self.store.recover_abandoned(stale_after_seconds=0)
        row = self.store.latest_stages(1)[0]

        self.assertEqual(1, recovered)
        self.assertEqual("abandoned", row.status)
        self.assertEqual("process_interrupted", row.failure_class)

    def test_recovery_does_not_abandon_a_fresh_stage_from_another_connection(self) -> None:
        self.store.begin_stage(
            run_id="0" * 32,
            stage_kind="agent_turn",
            intended_model="gpt-5.6-sol",
            endpoint="agents",
        )

        second_store = ModelTelemetryStore(self.db_path)
        try:
            self.assertEqual(0, second_store.recover_abandoned())
            summary = second_store.aggregate_since(3600)
        finally:
            second_store.close()

        self.assertEqual("pending", self.store.latest_stages(1)[0].status)
        self.assertEqual(1, summary["pending_count"])
        self.assertEqual(0, summary["stale_pending_count"])

    def test_aggregate_never_marks_empty_or_invalid_cost_as_complete(self) -> None:
        empty = self.store.aggregate_since(3600)
        self.assertFalse(empty["cost_complete"])
        self.assertEqual(0, empty["cost_incomplete_count"])

        handle = self.store.begin_stage(
            run_id="8" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        self.store.finish_stage(
            handle,
            status="succeeded",
            usage=response_usage(input_tokens=10, cached_tokens=20),
        )

        aggregate = self.store.aggregate_since(3600)
        self.assertFalse(aggregate["cost_complete"])
        self.assertEqual(1, aggregate["cost_incomplete_count"])

    def test_last_completed_at_uses_latest_completion_not_latest_start(self) -> None:
        earlier_started = self.store.begin_stage(
            run_id="7" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        later_started = self.store.begin_stage(
            run_id="6" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        self.store.finish_stage(later_started, status="succeeded", usage=response_usage())
        self.store.finish_stage(earlier_started, status="succeeded", usage=response_usage())
        rows = self.store.latest_stages(10)
        expected = max(row.completed_at for row in rows)

        self.assertEqual(expected, self.store.aggregate_since(3600)["last_completed_at"])

    def test_aggregate_window_uses_terminal_completion_time(self) -> None:
        handle = self.store.begin_stage(
            run_id="3" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        self.store.finish_stage(handle, status="succeeded", usage=response_usage())
        old_started_at = format_datetime(utc_now() - timedelta(days=2))
        self.store._conn.execute(
            "UPDATE model_telemetry_stages SET started_at = ? WHERE run_id = ?",
            (old_started_at, "3" * 32),
        )
        self.store._conn.commit()

        self.assertEqual(1, self.store.aggregate_since(3600)["stage_count"])

    def test_cleanup_removes_only_expired_terminal_runs(self) -> None:
        old = self.store.begin_stage(
            run_id="1" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        recent = self.store.begin_stage(
            run_id="2" * 32,
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )
        self.store.finish_stage(old, status="succeeded", usage=response_usage())
        self.store.finish_stage(recent, status="succeeded", usage=response_usage())
        expired = format_datetime(utc_now() - timedelta(days=60))
        self.store._conn.execute(
            "UPDATE model_telemetry_stages SET started_at = ?, completed_at = ? WHERE run_id = ?",
            (expired, expired, "1" * 32),
        )
        self.store._conn.commit()

        deleted = self.store.cleanup()
        rows = self.store.latest_stages(10)

        self.assertEqual(1, deleted)
        self.assertEqual(["2" * 32], [item.run_id for item in rows])

    def test_write_failure_is_best_effort(self) -> None:
        self.store.close()

        handle = self.store.begin_stage(
            stage_kind="plain",
            intended_model="gpt-5.6-sol",
            endpoint="responses",
        )

        self.assertIsNone(handle)
        self.assertEqual(1, self.store.write_failure_count)

    def test_schema_contains_no_payload_or_identity_columns(self) -> None:
        stage_columns = {
            str(row[1])
            for row in self.store._conn.execute("PRAGMA table_info(model_telemetry_stages)")
        }
        routing_columns = {
            str(row[1])
            for row in self.store._conn.execute("PRAGMA table_info(model_routing_decisions)")
        }
        forbidden = {
            "prompt",
            "output",
            "chat_id",
            "user_id",
            "username",
            "url",
            "path",
            "request_id",
            "exception_text",
        }

        self.assertTrue(forbidden.isdisjoint(stage_columns))
        self.assertTrue(forbidden.isdisjoint(routing_columns))
        self.assertEqual("ok", self.store._conn.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual([], self.store._conn.execute("PRAGMA foreign_key_check").fetchall())


if __name__ == "__main__":
    unittest.main()
