import unittest

from image_candidate_review import (
    image_candidate_review_schema,
    normalize_image_candidate_reviews,
    selected_candidate_indices,
)


def review(
    index: int,
    *,
    relevant: bool = True,
    direct: bool = True,
    safe: bool = True,
    quality: str = "good",
    duplicate_of: int | None = None,
    confidence: float = 0.95,
    score: int = 90,
    reject_reason: str = "none",
    description: str = "Кіт сидить на підвіконні.",
) -> dict:
    return {
        "index": index,
        "relevant": relevant,
        "direct_depiction": direct,
        "safe_for_group": safe,
        "quality": quality,
        "duplicate_of_index": duplicate_of,
        "confidence": confidence,
        "score": score,
        "reject_reason": reject_reason,
        "description_uk": description,
    }


class ImageCandidateReviewTests(unittest.TestCase):
    def test_schema_requires_one_result_per_candidate(self) -> None:
        schema = image_candidate_review_schema(5)
        reviews = schema["properties"]["reviews"]

        self.assertEqual(5, reviews["minItems"])
        self.assertEqual(5, reviews["maxItems"])
        self.assertFalse(reviews["items"]["additionalProperties"])

    def test_cat_mask_is_rejected_while_direct_cats_are_ranked(self) -> None:
        payload = {
            "reviews": [
                review(
                    0,
                    relevant=False,
                    direct=False,
                    score=20,
                    reject_reason="indirect_representation",
                    description="Маска у формі котячої морди.",
                ),
                review(1, score=82, description="Сірий кіт лежить на дивані."),
                review(2, score=96, description="Рудий кіт дивиться в камеру."),
            ]
        }

        normalized = normalize_image_candidate_reviews(payload, candidate_count=3)

        self.assertEqual((2, 1), selected_candidate_indices(normalized, confidence_threshold=0.8))

    def test_unsafe_duplicate_low_confidence_and_empty_description_fail_closed(self) -> None:
        payload = {
            "reviews": [
                review(0, safe=False, reject_reason="unsafe_for_group"),
                review(1, duplicate_of=0, reject_reason="near_duplicate"),
                review(2, confidence=0.4),
                review(3, description=""),
            ]
        }

        normalized = normalize_image_candidate_reviews(payload, candidate_count=4)

        self.assertEqual((), selected_candidate_indices(normalized, confidence_threshold=0.8))

    def test_structural_mismatch_rejects_entire_batch(self) -> None:
        with self.assertRaisesRegex(ValueError, "count mismatch"):
            normalize_image_candidate_reviews(
                {"reviews": [review(0)]},
                candidate_count=2,
            )

        with self.assertRaisesRegex(ValueError, "index mismatch"):
            normalize_image_candidate_reviews(
                {"reviews": [review(1)]},
                candidate_count=1,
            )

    def test_unknown_fields_and_forward_duplicate_references_are_invalid(self) -> None:
        unknown = review(0)
        unknown["prompt"] = "ignored"
        with self.assertRaisesRegex(ValueError, "invalid candidate review item"):
            normalize_image_candidate_reviews({"reviews": [unknown]}, candidate_count=1)

        with self.assertRaisesRegex(ValueError, "duplicate candidate reference"):
            normalize_image_candidate_reviews(
                {
                    "reviews": [
                        review(0),
                        review(1, duplicate_of=1, reject_reason="near_duplicate"),
                    ]
                },
                candidate_count=2,
            )


if __name__ == "__main__":
    unittest.main()
