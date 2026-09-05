"""Shared history budget, cancellation, citations and SDK meaning-search contracts."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests.support import configure_test_environment
configure_test_environment()
from tests import test_history_retrieval as retrieval_fixtures
from tests.test_agent_capabilities import ScriptedModel
from agents import Agent, Runner, RunConfig
from agent_capabilities import PrimaryCapabilities
from chat_history import ChatHistorySession, HistoryLimits
import main


vector = retrieval_fixtures.vector


class HistorySemanticSessionTests(retrieval_fixtures.HistoryRetrievalTests):
    for _name in tuple(name for name in vars(retrieval_fixtures.HistoryRetrievalTests) if name.startswith("test_")):
        locals()[_name] = None

    def session(self, embedder=None, **kwargs):
        scope = self.scope()
        return ChatHistorySession(self.store, chat_id=scope.chat_id, cutoff_memory_id=scope.cutoff_memory_id,
            cutoff_created_at=scope.cutoff_created_at, query_embedder=embedder, **kwargs)

    def run_read(self, session, **kwargs):
        return json.loads(asyncio.run(session.aread(**kwargs)))

    def test_semantic_results_share_canonical_citations_without_internal_fields(self):
        expected = self.add("old semantic statement", embedding=vector(), forward_origin="Synthetic origin")
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        result = self.run_read(session, mode="semantic", query="meaning")
        row = result["messages"][0]
        self.assertEqual(expected, row["id"])
        self.assertTrue(row["is_forwarded"])
        self.assertIsNotNone(session.resolve_citation_ref(row["citation_ref"]))
        self.assertEqual({expected}, session.exposed_ids)
        for internal in ("sort_time", "evidence_digest", "embedding_blob", "relevance_order"):
            self.assertNotIn(internal, json.dumps(result))
        self.assertEqual(1, embedder.await_count)

    def test_budget_discards_weakest_before_chronological_publication(self):
        strongest = self.add("older relevant " * 100, embedding=vector())
        weakest = self.add("newest irrelevant " * 100, embedding=vector(0, 1))
        session = self.session(AsyncMock(return_value=vector()), limits=HistoryLimits(max_response_chars=2600))
        result = self.run_read(session, mode="semantic", query="meaning", limit=20)
        self.assertEqual([strongest], [row["id"] for row in result["messages"]])
        self.assertNotIn(weakest, session.exposed_ids)
        self.assertGreater(result["coverage"]["omitted_due_to_response_budget"], 0)
        self.assertIsNone(result["next_cursor"])

    def test_all_modes_share_four_reads_and_actual_serialized_character_total(self):
        target = self.add(embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        async def run():
            return [await session.aread(), await session.aread(mode="semantic", query="meaning"),
                    await session.aread(mode="around", anchor_id=target),
                    await session.aread(mode="hybrid", query="blue lantern"), await session.aread()]
        results = asyncio.run(run())
        self.assertEqual(4, session.calls_used)
        self.assertEqual(2, embedder.await_count)
        self.assertEqual("budget_exhausted", json.loads(results[-1])["status"])
        self.assertEqual(sum(map(len, results)), session.chars_used)
        self.assertLessEqual(session.chars_used, 30000)

    def test_query_reuse_uses_cached_vector_but_fresh_records(self):
        target = self.add(embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        first = self.run_read(session, mode="semantic", query="meaning")
        self.store._conn.execute("DELETE FROM messages WHERE id=?", (target,))
        self.store._conn.commit()
        second = self.run_read(session, mode="semantic", query="meaning")
        self.assertTrue(first["messages"])
        self.assertEqual([], second["messages"])
        self.assertEqual(1, embedder.await_count)

    def test_identical_query_reuses_completed_embedding(self):
        self.add(embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        for _ in range(2):
            self.run_read(session, mode="hybrid", query="blue lantern")
        self.assertEqual(1, embedder.await_count)

    def test_cached_vector_skips_admission_but_rechecks_stale_source_index(self):
        target = self.add(embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        first = self.run_read(session, mode="semantic", query="meaning")
        self.assertTrue(first["coverage"]["embeddings_used"])
        self.change_embedding(target, content_hash="changed-after-first-read")
        with patch("chat_history.history_query_embedding_available") as admission, \
             patch.object(self.store, "read_history_retrieval_candidates", wraps=self.store.read_history_retrieval_candidates) as read:
            result = self.run_read(session, mode="semantic", query="meaning")
        admission.assert_not_called()
        self.assertEqual(1, read.call_count)
        self.assertEqual(1, embedder.await_count)
        self.assertEqual([], result["messages"])
        self.assertFalse(result["coverage"]["embeddings_used"])
        self.assertEqual("no_usable_index", result["coverage"]["fallback_reason"])

    def test_unavailable_provider_skips_admission_and_still_reads_literal_sources(self):
        target = self.add(embedding=vector())
        session = self.session()
        with patch("chat_history.history_query_embedding_available") as admission, \
             patch.object(self.store, "read_history_retrieval_candidates", wraps=self.store.read_history_retrieval_candidates) as read:
            result = self.run_read(session, mode="hybrid", query="blue lantern")
        admission.assert_not_called()
        self.assertEqual(1, read.call_count)
        self.assertEqual([target], [row["id"] for row in result["messages"]])
        self.assertEqual("query_embedding_unavailable", result["coverage"]["fallback_reason"])
        self.assertEqual(0, session.embedding_calls_used)

    def test_exhausted_provider_skips_admission_and_reports_actual_budget(self):
        target = self.add("blue lantern one two", embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        for query in ("one", "two"):
            self.run_read(session, mode="semantic", query=query)
        with patch("chat_history.history_query_embedding_available") as admission, \
             patch.object(self.store, "read_history_retrieval_candidates", wraps=self.store.read_history_retrieval_candidates) as read:
            result = self.run_read(session, mode="hybrid", query="blue lantern")
        admission.assert_not_called()
        self.assertEqual(1, read.call_count)
        self.assertEqual(2, embedder.await_count)
        self.assertEqual([target], [row["id"] for row in result["messages"]])
        self.assertEqual("embedding_budget_exhausted", result["coverage"]["fallback_reason"])
        self.assertEqual(2, result["coverage"]["embedding_calls_used"])

    def test_exhausted_provider_reports_fresh_missing_index_before_budget(self):
        for invalidation in ("stale", "deleted"):
            with self.subTest(invalidation=invalidation):
                target = self.add("blue lantern one two", embedding=vector())
                embedder = AsyncMock(return_value=vector())
                session = self.session(embedder)
                for query in ("one", "two"):
                    self.run_read(session, mode="semantic", query=query)
                if invalidation == "stale":
                    self.change_embedding(target, content_hash="changed-after-budget-used")
                else:
                    self.store._conn.execute("DELETE FROM message_embeddings")
                    self.store._conn.commit()
                with patch("chat_history.history_query_embedding_available") as admission, \
                     patch.object(self.store, "read_history_retrieval_candidates", wraps=self.store.read_history_retrieval_candidates) as read:
                    result = self.run_read(session, mode="hybrid", query="blue lantern")
                admission.assert_not_called()
                self.assertEqual(1, read.call_count)
                self.assertEqual(2, embedder.await_count)
                self.assertIn(target, [row["id"] for row in result["messages"]])
                self.assertEqual("no_usable_index", result["coverage"]["fallback_reason"])
                self.assertEqual(2, result["coverage"]["embedding_calls_used"])

    def test_exhausted_provider_reports_fresh_scope_refusal_before_budget(self):
        self.add("blue lantern one two", embedding=vector())
        self.add("second retained statement", embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        for query in ("one", "two"):
            self.run_read(session, mode="semantic", query=query)
        with patch("chat_history.history_query_embedding_available") as admission, \
             patch("history_retrieval.HISTORY_SCAN_LIMIT", 1):
            result = self.run_read(session, mode="hybrid", query="blue lantern")
        admission.assert_not_called()
        self.assertEqual(2, embedder.await_count)
        self.assertEqual([], result["messages"])
        self.assertEqual("scope_too_large", result["status"])
        self.assertEqual("scope_too_large", result["coverage"]["fallback_reason"])
        self.assertEqual(2, result["coverage"]["embedding_calls_used"])

    def test_parallel_queries_respect_provider_and_shared_reservations(self):
        self.add("blue lantern one two three", embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        async def run():
            return await asyncio.gather(*(session.aread(mode="hybrid", query=query)
                                          for query in ("one", "two", "three")))
        results = asyncio.run(run())
        self.assertEqual(2, embedder.await_count)
        self.assertEqual(2, session.embedding_calls_used)
        self.assertEqual(3, session.calls_used)
        self.assertEqual(sum(map(len, results)), session.chars_used)
        self.assertTrue(any(json.loads(text).get("coverage", {}).get("fallback_reason") == "embedding_budget_exhausted"
                            for text in results))

    def test_timeout_and_failure_preserve_literal_evidence_and_bounded_reason(self):
        expected = self.add(embedding=vector())
        async def slow(_query):
            await asyncio.sleep(1)
        for embedder, reason in ((slow, "query_embedding_timeout"),
                                  (AsyncMock(side_effect=RuntimeError("PRIVATE_PROVIDER_DETAIL")), "query_embedding_failed")):
            session = self.session(embedder, embedding_timeout_seconds=.01)
            result = self.run_read(session, mode="hybrid", query="blue lantern")
            self.assertEqual(expected, result["messages"][-1]["id"])
            self.assertEqual(reason, result["coverage"]["fallback_reason"])
            self.assertFalse(result["coverage"]["embeddings_used"])
            self.assertNotIn("PRIVATE_PROVIDER_DETAIL", json.dumps(result))

    def test_empty_oversized_or_invalid_scope_never_calls_embedder(self):
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        self.run_read(session, mode="semantic", query="absent")
        self.assertEqual(0, embedder.await_count)
        for kwargs in ({"participant_id": True}, {"after": "bad date"}, {"query": "x" * 257}, {"cursor": "made-up"}):
            request = dict(mode="semantic", query="blue")
            request.update(kwargs)
            self.assertEqual("invalid_filter", self.run_read(self.session(embedder), **request)["status"])
        self.assertEqual(0, embedder.await_count)
        self.add(embedding=vector())
        session = self.session(embedder)
        with patch("history_retrieval.HISTORY_SCAN_LIMIT", 0):
            # A scoped cap refusal must remain a refusal through the shared publisher.
            result = self.run_read(session, mode="hybrid", query="blue")
        self.assertEqual("scope_too_large", result["status"])
        self.assertEqual([], result["messages"])
        self.assertEqual(0, embedder.await_count)

    def test_cancellation_during_embedding_never_publishes_evidence(self):
        self.add(embedding=vector())
        async def run():
            entered = asyncio.Event()
            async def pending(_query):
                entered.set()
                await asyncio.Event().wait()
            session = self.session(pending)
            task = asyncio.create_task(session.aread(mode="semantic", query="meaning"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(frozenset(), session.exposed_ids)
            self.assertEqual(0, session.chars_used)
            self.assertEqual(0, session._reserved_chars)
            self.assertEqual(1, session.embedding_calls_used)
        asyncio.run(run())

    def test_sdk_model_can_refine_search_with_same_tool_and_session(self):
        self.add("submarine exploration", embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        capabilities = PrimaryCapabilities(history=session)
        model = ScriptedModel([("read_chat_history", {"mode": "search", "query": "misspelled"}),
                               ("read_chat_history", {"mode": "hybrid", "query": "underwater survival"}),
                               "Evidence found"])
        result = asyncio.run(Runner.run(Agent(name="Synthetic", model=model, tools=capabilities.tools()),
            "Recall the game", run_config=RunConfig(tracing_disabled=True)))
        self.assertEqual("Evidence found", result.final_output)
        self.assertEqual(2, session.calls_used)
        self.assertEqual(1, embedder.await_count)
        self.assertTrue(session.exposed_ids)

    def test_ordinary_sdk_response_has_no_history_or_embedding_calls(self):
        self.add(embedding=vector())
        embedder = AsyncMock(return_value=vector())
        session = self.session(embedder)
        model = ScriptedModel(["Hello"])
        asyncio.run(Runner.run(Agent(name="Synthetic", model=model,
            tools=PrimaryCapabilities(history=session).tools()), "Hello", run_config=RunConfig(tracing_disabled=True)))
        self.assertEqual(0, session.calls_used)
        self.assertEqual(0, embedder.await_count)

    def test_host_adapter_uses_current_512_configuration_and_bounded_transport(self):
        config = replace(main.CONFIG, memory_vector_enabled=True, memory_embedding_model="text-embedding-3-small",
                         memory_embedding_dimensions=512, openai_api_key="synthetic-not-a-key")
        response = SimpleNamespace(data=[SimpleNamespace(embedding=vector())], model="text-embedding-3-small",
                                   usage=SimpleNamespace(prompt_tokens=2, total_tokens=2))
        client = MagicMock()
        client.__enter__.return_value = client
        client.embeddings.create.return_value = response
        with patch.object(main, "CONFIG", config), patch.object(main, "MEMORY", self.store), \
             patch.object(main, "OpenAI", return_value=client) as constructor, \
             patch.object(main, "begin_model_stage", return_value=None), patch.object(main, "finish_model_stage"):
            actual = asyncio.run(main.create_history_query_embedding("meaning"))
        self.assertEqual(vector(), actual)
        constructor.assert_called_once_with(timeout=8., max_retries=0)
        self.assertEqual(512, client.embeddings.create.call_args.kwargs["dimensions"])
        self.assertEqual(["meaning"], client.embeddings.create.call_args.kwargs["input"])

    def test_host_adapter_does_not_use_incompatible_dimension_or_model(self):
        with patch.object(main, "MEMORY", self.store), patch.object(main, "create_embeddings", new_callable=AsyncMock) as provider:
            for overrides in ({"memory_embedding_dimensions": 1536}, {"memory_embedding_model": "different"},
                              {"memory_vector_enabled": False}):
                with patch.object(main, "CONFIG", replace(main.CONFIG, **overrides)):
                    self.assertEqual([], asyncio.run(main.create_history_query_embedding("meaning")))
            provider.assert_not_called()
