"""Request hints and single-operation result ownership, without providers."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, Lock
import unittest
from unittest.mock import Mock, call

from randomizer_capability import (
    CLARIFICATION_TEXT,
    FAILED_TEXT,
    INVALID_PARAMETERS_TEXT,
    RandomizerSession,
    is_randomization_repeat,
    is_randomization_request,
)


class RequestHintTests(unittest.TestCase):
    def test_direct_random_requests(self):
        prompts = [
            "Pick a random integer between -4 and 20",
            "Could you please choose one at random: oak, pine?",
            "Give me three random numbers from 1 to 50",
            "I would like a random number between 1 and 9",
            "Назви випадкове число від 1 до 50",
            "Будь ласка, обери навмання слово зі списку: дуб, сосна",
            "Можеш назвати випадкове число від 2 до 8?",
            "Выбери случайное слово: дуб, сосна",
            "Пожалуйста, дай случайное число от 3 до 9",
            "Можешь назвать случайное число от 1 до 10?",
            'Choose randomly between "red" and "blue"',
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_randomization_request(prompt))

    def test_shuffle_dice_and_coin_requests(self):
        for prompt in ["Shuffle this list: a, b, c", "Перемішай список: а, б, в",
                       "Перемешай варианты: а, б, в", "Roll a die", "Throw two dice",
                       "Flip a coin", "Підкинь монетку", "Брось кубик", "Кинь дві кості",
                       "Кинь 2 кості", "Roll two dice"]:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_randomization_request(prompt))

    def test_discussion_negation_and_sources_are_not_direct_requests(self):
        prompts = [
            "How do you pick a random number?", "Why are random numbers useful?",
            "Explain how you chose that random number", "What is SystemRandom?",
            "Can you explain random numbers?", "Як ти вибираєш випадкові числа?",
            "Поясни, як отримав випадкове число", "Как ты выбрал случайное число?",
            "Don't pick a random number", "Do not shuffle the list",
            "Не називай випадкове число", "Не выбирай случайное слово",
            '"Pick a random number from 1 to 10"', "'Roll a die'", "`Shuffle this list`",
            "Someone said: pick a random number", "> Назви випадкове число від 1 до 9",
            'Translate "pick a random number"', "Tell me what a random number means",
            "Say why random numbers matter", "Choose the best option", "I got 17 yesterday",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertFalse(is_randomization_request(prompt))

    def test_repeat_requires_separate_host_context(self):
        for prompt in ["Again!", "one more", "another one", "Please roll again",
                       "Ще раз", "ще одне", "Знову", "Еще раз", "ещё", "Снова"]:
            with self.subTest(prompt=prompt):
                self.assertTrue(is_randomization_repeat(prompt))
                self.assertFalse(is_randomization_request(prompt))
        for prompt in ["Explain again", "Why did you repeat?", "Choose again later",
                       "Не повторяй", "Переведи слово 'again'", ""]:
            with self.subTest(prompt=prompt):
                self.assertFalse(is_randomization_repeat(prompt))


class RandomizerSessionTests(unittest.TestCase):
    def test_exact_success_overrides_model_number_and_narration(self):
        rng = Mock()
        rng.randint.return_value = 23
        session = RandomizerSession(required=True, rng=rng)
        payload = json.loads(session.execute("integers", minimum=1, maximum=30))
        self.assertEqual({"status": "ok", "operation": "integers", "values": [23]}, payload)
        self.assertEqual("23", session.final_output("I used a randomizer and got 17."))
        self.assertTrue(session.completed)
        self.assertTrue(session.literal_output)
        rng.randint.assert_called_once_with(1, 30)

    def test_success_is_consumed_once_without_reroll(self):
        rng = Mock()
        rng.randint.side_effect = [17, 4]
        session = RandomizerSession(rng=rng)
        first = session.execute("integers", minimum=1, maximum=30)
        self.assertEqual(first, session.execute("integers", minimum=10, maximum=20))
        self.assertEqual("17", session.final_output("4"))
        self.assertEqual(1, rng.randint.call_count)

    def test_independent_requests_can_repeat_same_result(self):
        rng = Mock()
        rng.randint.return_value = 17
        for _ in range(2):
            session = RandomizerSession(rng=rng)
            session.execute("integers", minimum=1, maximum=30)
            self.assertEqual("17", session.final_output("anything"))
        self.assertEqual(2, rng.randint.call_count)

    def test_step_is_forwarded_without_changing_sampled_value(self):
        rng = Mock()
        rng.randrange.return_value = 205
        session = RandomizerSession(required=True, rng=rng)
        session.execute("integers", minimum=202, maximum=1002, step=3)
        rng.randrange.assert_called_once_with(202, 1003, 3)
        rng.randint.assert_not_called()
        self.assertEqual("205", session.final_output("17"))

    def test_two_dice_are_two_independent_draws_in_one_operation(self):
        rng = Mock()
        rng.randint.return_value = 6
        session = RandomizerSession(required=True, rng=rng)
        session.execute("integers", minimum=1, maximum=6, count=2)
        self.assertEqual(2, rng.randint.call_count)
        rng.randint.assert_has_calls([call(1, 6), call(1, 6)])
        self.assertEqual("1. 6\n2. 6", session.final_output("12"))

    def test_concurrent_calls_share_one_operation(self):
        sampling_started = Event()
        release_sampling = Event()
        contender_waiting = Event()

        class ObservedLock:
            """Signal the second acquisition attempt before it can proceed."""
            def __init__(self):
                self.lock = Lock()
                self.counter_lock = Lock()
                self.entries = 0

            def __enter__(self):
                with self.counter_lock:
                    self.entries += 1
                    if self.entries == 2:
                        contender_waiting.set()
                self.lock.acquire()

            def __exit__(self, *args):
                self.lock.release()

        def blocked_draw(minimum, maximum):
            sampling_started.set()
            if not release_sampling.wait(5):
                raise AssertionError("The test did not release the blocked draw")
            return 23

        rng = Mock()
        rng.randint.side_effect = blocked_draw
        session = RandomizerSession(rng=rng)
        session._lock = ObservedLock()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(session.execute, "integers", minimum=1, maximum=30)
            try:
                self.assertTrue(sampling_started.wait(5))
                second = pool.submit(session.execute, "integers", minimum=1, maximum=30)
                self.assertTrue(contender_waiting.wait(5))
            finally:
                release_sampling.set()
            self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        rng.randint.assert_called_once_with(1, 30)
        self.assertEqual("23", session.final_output("17"))

    def test_missing_required_call_blocks_model_only_number(self):
        session = RandomizerSession(required=True)
        self.assertEqual(FAILED_TEXT, session.final_output("17"))
        self.assertFalse(session.completed)
        self.assertFalse(session.literal_output)

    def test_unrelated_answer_is_unchanged_without_attempt(self):
        session = RandomizerSession()
        self.assertEqual("A randomizer uses a source of randomness.",
                         session.final_output("A randomizer uses a source of randomness."))

    def test_missing_bounds_clarify_without_guessing_defaults(self):
        for kwargs in [{}, {"minimum": 1}, {"maximum": 30}, {"maximum": 30, "step": 2}]:
            with self.subTest(kwargs=kwargs):
                rng = Mock()
                session = RandomizerSession(rng=rng)
                result = json.loads(session.execute("integers", **kwargs))
                self.assertEqual("clarification", result["status"])
                self.assertEqual(CLARIFICATION_TEXT, session.final_output("17"))
                self.assertTrue(session.completed)
                rng.randint.assert_not_called()

    def test_explicit_clarification_is_final(self):
        rng = Mock()
        session = RandomizerSession(rng=rng)
        first = session.execute("clarify")
        self.assertEqual(first, session.execute("integers", minimum=1, maximum=30))
        self.assertEqual(CLARIFICATION_TEXT, session.final_output("17"))
        rng.randint.assert_not_called()

    def test_validation_error_cannot_be_retried_or_fabricated(self):
        rng = Mock()
        session = RandomizerSession(rng=rng)
        first = session.execute("integers", minimum=30, maximum=1)
        self.assertEqual("invalid_range", json.loads(first)["code"])
        self.assertEqual(first, session.execute("integers", minimum=1, maximum=30))
        self.assertEqual(INVALID_PARAMETERS_TEXT, session.final_output("17"))
        rng.randint.assert_not_called()

    def test_failure_does_not_leak_exception_or_fall_back(self):
        rng = Mock()
        rng.randint.side_effect = RuntimeError("private diagnostic text")
        session = RandomizerSession(rng=rng)
        payload = session.execute("integers", minimum=1, maximum=30)
        self.assertEqual({"status": "error", "code": "randomization_failed"}, json.loads(payload))
        self.assertNotIn("private", payload)
        self.assertEqual(FAILED_TEXT, session.final_output("17"))
        self.assertTrue(session.completed)
        self.assertFalse(session.literal_output)

    def test_unicode_markup_and_duplicates_keep_literal_values(self):
        rng = Mock()
        values = ["<b>дуб</b>", "дуб", "дуб"]
        rng.sample.return_value = list(values)
        session = RandomizerSession(rng=rng)
        session.execute("choose", items=values, count=3)
        self.assertEqual("1. <b>дуб</b>\n2. дуб\n3. дуб", session.final_output("changed"))
        self.assertTrue(session.literal_output)


if __name__ == "__main__":
    unittest.main()
