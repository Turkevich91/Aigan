from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from openai import AsyncOpenAI


SUPPORTED_API_FLAVORS = frozenset({"chat_completions"})
SUPPORTED_MODALITIES = frozenset({"text", "image", "video"})
SUPPORTED_MEDIA_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "video/mp4"}
)
PROTECTED_EXTRA_BODY_KEYS = frozenset(
    {
        "messages",
        "model",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "temperature",
    }
)
SAFE_TASK_CLASS_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
DATA_URL_HEADER_RE = re.compile(
    r"^data:(?P<mime_type>(?:image|video)/[a-z0-9][a-z0-9.+-]{0,63});base64$",
    re.IGNORECASE,
)
MAX_INLINE_DATA_URL_HEADER_CHARS = 96


FAILURE_MESSAGES = {
    "disabled": "Heavy local inference is disabled.",
    "unconfigured": "Heavy local inference is not configured.",
    "invalid_request": "The heavy-model request is invalid.",
    "unsupported_modality": "The configured heavy model does not support this input type.",
    "payload_too_large": "The heavy-model input exceeds the configured limit.",
    "unsafe_media_url": "The media location is not allowed for heavy-model processing.",
    "busy": "The heavy-model backend is busy; retry this job later.",
    "timeout": "The heavy-model backend timed out.",
    "authentication_failed": "The heavy-model backend rejected authentication.",
    "rate_limited": "The heavy-model backend is temporarily rate limited.",
    "unavailable": "The heavy-model backend is unavailable.",
    "provider_rejected": "The heavy-model backend rejected the request.",
    "provider_error": "The heavy-model backend returned an error.",
    "malformed_response": "The heavy-model backend returned an unusable response.",
    "model_unavailable": "The configured heavy model is not available on the backend.",
    "cleanup_failed": "The heavy-model client did not close cleanly.",
}


@dataclass(frozen=True)
class HeavyMediaInput:
    modality: str
    url: str = field(repr=False)
    mime_type: str = ""


@dataclass(frozen=True)
class HeavyModelRequest:
    prompt: str = field(repr=False)
    media: tuple[HeavyMediaInput, ...] = ()
    task_class: str = field(default="heavy", repr=False)
    max_output_tokens: int | None = None
    temperature: float | None = None


@dataclass
class HeavyModelResult:
    ok: bool
    text: str = field(default="", repr=False)
    backend: str = "none"
    api_flavor: str = ""
    task_class: str = field(default="heavy", repr=False)
    failure_category: str = ""
    user_message: str = ""
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    media_count: int = 0
    output_chars: int = 0
    truncated: bool = False

    @classmethod
    def failure(
        cls,
        category: str,
        *,
        backend: str,
        api_flavor: str,
        task_class: str,
        duration_ms: int = 0,
        media_count: int = 0,
    ) -> "HeavyModelResult":
        safe_category = category if category in FAILURE_MESSAGES else "provider_error"
        return cls(
            ok=False,
            backend=backend,
            api_flavor=api_flavor,
            task_class=safe_task_class(task_class),
            failure_category=safe_category,
            user_message=FAILURE_MESSAGES[safe_category],
            duration_ms=max(0, int(duration_ms)),
            media_count=max(0, int(media_count)),
        )

    def public_dict(self) -> dict[str, Any]:
        """Return telemetry-safe metadata. Generated text is deliberately omitted."""

        return {
            "ok": self.ok,
            "backend": self.backend,
            "api_flavor": self.api_flavor,
            "failure_category": self.failure_category,
            "user_message": self.user_message,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "media_count": self.media_count,
            "output_chars": self.output_chars,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class HeavyModelProbeResult:
    ok: bool
    backend: str
    api_flavor: str
    failure_category: str = ""
    user_message: str = ""
    model_present: bool = False
    discovered_model_count: int = 0
    duration_ms: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "backend": self.backend,
            "api_flavor": self.api_flavor,
            "failure_category": self.failure_category,
            "user_message": self.user_message,
            "model_present": self.model_present,
            "discovered_model_count": self.discovered_model_count,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class HeavyModelSettings:
    base_url: str = field(repr=False)
    model: str = field(repr=False)
    api_key: str = field(default="", repr=False)
    api_flavor: str = "chat_completions"
    capabilities: tuple[str, ...] = ("text",)
    timeout_seconds: float = 180.0
    max_prompt_chars: int = 30_000
    max_inline_media_bytes: int = 20_000_000
    max_media_bytes: int = 50_000_000
    max_media_items: int = 8
    max_output_tokens: int = 2_000
    max_result_chars: int = 40_000
    max_concurrency: int = 1
    extra_body: Mapping[str, Any] = field(default_factory=dict, repr=False)


class HeavyModelAdapter(Protocol):
    async def analyze(self, request: HeavyModelRequest) -> HeavyModelResult:
        ...

    async def probe(self) -> HeavyModelProbeResult:
        ...

    def health_summary(self) -> dict[str, Any]:
        ...

    async def cleanup(self) -> None:
        ...


class NullHeavyModelAdapter:
    def __init__(
        self,
        *,
        name: str = "heavy_model",
        requested_enabled: bool = False,
        failure_category: str = "disabled",
    ) -> None:
        self.name = name
        self.requested_enabled = bool(requested_enabled)
        self.failure_category = failure_category if failure_category in {"disabled", "unconfigured"} else "disabled"

    async def analyze(self, request: HeavyModelRequest) -> HeavyModelResult:
        return HeavyModelResult.failure(
            self.failure_category,
            backend="null",
            api_flavor="",
            task_class=request.task_class,
            media_count=safe_media_count(request.media),
        )

    async def probe(self) -> HeavyModelProbeResult:
        return HeavyModelProbeResult(
            ok=False,
            backend="null",
            api_flavor="",
            failure_category=self.failure_category,
            user_message=FAILURE_MESSAGES[self.failure_category],
        )

    def health_summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": "models",
            "enabled": self.requested_enabled,
            "configured": False,
            "available": False,
            "adapter": "null",
            "status": self.failure_category,
            "error_count": 0,
            "backend": "none",
        }

    async def cleanup(self) -> None:
        return None


ClientFactory = Callable[[], Any]


class OpenAICompatibleHeavyModelAdapter:
    """Bounded adapter for operator-configured OpenAI-compatible local endpoints.

    The adapter owns transport and validation only. It does not schedule jobs,
    download media, manage model lifecycle, or fall back to a paid provider.
    """

    def __init__(
        self,
        *,
        settings: HeavyModelSettings,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.settings = validate_settings(settings)
        self._extra_body_json = json.dumps(
            self.settings.extra_body,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._client_factory = client_factory or self._default_client
        self._client: Any | None = None
        self._admission_lock = asyncio.Lock()
        self._in_flight = 0
        self.error_count = 0
        self.rejected_count = 0
        self.busy_count = 0
        self.last_failure_category = ""
        self._available: bool | None = None

    async def analyze(self, request: HeavyModelRequest) -> HeavyModelResult:
        started = time.monotonic()
        task_class = safe_task_class(request.task_class)
        media_count = safe_media_count(request.media)
        try:
            validation = self._validate_request(request, task_class=task_class)
        except (AttributeError, TypeError, ValueError):
            validation = "invalid_request"
        if validation is not None:
            self.rejected_count += 1
            self.last_failure_category = validation
            return HeavyModelResult.failure(
                validation,
                backend="openai_compatible",
                api_flavor=self.settings.api_flavor,
                task_class=task_class,
                duration_ms=elapsed_ms(started),
                media_count=media_count,
            )

        if not await self._try_acquire_slot():
            self.busy_count += 1
            self.last_failure_category = "busy"
            return HeavyModelResult.failure(
                "busy",
                backend="openai_compatible",
                api_flavor=self.settings.api_flavor,
                task_class=task_class,
                duration_ms=elapsed_ms(started),
                media_count=media_count,
            )

        try:
            try:
                response = await self._create_chat_completion(request)
                text = response_text(response)
                if not text:
                    self._record_provider_failure("malformed_response")
                    return HeavyModelResult.failure(
                        "malformed_response",
                        backend="openai_compatible",
                        api_flavor=self.settings.api_flavor,
                        task_class=task_class,
                        duration_ms=elapsed_ms(started),
                        media_count=media_count,
                    )
                truncated = len(text) > self.settings.max_result_chars
                if truncated:
                    text = text[: self.settings.max_result_chars]
                input_tokens, output_tokens, total_tokens = response_usage(response)
                self._available = True
                self.last_failure_category = ""
                return HeavyModelResult(
                    ok=True,
                    text=text,
                    backend="openai_compatible",
                    api_flavor=self.settings.api_flavor,
                    task_class=task_class,
                    duration_ms=elapsed_ms(started),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    media_count=media_count,
                    output_chars=len(text),
                    truncated=truncated,
                )
            except Exception as exc:
                category = categorize_provider_exception(exc)
                self._record_provider_failure(category)
                return HeavyModelResult.failure(
                    category,
                    backend="openai_compatible",
                    api_flavor=self.settings.api_flavor,
                    task_class=task_class,
                    duration_ms=elapsed_ms(started),
                    media_count=media_count,
                )
        finally:
            await self._release_slot()

    async def probe(self) -> HeavyModelProbeResult:
        """List models without sending content or running an inference request."""

        started = time.monotonic()
        if not await self._try_acquire_slot():
            self.busy_count += 1
            self.last_failure_category = "busy"
            return HeavyModelProbeResult(
                ok=False,
                backend="openai_compatible",
                api_flavor=self.settings.api_flavor,
                failure_category="busy",
                user_message=FAILURE_MESSAGES["busy"],
                duration_ms=elapsed_ms(started),
            )
        try:
            try:
                response = await self._get_client().models.list()
                models = sequence_value(response, "data")
                model_ids = {str(value(item, "id") or "") for item in models}
                model_present = self.settings.model in model_ids
                self._available = model_present
                if not model_present:
                    self.last_failure_category = "model_unavailable"
                    return HeavyModelProbeResult(
                        ok=False,
                        backend="openai_compatible",
                        api_flavor=self.settings.api_flavor,
                        failure_category="model_unavailable",
                        user_message=FAILURE_MESSAGES["model_unavailable"],
                        model_present=False,
                        discovered_model_count=len(model_ids),
                        duration_ms=elapsed_ms(started),
                    )
                self.last_failure_category = ""
                return HeavyModelProbeResult(
                    ok=True,
                    backend="openai_compatible",
                    api_flavor=self.settings.api_flavor,
                    model_present=True,
                    discovered_model_count=len(model_ids),
                    duration_ms=elapsed_ms(started),
                )
            except Exception as exc:
                category = categorize_provider_exception(exc)
                self._record_provider_failure(category)
                return HeavyModelProbeResult(
                    ok=False,
                    backend="openai_compatible",
                    api_flavor=self.settings.api_flavor,
                    failure_category=category,
                    user_message=FAILURE_MESSAGES[category],
                    duration_ms=elapsed_ms(started),
                )
        finally:
            await self._release_slot()

    def health_summary(self) -> dict[str, Any]:
        if self.error_count:
            status = "degraded"
        elif self._available is False:
            status = "degraded"
        elif self._available is True:
            status = "ok"
        else:
            status = "unknown"
        details: dict[str, Any] = {
            "name": "heavy_model",
            "family": "models",
            "enabled": True,
            "configured": True,
            "available": self._available is True,
            "adapter": type(self).__name__,
            "status": status,
            "error_count": self.error_count,
            "warning_count": self.rejected_count + self.busy_count,
            "backend": "openai_compatible",
            "api_flavor": self.settings.api_flavor,
            "capabilities": list(self.settings.capabilities),
            "max_concurrency": self.settings.max_concurrency,
            "model_configured": True,
            "text_supported": "text" in self.settings.capabilities,
            "image_supported": "image" in self.settings.capabilities,
            "video_supported": "video" in self.settings.capabilities,
            "active": self._in_flight,
        }
        if self.last_failure_category:
            details["last_failure_category"] = self.last_failure_category
        return details

    async def cleanup(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        close = getattr(client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            self._record_provider_failure("cleanup_failed")

    async def _create_chat_completion(self, request: HeavyModelRequest) -> Any:
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt.strip()}]
        content.extend(media_content_item(item) for item in request.media)
        max_tokens = self.settings.max_output_tokens
        if request.max_output_tokens is not None:
            max_tokens = min(max_tokens, int(request.max_output_tokens))
        kwargs: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "stream": False,
        }
        if request.temperature is not None:
            kwargs["temperature"] = float(request.temperature)
        if self._extra_body_json != "{}":
            kwargs["extra_body"] = json.loads(self._extra_body_json)
        return await self._get_client().chat.completions.create(**kwargs)

    def _validate_request(self, request: HeavyModelRequest, *, task_class: str) -> str | None:
        if not isinstance(request.task_class, str) or task_class != request.task_class.strip().lower():
            return "invalid_request"
        if not isinstance(request.prompt, str):
            return "invalid_request"
        prompt = request.prompt.strip()
        if not prompt or len(prompt) > self.settings.max_prompt_chars:
            return "invalid_request" if not prompt else "payload_too_large"
        if request.max_output_tokens is not None:
            try:
                requested_tokens = int(request.max_output_tokens)
            except (TypeError, ValueError):
                return "invalid_request"
            if requested_tokens < 1:
                return "invalid_request"
        if request.temperature is not None:
            try:
                temperature = float(request.temperature)
            except (TypeError, ValueError):
                return "invalid_request"
            if not 0.0 <= temperature <= 2.0:
                return "invalid_request"
        if len(request.media) > self.settings.max_media_items:
            return "payload_too_large"

        total_bytes = 0
        for item in request.media:
            if not isinstance(item, HeavyMediaInput) or not isinstance(item.modality, str):
                return "invalid_request"
            modality = item.modality.strip().lower()
            if modality not in SUPPORTED_MODALITIES - {"text"}:
                return "unsupported_modality"
            if modality not in self.settings.capabilities:
                return "unsupported_modality"
            validation, size_bytes = validate_media_input(item, self.settings)
            if validation:
                return validation
            total_bytes += size_bytes
            if total_bytes > self.settings.max_media_bytes:
                return "payload_too_large"
        return None

    def _default_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self.settings.api_key or "local-heavy-model",
            base_url=self.settings.base_url,
            timeout=self.settings.timeout_seconds,
            max_retries=0,
        )

    async def _try_acquire_slot(self) -> bool:
        async with self._admission_lock:
            if self._in_flight >= self.settings.max_concurrency:
                return False
            self._in_flight += 1
            return True

    async def _release_slot(self) -> None:
        async with self._admission_lock:
            self._in_flight = max(0, self._in_flight - 1)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def _record_provider_failure(self, category: str) -> None:
        self.error_count += 1
        self.last_failure_category = category
        self._available = False


def validate_settings(settings: HeavyModelSettings) -> HeavyModelSettings:
    api_flavor = settings.api_flavor.strip().lower()
    if api_flavor not in SUPPORTED_API_FLAVORS:
        raise ValueError("HEAVY_MODEL_API_FLAVOR is not supported")
    base_url = normalized_base_url(settings.base_url)
    model = settings.model.strip()
    if not model:
        raise ValueError("HEAVY_MODEL_MODEL is required when the connector is enabled")
    capabilities = tuple(dict.fromkeys(item.strip().lower() for item in settings.capabilities if item.strip()))
    if not capabilities or "text" not in capabilities:
        raise ValueError("HEAVY_MODEL_CAPABILITIES must include text")
    if any(item not in SUPPORTED_MODALITIES for item in capabilities):
        raise ValueError("HEAVY_MODEL_CAPABILITIES contains an unsupported modality")
    limits = {
        "HEAVY_MODEL_TIMEOUT_SECONDS": (float(settings.timeout_seconds), 1.0, 900.0),
        "HEAVY_MODEL_MAX_PROMPT_CHARS": (int(settings.max_prompt_chars), 1, 200_000),
        "HEAVY_MODEL_MAX_INLINE_MEDIA_BYTES": (int(settings.max_inline_media_bytes), 1, 100_000_000),
        "HEAVY_MODEL_MAX_MEDIA_BYTES": (int(settings.max_media_bytes), 1, 500_000_000),
        "HEAVY_MODEL_MAX_MEDIA_ITEMS": (int(settings.max_media_items), 0, 16),
        "HEAVY_MODEL_MAX_OUTPUT_TOKENS": (int(settings.max_output_tokens), 1, 32_000),
        "HEAVY_MODEL_MAX_RESULT_CHARS": (int(settings.max_result_chars), 1, 500_000),
        "HEAVY_MODEL_MAX_CONCURRENCY": (int(settings.max_concurrency), 1, 8),
    }
    for name, (value_to_check, minimum, maximum) in limits.items():
        if not minimum <= value_to_check <= maximum:
            raise ValueError(f"{name} is outside the supported range")
    if settings.max_inline_media_bytes > settings.max_media_bytes:
        raise ValueError("HEAVY_MODEL_MAX_INLINE_MEDIA_BYTES cannot exceed HEAVY_MODEL_MAX_MEDIA_BYTES")
    try:
        extra_body = json.loads(json.dumps(dict(settings.extra_body), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("HEAVY_MODEL_EXTRA_BODY_JSON must contain JSON-compatible values") from exc
    if any(str(key).strip().lower() in PROTECTED_EXTRA_BODY_KEYS for key in extra_body):
        raise ValueError("HEAVY_MODEL_EXTRA_BODY_JSON cannot override protected request fields")
    return HeavyModelSettings(
        base_url=base_url,
        model=model,
        api_key=settings.api_key.strip(),
        api_flavor=api_flavor,
        capabilities=capabilities,
        timeout_seconds=float(settings.timeout_seconds),
        max_prompt_chars=int(settings.max_prompt_chars),
        max_inline_media_bytes=int(settings.max_inline_media_bytes),
        max_media_bytes=int(settings.max_media_bytes),
        max_media_items=int(settings.max_media_items),
        max_output_tokens=int(settings.max_output_tokens),
        max_result_chars=int(settings.max_result_chars),
        max_concurrency=int(settings.max_concurrency),
        extra_body=extra_body,
    )


def normalized_base_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HEAVY_MODEL_BASE_URL must be an HTTP(S) endpoint without credentials, query, or fragment")
    return raw_url.strip().rstrip("/")


def validate_media_input(item: HeavyMediaInput, settings: HeavyModelSettings) -> tuple[str | None, int]:
    modality = item.modality.strip().lower()
    if not isinstance(item.url, str) or not isinstance(item.mime_type, str):
        return "invalid_request", 0
    raw_url = item.url.strip()
    if not raw_url:
        return "invalid_request", 0
    if raw_url[:5].casefold() == "data:":
        header, separator, encoded = raw_url.partition(",")
        if (
            not separator
            or not encoded
            or len(header) > MAX_INLINE_DATA_URL_HEADER_CHARS
        ):
            return "invalid_request", 0
        match = DATA_URL_HEADER_RE.fullmatch(header)
        if match is None:
            return "invalid_request", 0
        mime_type = match.group("mime_type").lower()
        if mime_type not in SUPPORTED_MEDIA_MIME_TYPES:
            return "unsupported_modality", 0
        if not mime_type.startswith(f"{modality}/"):
            return "invalid_request", 0
        declared_mime_type = item.mime_type.strip().lower()
        if declared_mime_type and declared_mime_type != mime_type:
            return "invalid_request", 0
        estimated_size = max(0, (len(encoded) * 3) // 4 - encoded[-2:].count("="))
        if estimated_size > settings.max_inline_media_bytes:
            return "payload_too_large", 0
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return "invalid_request", 0
        size_bytes = len(decoded)
        if size_bytes > settings.max_inline_media_bytes:
            return "payload_too_large", 0
        return None, size_bytes
    return "unsafe_media_url", 0


def media_content_item(item: HeavyMediaInput) -> dict[str, Any]:
    modality = item.modality.strip().lower()
    field_name = f"{modality}_url"
    media_url = item.url.strip()
    if media_url[:5].casefold() == "data:":
        media_url = "data:" + media_url[5:]
    return {"type": field_name, field_name: {"url": media_url}}


def response_text(response: Any) -> str:
    choices = sequence_value(response, "choices")
    if not choices:
        return ""
    message = value(choices[0], "message")
    content = value(message, "content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes, bytearray)):
        return ""
    parts: list[str] = []
    for item in content:
        text_value = value(item, "text")
        if isinstance(text_value, str) and text_value.strip():
            parts.append(text_value.strip())
    return "\n".join(parts).strip()


def response_usage(response: Any) -> tuple[int, int, int]:
    usage = value(response, "usage")
    input_tokens = nonnegative_int(value(usage, "prompt_tokens") or value(usage, "input_tokens"))
    output_tokens = nonnegative_int(value(usage, "completion_tokens") or value(usage, "output_tokens"))
    total_tokens = nonnegative_int(value(usage, "total_tokens"))
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return input_tokens, output_tokens, total_tokens


def categorize_provider_exception(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    status_code = nonnegative_int(getattr(exc, "status_code", 0))
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in name:
        return "timeout"
    if status_code in {401, 403} or "authentication" in name or "permission" in name:
        return "authentication_failed"
    if status_code == 429 or "ratelimit" in name or "rate_limit" in name:
        return "rate_limited"
    if status_code in {400, 404, 409, 413, 422} or "badrequest" in name or "unprocessable" in name:
        return "provider_rejected"
    if status_code >= 500 or "connection" in name or "unavailable" in name:
        return "unavailable"
    return "provider_error"


def safe_task_class(value_to_clean: str) -> str:
    normalized = str(value_to_clean or "").strip().lower()
    return normalized if SAFE_TASK_CLASS_RE.fullmatch(normalized) else "invalid"


def elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def value(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def sequence_value(item: Any, name: str) -> list[Any]:
    candidate = value(item, name)
    if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
        return list(candidate)
    return []


def nonnegative_int(value_to_parse: Any) -> int:
    try:
        return max(0, int(value_to_parse or 0))
    except (TypeError, ValueError):
        return 0


def safe_media_count(media: Any) -> int:
    try:
        return max(0, len(media))
    except (TypeError, ValueError):
        return 0
