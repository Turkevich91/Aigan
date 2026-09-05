"""Cross-feature regression for semantic search, reply branches and citations."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from tests.support import configure_test_environment
configure_test_environment()
from tests import test_history_retrieval as fixtures
from tests.test_agent_capabilities import ScriptedModel
from agents import Agent, Runner, RunConfig
from agent_capabilities import PrimaryCapabilities
from chat_history import ChatHistorySession
from history_citations import HistoryCitationSession


class HistoryCombinedTests(unittest.TestCase):
    setUp = fixtures.HistoryRetrievalTests.setUp
    tearDown = fixtures.HistoryRetrievalTests.tearDown
    add = fixtures.HistoryRetrievalTests.add
    change_embedding = fixtures.HistoryRetrievalTests.change_embedding
    scope = fixtures.HistoryRetrievalTests.scope

    def seed(self):
        anchor = self.add("The old plan was to walk north", embedding=fixtures.vector())
        child = self.add("Correction: the bridge is closed, walk south", reply_to_message_id=1)
        scope = self.scope()
        embedder = AsyncMock(return_value=fixtures.vector())
        history = ChatHistorySession(self.store, chat_id=scope.chat_id,
            cutoff_memory_id=scope.cutoff_memory_id, cutoff_created_at=scope.cutoff_created_at,
            query_embedder=embedder)
        citations = HistoryCitationSession(self.store, chat_id=scope.chat_id, chat_type="private",
            cutoff_memory_id=scope.cutoff_memory_id, cutoff_created_at=scope.cutoff_created_at,
            history=history)
        return anchor, child, history, citations, embedder

    def test_sdk_semantic_anchor_to_branch_to_validated_final_source(self):
        anchor, child, history, citations, embedder = self.seed()

        class CombinedModel(ScriptedModel):
            async def get_response(self, *args, **kwargs):
                if self.steps[0] == "FINAL_SOURCE":
                    evidence = next(row for row in history.exposed_items if row.id == child)
                    self.steps[0] = "Пізніше напрямок змінили. " + evidence.citation_ref
                return await super().get_response(*args, **kwargs)

        capabilities = PrimaryCapabilities(history=history, citations=citations)
        model = CombinedModel([
            ("read_chat_history", {"mode": "semantic", "query": "earlier route", "limit": 1}),
            ("read_conversation_branch", {"anchor_id": anchor}),
            "FINAL_SOURCE",
        ])
        agent = Agent(name="Synthetic history integration", instructions=capabilities.guidance(),
            model=model, tools=capabilities.tools())
        result = asyncio.run(Runner.run(agent, "What changed about the earlier route?", max_turns=4,
            run_config=RunConfig(tracing_disabled=True)))
        rendered = citations.render(result.final_output)
        self.assertIn("Джерела:", rendered)
        self.assertNotIn("[[history:", rendered)
        self.assertIn("пряме посилання недоступне", rendered)
        self.assertEqual({anchor, child}, history.exposed_ids)
        self.assertEqual(2, history.calls_used)
        self.assertEqual(1, embedder.await_count)
        self.assertEqual({str(anchor), str(child)}, set(history.last_coverage["branch"]["relations"]))

    def test_mixed_modes_share_one_budget_and_keep_each_coverage_kind(self):
        anchor, child, history, _, embedder = self.seed()

        async def run():
            outputs = [await history.aread(mode="semantic", query="earlier route", limit=1)]
            outputs.append(await history.aread_branch(anchor_id=anchor))
            outputs.append(await history.aread(mode="hybrid", query="bridge"))
            outputs.append(await history.aread(mode="around", anchor_id=child))
            outputs.append(await history.aread_branch(anchor_id=anchor))
            return outputs

        outputs = asyncio.run(run())
        packets = list(map(json.loads, outputs))
        self.assertTrue(packets[0]["coverage"]["embeddings_used"])
        self.assertIn("branch", packets[1]["coverage"])
        self.assertNotIn("branch", packets[2]["coverage"])
        self.assertEqual("budget_exhausted", packets[4]["status"])
        self.assertEqual(4, history.calls_used)
        self.assertEqual(2, embedder.await_count)
        self.assertEqual(sum(map(len, outputs)), history.chars_used)
        self.assertLessEqual(history.chars_used, 30000)
