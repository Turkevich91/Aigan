from __future__ import annotations

import json
import math
import unittest
from collections import Counter
from pathlib import Path

from model_routing import (
    MODEL_TIERS,
    ModelTierAliases,
    canonical_fallback_chain,
    failed_model_routing_decision,
    model_routing_schema,
    normalize_model_routing_decision,
    normalize_routing_mode,
    opaque_episode_key,
    shadow_canary_eligibility,
    valid_assignment_key,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "model_routing_v1.jsonl"
TIER_RANK = {tier: index for index, tier in enumerate(MODEL_TIERS)}


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_class": "simple_utility",
        "complexity": "low",
        "freshness": "not_required",
        "risk": "low",
        "ambiguity": "low",
        "selected_tier": "economy",
        "reasoning_effort": "none",
        "confidence": 0.95,
        "reason_codes": ["low_complexity"],
        "fallback_chain": ["economy", "balanced", "premium"],
    }
    payload.update(overrides)
    return payload


class ModelRoutingPolicyTests(unittest.TestCase):
    def test_routing_modes_are_strict(self) -> None:
        self.assertEqual("off", normalize_routing_mode("off"))
        self.assertEqual("shadow", normalize_routing_mode("SHADOW"))
        with self.assertRaisesRegex(ValueError, "MODEL_ROUTING_MODE"):
            normalize_routing_mode("active")

    def test_tier_aliases_keep_premium_equal_to_primary(self) -> None:
        aliases = ModelTierAliases("gpt-5.4-nano", "gpt-5.6-terra", "gpt-5.6-sol")
        self.assertIs(aliases, aliases.validate(primary_model="gpt-5.6-sol"))
        with self.assertRaisesRegex(ValueError, "premium"):
            ModelTierAliases("gpt-5.4-nano", "gpt-5.6-terra", "gpt-5.5").validate(
                primary_model="gpt-5.6-sol"
            )

    def test_schema_contains_no_tool_authority_or_free_rationale(self) -> None:
        schema = model_routing_schema()
        properties = set(schema["properties"])
        self.assertNotIn("tools", properties)
        self.assertNotIn("allowed_toolsets", properties)
        self.assertNotIn("rationale", properties)
        self.assertTrue(schema["additionalProperties"] is False)

    def test_exact_utility_can_recommend_economy(self) -> None:
        decision = normalize_model_routing_decision(
            valid_payload(), confidence_threshold=0.75, route_bucket="normal"
        )
        self.assertEqual("economy", decision.selected_tier)
        self.assertEqual("none", decision.reasoning_effort)
        self.assertFalse(decision.degraded)

    def test_policy_only_upgrades_router_recommendations(self) -> None:
        cases = (
            ({"task_class": "creative_social", "reason_codes": ["creative_persona"]}, {}, "premium"),
            ({"task_class": "memory_sensitive", "reason_codes": ["memory_required"]}, {}, "premium"),
            (
                {
                    "task_class": "high_risk",
                    "risk": "low",
                    "selected_tier": "economy",
                    "reasoning_effort": "none",
                    "reason_codes": ["low_complexity"],
                },
                {},
                "premium",
            ),
            ({"risk": "medium", "reason_codes": ["safety_sensitive"]}, {}, "premium"),
            ({"ambiguity": "medium", "reason_codes": ["high_ambiguity"]}, {}, "premium"),
            ({"complexity": "high", "reason_codes": ["high_complexity"]}, {}, "premium"),
            ({"freshness": "live", "reason_codes": ["current_grounding"]}, {}, "premium"),
            (
                {
                    "task_class": "grounded_current",
                    "freshness": "live",
                    "selected_tier": "balanced",
                    "reasoning_effort": "low",
                    "reason_codes": ["current_grounding"],
                },
                {},
                "premium",
            ),
            ({}, {"route_bucket": "memory_recall"}, "premium"),
            ({}, {"route_bucket": "time_sensitive"}, "premium"),
            ({}, {"mutation_capability": True}, "premium"),
            ({}, {"mutation_requested": True}, "premium"),
            ({}, {"short_unanchored_followup": True}, "premium"),
            ({}, {"source_context": True}, "balanced"),
            (
                {
                    "task_class": "tool_result_synthesis",
                    "complexity": "medium",
                    "selected_tier": "economy",
                    "reasoning_effort": "none",
                    "reason_codes": ["tool_synthesis"],
                },
                {},
                "balanced",
            ),
        )
        for payload_changes, policy_changes, expected in cases:
            with self.subTest(payload=payload_changes, policy=policy_changes):
                payload = valid_payload(**payload_changes)
                payload["fallback_chain"] = list(canonical_fallback_chain(str(payload["selected_tier"])))
                decision = normalize_model_routing_decision(
                    payload,
                    confidence_threshold=0.75,
                    route_bucket=str(policy_changes.get("route_bucket", "normal")),
                    mutation_capability=bool(policy_changes.get("mutation_capability", False)),
                    mutation_requested=bool(policy_changes.get("mutation_requested", False)),
                    short_unanchored_followup=bool(
                        policy_changes.get("short_unanchored_followup", False)
                    ),
                    source_context=bool(policy_changes.get("source_context", False)),
                )
                self.assertEqual(expected, decision.selected_tier)
                self.assertGreaterEqual(
                    TIER_RANK[decision.selected_tier],
                    TIER_RANK[str(payload["selected_tier"])],
                )

    def test_fallback_chain_is_built_by_policy_not_trusted_from_model(self) -> None:
        decision = normalize_model_routing_decision(
            valid_payload(fallback_chain=["premium", "economy"]),
            confidence_threshold=0.75,
            route_bucket="normal",
        )
        self.assertEqual(("economy", "balanced", "premium"), decision.fallback_chain)

    def test_invalid_and_low_confidence_results_fall_back_to_premium(self) -> None:
        invalid_payloads = (
            "not-a-dict",
            {**valid_payload(), "unexpected": True},
            valid_payload(confidence=math.nan),
            valid_payload(task_class="unknown"),
            valid_payload(reason_codes=[]),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                decision = normalize_model_routing_decision(
                    payload, confidence_threshold=0.75
                )
                self.assertEqual("premium", decision.selected_tier)
                self.assertTrue(decision.degraded)
                self.assertEqual("invalid", decision.outcome)
        low = normalize_model_routing_decision(
            valid_payload(confidence=0.74), confidence_threshold=0.75
        )
        self.assertEqual("premium", low.selected_tier)
        self.assertEqual("below_confidence", low.fallback_reason)
        self.assertEqual("degraded", low.outcome)
        self.assertEqual("premium", failed_model_routing_decision().selected_tier)

    def test_only_narrow_exact_utility_is_future_canary_eligible(self) -> None:
        decision = normalize_model_routing_decision(
            valid_payload(), confidence_threshold=0.75, route_bucket="normal"
        )
        eligible = shadow_canary_eligibility(
            decision,
            route_bucket="normal",
            has_url=False,
            has_attachment=False,
            has_reference=False,
            mutation_capability=False,
            short_followup=False,
        )
        self.assertEqual((True, "eligible_exact_utility"), eligible)
        self.assertEqual(
            (False, "unsafe_context"),
            shadow_canary_eligibility(
                decision,
                route_bucket="normal",
                has_url=True,
                has_attachment=False,
                has_reference=False,
                mutation_capability=False,
                short_followup=False,
            ),
        )
        self.assertEqual(
            (False, "unsafe_context"),
            shadow_canary_eligibility(
                decision,
                route_bucket="normal",
                has_url=False,
                has_attachment=False,
                has_reference=False,
                mutation_capability=False,
                short_followup=False,
                mutation_requested=True,
            ),
        )

    def test_episode_keys_are_stable_opaque_and_secret_bound(self) -> None:
        first = opaque_episode_key("private-a", "chat", 1, "thread", 2)
        same = opaque_episode_key("private-a", "chat", 1, "thread", 2)
        different = opaque_episode_key("private-a", "chat", 1, "thread", 3)
        different_secret = opaque_episode_key("private-b", "chat", 1, "thread", 2)
        self.assertEqual(first, same)
        self.assertNotEqual(first, different)
        self.assertNotEqual(first, different_secret)
        self.assertEqual(first, valid_assignment_key(first))
        self.assertNotIn("chat", first)

    def test_frozen_suite_has_120_sanitized_balanced_cases(self) -> None:
        cases = [json.loads(line) for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(120, len(cases))
        counts = Counter(str(case["expected"]["task_class"]) for case in cases)
        self.assertEqual(
            {
                "creative_social": 20,
                "grounded_current": 20,
                "high_risk": 20,
                "memory_sensitive": 20,
                "simple_utility": 20,
                "tool_result_synthesis": 20,
            },
            dict(counts),
        )
        serialized = "\n".join(json.dumps(case, ensure_ascii=False) for case in cases)
        forbidden = ("@", "file://", "C:\\", "/home/", "api_key", "telegram_token")
        self.assertFalse(any(marker.casefold() in serialized.casefold() for marker in forbidden))
        ids = {str(case["id"]) for case in cases}
        self.assertEqual(120, len(ids))
        for case in cases:
            expected = case["expected"]
            payload = valid_payload(
                task_class=expected["task_class"],
                complexity=expected["complexity"],
                freshness=expected["freshness"],
                risk=expected["risk"],
                ambiguity=expected["ambiguity"],
                selected_tier=case["router_seed_tier"],
                reasoning_effort=case["router_seed_effort"],
                reason_codes=expected["reason_codes"],
                fallback_chain=list(canonical_fallback_chain(case["router_seed_tier"])),
            )
            decision = normalize_model_routing_decision(
                payload,
                confidence_threshold=0.75,
                route_bucket=case["input"]["request_route"],
                mutation_capability=bool(case["input"]["mutation_capability"]),
                short_unanchored_followup=bool(case["input"]["short_unanchored_followup"]),
                source_context=bool(case["input"].get("has_reference", False)),
            )
            self.assertGreaterEqual(
                TIER_RANK[decision.selected_tier],
                TIER_RANK[expected["minimum_tier"]],
                case["id"],
            )


if __name__ == "__main__":
    unittest.main()
