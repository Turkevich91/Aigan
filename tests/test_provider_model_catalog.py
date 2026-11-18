"""Provider discovery uses the authenticated SDK and a bounded shared cache."""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from chat_model_settings import MODEL_CATALOG
from provider_model_catalog import ProviderModelCatalog


class ProviderModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.calls = 0
        self.status = 200
        self.rows = []
        self.enterContext(patch("provider_model_catalog.monotonic", lambda: self.now))
        self.catalog = ProviderModelCatalog(lambda: AsyncOpenAI(
            api_key="test", base_url="https://example.invalid/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond)),
        ))

    async def respond(self, request):
        self.assertEqual("/v1/models", request.url.path)
        self.assertEqual("Bearer test", request.headers["authorization"])
        self.calls += 1
        await asyncio.sleep(0)
        return httpx.Response(self.status, json={"object": "list", "data": self.rows})

    def model(self, model_id, shutdown=None):
        return {"id": model_id, "created": 1, "owned_by": "system", "object": "model",
                "shutdown_date": shutdown}

    async def test_concurrent_controls_refresh_once_then_discover_next_version(self):
        self.rows = [self.model("gpt-6.2-sol"), self.model("gpt-6.3-astra", "2001-01-01"),
                     self.model("gpt-6.3-terra", "bad-date")]
        first, concurrent = await asyncio.gather(self.catalog.get(), self.catalog.get())
        self.assertIs(first, concurrent)
        self.assertEqual(1, self.calls)
        self.assertIn("gpt-6.2-sol", [m.model for m in first])
        self.assertNotIn("gpt-6.3-astra", [m.model for m in first])
        self.assertNotIn("gpt-6.3-terra", [m.model for m in first])
        self.rows = [self.model("gpt-6.3-sol", "2999-01-01")]
        self.now += 301
        second = await self.catalog.get()
        self.assertEqual(["gpt-6.3-sol", "spark-qwen"], [m.model for m in second])
        self.assertEqual(2, self.calls)

    async def test_failure_backoff_before_and_after_success(self):
        self.status = 503
        with self.assertLogs("provider_model_catalog", "WARNING"):
            self.assertEqual(MODEL_CATALOG, await self.catalog.get())
        self.now += 29
        await self.catalog.get()
        self.assertEqual(1, self.calls)
        self.now += 2
        self.status = 200
        self.rows = [self.model("gpt-6.2-astra")]
        previous = await self.catalog.get()
        self.assertNotEqual(MODEL_CATALOG, previous)
        self.now += 301
        self.status = 401
        with self.assertLogs("provider_model_catalog", "WARNING"):
            self.assertIs(previous, await self.catalog.get())
        self.assertEqual(3, self.calls)
        self.now += 31
        self.status = 200
        self.rows = []
        self.assertEqual(["spark-qwen"], [m.model for m in await self.catalog.get()])
