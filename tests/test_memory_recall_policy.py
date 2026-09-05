from __future__ import annotations

import unittest

from memory_recall import (
    RecallSemanticGuard,
    evaluate_recall_admission,
    normalize_recall_policy_mode,
    recall_semantic_guard,
    resolve_recall_rollout,
)


class RecallAdmissionPolicyTests(unittest.TestCase):
    def test_unknown_text_defers_without_invented_history(self) -> None:
        self.assertEqual("defer", recall_semantic_guard("synthetic request").verdict)

    def test_multilingual_recall_needs_request_and_prior_shared_evidence(self) -> None:
        cases = (
            "Яку модель принтера ми обрали минулого місяця?",
            "Какую модель принтера мы выбрали в прошлый раз?",
            "Which printer did we choose last month?",
            "Знайди в нашому листуванні адресу складу.",
            "Найди в нашей переписке адрес склада.",
            "Find the warehouse address in our conversation.",
            "Що ти раніше радив для утеплення?",
            "Что ты раньше советовал для утепления?",
            "What did you recommend for insulation?",
            "Пошукай старе повідомлення зі словами «ранкова зірка».",
            "Поищи старое сообщение со словами «утренняя звезда».",
            "Find the old message containing 'morning star'.",
            "Хто тут надсилав креслення минулого тижня?",
            "Кто сюда присылал чертёж на прошлой неделе?",
            "Who shared the drawing here last week?",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", recall_semantic_guard(prompt).verdict)

    def test_quoted_instructions_do_not_override_transformation(self) -> None:
        for prompt in (
            "Перефразуй «Що ми обрали раніше?» коротше.",
            "Перефразируй «Что мы выбрали раньше?» короче.",
            "Rephrase 'What did we choose earlier?' more briefly.",
            "Translate: Find the old chat message.",
            "«Знайди, що ми раніше вирішили»",
            '"Find what we agreed earlier"',
            "```Find what we agreed earlier```",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual("veto", recall_semantic_guard(prompt).verdict)

    def test_negated_history_stays_normal_even_with_high_cosine(self) -> None:
        for prompt in (
            "Не шукай попередню розмову. Порахуй площу.",
            "Не ищи предыдущую переписку. Посчитай площадь.",
            "Don't search earlier messages. Calculate the area.",
            "Попередні повідомлення не потрібні. Поясни вкладення.",
            "Старые сообщения не нужны. Объясни вложение.",
            "Earlier messages are not needed. Explain the attachment.",
            "Не використовуй пам'ять. Дай загальне правило.",
            "Не используй историю. Дай общее правило.",
            "Don’t use memory. Give the general rule.",
        ):
            with self.subTest(prompt=prompt):
                guard = recall_semantic_guard(prompt)
                self.assertEqual("veto", guard.verdict)
                self.assertFalse(self.evaluate(confidence=0.99, guard=guard).is_recall)

    def test_reminders_new_facts_and_general_refreshers_are_not_recall(self) -> None:
        for prompt in (
            "Нагадай мені завтра перевірити батареї.",
            "Напомни мне завтра проверить батареи.",
            "Remind me tomorrow to check the batteries.",
            "Запам'ятай: у вигаданому прикладі коробка важить два кілограми.",
            "Запомни: в вымышленном примере коробка весит два килограмма.",
            "Remember this: in the fictional example the box weighs two kilograms.",
            "Нагадай, як ділити дроби.",
            "Напомни, как делить дроби.",
            "Remind me how to divide fractions.",
            "Поясни, як працює оперативна пам'ять.",
            "Расскажи историю микрофонов.",
            "How do I clear my browser history?",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual("veto", recall_semantic_guard(prompt).verdict)

    def test_current_reference_does_not_become_old_history(self) -> None:
        for prompt in ("Поясни цей абзац.", "Объясни этот абзац.", "Explain this paragraph."):
            with self.subTest(prompt=prompt):
                self.assertEqual("veto", recall_semantic_guard(prompt, has_reference=True).verdict)
        self.assertEqual(
            "allow", recall_semantic_guard("What had we agreed about this before?", has_reference=True).verdict,
        )

    def test_unanchored_followup_is_not_expanded_recall(self) -> None:
        for prompt in ("А скільки?", "А сколько?", "And how many?"):
            with self.subTest(prompt=prompt):
                self.assertEqual("veto", recall_semantic_guard(prompt).verdict)

    def test_quote_payload_and_non_memory_negation_do_not_control_recall(self) -> None:
        self.assertEqual(
            "allow", recall_semantic_guard("Find the old message with 'Do not search earlier messages'.").verdict,
        )
        self.assertEqual(
            "allow", recall_semantic_guard("Don't search the web. What did we agree earlier?").verdict,
        )
        self.assertEqual("defer", recall_semantic_guard("Ми завтра домовимося про час.").verdict)

    def test_transforming_an_unsupplied_prior_decision_requires_recall(self) -> None:
        for prompt in (
            "Перефразуй те, що ми раніше вирішили в чаті про доставку.",
            "Перефразируй то, что мы раньше решили в чате про доставку.",
            "Rephrase what we previously agreed in the chat about delivery.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", recall_semantic_guard(prompt).verdict)

    def test_negation_of_topic_details_does_not_negate_recall(self) -> None:
        for prompt in (
            "Що ми раніше вирішили без участі замовника?",
            "Что мы раньше решили без участия заказчика?",
            "What did we decide earlier without the customer's participation?",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", recall_semantic_guard(prompt).verdict)

    def test_addressing_the_assistant_and_old_topic_are_not_shared_history(self) -> None:
        for prompt in (
            "Можеш порадити мені старий фільм?",
            "Можешь посоветовать мне старый фильм?",
            "Can you recommend an old film?",
            "How do we restore old paint?",
        ):
            with self.subTest(prompt=prompt):
                self.assertNotEqual("allow", recall_semantic_guard(prompt).verdict)

    def test_new_fact_about_a_prior_event_is_still_an_instruction_to_remember(self) -> None:
        for prompt in (
            "Запам'ятай: ми раніше погодили синю обкладинку.",
            "Запомни: мы раньше согласовали синюю обложку.",
            "Remember that: we earlier agreed on the blue cover.",
        ):
            with self.subTest(prompt=prompt):
                guard = recall_semantic_guard(prompt)
                self.assertEqual("new_memory_statement", guard.reason)
                self.assertFalse(self.evaluate(confidence=0.99, guard=guard).is_recall)

    def evaluate(self, **changes):
        values = {
            "confidence": 0.3,
            "guard": RecallSemanticGuard("defer", "no_explicit_intent"),
            "threshold": 0.62,
            "ambiguous_threshold": 0.48,
            "has_context_hint": False,
        }
        values.update(changes)
        return evaluate_recall_admission(**values)

    def test_explicit_guard_preserves_observed_confidence(self) -> None:
        decision = self.evaluate(guard=RecallSemanticGuard("allow", "prior_shared_conversation"))
        self.assertTrue(decision.is_recall)
        self.assertEqual(0.3, decision.confidence)
        self.assertEqual("prior_shared_conversation", decision.reason)
        self.assertFalse(decision.degraded)

    def test_veto_is_not_overridden_by_high_similarity(self) -> None:
        decision = self.evaluate(
            confidence=0.99,
            has_context_hint=True,
            guard=RecallSemanticGuard("veto", "explicit_non_recall"),
        )
        self.assertFalse(decision.is_recall)
        self.assertEqual(0.99, decision.confidence)
        self.assertEqual("explicit_non_recall", decision.reason)

    def test_defer_uses_exact_shared_score_and_threshold_boundaries(self) -> None:
        for score, hint, expected in (
            (0.479999, True, False),
            (0.48, False, False),
            (0.48, True, True),
            (0.619999, False, False),
            (0.62, False, True),
            (-0.1, True, False),
        ):
            with self.subTest(score=score, hint=hint):
                decision = self.evaluate(confidence=score, has_context_hint=hint)
                self.assertEqual(expected, decision.is_recall)
                self.assertEqual(score, decision.confidence)

    def test_degraded_observation_only_admits_explicit_evidence(self) -> None:
        for verdict, expected in (("allow", True), ("veto", False), ("defer", False)):
            with self.subTest(verdict=verdict):
                decision = self.evaluate(
                    confidence=0.99,
                    guard=RecallSemanticGuard(verdict, "synthetic_guard"),
                    has_context_hint=True,
                    degraded=True,
                )
                self.assertEqual(expected, decision.is_recall)
                self.assertEqual(0.99, decision.confidence)
                self.assertTrue(decision.degraded)

    def test_invalid_scores_and_thresholds_fail_for_adapter_fallback(self) -> None:
        for changes in (
            {"confidence": float("nan")},
            {"confidence": float("inf")},
            {"threshold": float("nan")},
            {"threshold": 1.1},
            {"ambiguous_threshold": -0.1},
            {"ambiguous_threshold": 0.7},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.evaluate(**changes)
        with self.assertRaises(ValueError):
            RecallSemanticGuard("unknown", "invalid")


class RecallRolloutPolicyTests(unittest.TestCase):
    def test_mode_defaults_off_and_rejects_unknown_values(self) -> None:
        self.assertEqual("off", normalize_recall_policy_mode(None))
        self.assertEqual("off", normalize_recall_policy_mode(""))
        self.assertEqual("shadow", normalize_recall_policy_mode(" SHADOW "))
        self.assertEqual("enforce", normalize_recall_policy_mode("enforce"))
        with self.assertRaisesRegex(ValueError, "MEMORY_RECALL_POLICY_MODE"):
            normalize_recall_policy_mode("active")

    def test_off_ignores_candidate_and_candidate_failure(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                result = resolve_recall_rollout(
                    mode="off", legacy_is_recall=legacy,
                    candidate_is_recall=not legacy, candidate_failed=True,
                )
                self.assertEqual(legacy, result.applied_is_recall)
                self.assertEqual("legacy", result.applied_source)
                self.assertIsNone(result.candidate_is_recall)
                self.assertIsNone(result.differs)
                self.assertEqual("", result.fallback_reason)

    def test_shadow_compares_both_directions_but_always_applies_legacy(self) -> None:
        for legacy in (False, True):
            for candidate in (False, True):
                with self.subTest(legacy=legacy, candidate=candidate):
                    result = resolve_recall_rollout(
                        mode="shadow", legacy_is_recall=legacy, candidate_is_recall=candidate,
                    )
                    self.assertEqual(legacy, result.applied_is_recall)
                    self.assertEqual("legacy", result.applied_source)
                    self.assertEqual(candidate, result.candidate_is_recall)
                    self.assertEqual(legacy != candidate, result.differs)

    def test_enforce_applies_both_positive_and_negative_candidate_decisions(self) -> None:
        for candidate in (False, True):
            with self.subTest(candidate=candidate):
                result = resolve_recall_rollout(
                    mode="enforce", legacy_is_recall=not candidate, candidate_is_recall=candidate,
                )
                self.assertEqual(candidate, result.applied_is_recall)
                self.assertEqual("candidate", result.applied_source)
                self.assertTrue(result.differs)

    def test_candidate_missing_or_failed_preserves_legacy_in_enabled_modes(self) -> None:
        for mode in ("shadow", "enforce"):
            for legacy in (False, True):
                for failed in (False, True):
                    with self.subTest(mode=mode, legacy=legacy, failed=failed):
                        result = resolve_recall_rollout(
                            mode=mode, legacy_is_recall=legacy,
                            candidate_is_recall=not legacy if failed else None,
                            candidate_failed=failed,
                        )
                        self.assertEqual(legacy, result.applied_is_recall)
                        self.assertEqual("legacy", result.applied_source)
                        self.assertIsNone(result.differs)
                        self.assertIsNone(result.candidate_is_recall)
                        self.assertEqual(
                            "candidate_failed" if failed else "candidate_missing", result.fallback_reason,
                        )


if __name__ == "__main__":
    unittest.main()
