import asyncio
import base64
import unittest
from types import SimpleNamespace

from heavy_model_backend import (
    HeavyMediaInput,
    HeavyModelRequest,
    HeavyModelSettings,
    NullHeavyModelAdapter,
    OpenAICompatibleHeavyModelAdapter,
)
from tool_diagnostics import adapter_row


class FakeCompletions:
    def __init__(self, *, response=None, error=None, started=None, release=None):
        self.response = response
        self.error = error
        self.started = started
        self.release = release
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self.response


class FakeModels:
    def __init__(self, model_ids=(), *, error=None):
        self.model_ids = tuple(model_ids)
        self.error = error
        self.calls = 0

    async def list(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=[SimpleNamespace(id=model_id) for model_id in self.model_ids])


class FakeClient:
    def __init__(self, *, completions=None, model_ids=("test-model",), model_error=None):
        self.completions = completions or FakeCompletions(response=success_response())
        self.chat = SimpleNamespace(completions=self.completions)
        self.models = FakeModels(model_ids, error=model_error)
        self.closed = False

    async def close(self):
        self.closed = True


class FakeProviderError(Exception):
    def __init__(self, status_code):
        super().__init__("private provider detail must not escape")
        self.status_code = status_code


def success_response(text="summary", *, input_tokens=10, output_tokens=4):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def data_url(mime_type, payload):
    return f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"


def settings(**overrides):
    values = {
        "base_url": "https://heavy.invalid/v1",
        "model": "test-model",
        "api_key": "private-test-key",
        "capabilities": ("text", "image", "video"),
        "max_inline_media_bytes": 100,
        "max_media_bytes": 200,
        "max_result_chars": 100,
        "max_concurrency": 1,
    }
    values.update(overrides)
    return HeavyModelSettings(**values)


class HeavyModelBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_adapter_never_uses_a_provider(self):
        adapter = NullHeavyModelAdapter()

        result = await adapter.analyze(HeavyModelRequest(prompt="secret prompt"))
        probe = await adapter.probe()

        self.assertFalse(result.ok)
        self.assertEqual("disabled", result.failure_category)
        self.assertNotIn("secret prompt", repr(result))
        self.assertFalse(probe.ok)
        self.assertEqual("disabled", adapter.health_summary()["status"])

    async def test_null_adapter_distinguishes_requested_but_unconfigured_backend(self):
        adapter = NullHeavyModelAdapter(requested_enabled=True, failure_category="unconfigured")

        result = await adapter.analyze(HeavyModelRequest(prompt="secret prompt"))
        health = adapter.health_summary()

        self.assertEqual("unconfigured", result.failure_category)
        self.assertTrue(health["enabled"])
        self.assertFalse(health["configured"])
        self.assertFalse(health["available"])
        self.assertEqual("unconfigured", health["status"])

    async def test_fixed_model_multimodal_request_and_safe_public_result(self):
        completions = FakeCompletions(response=success_response("private generated summary"))
        client = FakeClient(completions=completions)
        adapter = OpenAICompatibleHeavyModelAdapter(
            settings=settings(extra_body={"chat_template_kwargs": {"enable_thinking": False}}),
            client_factory=lambda: client,
        )
        request = HeavyModelRequest(
            prompt="private prompt",
            task_class="video_context",
            media=(
                HeavyMediaInput("image", data_url("image/jpeg", b"image")),
                HeavyMediaInput("video", data_url("video/mp4", b"video")),
            ),
            max_output_tokens=80,
            temperature=0.1,
        )

        result = await adapter.analyze(request)

        self.assertTrue(result.ok)
        self.assertEqual("private generated summary", result.text)
        self.assertEqual(14, result.total_tokens)
        self.assertEqual(1, len(completions.calls))
        call = completions.calls[0]
        self.assertEqual("test-model", call["model"])
        self.assertEqual(80, call["max_tokens"])
        self.assertFalse(call["stream"])
        self.assertEqual(0.1, call["temperature"])
        content = call["messages"][0]["content"]
        self.assertEqual(["text", "image_url", "video_url"], [item["type"] for item in content])
        self.assertEqual({"enable_thinking": False}, call["extra_body"]["chat_template_kwargs"])
        public = result.public_dict()
        self.assertNotIn("text", public)
        self.assertNotIn("task_class", public)
        self.assertNotIn("private prompt", str(public))
        self.assertNotIn("private generated summary", str(public))

    async def test_endpoint_model_key_and_media_urls_are_absent_from_health_and_repr(self):
        configured = settings(extra_body={"private_extension": "private-value"})
        adapter = OpenAICompatibleHeavyModelAdapter(settings=configured, client_factory=FakeClient)

        rendered = repr(configured) + str(adapter.health_summary())

        self.assertNotIn("heavy.invalid", rendered)
        self.assertNotIn("test-model", rendered)
        self.assertNotIn("private-test-key", rendered)
        self.assertNotIn("media.invalid", rendered)
        self.assertNotIn("private-value", rendered)

        row = adapter_row(adapter.health_summary())
        self.assertEqual("unknown", row.status)
        self.assertTrue(row.details["model_configured"])
        self.assertTrue(row.details["video_supported"])
        self.assertEqual(1, row.details["max_concurrency"])

    async def test_remote_media_is_rejected_until_trusted_staging_exists(self):
        completions = FakeCompletions(response=success_response())
        client = FakeClient(completions=completions)
        adapter = OpenAICompatibleHeavyModelAdapter(
            settings=settings(),
            client_factory=lambda: client,
        )
        urls = (
            "https://media.invalid/signed/object?signature=opaque",
            "https://user@media.invalid/object",
            "relative-video.mp4",
            "http://[",
        )

        results = [
            await adapter.analyze(
                HeavyModelRequest(prompt="analyze", media=(HeavyMediaInput("video", url),))
            )
            for url in urls
        ]

        self.assertTrue(all(result.failure_category == "unsafe_media_url" for result in results))
        self.assertEqual([], completions.calls)

    async def test_validation_rejects_oversize_unsupported_and_invalid_inputs_before_client_creation(self):
        created = 0

        def factory():
            nonlocal created
            created += 1
            return FakeClient()

        adapter = OpenAICompatibleHeavyModelAdapter(
            settings=settings(max_inline_media_bytes=3, max_media_bytes=5),
            client_factory=factory,
        )

        oversize = await adapter.analyze(
            HeavyModelRequest(
                prompt="analyze",
                media=(HeavyMediaInput("image", data_url("image/jpeg", b"four")),),
            )
        )
        unsupported = await adapter.analyze(
            HeavyModelRequest(
                prompt="analyze",
                media=(HeavyMediaInput("audio", data_url("audio/wav", b"a")),),
            )
        )
        malformed = await adapter.analyze(
            HeavyModelRequest(prompt="analyze", media=(HeavyMediaInput("image", "not-a-url"),))
        )
        malformed_bracket = await adapter.analyze(
            HeavyModelRequest(prompt="analyze", media=(HeavyMediaInput("image", "http://["),))
        )
        oversized_header = await adapter.analyze(
            HeavyModelRequest(
                prompt="analyze",
                media=(
                    HeavyMediaInput(
                        "image",
                        "data:image/" + ("x" * 200) + ";base64,YQ==",
                    ),
                ),
            )
        )
        mime_mismatch = await adapter.analyze(
            HeavyModelRequest(
                prompt="analyze",
                media=(
                    HeavyMediaInput(
                        "image",
                        data_url("image/jpeg", b"a"),
                        mime_type="image/png",
                    ),
                ),
            )
        )
        unsupported_mime = await adapter.analyze(
            HeavyModelRequest(
                prompt="analyze",
                media=(
                    HeavyMediaInput(
                        "image",
                        data_url("image/svg+xml", b"<svg/>")
                    ),
                ),
            )
        )

        self.assertEqual("payload_too_large", oversize.failure_category)
        self.assertEqual("unsupported_modality", unsupported.failure_category)
        self.assertEqual("unsafe_media_url", malformed.failure_category)
        self.assertEqual("unsafe_media_url", malformed_bracket.failure_category)
        self.assertEqual("invalid_request", oversized_header.failure_category)
        self.assertEqual("invalid_request", mime_mismatch.failure_category)
        self.assertEqual("unsupported_modality", unsupported_mime.failure_category)
        self.assertEqual(0, created)

    async def test_concurrency_limit_is_fail_fast(self):
        started = asyncio.Event()
        release = asyncio.Event()
        completions = FakeCompletions(response=success_response(), started=started, release=release)
        client = FakeClient(completions=completions)
        adapter = OpenAICompatibleHeavyModelAdapter(settings=settings(), client_factory=lambda: client)

        first_task = asyncio.create_task(adapter.analyze(HeavyModelRequest(prompt="first")))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = await adapter.analyze(HeavyModelRequest(prompt="second"))
        release.set()
        first = await asyncio.wait_for(first_task, timeout=1)

        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual("busy", second.failure_category)
        self.assertEqual(1, len(completions.calls))

    async def test_concurrency_admission_is_atomic_under_burst(self):
        release = asyncio.Event()
        two_started = asyncio.Event()

        class BurstCompletions(FakeCompletions):
            async def create(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 2:
                    two_started.set()
                await release.wait()
                return self.response

        completions = BurstCompletions(response=success_response())
        client = FakeClient(completions=completions)
        adapter = OpenAICompatibleHeavyModelAdapter(
            settings=settings(max_concurrency=2),
            client_factory=lambda: client,
        )
        tasks = [
            asyncio.create_task(adapter.analyze(HeavyModelRequest(prompt=f"request-{index}")))
            for index in range(20)
        ]

        await asyncio.wait_for(two_started.wait(), timeout=1)
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(2, len(completions.calls))
        self.assertEqual(2, adapter.health_summary()["active"])
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

        self.assertEqual(2, sum(result.ok for result in results))
        self.assertEqual(18, sum(result.failure_category == "busy" for result in results))
        self.assertEqual(0, adapter.health_summary()["active"])

    async def test_provider_errors_are_categorized_without_raw_details(self):
        cases = (
            (FakeProviderError(401), "authentication_failed"),
            (FakeProviderError(429), "rate_limited"),
            (FakeProviderError(503), "unavailable"),
            (FakeProviderError(400), "provider_rejected"),
            (TimeoutError("private timeout detail"), "timeout"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                client = FakeClient(completions=FakeCompletions(error=error))
                adapter = OpenAICompatibleHeavyModelAdapter(
                    settings=settings(),
                    client_factory=lambda client=client: client,
                )

                result = await adapter.analyze(HeavyModelRequest(prompt="private prompt"))

                self.assertEqual(expected, result.failure_category)
                self.assertNotIn("private", str(result.public_dict()))
                self.assertEqual("degraded", adapter.health_summary()["status"])

    async def test_probe_lists_models_without_generating_content(self):
        present_client = FakeClient(model_ids=("other-model", "test-model"))
        present = OpenAICompatibleHeavyModelAdapter(
            settings=settings(),
            client_factory=lambda: present_client,
        )

        present_result = await present.probe()

        self.assertTrue(present_result.ok)
        self.assertTrue(present_result.model_present)
        self.assertEqual(2, present_result.discovered_model_count)
        self.assertEqual(1, present_client.models.calls)
        self.assertEqual([], present_client.completions.calls)
        self.assertEqual("ok", present.health_summary()["status"])

        missing_client = FakeClient(model_ids=("other-model",))
        missing = OpenAICompatibleHeavyModelAdapter(
            settings=settings(),
            client_factory=lambda: missing_client,
        )
        missing_result = await missing.probe()
        self.assertFalse(missing_result.ok)
        self.assertEqual("model_unavailable", missing_result.failure_category)
        self.assertNotIn("test-model", str(missing_result.public_dict()))
        self.assertEqual([], missing_client.completions.calls)

    async def test_malformed_and_long_responses_are_bounded(self):
        malformed = OpenAICompatibleHeavyModelAdapter(
            settings=settings(),
            client_factory=lambda: FakeClient(
                completions=FakeCompletions(response=SimpleNamespace(choices=[]))
            ),
        )
        malformed_result = await malformed.analyze(HeavyModelRequest(prompt="analyze"))
        self.assertEqual("malformed_response", malformed_result.failure_category)

        long_text = OpenAICompatibleHeavyModelAdapter(
            settings=settings(max_result_chars=5),
            client_factory=lambda: FakeClient(
                completions=FakeCompletions(response=success_response("123456789"))
            ),
        )
        long_result = await long_text.analyze(HeavyModelRequest(prompt="analyze"))
        self.assertTrue(long_result.ok)
        self.assertEqual("12345", long_result.text)
        self.assertTrue(long_result.truncated)
        self.assertEqual(5, long_result.output_chars)

    async def test_cleanup_closes_lazy_client(self):
        client = FakeClient()
        adapter = OpenAICompatibleHeavyModelAdapter(settings=settings(), client_factory=lambda: client)

        await adapter.probe()
        await adapter.cleanup()

        self.assertTrue(client.closed)

    def test_protected_extra_body_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "protected request fields"):
            OpenAICompatibleHeavyModelAdapter(settings=settings(extra_body={"model": "override"}))

    async def test_extra_body_mutation_after_initialization_cannot_override_fixed_fields(self):
        completions = FakeCompletions(response=success_response())
        client = FakeClient(completions=completions)
        adapter = OpenAICompatibleHeavyModelAdapter(
            settings=settings(extra_body={"chat_template_kwargs": {"enable_thinking": False}}),
            client_factory=lambda: client,
        )
        adapter.settings.extra_body["model"] = "mutated-model"
        adapter.settings.extra_body["messages"] = [{"role": "user", "content": "mutated"}]
        adapter.settings.extra_body["chat_template_kwargs"]["enable_thinking"] = True

        result = await adapter.analyze(HeavyModelRequest(prompt="original"))

        self.assertTrue(result.ok)
        call = completions.calls[0]
        self.assertEqual("test-model", call["model"])
        self.assertEqual("original", call["messages"][0]["content"][0]["text"])
        self.assertEqual({"enable_thinking": False}, call["extra_body"]["chat_template_kwargs"])
        self.assertNotIn("model", call["extra_body"])
        self.assertNotIn("messages", call["extra_body"])


if __name__ == "__main__":
    unittest.main()
