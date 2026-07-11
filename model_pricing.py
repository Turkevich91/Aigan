from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence


# Public standard-processing prices verified against official OpenAI model pages
# on 2026-07-09. Persist the snapshot version and exact rates with every priced
# stage so later catalog changes never rewrite historical estimates.
PRICE_SNAPSHOT_VERSION = "openai-standard-2026-07-09"
PRICE_SOURCE_URLS = (
    "https://developers.openai.com/api/docs/models/gpt-5.6-sol",
    "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
    "https://developers.openai.com/api/docs/models/gpt-5.4-nano",
    "https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe",
    "https://openai.com/index/gpt-5-6/",
    "https://openai.com/index/new-embedding-models-and-api-updates/",
)


@dataclass(frozen=True)
class TokenPrice:
    input_nano_usd: int
    cached_input_nano_usd: int
    cache_write_nano_usd: int
    output_nano_usd: int
    long_context_threshold: int | None = None
    long_input_numerator: int = 1
    long_input_denominator: int = 1
    long_output_numerator: int = 1
    long_output_denominator: int = 1


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class CostEstimate:
    nano_usd: int | None
    status: str
    complete: bool
    snapshot_version: str | None
    basis_model: str | None
    input_rate_nano_usd: int | None
    cached_input_rate_nano_usd: int | None
    cache_write_rate_nano_usd: int | None
    output_rate_nano_usd: int | None


def _price(
    input_dollars_per_million: float,
    cached_dollars_per_million: float,
    output_dollars_per_million: float,
    *,
    cache_write_multiplier: float = 1.0,
    long_context_threshold: int | None = None,
    long_input_numerator: int = 1,
    long_input_denominator: int = 1,
    long_output_numerator: int = 1,
    long_output_denominator: int = 1,
) -> TokenPrice:
    # One USD per million tokens equals 1,000 nano-USD per token.
    input_rate = round(input_dollars_per_million * 1000)
    return TokenPrice(
        input_nano_usd=input_rate,
        cached_input_nano_usd=round(cached_dollars_per_million * 1000),
        cache_write_nano_usd=round(input_rate * cache_write_multiplier),
        output_nano_usd=round(output_dollars_per_million * 1000),
        long_context_threshold=long_context_threshold,
        long_input_numerator=long_input_numerator,
        long_input_denominator=long_input_denominator,
        long_output_numerator=long_output_numerator,
        long_output_denominator=long_output_denominator,
    )


_SOL = _price(
    5.0,
    0.5,
    30.0,
    cache_write_multiplier=1.25,
    long_context_threshold=272_000,
    long_input_numerator=2,
    long_output_numerator=3,
    long_output_denominator=2,
)
_TERRA = _price(
    2.5,
    0.25,
    15.0,
    cache_write_multiplier=1.25,
    long_context_threshold=272_000,
    long_input_numerator=2,
    long_output_numerator=3,
    long_output_denominator=2,
)
_LUNA = _price(
    1.0,
    0.1,
    6.0,
    cache_write_multiplier=1.25,
    long_context_threshold=272_000,
    long_input_numerator=2,
    long_output_numerator=3,
    long_output_denominator=2,
)
_GPT_54 = _price(2.5, 0.25, 15.0)
_GPT_54_MINI = _price(0.75, 0.075, 4.5)
_GPT_54_NANO = _price(0.2, 0.02, 1.25)
_GPT_55 = _price(5.0, 0.5, 30.0)
_EMBEDDING_3_SMALL = _price(0.02, 0.02, 0.0)
_GPT_4O_MINI_TRANSCRIBE = _price(1.25, 1.25, 5.0)


MODEL_TOKEN_PRICES: dict[str, TokenPrice] = {
    "gpt-5.6-sol": _SOL,
    "gpt-5.6-terra": _TERRA,
    "gpt-5.6-luna": _LUNA,
    "gpt-5.5": _GPT_55,
    "gpt-5.4": _GPT_54,
    "gpt-5.4-mini": _GPT_54_MINI,
    "gpt-5.4-mini-2026-03-17": _GPT_54_MINI,
    "gpt-5.4-nano": _GPT_54_NANO,
    "gpt-5.4-nano-2026-03-17": _GPT_54_NANO,
    "text-embedding-3-small": _EMBEDDING_3_SMALL,
    "gpt-4o-mini-transcribe": _GPT_4O_MINI_TRANSCRIBE,
    "gpt-4o-mini-transcribe-2025-03-20": _GPT_4O_MINI_TRANSCRIBE,
    "gpt-4o-mini-transcribe-2025-12-15": _GPT_4O_MINI_TRANSCRIBE,
}


def token_price_for_model(model: str) -> TokenPrice | None:
    return MODEL_TOKEN_PRICES.get(str(model or "").strip().casefold())


def _scaled_cost(tokens: int, rate: int, numerator: int = 1, denominator: int = 1) -> int:
    return (tokens * rate * numerator + denominator // 2) // denominator


def estimate_token_cost(model: str, requests: Sequence[TokenUsage]) -> CostEstimate:
    basis_model = str(model or "").strip().casefold()
    price = token_price_for_model(basis_model)
    empty = CostEstimate(
        nano_usd=None,
        status="missing_price" if price is None else "missing_usage",
        complete=False,
        snapshot_version=PRICE_SNAPSHOT_VERSION if price is not None else None,
        basis_model=basis_model or None,
        input_rate_nano_usd=price.input_nano_usd if price else None,
        cached_input_rate_nano_usd=price.cached_input_nano_usd if price else None,
        cache_write_rate_nano_usd=price.cache_write_nano_usd if price else None,
        output_rate_nano_usd=price.output_nano_usd if price else None,
    )
    if price is None or not requests:
        return empty

    total = 0
    has_known_component = False
    complete = True
    for usage in requests:
        try:
            input_tokens = int(usage.input_tokens) if usage.input_tokens is not None else None
            cached_tokens = int(usage.cached_input_tokens or 0)
            cache_write_tokens = int(usage.cache_write_tokens or 0)
            output_tokens = int(usage.output_tokens) if usage.output_tokens is not None else None
        except (TypeError, ValueError):
            return replace(empty, status="invalid_usage")
        if (
            (input_tokens is not None and input_tokens < 0)
            or cached_tokens < 0
            or cache_write_tokens < 0
            or (
                input_tokens is not None
                and cached_tokens + cache_write_tokens > input_tokens
            )
            or (output_tokens is not None and output_tokens < 0)
        ):
            return replace(empty, status="invalid_usage")
        if input_tokens is None:
            complete = False
        if output_tokens is None and price.output_nano_usd:
            complete = False

        input_numerator = 1
        input_denominator = 1
        output_numerator = 1
        output_denominator = 1
        if (
            price.long_context_threshold is not None
            and input_tokens is not None
            and input_tokens > price.long_context_threshold
        ):
            input_numerator = price.long_input_numerator
            input_denominator = price.long_input_denominator
            output_numerator = price.long_output_numerator
            output_denominator = price.long_output_denominator

        if input_tokens is not None:
            ordinary_tokens = input_tokens - cached_tokens - cache_write_tokens
            total += _scaled_cost(
                ordinary_tokens,
                price.input_nano_usd,
                input_numerator,
                input_denominator,
            )
            total += _scaled_cost(
                cached_tokens,
                price.cached_input_nano_usd,
                input_numerator,
                input_denominator,
            )
            total += _scaled_cost(
                cache_write_tokens,
                price.cache_write_nano_usd,
                input_numerator,
                input_denominator,
            )
            has_known_component = True
        if output_tokens is not None:
            total += _scaled_cost(
                output_tokens,
                price.output_nano_usd,
                output_numerator,
                output_denominator,
            )
            has_known_component = True

    if not complete:
        status = "partial"
    else:
        status = "estimated"
    return CostEstimate(
        nano_usd=total if has_known_component else None,
        status=status,
        complete=complete,
        snapshot_version=PRICE_SNAPSHOT_VERSION,
        basis_model=basis_model,
        input_rate_nano_usd=price.input_nano_usd,
        cached_input_rate_nano_usd=price.cached_input_nano_usd,
        cache_write_rate_nano_usd=price.cache_write_nano_usd,
        output_rate_nano_usd=price.output_nano_usd,
    )
