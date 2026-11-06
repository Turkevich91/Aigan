"""Pure randomizer contract tests without providers, persistence, or transport.

Owner: randomizer
"""
import random
import unittest
from unittest.mock import Mock, patch

from randomizer import (
    MAX_INTEGER, RandomizerResult, RandomizerValidationError, randomize,
)


class RandomizerTests(unittest.TestCase):
    def test_default_source_is_system_random_and_repeated_results_are_kept(self):
        source = Mock(spec=random.SystemRandom)
        source.randint.side_effect = [17, 17, 23]
        with patch("randomizer.random.SystemRandom", return_value=source) as constructor:
            outputs = [randomize("integers", minimum=1, maximum=30).values for _ in range(3)]
        self.assertEqual([(17,), (17,), (23,)], outputs)
        self.assertEqual(3, constructor.call_count)
        self.assertEqual(3, source.randint.call_count)
        source.randint.assert_called_with(1, 30)

    def test_inclusive_endpoints_and_signed_singleton(self):
        source = Mock(spec=random.SystemRandom)
        source.randint.side_effect = [-5, 8]
        result = randomize("integers", minimum=-5, maximum=8, count=2, rng=source)
        self.assertEqual((-5, 8), result.values)
        self.assertEqual((-MAX_INTEGER,), randomize(
            "integers", minimum=-MAX_INTEGER, maximum=-MAX_INTEGER).values)
        self.assertEqual((MAX_INTEGER,), randomize(
            "integers", minimum=MAX_INTEGER, maximum=MAX_INTEGER).values)

    def test_integer_without_replacement_covers_range(self):
        result = randomize("integers", minimum=-2, maximum=2, count=5, replacement=False)
        self.assertEqual([-2, -1, 0, 1, 2], sorted(result.values))

    def test_large_integer_population_is_a_range_not_a_materialized_list(self):
        source = Mock(spec=random.SystemRandom)
        source.sample.return_value = [-MAX_INTEGER, MAX_INTEGER]
        result = randomize("integers", minimum=-MAX_INTEGER, maximum=MAX_INTEGER,
                           count=2, replacement=False, rng=source)
        population, count = source.sample.call_args.args
        self.assertIsInstance(population, range)
        self.assertEqual(2, count)
        self.assertEqual((-MAX_INTEGER, MAX_INTEGER), result.values)

    def test_choice_preserves_unicode_and_duplicate_positions(self):
        items = ["кіт", "кіт", "犬 🐕"]
        original = list(items)
        result = randomize("choose", items=items, count=3)
        self.assertCountEqual(original, result.values)
        self.assertEqual(original, items)

    def test_choice_with_replacement_keeps_repeats(self):
        self.assertEqual(("слово",) * 4, randomize(
            "choose", items=["слово"], count=4, replacement=True).values)

    def test_shuffle_preserves_multiset_and_does_not_mutate_input(self):
        items = ["A", "A", "Б", "犬"]
        original = list(items)
        source = Mock(spec=random.SystemRandom)
        source.shuffle.side_effect = lambda values: values.reverse()
        result = randomize("shuffle", items=items, rng=source)
        self.assertEqual(tuple(reversed(original)), result.values)
        self.assertCountEqual(original, result.values)
        self.assertEqual(original, items)

    def test_valid_limits(self):
        self.assertEqual((7,), randomize("integers", minimum=7, maximum=7, items=None).values)
        self.assertEqual(100, len(randomize("integers", count=100).values))
        self.assertEqual(100, len(randomize("choose", items=["x"] * 200, count=100).values))
        self.assertEqual(100, len(randomize("shuffle", items=["x"] * 100).values))
        self.assertEqual(("x" * 200,) * 50, randomize(
            "choose", items=["x" * 200] * 50, count=50).values)

    def test_invalid_requests_fail_before_random_source_is_created(self):
        requests = [
            {"operation": "unknown"},
            {"operation": "integers", "count": True},
            {"operation": "integers", "count": 1.0},
            {"operation": "integers", "count": 0},
            {"operation": "integers", "count": 101},
            {"operation": "integers", "minimum": False},
            {"operation": "integers", "maximum": "3"},
            {"operation": "integers", "minimum": -MAX_INTEGER - 1},
            {"operation": "integers", "maximum": MAX_INTEGER + 1},
            {"operation": "integers", "minimum": 5, "maximum": 4},
            {"operation": "integers", "minimum": 1, "maximum": 2, "count": 3, "replacement": False},
            {"operation": "integers", "replacement": 1},
            {"operation": "integers", "items": []},
            {"operation": "integers", "items": ["not an integer input"]},
            {"operation": "choose"},
            {"operation": "choose", "items": []},
            {"operation": "choose", "items": ["x"] * 201},
            {"operation": "choose", "items": ["x" * 201]},
            {"operation": "choose", "items": ["x" * 200] * 51},
            {"operation": "choose", "items": [""]},
            {"operation": "choose", "items": [" \t"]},
            {"operation": "choose", "items": [1]},
            {"operation": "choose", "items": ["x"], "count": 2},
            {"operation": "shuffle", "items": ["x"] * 101},
            {"operation": "shuffle", "items": ["x"], "replacement": True},
        ]
        for kwargs in requests:
            with self.subTest(kwargs=kwargs), patch("randomizer.random.SystemRandom") as constructor:
                with self.assertRaises(RandomizerValidationError):
                    randomize(**kwargs)
                constructor.assert_not_called()

    def test_replacement_output_bound_rejects_before_random_source(self):
        for items in (["x" * 200], ["short", "x" * 200]):
            with self.subTest(item_count=len(items)), patch("randomizer.random.SystemRandom") as source:
                with self.assertRaises(RandomizerValidationError) as error:
                    randomize("choose", items=items, count=51, replacement=True)
                self.assertEqual("result_too_large", error.exception.code)
                source.assert_not_called()

    def test_replacement_output_bound_accepts_exact_limit(self):
        source = Mock(spec=random.SystemRandom)
        source.choice.return_value = "x" * 200
        result = randomize("choose", items=["short", "x" * 200],
                           count=50, replacement=True, rng=source)
        self.assertEqual(("x" * 200,) * 50, result.values)
        self.assertEqual(10000, sum(map(len, result.values)))
        self.assertEqual(50, source.choice.call_count)

    def test_source_failure_is_not_replaced_with_a_success(self):
        source = Mock(spec=random.SystemRandom)
        source.randint.side_effect = OSError("random source unavailable")
        with self.assertRaises(OSError):
            randomize("integers", rng=source)
        self.assertEqual(1, source.randint.call_count)

    def test_error_does_not_include_supplied_payload(self):
        private_item = "sensitive sample " * 20
        with self.assertRaises(RandomizerValidationError) as error:
            randomize("choose", items=[private_item])
        self.assertNotIn(private_item, str(error.exception))
        self.assertEqual("invalid_item", error.exception.code)

    def test_results_are_immutable_and_render_without_tool_narration(self):
        result = RandomizerResult("integers", (17,))
        self.assertEqual({"status": "ok", "operation": "integers", "values": [17]}, result.to_dict())
        self.assertEqual("17", result.format_text())
        self.assertEqual("1. кіт\n2. 犬", RandomizerResult("choose", ("кіт", "犬")).format_text())
        external = result.to_dict()["values"]
        external.append(23)
        self.assertEqual((17,), result.values)


    def test_even_stepped_range_is_inclusive_and_uniform_source_receives_progression(self):
        source = Mock(spec=random.SystemRandom)
        source.randrange.side_effect = [2, 102]
        result = randomize("integers", minimum=2, maximum=102, step=2, count=2, rng=source)
        self.assertEqual((2, 102), result.values)
        self.assertEqual(2, source.randrange.call_count)
        source.randrange.assert_called_with(2, 103, 2)
        source.randint.assert_not_called()
        all_values = randomize("integers", minimum=2, maximum=102, step=2,
                               count=51, replacement=False)
        self.assertEqual(list(range(2, 103, 2)), sorted(all_values.values))

    def test_stepped_range_excludes_nonmember_upper_bound_without_enumeration(self):
        source = Mock(spec=random.SystemRandom)
        source.sample.return_value = [202, 1000]
        result = randomize("integers", minimum=202, maximum=1002, step=3,
                           count=2, replacement=False, rng=source)
        population, count = source.sample.call_args.args
        self.assertIsInstance(population, range)
        self.assertEqual(202, population[0])
        self.assertEqual(1000, population[-1])
        self.assertNotIn(1002, population)
        self.assertEqual(267, len(population))
        self.assertEqual(2, count)
        self.assertEqual((202, 1000), result.values)

    def test_step_validation_and_sample_size_use_progression_population(self):
        for step in (0, -1, True, 1.5, "2"):
            with self.subTest(step=step), patch("randomizer.random.SystemRandom") as source:
                with self.assertRaises(RandomizerValidationError) as error:
                    randomize("integers", step=step)
                self.assertEqual("invalid_step", error.exception.code)
                source.assert_not_called()
        with self.assertRaises(RandomizerValidationError) as error:
            randomize("integers", minimum=2, maximum=102, step=2, count=52, replacement=False)
        self.assertEqual("insufficient_population", error.exception.code)
        self.assertEqual((2,), randomize("integers", minimum=2, maximum=102, step=10**100).values)

    def test_two_dice_are_two_independent_draws_with_replacement(self):
        source = Mock(spec=random.SystemRandom)
        source.randint.side_effect = [4, 4]
        result = randomize("integers", minimum=1, maximum=6, count=2, rng=source)
        self.assertEqual((4, 4), result.values)
        self.assertEqual(2, source.randint.call_count)
        self.assertEqual([((1, 6), {}), ((1, 6), {})], source.randint.call_args_list)

    def test_animal_choice_uses_only_the_supplied_words(self):
        animals = ["кіт", "пес", "лисиця"]
        source = Mock(spec=random.SystemRandom)
        source.sample.return_value = ["лисиця"]
        result = randomize("choose", items=animals, rng=source)
        self.assertEqual(("лисиця",), result.values)
        source.sample.assert_called_once_with(animals, 1)


if __name__ == "__main__":
    unittest.main()
