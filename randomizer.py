"""Bounded randomization from an operating-system random source.

Owner: randomizer
The SDK adapter owns request intent and delivery; this module owns validation,
sampling, and its exact values. Duplicate input strings are distinct positions.
"""
from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Literal

Operation = Literal["integers", "choose", "shuffle"]
MAX_RESULTS = 100
MAX_ITEMS = 200
MAX_ITEM_CHARACTERS = 200
MAX_TOTAL_CHARACTERS = 10_000
MAX_INTEGER = 2**53 - 1


class RandomizerValidationError(ValueError):
    """A safe validation failure that never embeds supplied item text."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RandomizerResult:
    operation: Operation
    values: tuple[int | str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"status": "ok", "operation": self.operation, "values": list(self.values)}

    def format_text(self) -> str:
        """Render plain text; a transport adapter must escape its own markup."""
        if len(self.values) == 1:
            return str(self.values[0])
        return "\n".join(f"{index}. {value}" for index, value in enumerate(self.values, 1))


def _integer(name: str, value: object, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise RandomizerValidationError(
            f"invalid_{name}", f"{name} must be an integer between {lower} and {upper}."
        )
    return value


def _items(items: list[str] | None) -> list[str]:
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise RandomizerValidationError("invalid_items", f"Supply between 1 and {MAX_ITEMS} items.")
    if any(not isinstance(item, str) or not item.strip() or len(item) > MAX_ITEM_CHARACTERS
           for item in items):
        raise RandomizerValidationError(
            "invalid_item", f"Each item must be nonblank text of at most {MAX_ITEM_CHARACTERS} characters."
        )
    if sum(map(len, items)) > MAX_TOTAL_CHARACTERS:
        raise RandomizerValidationError(
            "items_too_large", f"Items must total at most {MAX_TOTAL_CHARACTERS} characters."
        )
    return list(items)


def randomize(
    operation: Operation,
    *,
    minimum: int = 1,
    maximum: int = 100,
    count: int = 1,
    step: int = 1,
    items: list[str] | None = None,
    replacement: bool | None = None,
    rng: random.Random | None = None,
) -> RandomizerResult:
    """Sample integers/items or return a shuffled copy.

    Integer bounds are inclusive; step selects minimum + k*step <= maximum.
    Integers allow replacement by default;
    item selection defaults to distinct positions (equal strings may recur).
    Shuffle returns every item, so count is unused and input is capped at 100.
    Replacement choices reserve the output budget using the longest input item.
    No seed, previous response, persistence, or rejection reroll is involved.
    """
    if operation not in ("integers", "choose", "shuffle"):
        raise RandomizerValidationError("invalid_operation", "Unknown randomization operation.")
    count = _integer("count", count, 1, MAX_RESULTS)
    if type(step) is not int or step <= 0:
        raise RandomizerValidationError("invalid_step", "step must be a positive integer.")
    if replacement is not None and type(replacement) is not bool:
        raise RandomizerValidationError("invalid_replacement", "replacement must be a boolean or omitted.")
    if operation == "integers":
        minimum = _integer("minimum", minimum, -MAX_INTEGER, MAX_INTEGER)
        maximum = _integer("maximum", maximum, -MAX_INTEGER, MAX_INTEGER)
        if minimum > maximum:
            raise RandomizerValidationError("invalid_range", "minimum must not exceed maximum.")
        if items is not None:
            raise RandomizerValidationError("unexpected_items", "Integer sampling does not accept items.")
        replace = True if replacement is None else replacement
        if not replace and count > (maximum - minimum) // step + 1:
            raise RandomizerValidationError("insufficient_population", "The range has too few distinct integers.")
        source = rng if rng is not None else random.SystemRandom()
        values = (
            [(source.randint(minimum, maximum) if step == 1
              else source.randrange(minimum, maximum + 1, step)) for _ in range(count)]
            if replace else source.sample(range(minimum, maximum + 1, step), count)
        )
    else:
        population = _items(items)
        if operation == "shuffle":
            if replacement is True:
                raise RandomizerValidationError("invalid_replacement", "Shuffle cannot use replacement.")
            if len(population) > MAX_RESULTS:
                raise RandomizerValidationError("too_many_results", f"Shuffle accepts at most {MAX_RESULTS} items.")
            source = rng if rng is not None else random.SystemRandom()
            source.shuffle(population)
            values = population
        else:
            replace = False if replacement is None else replacement
            if not replace and count > len(population):
                raise RandomizerValidationError("insufficient_population", "The list has too few positions.")
            if replace and count * max(map(len, population)) > MAX_TOTAL_CHARACTERS:
                raise RandomizerValidationError(
                    "result_too_large", f"Worst-case selected items exceed {MAX_TOTAL_CHARACTERS} characters."
                )
            source = rng if rng is not None else random.SystemRandom()
            values = (
                [source.choice(population) for _ in range(count)]
                if replace else source.sample(population, count)
            )
    return RandomizerResult(operation=operation, values=tuple(values))
