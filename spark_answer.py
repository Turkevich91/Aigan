"""Explicit local Chat Completions client for the Spark text-answer preset."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ChatCompletion


@dataclass
class SparkAnswerProvider:
    base_url: str = field(repr=False)
    model: str = field(repr=False)
    api_key: str = field(repr=False)
    _client: AsyncOpenAI | None = field(default=None, init=False, repr=False)

    @classmethod
    def from_environment(cls) -> SparkAnswerProvider | None:
        base_url = os.getenv("SPARK_BASE_URL", "").strip()
        model = os.getenv("SPARK_ANSWER_MODEL", "").strip()
        try:
            parts = urlsplit(base_url)
            if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                    or parts.password or parts.query or parts.fragment or not model):
                return None
        except ValueError:
            return None
        # An unauthenticated local endpoint still needs a nonempty SDK key.
        # Always supply it explicitly to avoid inheriting the cloud credential.
        api_key = os.getenv("SPARK_API_KEY", "").strip() or "local"
        return cls(base_url=base_url, model=model, api_key=api_key)

    def agents_model(self) -> OpenAIChatCompletionsModel:
        if self._client is None:
            self._client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key,
                                       timeout=90, max_retries=0, organization="", project="")
        return OpenAIChatCompletionsModel(self.model, self._client)

    def plain_completion(self, prompt: str, max_tokens: int) -> ChatCompletion:
        with OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=90,
                    max_retries=0, organization="", project="") as client:
            return client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.close()
