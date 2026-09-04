import unittest

import model_pricing as historical_pricing
import runtime_model_pricing as runtime_pricing
from model_telemetry import normalize_policy_version


class RuntimeModelPricingTests(unittest.TestCase):
    def test_astra_cached_and_write_tokens_are_charged_once(self) -> None:
        estimate = runtime_pricing.estimate_token_cost(
            "gpt-6-astra",
            [
                runtime_pricing.TokenUsage(
                    input_tokens=1000,
                    cached_input_tokens=100,
                    cache_write_tokens=100,
                    output_tokens=50,
                )
            ],
        )

        self.assertEqual(11_850_000, estimate.nano_usd)
        self.assertEqual("estimated", estimate.status)
        self.assertTrue(estimate.complete)
        self.assertEqual("gpt-6-astra", estimate.basis_model)
        self.assertEqual(10_000, estimate.input_rate_nano_usd)
        self.assertEqual(1_000, estimate.cached_input_rate_nano_usd)
        self.assertEqual(12_500, estimate.cache_write_rate_nano_usd)
        self.assertEqual(50_000, estimate.output_rate_nano_usd)

    def test_astra_long_context_threshold_and_cached_write_tiers(self) -> None:
        boundary = runtime_pricing.TokenUsage(
            input_tokens=272_000,
            cached_input_tokens=2_000,
            cache_write_tokens=4_000,
            output_tokens=20,
        )
        above = runtime_pricing.TokenUsage(
            input_tokens=272_001,
            cached_input_tokens=2_000,
            cache_write_tokens=4_000,
            output_tokens=20,
        )

        at_limit = runtime_pricing.estimate_token_cost("gpt-6-astra", [boundary])
        over_limit = runtime_pricing.estimate_token_cost("gpt-6-astra", [above])
        combined = runtime_pricing.estimate_token_cost("gpt-6-astra", [boundary, above])

        self.assertEqual(2_713_000_000, at_limit.nano_usd)
        self.assertEqual(5_425_520_000, over_limit.nano_usd)
        self.assertEqual(8_138_520_000, combined.nano_usd)
        self.assertTrue(combined.complete)

    def test_current_5_6_rates_and_cache_writes(self) -> None:
        expected_costs = {
            "gpt-5.6-sol": 4_740_000,
            "gpt-5.6-terra": 2_470_000,
            "gpt-5.6-luna": 247_000,
        }
        for model, expected in expected_costs.items():
            with self.subTest(model=model):
                estimate = runtime_pricing.estimate_token_cost(
                    model,
                    [
                        runtime_pricing.TokenUsage(
                            input_tokens=1000,
                            cached_input_tokens=100,
                            cache_write_tokens=100,
                            output_tokens=50,
                        )
                    ],
                )
                self.assertEqual(expected, estimate.nano_usd)
                self.assertTrue(estimate.complete)

    def test_current_snapshot_and_promotional_provenance_are_explicit(self) -> None:
        estimate = runtime_pricing.estimate_token_cost(
            "gpt-6-astra", [runtime_pricing.TokenUsage(100, output_tokens=10)]
        )

        self.assertEqual("openai-standard-2026-09-04", estimate.snapshot_version)
        self.assertIn(
            "https://developers.openai.com/api/docs/models/gpt-6-astra",
            runtime_pricing.PRICE_SOURCE_URLS,
        )
        self.assertIn(
            "https://developers.openai.com/api/docs/guides/prompt-caching",
            runtime_pricing.PRICE_SOURCE_URLS,
        )
        self.assertEqual(
            "2026-11-21",
            runtime_pricing.SOL_PROMOTIONAL_PRICING_AT_LEAST_THROUGH,
        )

    def test_unknown_models_and_missing_usage_never_become_zero_cost(self) -> None:
        for model in ("unpriced-model", "gpt-6", "gpt-6-astra-2026-09-04"):
            with self.subTest(model=model):
                estimate = runtime_pricing.estimate_token_cost(
                    model, [runtime_pricing.TokenUsage(100, output_tokens=10)]
                )
                self.assertEqual("missing_price", estimate.status)
                self.assertIsNone(estimate.nano_usd)
                self.assertIsNone(estimate.snapshot_version)
                self.assertFalse(estimate.complete)

        missing = runtime_pricing.estimate_token_cost("gpt-6-astra", [])
        self.assertEqual("missing_usage", missing.status)
        self.assertIsNone(missing.nano_usd)
        self.assertFalse(missing.complete)

    def test_partial_and_invalid_usage_keep_truthful_status(self) -> None:
        partial = runtime_pricing.estimate_token_cost(
            "gpt-6-astra", [runtime_pricing.TokenUsage(100)]
        )
        invalid = runtime_pricing.estimate_token_cost(
            "gpt-6-astra",
            [runtime_pricing.TokenUsage(100, cached_input_tokens=60, cache_write_tokens=50)],
        )

        self.assertEqual("partial", partial.status)
        self.assertEqual(1_000_000, partial.nano_usd)
        self.assertFalse(partial.complete)
        self.assertEqual("invalid_usage", invalid.status)
        self.assertIsNone(invalid.nano_usd)
        self.assertFalse(invalid.complete)

    def test_runtime_estimates_do_not_mutate_frozen_memory_pricing(self) -> None:
        historical_usage = historical_pricing.TokenUsage(
            input_tokens=1000,
            cached_input_tokens=100,
            cache_write_tokens=100,
            output_tokens=50,
        )
        before = historical_pricing.estimate_token_cost("gpt-5.6-sol", [historical_usage])
        current = runtime_pricing.estimate_token_cost(
            "gpt-5.6-sol",
            [
                runtime_pricing.TokenUsage(
                    input_tokens=1000,
                    cached_input_tokens=100,
                    cache_write_tokens=100,
                    output_tokens=50,
                )
            ],
        )
        after = historical_pricing.estimate_token_cost("gpt-5.6-sol", [historical_usage])

        self.assertEqual(before, after)
        self.assertEqual("openai-standard-2026-07-09", after.snapshot_version)
        self.assertEqual(6_175_000, after.nano_usd)
        self.assertEqual(4_740_000, current.nano_usd)
        self.assertIsNone(historical_pricing.token_price_for_model("gpt-6-astra"))
        self.assertIsNot(
            historical_pricing.MODEL_TOKEN_PRICES,
            runtime_pricing.MODEL_TOKEN_PRICES,
        )

    def test_new_policy_preserves_historical_and_shadow_labels(self) -> None:
        for policy in (
            "primary_astra_low_v1",
            "primary_sol_low_v1",
            "shadow_tier_router_v1",
        ):
            with self.subTest(policy=policy):
                self.assertEqual(policy, normalize_policy_version(policy))
        self.assertEqual("", normalize_policy_version("unverified_policy"))


if __name__ == "__main__":
    unittest.main()
