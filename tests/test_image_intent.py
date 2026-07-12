import unittest

import image_intent
from image_intent import (
    IMAGE_INTENT_TRUSTED_TEXT_MAX_CHARS,
    ImageIntentDecision,
    ImageOperationAuthorization,
    derive_image_route_policy,
    failed_image_operation_authorization,
    image_intent_schema,
    image_operation_authorizer_schema,
    image_operation_has_deterministic_deny_signal,
    normalize_image_intent_decision,
    normalize_image_intent_routing_mode,
    normalize_image_operation_authorization,
    public_image_scope_is_unsafe,
    public_image_subject_is_sensitive,
    semantic_image_claim_requires_guard,
)


_UNSET = object()


def frame(**overrides):
    data = {
        "intent": "public_web_delivery",
        "target": "current_chat",
        "source_scope": "public_web",
        "subject_grounding": "explicit_current_text",
        "subject_text": "кота в капелюсі",
        "quantity_kind": "singular",
        "quantity_value": 0,
        "language": "ua",
        "execution": "requested_now",
        "confidence": 0.98,
        "reason_codes": ["explicit_delivery", "public_subject"],
    }
    data.update(overrides)
    return data


def decision_for(prompt: str, **overrides) -> ImageIntentDecision:
    return normalize_image_intent_decision(
        frame(**overrides),
        trusted_prompt=prompt,
        confidence_threshold=0.85,
    )


def authorization_payload(
    prompt: str,
    *,
    subject_text: str | None,
    operation_text=_UNSET,
    **overrides,
):
    data = {
        "verdict": "allow",
        "operation": "public_web_delivery",
        "execution": "explicit_now",
        "source": "public_web",
        "destination": "current_chat",
        "operation_text": prompt if operation_text is _UNSET else operation_text,
        "subject_grounding": "exact_current_prompt",
        "subject_text": subject_text,
        "private_or_internal": False,
        "external_source_or_destination": False,
        "reference_dependent": False,
        "meta_or_classifier_test": False,
        "negated": False,
        "confidence": 0.99,
        "reason_codes": ["explicit_current_request", "public_subject"],
    }
    data.update(overrides)
    return data


def authorization_for(
    prompt: str,
    *,
    subject_text: str | None,
    operation_text=_UNSET,
    **overrides,
) -> ImageOperationAuthorization:
    return normalize_image_operation_authorization(
        authorization_payload(
            prompt,
            subject_text=subject_text,
            operation_text=operation_text,
            **overrides,
        ),
        trusted_prompt=prompt,
        confidence_threshold=0.85,
    )


def policy_for(
    prompt: str,
    decision: ImageIntentDecision,
    *,
    authorization: ImageOperationAuthorization | None = None,
    fallback_media_signal: bool = False,
    has_reply_image: bool = False,
    has_external_visual: bool = False,
    has_reply_visual_media: bool = False,
    unsafe_public_scope_signal: bool | None = None,
    deterministic_deny_signal: bool | None = None,
):
    operation_text = authorization.operation_text if authorization is not None else None
    if unsafe_public_scope_signal is None:
        unsafe_public_scope_signal = bool(
            decision.intent == "public_web_delivery"
            and (
                public_image_scope_is_unsafe(operation_text or prompt)
                or public_image_subject_is_sensitive(decision.subject_text)
                or (
                    authorization is not None
                    and public_image_subject_is_sensitive(authorization.subject_text)
                )
            )
        )
    if deterministic_deny_signal is None:
        deterministic_deny_signal = bool(
            decision.intent in {"public_web_delivery", "referenced_visual_analysis"}
            and image_operation_has_deterministic_deny_signal(
                prompt,
                operation_text=operation_text,
            )
        )
    return derive_image_route_policy(
        decision,
        authorization=authorization,
        fallback_media_signal=fallback_media_signal,
        has_reply_image=has_reply_image,
        has_external_visual=has_external_visual,
        has_reply_visual_media=has_reply_visual_media,
        unsafe_public_scope_signal=unsafe_public_scope_signal,
        deterministic_deny_signal=deterministic_deny_signal,
    )


class ImageIntentContractTests(unittest.TestCase):
    def test_schemas_are_strict_and_require_complete_frames(self) -> None:
        for schema in (image_intent_schema(), image_operation_authorizer_schema()):
            with self.subTest(schema=schema):
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertIn("ambiguous", image_intent_schema()["properties"]["intent"]["enum"])
        self.assertIn(
            "allow",
            image_operation_authorizer_schema()["properties"]["verdict"]["enum"],
        )

    def test_mode_contract(self) -> None:
        self.assertEqual("off", normalize_image_intent_routing_mode(None))
        self.assertEqual("shadow", normalize_image_intent_routing_mode("SHADOW"))
        self.assertEqual("enforce", normalize_image_intent_routing_mode("enforce"))
        with self.assertRaises(ValueError):
            normalize_image_intent_routing_mode("active")

    def test_semantic_public_route_does_not_depend_on_noun_candidate_gate(self) -> None:
        prompt = "Покажи кота в капелюсі"
        decision = decision_for(prompt)
        authorization = authorization_for(prompt, subject_text="кота в капелюсі")

        self.assertFalse(hasattr(image_intent, "is_image_semantic_candidate"))
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertEqual("internet_image_send", policy.route)
        self.assertEqual("кота в капелюсі", policy.plan.query)

    def test_ukrainian_public_web_delivery_uses_only_current_prompt_subject(self) -> None:
        prompt = "Покажи фото кота в капелюсі"
        decision = decision_for(prompt)
        authorization = authorization_for(prompt, subject_text="кота в капелюсі")
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertFalse(decision.degraded)
        self.assertFalse(authorization.degraded)
        self.assertEqual("internet_image_send", policy.route)
        self.assertEqual("кота в капелюсі", policy.plan.query)
        self.assertEqual(1, policy.plan.target_count)

    def test_ukrainian_declarative_request_can_produce_album_plan(self) -> None:
        prompt = "Мені потрібні кілька фото нічного Києва"
        decision = decision_for(
            prompt,
            subject_text="нічного Києва",
            quantity_kind="few",
            reason_codes=["declarative_delivery", "public_subject"],
        )
        authorization = authorization_for(prompt, subject_text="нічного Києва")
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertEqual("internet_image_send", policy.route)
        self.assertEqual(3, policy.plan.target_count)
        self.assertEqual("нічного Києва", policy.plan.query)

    def test_exact_count_above_transport_limit_is_naturally_bounded(self) -> None:
        prompt = "Надішли 10 фото котів"
        decision = decision_for(
            prompt,
            subject_text="котів",
            quantity_kind="exact",
            quantity_value=10,
        )
        authorization = authorization_for(prompt, subject_text="котів")
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertEqual("internet_image_send", policy.route)
        self.assertEqual(5, policy.plan.target_count)
        self.assertEqual(10, policy.plan.requested_count)
        self.assertEqual("", policy.response_text)

    def test_subject_must_be_an_exact_span_of_current_prompt(self) -> None:
        decision = decision_for(
            "Знайди її фото",
            subject_text="Ада Лавлейс",
            reason_codes=["unresolved_reference"],
        )

        self.assertTrue(decision.degraded)
        self.assertEqual("invalid_subject_grounding", decision.fallback_reason)

    def test_canonical_unicode_apostrophe_and_newline_spans_remain_exact(self) -> None:
        prompt = "Покажи фото Straße\nО’Коннор"
        decision = decision_for(
            prompt,
            subject_text="STRASSE О'КОННОР",
            language="mixed",
        )
        authorization = authorization_for(
            prompt,
            operation_text="ПОКАЖИ ФОТО STRASSE О'КОННОР",
            subject_text="STRASSE О'КОННОР",
        )
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertFalse(decision.degraded)
        self.assertFalse(authorization.degraded)
        self.assertEqual("Straße\nО’Коннор", decision.subject_text)
        self.assertEqual("Straße\nО’Коннор", authorization.subject_text)
        self.assertEqual(prompt, authorization.operation_text)
        self.assertEqual("internet_image_send", policy.route)

    def test_cyrillic_word_with_one_latin_confusable_still_grounds_exact_prompt_span(self) -> None:
        prompt = "Знайди 3 фото капібар"
        authorization = authorization_for(
            prompt,
            subject_text="капiбар",
        )

        self.assertFalse(authorization.degraded)
        self.assertEqual("капібар", authorization.subject_text)

    def test_reply_text_never_supplies_a_public_search_subject(self) -> None:
        prompt = "Знайди її фото"
        decision = decision_for(
            prompt,
            source_scope="reference",
            subject_grounding="reference_only",
            subject_text=None,
            reason_codes=["unresolved_reference"],
        )
        policy = policy_for(prompt, decision)

        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_private_photos_never_fall_back_to_public_web(self) -> None:
        prompt = "Знайди мої фото з історії чату"
        decision = decision_for(
            prompt,
            intent="private_media_retrieval",
            source_scope="chat_memory",
            subject_grounding="missing",
            subject_text=None,
            quantity_kind="plural_unspecified",
            reason_codes=["private_or_memory_source"],
        )
        policy = policy_for(prompt, decision)

        self.assertEqual("image_source_unavailable", policy.route)
        self.assertIsNone(policy.plan)

    def test_external_destination_never_becomes_current_chat_delivery(self) -> None:
        prompt = "Додай фото кота в Notion"
        decision = decision_for(
            prompt,
            intent="external_media_operation",
            target="other_destination",
            source_scope="external_system",
            subject_grounding="missing",
            subject_text=None,
            reason_codes=["explicit_external_target"],
        )
        policy = policy_for(prompt, decision)

        self.assertEqual("image_source_unavailable", policy.route)
        self.assertIsNone(policy.plan)

    def test_public_intent_with_unspecified_destination_mismatches_allow(self) -> None:
        prompt = "Додай фото кота в чат з Іваном"
        decision = decision_for(prompt, target="unspecified", subject_text="кота")
        authorization = authorization_for(prompt, subject_text="кота")
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertEqual("allow", authorization.verdict)
        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_first_semantic_frame_alone_cannot_authorize_delivery(self) -> None:
        prompt = "Покажи кота"
        decision = decision_for(prompt, subject_text="кота")
        policy = policy_for(prompt, decision, authorization=None)

        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_conflicting_negated_public_frame_is_blocked_by_deterministic_deny(self) -> None:
        prompt = "Не надсилай фото кота"
        decision = decision_for(prompt, subject_text="кота")
        conflicting_allow = authorization_for(prompt, subject_text="кота")
        deterministic_deny = image_operation_has_deterministic_deny_signal(
            prompt,
            operation_text=conflicting_allow.operation_text,
        )
        policy = policy_for(
            prompt,
            decision,
            authorization=conflicting_allow,
            deterministic_deny_signal=deterministic_deny,
        )

        self.assertFalse(conflicting_allow.degraded)
        self.assertTrue(deterministic_deny)
        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_statement_misclassified_by_first_frame_is_denied_by_second(self) -> None:
        prompt = "Photos changed journalism"
        decision = decision_for(
            prompt,
            subject_text="journalism",
            language="en",
        )
        authorization = authorization_for(
            prompt,
            operation_text=None,
            subject_text=None,
            verdict="deny",
            operation="none",
            execution="informational_or_hypothetical",
            source="unspecified",
            destination="none",
            subject_grounding="missing",
            reason_codes=["informational_or_hypothetical"],
        )
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertFalse(authorization.degraded)
        self.assertEqual("deny", authorization.verdict)
        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_external_and_private_conflicts_are_denied_independently(self) -> None:
        cases = (
            (
                "Додай фото кота з Confluence сюди",
                "кота",
                {
                    "source": "external_system",
                    "external_source_or_destination": True,
                    "reason_codes": ["external_source_or_destination"],
                },
            ),
            (
                "Attach photo from Linear ticket SECRET-123 here",
                "SECRET-123",
                {
                    "source": "external_system",
                    "external_source_or_destination": True,
                    "reason_codes": ["external_source_or_destination"],
                },
            ),
            (
                "Знайди фото з мого телефону",
                "мого телефону",
                {
                    "source": "private_or_internal",
                    "private_or_internal": True,
                    "reason_codes": ["private_or_internal_source"],
                },
            ),
            (
                "Знайди мої фото з історії чату",
                "мої фото",
                {
                    "source": "private_or_internal",
                    "private_or_internal": True,
                    "reason_codes": ["private_or_internal_source"],
                },
            ),
            (
                "Find your photos",
                "your photos",
                {
                    "source": "private_or_internal",
                    "private_or_internal": True,
                    "reason_codes": ["private_or_internal_source"],
                },
            ),
        )
        for prompt, subject, deny_overrides in cases:
            with self.subTest(prompt=prompt):
                decision = decision_for(
                    prompt,
                    subject_text=subject,
                    language="en" if prompt.startswith(("Attach", "Find")) else "ua",
                )
                authorization = authorization_for(
                    prompt,
                    operation_text=None,
                    subject_text=None,
                    verdict="deny",
                    operation="none",
                    execution="explicit_now",
                    destination="current_chat",
                    subject_grounding="missing",
                    **deny_overrides,
                )
                policy = policy_for(prompt, decision, authorization=authorization)

                self.assertFalse(authorization.degraded)
                self.assertEqual("deny", authorization.verdict)
                self.assertEqual("image_source_unavailable", policy.route)
                self.assertIsNone(policy.plan)

    def test_possessive_inside_public_subject_is_not_overblocked(self) -> None:
        cases = (
            ("Find photos of my favorite actor", "my favorite actor"),
            ("Find a photo of your favorite actor", "your favorite actor"),
        )
        for prompt, subject in cases:
            with self.subTest(prompt=prompt):
                decision = decision_for(
                    prompt,
                    subject_text=subject,
                    language="en",
                )
                authorization = authorization_for(prompt, subject_text=subject)
                policy = policy_for(prompt, decision, authorization=authorization)

                self.assertFalse(public_image_scope_is_unsafe(prompt))
                self.assertFalse(public_image_scope_is_unsafe(authorization.operation_text))
                self.assertEqual("internet_image_send", policy.route)

    def test_mixed_negative_private_clause_does_not_block_selected_public_clause(self) -> None:
        prompt = "Не надсилай моє фото, а знайди публічне фото Києва"
        operation_text = "знайди публічне фото Києва"
        decision = decision_for(prompt, subject_text="Києва")
        authorization = authorization_for(
            prompt,
            operation_text=operation_text,
            subject_text="Києва",
        )
        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertTrue(public_image_scope_is_unsafe(prompt))
        self.assertFalse(public_image_scope_is_unsafe(operation_text))
        self.assertFalse(
            image_operation_has_deterministic_deny_signal(
                prompt,
                operation_text=operation_text,
            )
        )
        self.assertEqual("internet_image_send", policy.route)
        self.assertEqual("Києва", policy.plan.query)

    def test_nested_semantic_subject_spans_agree_without_losing_richer_query(self) -> None:
        prompt = "Покажи скан першої сторінки Конституції України"
        decision = decision_for(
            prompt,
            subject_text="скан першої сторінки Конституції України",
        )
        authorization = authorization_for(
            prompt,
            subject_text="Конституції України",
        )

        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertEqual("internet_image_send", policy.route)
        self.assertEqual(
            "скан першої сторінки Конституції України",
            policy.plan.query,
        )

    def test_nested_subject_agreement_cannot_bypass_sensitive_identifier_gate(self) -> None:
        prompt = "Покажи схему внутрішнього проєкту ENG-42"
        decision = decision_for(
            prompt,
            subject_text="внутрішнього проєкту ENG-42",
        )
        authorization = authorization_for(
            prompt,
            subject_text="проєкту ENG-42",
        )

        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertTrue(public_image_subject_is_sensitive(decision.subject_text))
        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_authorizer_timeout_invalid_low_confidence_and_mismatch_fail_closed(self) -> None:
        prompt = "Покажи фото кота і собаки"
        decision = decision_for(prompt, subject_text="кота")

        timeout = failed_image_operation_authorization("authorizer_timeout")

        invalid_payload = authorization_payload(prompt, subject_text="кота")
        invalid_payload["unexpected"] = True
        invalid = normalize_image_operation_authorization(
            invalid_payload,
            trusted_prompt=prompt,
            confidence_threshold=0.85,
        )

        low_confidence = authorization_for(
            prompt,
            subject_text="кота",
            confidence=0.61,
        )
        mismatch = authorization_for(prompt, subject_text="собаки")

        for name, authorization in (
            ("timeout", timeout),
            ("invalid", invalid),
            ("low_confidence", low_confidence),
            ("subject_mismatch", mismatch),
        ):
            with self.subTest(name=name):
                policy = policy_for(prompt, decision, authorization=authorization)
                self.assertNotEqual("internet_image_send", policy.route)
                self.assertEqual("image_intent_clarify", policy.route)
                self.assertIsNone(policy.plan)

        self.assertTrue(timeout.degraded)
        self.assertEqual("authorizer_timeout", timeout.fallback_reason)
        self.assertTrue(invalid.degraded)
        self.assertEqual("invalid_payload", invalid.fallback_reason)
        self.assertTrue(low_confidence.degraded)
        self.assertEqual("below_confidence", low_confidence.fallback_reason)
        self.assertFalse(mismatch.degraded)

    def test_authorizer_contradictory_deny_is_marked_for_bounded_retry(self) -> None:
        prompt = "Покажи скан першої сторінки Конституції України"
        authorization = authorization_for(
            prompt,
            subject_text="першої сторінки Конституції України",
            verdict="deny",
        )

        self.assertTrue(authorization.degraded)
        self.assertEqual("conflicting_verdict_fields", authorization.fallback_reason)

    def test_replied_image_analysis_has_a_dedicated_owner(self) -> None:
        prompt = "Що на цьому фото?"
        decision = decision_for(
            prompt,
            intent="referenced_visual_analysis",
            target="none",
            source_scope="reference",
            subject_grounding="reference_only",
            subject_text=None,
            quantity_kind="none",
            reason_codes=["analysis_request", "replied_visual"],
        )
        authorization = authorization_for(
            prompt,
            subject_text=None,
            operation="referenced_visual_analysis",
            source="reference",
            destination="none",
            subject_grounding="reference_only",
            reference_dependent=True,
            reason_codes=["explicit_current_request", "referenced_visual"],
        )
        policy = policy_for(
            prompt,
            decision,
            authorization=authorization,
            has_reply_image=True,
        )

        self.assertFalse(authorization.degraded)
        self.assertEqual("referenced_visual_analysis", policy.route)
        self.assertIsNone(policy.plan)

    def test_real_reply_and_independent_authorizer_canonicalize_subject_leak(self) -> None:
        prompt = "Опиши це фото"
        decision = decision_for(
            prompt,
            intent="referenced_visual_analysis",
            target="none",
            source_scope="reference",
            subject_grounding="explicit_current_text",
            subject_text="це фото",
            quantity_kind="none",
            reason_codes=["analysis_request", "replied_visual"],
        )
        authorization = authorization_for(
            prompt,
            subject_text=None,
            operation="referenced_visual_analysis",
            source="reference",
            destination="none",
            subject_grounding="reference_only",
            reference_dependent=True,
            reason_codes=["explicit_current_request", "referenced_visual"],
        )

        policy = policy_for(
            prompt,
            decision,
            authorization=authorization,
            has_reply_image=True,
        )

        self.assertFalse(decision.degraded)
        self.assertEqual("referenced_visual_analysis", policy.route)

    def test_subject_leak_is_not_canonicalized_without_real_reply_metadata(self) -> None:
        prompt = "Опиши це фото"
        decision = decision_for(
            prompt,
            intent="referenced_visual_analysis",
            target="none",
            source_scope="reference",
            subject_grounding="explicit_current_text",
            subject_text="це фото",
            quantity_kind="none",
            reason_codes=["analysis_request", "unresolved_reference"],
        )
        authorization = authorization_for(
            prompt,
            subject_text=None,
            operation="referenced_visual_analysis",
            source="reference",
            destination="none",
            subject_grounding="reference_only",
            reference_dependent=True,
            reason_codes=["explicit_current_request", "referenced_visual"],
        )

        policy = policy_for(prompt, decision, authorization=authorization)

        self.assertEqual("image_intent_clarify", policy.route)

    def test_replied_video_gets_accurate_unavailable_route(self) -> None:
        prompt = "Опиши це відео"
        decision = decision_for(
            prompt,
            intent="referenced_visual_analysis",
            target="none",
            source_scope="reference",
            subject_grounding="reference_only",
            subject_text=None,
            quantity_kind="none",
            reason_codes=["analysis_request", "replied_visual"],
        )
        authorization = authorization_for(
            prompt,
            subject_text=None,
            operation="referenced_visual_analysis",
            source="reference",
            destination="none",
            subject_grounding="reference_only",
            reference_dependent=True,
            reason_codes=["explicit_current_request", "referenced_visual"],
        )
        policy = policy_for(
            prompt,
            decision,
            authorization=authorization,
            has_reply_visual_media=True,
        )

        self.assertEqual("referenced_visual_unavailable", policy.route)
        self.assertIn("відео", policy.response_text)

    def test_negated_referenced_analysis_is_blocked_deterministically(self) -> None:
        prompt = "Не аналізуй це фото"
        decision = decision_for(
            prompt,
            intent="referenced_visual_analysis",
            target="none",
            source_scope="reference",
            subject_grounding="reference_only",
            subject_text=None,
            quantity_kind="none",
            reason_codes=["analysis_request", "replied_visual"],
        )
        conflicting_allow = authorization_for(
            prompt,
            subject_text=None,
            operation="referenced_visual_analysis",
            source="reference",
            destination="none",
            subject_grounding="reference_only",
            reference_dependent=True,
            reason_codes=["explicit_current_request", "referenced_visual"],
        )
        deterministic_deny = image_operation_has_deterministic_deny_signal(
            prompt,
            operation_text=conflicting_allow.operation_text,
        )
        policy = policy_for(
            prompt,
            decision,
            authorization=conflicting_allow,
            has_reply_image=True,
            deterministic_deny_signal=deterministic_deny,
        )

        self.assertTrue(deterministic_deny)
        self.assertEqual("image_intent_clarify", policy.route)
        self.assertNotEqual("referenced_visual_analysis", policy.route)

    def test_nonexecuting_private_external_redelivery_and_similarity_are_normal(self) -> None:
        templates = {
            "private_media_retrieval": {
                "target": "current_chat",
                "source_scope": "chat_memory",
                "subject_grounding": "missing",
                "subject_text": None,
                "quantity_kind": "none",
                "reason_codes": ["private_or_memory_source"],
            },
            "external_media_operation": {
                "target": "other_destination",
                "source_scope": "external_system",
                "subject_grounding": "missing",
                "subject_text": None,
                "quantity_kind": "none",
                "reason_codes": ["explicit_external_target"],
            },
            "referenced_visual_redelivery": {
                "target": "current_chat",
                "source_scope": "reference",
                "subject_grounding": "reference_only",
                "subject_text": None,
                "quantity_kind": "none",
                "reason_codes": ["redelivery_request", "replied_visual"],
            },
            "referenced_visual_similarity": {
                "target": "current_chat",
                "source_scope": "reference",
                "subject_grounding": "reference_only",
                "subject_text": None,
                "quantity_kind": "none",
                "reason_codes": ["similarity_request", "replied_visual"],
            },
        }
        for intent, template in templates.items():
            for execution in ("not_requested", "negated"):
                with self.subTest(intent=intent, execution=execution):
                    prompt = f"Контекст без поточної операції: {intent}"
                    reason_codes = list(template["reason_codes"])
                    if execution == "negated":
                        reason_codes = ["negated_operation"]
                    decision = decision_for(
                        prompt,
                        intent=intent,
                        execution=execution,
                        **{**template, "reason_codes": reason_codes},
                    )
                    policy = policy_for(
                        prompt,
                        decision,
                        has_reply_image=intent.startswith("referenced_"),
                    )

                    self.assertEqual("normal", policy.route)
                    self.assertTrue(policy.guard_unconfirmed_delivery)
                    self.assertIsNone(policy.plan)

    def test_informational_question_has_no_side_effect_route(self) -> None:
        prompt = "Як додати фото до повідомлення?"
        decision = decision_for(
            prompt,
            intent="image_information",
            target="none",
            source_scope="unspecified",
            subject_grounding="missing",
            subject_text=None,
            quantity_kind="none",
            reason_codes=["informational_question"],
            execution="not_requested",
        )
        policy = policy_for(prompt, decision)

        self.assertEqual("normal", policy.route)
        self.assertIsNone(policy.plan)

    def test_low_first_frame_confidence_fails_closed_without_side_effect(self) -> None:
        prompt = "скинь фотку котика"
        decision = decision_for(prompt, subject_text="котика", confidence=0.62)
        policy = policy_for(prompt, decision)

        self.assertTrue(decision.degraded)
        self.assertEqual("image_intent_clarify", policy.route)
        self.assertIsNone(policy.plan)

    def test_semantic_postcondition_recovers_false_negative_without_lexical_authority(self) -> None:
        prompt = "Покажи кота в капелюсі"
        false_negative = decision_for(
            prompt,
            intent="not_image",
            target="none",
            source_scope="unspecified",
            subject_grounding="missing",
            subject_text=None,
            quantity_kind="none",
            execution="not_requested",
            reason_codes=["non_delivery_statement"],
        )
        independent_visual_request = authorization_for(
            prompt,
            subject_text="кота в капелюсі",
        )

        self.assertTrue(
            semantic_image_claim_requires_guard(
                false_negative,
                independent_visual_request,
            )
        )

    def test_semantic_postcondition_preserves_confirmed_nonvisual_completion(self) -> None:
        prompt = "Додай приклад до пояснення"
        not_image = decision_for(
            prompt,
            intent="not_image",
            target="none",
            source_scope="unspecified",
            subject_grounding="missing",
            subject_text=None,
            quantity_kind="none",
            execution="not_requested",
            reason_codes=["non_delivery_statement"],
        )
        nonvisual = authorization_for(
            prompt,
            subject_text=None,
            operation_text=None,
            verdict="deny",
            operation="none",
            execution="informational_or_hypothetical",
            source="unspecified",
            destination="none",
            subject_grounding="missing",
            reason_codes=["informational_or_hypothetical"],
        )

        self.assertFalse(semantic_image_claim_requires_guard(not_image, nonvisual))

    def test_semantic_postcondition_fails_closed_when_verifier_is_degraded(self) -> None:
        prompt = "Покажи кота в капелюсі"
        low_confidence = decision_for(prompt, subject_text="кота в капелюсі", confidence=0.2)

        self.assertTrue(
            semantic_image_claim_requires_guard(
                low_confidence,
                failed_image_operation_authorization("authorizer_failed"),
            )
        )

    def test_semantic_router_text_bound_is_explicit(self) -> None:
        self.assertEqual(2500, IMAGE_INTENT_TRUSTED_TEXT_MAX_CHARS)

    def test_model_cannot_add_an_allow_send_field(self) -> None:
        payload = frame()
        payload["allow_send"] = True
        decision = normalize_image_intent_decision(
            payload,
            trusted_prompt="Покажи фото кота в капелюсі",
            confidence_threshold=0.85,
        )

        self.assertTrue(decision.degraded)
        self.assertEqual("invalid_payload", decision.fallback_reason)


if __name__ == "__main__":
    unittest.main()
