"""Discover available answer models when a Telegram settings control needs them."""
import asyncio
from collections.abc import Callable
from datetime import date, datetime, timezone
import logging
from time import monotonic

from openai import APIError, AsyncOpenAI

from chat_model_settings import MINI_VISION_MODEL, MODEL_CATALOG, TRANSCRIPTION_MODELS, ModelCapability, catalog_from_provider_ids


class ProviderModelCatalog:
    def __init__(self, client_factory: Callable[[], AsyncOpenAI]) -> None:
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._snapshot = MODEL_CATALOG
        self._ids = frozenset([MINI_VISION_MODEL, *TRANSCRIPTION_MODELS, *(cap.model for cap in MODEL_CATALOG)])
        self._refresh_at = 0.0

    async def get(self) -> tuple[ModelCapability, ...]:
        async with self._lock:
            if monotonic() < self._refresh_at:
                return self._snapshot
            try:
                async with asyncio.timeout(4), self._client_factory() as client:
                    today = datetime.now(timezone.utc).date()
                    ids = []
                    async for model in client.models.list():
                        # The API optionally supplies a UTC shutdown date as YYYY-MM-DD.
                        shutdown = getattr(model, "shutdown_date", None)
                        if shutdown is not None:
                            try:
                                if date.fromisoformat(shutdown) <= today:
                                    continue
                            except (TypeError, ValueError):
                                continue
                        ids.append(model.id)
                    snapshot = catalog_from_provider_ids(ids)
            except (APIError, OSError, ValueError, TypeError, AttributeError) as exc:
                logging.getLogger(__name__).warning("Model discovery unavailable: %s", type(exc).__name__)
                self._refresh_at = monotonic() + 30
            else:
                self._snapshot = snapshot
                self._ids = frozenset(ids)
                self._refresh_at = monotonic() + 300
            return self._snapshot

    async def media_models(self) -> tuple[str, ...]:
        catalog = await self.get()
        latest = {}
        for capability in catalog:
            if capability.supports_images:
                latest.setdefault(capability.aliases[0], capability.model)
        return tuple(model for model in (*TRANSCRIPTION_MODELS, MINI_VISION_MODEL, *reversed(latest.values()))
                     if model in self._ids)
