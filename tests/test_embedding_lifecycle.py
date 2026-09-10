from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
from telegram import User

from tests.support import configure_test_environment

configure_test_environment()
import main
from memory import MemoryStore


class EmbeddingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3", retention_days=30)
        self.enterContext(patch.multiple(
            main,
            CONFIG=replace(
                main.CONFIG, memory_vector_enabled=True,
                memory_embedding_model="text-embedding-3-small",
                memory_embedding_dimensions=2, memory_embedding_batch_size=2,
                memory_vector_backfill_on_start=False,
                openai_api_key="unit-test-key",
            ),
            MEMORY=self.store, embedding_queue=None, embedding_worker_task=None,
            embedding_backfill_task=None,
        ))
        self.events = self.enterContext(patch.object(main, "system_event"))
        self.logs = self.enterContext(patch.object(main, "LOGGER"))
        self.enterContext(patch.object(main, "begin_model_stage", return_value=object()))
        self.finish = self.enterContext(patch.object(main, "finish_model_stage"))

    async def asyncTearDown(self):
        await main.stop_memory_embedding_tasks()
        self.store.close()
        self.tmp.cleanup()

    def save(self, number):
        return self.store.save_message(
            chat_id=1, message_id=number, sender_label="Synthetic user",
            text=f"Synthetic retained message {number}",
        )

    def application(self):
        application = main.Application.builder().application_class(
            main.AiganApplication,
        ).token("123:synthetic").updater(None).job_queue(None).build()
        application.bot._bot_user = User(123, "Synthetic", is_bot=True)
        application._initialized = True
        return application

    async def test_real_application_start_stop_cancels_idle_worker_before_loop_closes(self):
        application = self.application()
        await main.start_memory_embedding_tasks()
        worker, queue = main.embedding_worker_task, main.embedding_queue
        await application.start()
        await asyncio.sleep(0)
        await asyncio.wait_for(application.stop(), 0.5)
        await application.shutdown()
        self.assertTrue(worker.cancelled())
        self.assertFalse(application.running)
        self.assertIsNone(main.embedding_worker_task)
        self.assertIsNone(main.embedding_queue)
        await asyncio.wait_for(queue.join(), 0.1)
        self.events.assert_not_called()

    async def test_stop_before_worker_starts_discards_queue_without_provider_work(self):
        with patch.object(main, "create_embeddings", new_callable=AsyncMock) as provider:
            await main.start_memory_embedding_tasks()
            queue = main.embedding_queue
            main.enqueue_memory_embedding(self.save(1))
            main.enqueue_memory_embedding(self.save(2))
            await main.stop_memory_embedding_tasks()
            await asyncio.wait_for(queue.join(), 0.1)
            provider.assert_not_awaited()
        self.assertEqual(2, self.store.embedding_backlog_count(
            model=main.CONFIG.memory_embedding_model, dimensions=2, lookback_days=30,
        ))

    async def test_stop_cancels_inflight_batch_acks_taken_and_unstarted_ids(self):
        started = asyncio.Event()
        cancelled = []

        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        with patch.object(main, "create_embeddings", new=AsyncMock(side_effect=blocked)) as provider:
            await main.start_memory_embedding_tasks()
            queue = main.embedding_queue
            for number in range(1, 6):
                main.enqueue_memory_embedding(self.save(number))
            await asyncio.wait_for(started.wait(), 0.5)
            await asyncio.wait_for(main.stop_memory_embedding_tasks(), 0.5)
            main.enqueue_memory_embedding(self.save(6))
            await asyncio.sleep(0)
            await asyncio.wait_for(queue.join(), 0.1)
            self.assertEqual(1, provider.await_count)
        self.assertEqual([True], cancelled)
        self.assertEqual(6, self.store.embedding_backlog_count(
            model=main.CONFIG.memory_embedding_model, dimensions=2, lookback_days=30,
        ))

    async def test_partial_startup_shutdown_cancels_backfill_and_worker(self):
        main.CONFIG = replace(main.CONFIG, memory_vector_backfill_on_start=True)
        started = asyncio.Event()
        finalized = []

        async def backfill():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finalized.append(True)

        application = self.application()
        with patch.object(main, "memory_vector_backfill_loop", new=backfill):
            await main.start_memory_embedding_tasks()
            worker, backfill_task = main.embedding_worker_task, main.embedding_backfill_task
            await asyncio.wait_for(started.wait(), 0.5)
            # A later post_init failure means PTB never calls application.stop().
            await asyncio.wait_for(application.shutdown(), 0.5)
        self.assertTrue(worker.cancelled())
        self.assertTrue(backfill_task.cancelled())
        self.assertEqual([True], finalized)
        self.assertIsNone(main.embedding_backfill_task)

    async def test_repeated_start_stop_replaces_queue_and_leaves_no_owned_tasks(self):
        for _ in range(3):
            await main.start_memory_embedding_tasks()
            worker, queue = main.embedding_worker_task, main.embedding_queue
            await asyncio.sleep(0)
            await main.start_memory_embedding_tasks()
            self.assertTrue(worker.cancelled())
            self.assertIsNot(queue, main.embedding_queue)
            await main.stop_memory_embedding_tasks()
            await main.stop_memory_embedding_tasks()
            self.assertIsNone(main.embedding_worker_task)
        self.assertFalse([
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and task.get_name().startswith("memory-embedding-")
        ])

    async def test_terminal_queue_failure_exits_once_instead_of_retrying(self):
        queue = asyncio.Queue()
        # Exercise the actual asyncio queue's wrong-loop error, not a worker mock.
        foreign_loop = asyncio.new_event_loop()
        queue._loop = foreign_loop
        main.embedding_queue = queue
        try:
            await asyncio.wait_for(main.memory_embedding_worker(), 0.1)
        finally:
            foreign_loop.close()
        self.events.assert_called_once()
        self.assertEqual("queue_unavailable", self.events.call_args.kwargs["message"])
        self.logs.error.assert_called_once()

    async def test_recoverable_batch_failure_yields_and_next_batch_is_processed(self):
        main.CONFIG = replace(main.CONFIG, memory_embedding_batch_size=1)
        ids = [self.save(1), self.save(2)]
        real_candidates = self.store.embedding_candidates_by_ids
        processed = asyncio.Event()
        original_sleep = asyncio.sleep
        delays = []

        async def sleep(delay):
            delays.append(delay)
            await original_sleep(0)

        async def provider(*args, **kwargs):
            processed.set()
            return [[1.0, 0.0]]

        with patch.object(self.store, "embedding_candidates_by_ids", side_effect=[
            RuntimeError("synthetic temporary store failure"), real_candidates(
                [ids[1]], model=main.CONFIG.memory_embedding_model, dimensions=2, limit=1,
            ),
        ]), patch.object(main, "create_embeddings", new=AsyncMock(side_effect=provider)), patch.object(
            main.asyncio, "sleep", new=sleep,
        ):
            await main.start_memory_embedding_tasks()
            queue = main.embedding_queue
            for item_id in ids:
                main.enqueue_memory_embedding(item_id)
            await asyncio.wait_for(processed.wait(), 0.5)
            await asyncio.wait_for(queue.join(), 0.1)
            await main.stop_memory_embedding_tasks()
        self.assertIn(1.0, delays)
        self.assertEqual(1, self.store.embedding_backlog_count(
            model=main.CONFIG.memory_embedding_model, dimensions=2, lookback_days=30,
        ))
        self.events.assert_any_call(
            level="error", component="memory_vector", event_type="worker_failed",
            details={"error_type": "RuntimeError"},
        )

    async def test_index_sdk_cancellation_closes_transport_without_thread_or_retry(self):
        started = asyncio.Event()

        class BlockingTransport(httpx.AsyncBaseTransport):
            calls = 0
            cancelled = False
            closed = False

            async def handle_async_request(self, request):
                self.calls += 1
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

            async def aclose(self):
                self.closed = True

        transport = BlockingTransport()
        client = AsyncOpenAI(api_key="synthetic", http_client=httpx.AsyncClient(transport=transport))
        with patch.object(main, "AsyncOpenAI", return_value=client), patch.object(
            main, "create_embeddings_sync",
        ) as sync_provider:
            await main.start_memory_embedding_tasks()
            queue = main.embedding_queue
            main.enqueue_memory_embedding(self.save(1))
            await asyncio.wait_for(started.wait(), 0.5)
            await asyncio.wait_for(main.stop_memory_embedding_tasks(), 0.5)
            await asyncio.wait_for(queue.join(), 0.1)
            sync_provider.assert_not_called()
        self.assertTrue(transport.cancelled)
        self.assertTrue(transport.closed)
        self.assertTrue(client.is_closed())
        self.assertEqual(1, transport.calls)
        self.assertEqual("cancelled", self.finish.call_args.kwargs["status"])

    async def test_index_sdk_request_normalization_and_recoverable_retry_are_preserved(self):
        requests = []

        async def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(429, headers={"retry-after-ms": "1"}, json={
                    "error": {"message": "synthetic retry", "type": "rate_limit_error"},
                })
            return httpx.Response(200, json={
                "object": "list", "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": 0, "embedding": [3.0, 4.0]}],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            })

        client = AsyncOpenAI(api_key="synthetic", http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
        ))
        with patch.object(main, "AsyncOpenAI", return_value=client):
            result = await main.create_embeddings(
                ["synthetic text"], stage_kind="embedding_index", route_bucket="memory_index",
                task_class_bucket="embedding_index",
            )
        self.assertEqual([[0.6, 0.8]], result)
        self.assertEqual(2, len(requests))
        self.assertEqual(requests[0], requests[1])
        self.assertEqual({
            "input": ["synthetic text"], "model": "text-embedding-3-small",
            "encoding_format": "float", "dimensions": 2,
        }, requests[0])
        self.assertEqual(2, client.max_retries)
        self.assertEqual("succeeded", self.finish.call_args.kwargs["status"])
        self.assertTrue(client.is_closed())

    async def test_explicit_index_timeout_keeps_no_retry_bound(self):
        with patch.object(main, "AsyncOpenAI") as client_factory:
            client = client_factory.return_value.__aenter__.return_value
            client.embeddings.create.return_value.data = []
            await main.create_embeddings(
                ["synthetic text"], stage_kind="embedding_index", timeout_seconds=8.0,
            )
        client_factory.assert_called_once_with(timeout=8.0, max_retries=0)

    async def test_interactive_query_keeps_existing_thread_transport_and_bound(self):
        with patch.object(main, "create_embeddings_sync", return_value=[[1.0, 0.0]]) as sync_provider, patch.object(
            main, "AsyncOpenAI",
        ) as async_provider:
            result = await main.create_embeddings(
                ["synthetic query"], stage_kind="embedding_query", route_bucket="memory_recall",
                timeout_seconds=8.0,
            )
        self.assertEqual([[1.0, 0.0]], result)
        sync_provider.assert_called_once_with(
            ["synthetic query"], "embedding_query", route_bucket="memory_recall",
            task_class_bucket="", timeout_seconds=8.0,
        )
        async_provider.assert_not_called()


class EmbeddingPollingShutdownSmokeTests(unittest.TestCase):
    def test_real_ptb_sigterm_closes_loop_with_no_worker_or_log_burst(self):
        script = textwrap.dedent("""
            import asyncio
            import os
            import signal
            from unittest.mock import AsyncMock, patch
            from telegram import User
            from telegram.ext import ExtBot, Updater
            from tests.support import configure_test_environment
            configure_test_environment()
            import main

            async def start(application):
                await main.start_memory_embedding_tasks()
                asyncio.get_running_loop().call_later(
                    0.02, os.kill, os.getpid(), signal.SIGTERM,
                )

            application = main.Application.builder().application_class(
                main.AiganApplication,
            ).token("123:synthetic").job_queue(None).post_init(start).build()
            application.bot._bot_user = User(123, "Synthetic", is_bot=True)
            with patch.object(ExtBot, "initialize", new_callable=AsyncMock), patch.object(
                Updater, "start_polling", new_callable=AsyncMock,
            ), patch.object(main, "create_embeddings", new_callable=AsyncMock) as provider:
                application.run_polling()
                provider.assert_not_awaited()
            assert main.embedding_worker_task is None
            assert main.embedding_backfill_task is None
            assert main.embedding_queue is None
            print("clean_shutdown")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("clean_shutdown", result.stdout.strip())
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
