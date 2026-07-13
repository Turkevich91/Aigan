import asyncio
import io
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from PIL import Image

from tests.support import FakeMessage, VALID_JPEG, configure_test_environment


configure_test_environment()
import main


class ImageDeliveryPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_config = main.CONFIG
        main.CONFIG = replace(
            main.CONFIG,
            image_candidate_review_enabled=True,
            image_candidate_review_model="gpt-5.4-mini",
            image_candidate_review_timeout_seconds=30,
        )
        if main.SYSTEM_LOG is not None:
            main.SYSTEM_LOG.clear_all()
        if main.MEMORY is not None:
            main.MEMORY.clear_all()

    def tearDown(self) -> None:
        main.CONFIG = self.original_config
        if main.SYSTEM_LOG is not None:
            main.SYSTEM_LOG.clear_all()
        if main.MEMORY is not None:
            main.MEMORY.clear_all()

    @staticmethod
    def image(
        perceptual_hash: str = "",
        *,
        data: bytes = VALID_JPEG,
    ) -> main.WebImageResult:
        return main.WebImageResult(
            data=data,
            mime_type="image/jpeg",
            source_url="https://synthetic.invalid/private-source",
            source_title="UNTRUSTED SOURCE TITLE",
            final_url="https://synthetic.invalid/private-image.jpg",
            perceptual_hash=perceptual_hash,
        )

    @staticmethod
    def accepted_review(index: int = 0, *, score: int = 95) -> main.ImageCandidateReview:
        return main.ImageCandidateReview(
            index=index,
            relevant=True,
            direct_depiction=True,
            safe_for_group=True,
            quality="good",
            duplicate_of_index=None,
            confidence=0.99,
            score=score,
            reject_reason="none",
            description_uk="Справжній кіт на нейтральному тлі.",
        )

    @staticmethod
    def synthetic_jpeg(color: tuple[int, int, int]) -> bytes:
        stream = io.BytesIO()
        Image.new("RGB", (16, 16), color).save(stream, format="JPEG", quality=95)
        return stream.getvalue()

    def test_review_request_is_low_detail_and_omits_source_metadata(self) -> None:
        response = SimpleNamespace(
            output_text=json.dumps(
                {
                    "reviews": [
                        {
                            "index": 0,
                            "relevant": True,
                            "direct_depiction": True,
                            "safe_for_group": True,
                            "quality": "good",
                            "duplicate_of_index": None,
                            "confidence": 0.97,
                            "score": 95,
                            "reject_reason": "none",
                            "description_uk": "Синтетичний предмет на нейтральному тлі.",
                        }
                    ]
                }
            ),
            status="completed",
            model="gpt-5.4-mini-2026-03-17",
            reasoning=SimpleNamespace(effort="none"),
            usage=None,
        )

        with patch.object(main, "AsyncOpenAI") as client_class:
            client = client_class.return_value.__aenter__.return_value
            client.responses.create = AsyncMock(return_value=response)
            reviews = asyncio.run(
                main.run_image_candidate_review(
                    "синтетичний предмет",
                    [self.image()],
                    run_id="a" * 32,
                )
            )
            request = client.responses.create.await_args.kwargs

        serialized_input = json.dumps(request["input"], ensure_ascii=False)
        image_parts = [
            part
            for item in request["input"]
            for part in item["content"]
            if part.get("type") == "input_image"
        ]
        self.assertEqual(1, len(reviews))
        self.assertEqual(["low"], [part["detail"] for part in image_parts])
        self.assertNotIn("UNTRUSTED SOURCE TITLE", serialized_input)
        self.assertNotIn("synthetic.invalid", serialized_input)
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertEqual("gpt-5.4-mini", request["model"])
        self.assertEqual({"effort": "none"}, request["reasoning"])
        self.assertEqual(0, client_class.call_args.kwargs["max_retries"])

    def test_candidate_validation_checks_structure_dimensions_and_rejects_gif(self) -> None:
        self.assertEqual(
            "image/jpeg",
            main.validate_image_bytes(VALID_JPEG, "image/jpeg", 2_000_000),
        )
        with self.assertRaises(ValueError):
            main.validate_image_bytes(b"\xff\xd8\xffbroken", "image/jpeg", 2_000_000)
        with self.assertRaises(ValueError):
            main.validate_image_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x01" * 2,
                "image/png",
                2_000_000,
            )
        with self.assertRaises(ValueError):
            main.validate_image_bytes(
                b"GIF89a\x01\x00\x01\x00" + b"\x00" * 32,
                "image/gif",
                2_000_000,
            )

    def test_oversized_pixel_dimensions_are_rejected_before_decode(self) -> None:
        oversized = MagicMock()
        oversized.__enter__.return_value = oversized
        oversized.__exit__.return_value = None
        oversized.size = (8_000, 6_000)

        with patch.object(main.Image, "open", return_value=oversized) as image_open:
            with self.assertRaisesRegex(ValueError, "pixel count"):
                main.decoded_image_properties(b"synthetic", "image/png")

        image_open.assert_called_once()
        oversized.verify.assert_not_called()
        oversized.load.assert_not_called()

    def test_config_rejects_group_or_non_admin_operator_destination(self) -> None:
        with patch.dict(
            os.environ,
            {"OPERATOR_ALERTS_ENABLED": "true", "OPERATOR_ALERT_CHAT_ID": "-1001"},
        ):
            with self.assertRaisesRegex(RuntimeError, "positive private"):
                main.Config.from_env()
        with patch.dict(
            os.environ,
            {"OPERATOR_ALERTS_ENABLED": "true", "OPERATOR_ALERT_CHAT_ID": "123"},
        ):
            with self.assertRaisesRegex(RuntimeError, "ADMIN_USER_IDS"):
                main.Config.from_env()

    def test_review_failure_retries_twice_then_fails_closed_and_alerts(self) -> None:
        message = FakeMessage("покажи фото кота", message_id=820)
        provenance = main.provenance_for_message(message, "internet_image_send")

        with patch.object(
            main,
            "run_image_candidate_review",
            new=AsyncMock(side_effect=RuntimeError("synthetic failure")),
        ) as run_review:
            with patch.object(
                main,
                "notify_operator_alert",
                new=AsyncMock(return_value="sent"),
            ) as notify:
                result = asyncio.run(
                    main.review_web_image_candidates(
                        message,
                        "кота",
                        [self.image()],
                        provenance,
                    )
                )

        self.assertFalse(result.succeeded)
        self.assertEqual((), result.images)
        self.assertEqual(2, run_review.await_count)
        notify.assert_awaited_once_with(
            message,
            "image_candidate_review_failed",
            {"candidate_count": 1, "attempts": 2},
        )

    def test_disabled_review_blocks_web_delivery_instead_of_bypassing_safety(self) -> None:
        main.CONFIG = replace(main.CONFIG, image_candidate_review_enabled=False)
        message = FakeMessage("покажи фото кота", message_id=822)

        with patch.object(
            main,
            "notify_operator_alert",
            new=AsyncMock(return_value="sent"),
        ) as notify:
            result = asyncio.run(
                main.review_web_image_candidates(message, "кота", [self.image()], None)
            )

        self.assertFalse(result.succeeded)
        self.assertEqual((), result.images)
        notify.assert_awaited_once_with(
            message,
            "image_candidate_review_failed",
            {"candidate_count": 1, "attempts": 0},
        )

    def test_failed_batch_isolates_poisoned_candidate_and_keeps_good_image(self) -> None:
        message = FakeMessage("покажи фото кота", message_id=823)
        good = self.image(
            "0000000000000000",
            data=self.synthetic_jpeg((255, 0, 0)),
        )
        poisoned = self.image(
            "ffffffffffffffff",
            data=self.synthetic_jpeg((0, 0, 255)),
        )

        with patch.object(
            main,
            "run_image_candidate_review",
            new=AsyncMock(
                side_effect=[
                    RuntimeError("batch rejected"),
                    (self.accepted_review(),),
                    RuntimeError("candidate rejected"),
                ]
            ),
        ) as run_review:
            result = asyncio.run(
                main.review_web_image_candidates(
                    message,
                    "кота",
                    [good, poisoned],
                    None,
                    target_count=2,
                )
            )

        self.assertTrue(result.succeeded)
        self.assertEqual((good,), result.images)
        self.assertEqual(3, run_review.await_count)

    def test_review_batches_respect_cumulative_raw_byte_limit(self) -> None:
        main.CONFIG = replace(
            main.CONFIG,
            image_candidate_review_max_total_bytes=len(VALID_JPEG) + 1,
        )
        message = FakeMessage("покажи фото котів", message_id=824)
        images = [
            self.image(
                f"{index * 0x5555555555555555:016x}",
                data=self.synthetic_jpeg(color),
            )
            for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255)))
        ]

        with patch.object(
            main,
            "run_image_candidate_review",
            new=AsyncMock(return_value=(self.accepted_review(),)),
        ) as run_review:
            result = asyncio.run(
                main.review_web_image_candidates(
                    message,
                    "котів",
                    images,
                    None,
                    target_count=3,
                )
            )

        self.assertTrue(result.succeeded)
        self.assertEqual(3, result.accepted_count)
        self.assertEqual(3, run_review.await_count)
        self.assertTrue(
            all(len(call.args[1]) == 1 for call in run_review.await_args_list)
        )

    def test_review_globally_ranks_all_batches_and_keeps_single_image_reserves(self) -> None:
        images = [
            self.image(
                f"{index * 0x5555555555555555:016x}",
                data=self.synthetic_jpeg(color),
            )
            for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255)))
        ]
        main.CONFIG = replace(
            main.CONFIG,
            image_candidate_review_max_total_bytes=max(len(image.data) for image in images) + 1,
        )
        message = FakeMessage("покажи фото кота", message_id=827)

        with patch.object(
            main,
            "run_image_candidate_review",
            new=AsyncMock(
                side_effect=[
                    (self.accepted_review(score=20),),
                    (self.accepted_review(score=99),),
                    (self.accepted_review(score=60),),
                ]
            ),
        ) as run_review:
            result = asyncio.run(
                main.review_web_image_candidates(
                    message,
                    "кота",
                    images,
                    None,
                    target_count=1,
                )
            )

        self.assertEqual(3, run_review.await_count)
        self.assertEqual((images[1], images[2], images[0]), result.images)

    def test_mixed_review_failure_is_degraded_and_alerted_when_target_unmet(self) -> None:
        images = [
            self.image(
                f"{index * 0x5555555555555555:016x}",
                data=self.synthetic_jpeg(color),
            )
            for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255)))
        ]
        main.CONFIG = replace(
            main.CONFIG,
            image_candidate_review_max_total_bytes=max(len(image.data) for image in images) + 1,
        )
        message = FakeMessage("покажи 3 фото котів", message_id=828)

        with patch.object(
            main,
            "run_image_candidate_review",
            new=AsyncMock(
                side_effect=[
                    (self.accepted_review(),),
                    RuntimeError("batch failed"),
                    RuntimeError("isolated failed"),
                    (self.accepted_review(),),
                ]
            ),
        ):
            with patch.object(
                main,
                "notify_operator_alert",
                new=AsyncMock(return_value="sent"),
            ) as notify:
                result = asyncio.run(
                    main.review_web_image_candidates(
                        message,
                        "котів",
                        images,
                        None,
                        target_count=3,
                    )
                )

        self.assertTrue(result.succeeded)
        self.assertTrue(result.degraded)
        self.assertEqual(1, result.unreviewed_count)
        self.assertEqual(2, result.accepted_count)
        notify.assert_awaited_once_with(
            message,
            "image_candidate_review_failed",
            {"candidate_count": 3, "attempts": 4},
        )

    def test_cross_batch_perceptual_duplicate_is_reviewed_once(self) -> None:
        first = self.image(
            "0000000000000000",
            data=self.synthetic_jpeg((255, 0, 0)),
        )
        near_duplicate = self.image(
            "0000000000000003",
            data=self.synthetic_jpeg((254, 0, 0)),
        )
        message = FakeMessage("покажи фото кота", message_id=829)

        with patch.object(
            main,
            "run_image_candidate_review",
            new=AsyncMock(return_value=(self.accepted_review(),)),
        ) as run_review:
            result = asyncio.run(
                main.review_web_image_candidates(
                    message,
                    "кота",
                    [first, near_duplicate],
                    None,
                    target_count=1,
                )
            )

        self.assertEqual(1, run_review.await_count)
        self.assertEqual(1, result.duplicate_count)
        self.assertEqual((first,), result.images)

    def test_fetch_pool_uses_bounded_concurrency_and_preserves_order(self) -> None:
        main.CONFIG = replace(main.CONFIG, web_image_fetch_concurrency=2)
        candidates = [{"image": str(index)} for index in range(6)]
        active = 0
        max_active = 0

        async def load(candidate, outbound_provenance):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return self.image(perceptual_hash=f"{int(candidate['image']):016x}")

        with patch.object(main, "load_web_image_result", new=load):
            loaded = asyncio.run(main.load_web_image_results(candidates, None))

        self.assertEqual(6, len(loaded))
        self.assertEqual(2, max_active)
        self.assertEqual(
            [f"{index:016x}" for index in range(6)],
            [image.perceptual_hash for image in loaded],
        )

    def test_overall_pipeline_deadline_stops_presence_and_alerts_privately(self) -> None:
        main.CONFIG = replace(main.CONFIG, web_image_pipeline_timeout_seconds=0.01)
        message = FakeMessage("покажи фото кота", message_id=830)
        plan = main.ImageDeliveryPlan(
            query="кота",
            target_count=1,
            requested_count=1,
            quantity_kind="singular",
        )

        async def slow_prepare(*args, **kwargs):
            await asyncio.sleep(1)

        with patch.object(main, "prepare_web_image_review", new=slow_prepare):
            with patch.object(
                main,
                "notify_operator_alert",
                new=AsyncMock(return_value="sent"),
            ) as notify:
                outcome = asyncio.run(
                    main.maybe_send_internet_image(message, message.text, plan=plan)
                )

        self.assertFalse(outcome.confirmed_delivery)
        notify.assert_awaited_once_with(
            message,
            "image_pipeline_timeout",
            {"target_count": 1, "timeout_seconds": 0},
        )
        reply = message.reply_calls[0]["text"]
        self.assertIn("забарився", reply)
        self.assertNotIn("Telegram", reply)

    def test_partial_transport_notice_is_private_only(self) -> None:
        message = FakeMessage("album", message_id=821)

        with patch.object(
            main,
            "notify_operator_alert",
            new=AsyncMock(return_value="sent"),
        ) as notify:
            asyncio.run(
                main.send_image_delivery_outcome_notice(
                    message,
                    confirmed_count=1,
                    intended_count=5,
                    ambiguous=False,
                )
            )

        self.assertEqual([], message.reply_calls)
        notify.assert_awaited_once_with(
            message,
            "image_delivery_partial",
            {"confirmed_parts": 1, "intended_parts": 5},
        )

    def test_memory_disabled_is_not_counted_as_persistence_failure(self) -> None:
        message = FakeMessage("покажи фото кота", message_id=825)
        delivered = SimpleNamespace(
            message_id=30_825,
            chat=message.chat,
            date=None,
        )
        provenance = main.new_outbound_provenance(
            chat_id=message.chat_id,
            route="internet_image_send",
            trigger_message_id=message.message_id,
        )

        with patch.object(main, "MEMORY", None):
            result = main.save_sent_web_images(
                message,
                [main.DeliveredWebImage(self.image(), delivered)],
                provenance,
                intended_count=1,
            )

        self.assertEqual((), result.item_ids)
        self.assertEqual(0, result.failed_parts)

    def test_real_cache_failure_is_reported_privately_after_delivery(self) -> None:
        message = FakeMessage("покажи фото кота", message_id=826)
        plan = main.ImageDeliveryPlan(
            query="кота",
            target_count=1,
            requested_count=1,
            quantity_kind="singular",
        )
        search_batch = SimpleNamespace(
            candidates=(
                {
                    "title": "Real cat",
                    "image": "https://example.com/cat.jpg",
                    "source": "https://example.com/cat",
                },
            ),
            raw_count=1,
            url_rejected_count=0,
            duplicate_count=0,
            search_passes=1,
            provider_failures=0,
        )
        reviewed = self.image()
        reviewed.source_title = "Real cat"

        with patch.object(main, "search_image_candidate_batch", return_value=search_batch):
            with patch.object(
                main,
                "fetch_binary_url",
                return_value=(VALID_JPEG, "image/jpeg", "https://example.com/cat.jpg"),
            ):
                with patch.object(
                    main,
                    "review_web_image_candidates",
                    new=AsyncMock(
                        return_value=main.WebImageReviewResult((reviewed,), 1, 1, True)
                    ),
                ):
                    with patch.object(Path, "write_bytes", side_effect=OSError("disk unavailable")):
                        with patch.object(
                            main,
                            "notify_operator_alert",
                            new=AsyncMock(return_value="sent"),
                        ) as notify:
                            outcome = asyncio.run(
                                main.maybe_send_internet_image(message, message.text, plan=plan)
                            )

        self.assertTrue(outcome.request_fulfilled)
        notify.assert_awaited_once_with(
            message,
            "image_delivery_persistence_failed",
            {"failed_parts": 1},
        )
        stored = main.MEMORY.message_by_message_id(
            message.chat_id,
            message.message_id + 20_001,
        )
        self.assertIsNotNone(stored)
        self.assertEqual("", stored.local_media_path)
        self.assertEqual(
            "internet_image_send",
            main.MEMORY.provenance_for_output(stored.id).route,
        )


if __name__ == "__main__":
    unittest.main()
