"""Bounded Telegram turn assembly, independent of transport and model calls.

The first arrival fixes the deadline. Sealed turns remain accounted for until
their worker finishes; another message can never mutate a dispatched turn.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace


MAX_TURN_PARTS = 10
MAX_TURN_MEDIA = 10
MAX_SOURCE_CHARACTERS = 12000
MAX_OUTSTANDING_TURNS = 64
MAX_COHORT_TURNS = 4


@dataclass(frozen=True)
class TurnCohort:
    chat_id: int
    user_id: int
    thread_id: int | None = None
    reply_target_id: int | None = None
    business_connection_id: str = ""


@dataclass(frozen=True)
class TurnPart:
    message_id: int
    instruction: str = ""
    source_characters: int = 0
    media_count: int = 0
    sent_at: float | None = None


@dataclass(frozen=True)
class AssembledTurn:
    token: int
    cohort: TurnCohort
    opened_at: float
    deadline: float
    parts: tuple[TurnPart, ...]
    sealed: bool = False

    @property
    def prompt(self) -> str:
        return "\n".join(part.instruction for part in self.parts if part.instruction)


@dataclass(frozen=True)
class TurnAdmission:
    status: str
    turn: AssembledTurn | None = None
    created: bool = False


class TurnAssembler:
    """Synchronous ownership decisions; callers must invoke before their first await."""

    def __init__(self) -> None:
        self._next_token = 0
        self._open: dict[TurnCohort, int] = {}
        self._turns: dict[int, AssembledTurn] = {}
        self._seen: set[tuple[TurnCohort, int]] = set()
        self._seen_order: deque[tuple[TurnCohort, int]] = deque()

    def has_open(self, cohort: TurnCohort, now: float) -> bool:
        turn = self._turns.get(self._open.get(cohort, -1))
        return bool(turn is not None and not turn.sealed and now < turn.deadline)

    def _fits(self, parts: tuple[TurnPart, ...], max_instruction_chars: int) -> bool:
        return (
            len(parts) <= MAX_TURN_PARTS
            and sum(part.media_count for part in parts) <= MAX_TURN_MEDIA
            and sum(part.source_characters for part in parts) <= MAX_SOURCE_CHARACTERS
            and len("\n".join(part.instruction for part in parts if part.instruction))
            <= max_instruction_chars
        )

    def offer(
        self,
        cohort: TurnCohort,
        part: TurnPart,
        *,
        now: float,
        window_seconds: float,
        can_start: bool,
        max_instruction_chars: int,
    ) -> TurnAdmission:
        identity = (cohort, part.message_id)
        if identity in self._seen:
            return TurnAdmission("duplicate")
        # Rejection is still an observed update. Replays must not repeat notices
        # or become newly admitted when queue capacity or invocation changes.
        self._seen.add(identity)
        self._seen_order.append(identity)
        if len(self._seen_order) > 2048:
            self._seen.discard(self._seen_order.popleft())
        if not self._fits((part,), max_instruction_chars):
            return TurnAdmission("oversized")
        current = self._turns.get(self._open.get(cohort, -1))
        if current is not None and (current.sealed or now >= current.deadline):
            self._open.pop(cohort, None)
            current = None
        if current is not None and part.message_id <= current.parts[-1].message_id:
            return TurnAdmission("out_of_order")
        if current is not None:
            first_sent_at = current.parts[0].sent_at
            if (
                first_sent_at is not None and part.sent_at is not None
                and not 0 <= part.sent_at - first_sent_at <= window_seconds
            ):
                # Delayed delivery of old updates is not evidence of one burst.
                self._open.pop(cohort, None)
                current = None
        if current is not None and not self._fits((*current.parts, part), max_instruction_chars):
            # The full turn keeps its original timer. Overflow starts a following
            # turn only if this message independently has invocation admission.
            self._open.pop(cohort, None)
            current = None
        created = current is None
        if created:
            if not can_start:
                return TurnAdmission("not_invoked")
            cohort_count = sum(turn.cohort == cohort for turn in self._turns.values())
            if len(self._turns) >= MAX_OUTSTANDING_TURNS or cohort_count >= MAX_COHORT_TURNS:
                return TurnAdmission("busy")
            self._next_token += 1
            current = AssembledTurn(
                self._next_token, cohort, now, now + window_seconds, (part,)
            )
            self._open[cohort] = current.token
        else:
            current = replace(current, parts=(*current.parts, part))
        self._turns[current.token] = current
        return TurnAdmission("accepted", current, created)

    def get(self, token: int) -> AssembledTurn | None:
        return self._turns.get(token)

    def claim(self, token: int, *, now: float) -> AssembledTurn | None:
        turn = self._turns.get(token)
        if turn is None or turn.sealed or now < turn.deadline:
            return None
        turn = replace(turn, sealed=True)
        self._turns[token] = turn
        if self._open.get(turn.cohort) == token:
            self._open.pop(turn.cohort, None)
        return turn

    def finish(self, token: int) -> None:
        turn = self._turns.pop(token, None)
        if turn is not None and self._open.get(turn.cohort) == token:
            self._open.pop(turn.cohort, None)

    @property
    def outstanding_count(self) -> int:
        return len(self._turns)
