"""Request-local randomization result ownership and conservative request hints.

Owner: agent_capabilities
The host supplies authored request text, never an enriched history/prompt. These
partial hints make known direct requests mandatory; they do not restrict the
model's access to the tool or claim universal language understanding.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from threading import Lock

from randomizer import RandomizerResult, RandomizerValidationError, randomize


CLARIFICATION_TEXT = "Уточни діапазон чисел або список варіантів."
INVALID_PARAMETERS_TEXT = "Уточни діапазон, список і кількість: із цими параметрами вибір неможливий."
FAILED_TEXT = "Не вдалося зробити випадковий вибір. Спробуй ще раз."

_QUOTED = re.compile(r'"[^"\n]*"|«[^»]*»|“[^”]*”|`[^`]*`|(?<!\w)\x27[^\x27\n]*\x27(?!\w)')
_NEGATED = re.compile(r"\b(?:don['’]t|do\s+not|never|не|ніколи|никогда)\b", re.I)
_EXPLANATION = re.compile(
    r"^(?:how|why|what|explain|describe|translate|say\s+(?:how|why|what)|tell\s+me\s+(?:how|why|what|about)|"
    r"як|чому|що|поясни|поясніть|розкажи|переклади|как|почему|что|объясни|расскажи|переведи)\b", re.I
)
_PREFIX = re.compile(
    r"^(?:(?:please|будь\s+ласка|будь\s+ласкава|пожалуйста|aigan|айган|аіган)[,!: ]+|"
    r"(?:can|could|would|will)\s+you\s+|(?:можеш|можете|можешь)\s+)", re.I
)
_DRAW_ACTION = re.compile(
    r"^(?:pick|choose|select|draw|give|generate|say|tell|name|i\s+(?:want|need|would\s+like)|"
    r"обери|оберіть|вибери|виберіть|вибрати|назви|назвіть|назвати|скажи|скажіть|"
    r"згенеруй|згенеруйте|дай|дайте|хочу|мені|выбери|выберите|выбрать|назови|назовите|назвать|"
    r"скажи|скажите|сгенерируй|сгенерируйте|мне)\b", re.I
)
_RANDOM = re.compile(r"\b(?:random(?:ly)?|випадков\w*|навмання|наугад|рандом\w*|случайн\w*)\b", re.I)
_SHUFFLE = re.compile(r"^(?:shuffle|перемішай|перемішайте|перемішати|перемешай|перемешайте|перемешать)\b", re.I)
_DICE = re.compile(
    r"^(?:(?:roll|throw)\b.*\b(?:die|dice)\b|(?:flip|toss)\b.*\bcoin\b|"
    r"(?:кинь|киньте|брось|бросьте|підкинь|підкиньте|подбрось|подбросьте)\b.*\b(?:кубик\w*|кост[ьіи]\w*|монет\w*)\b)", re.I
)
_REPEAT = re.compile(
    r"^(?:again|once\s+more|one\s+more|another(?:\s+one)?|repeat|roll\s+again|"
    r"ще(?:\s+(?:раз|одне|один))?|знову|повтори|повторіть|"
    r"ещ[её](?:\s+(?:раз|одно|один))?|снова|повтори(?:\s+бросок)?|перебрось)[.!?\s]*$", re.I
)


def _request_text(text: str) -> str:
    text = " ".join(line for line in (text or "").splitlines() if not line.lstrip().startswith(">"))
    return " ".join(text.strip().split())


def _without_prefix(text: str) -> str:
    for _ in range(3):
        updated = _PREFIX.sub("", text, count=1).strip()
        if updated == text:
            break
        text = updated
    return text


def is_randomization_request(trusted_text: str) -> bool:
    """Recognize a bounded subset of direct requests, not quoted discussion.

    The model can still call the randomizer for phrasing outside this subset.
    Parameter extraction belongs to the tool call, not this language hint.
    """
    text = _without_prefix(_request_text(trusted_text))
    if not text or _EXPLANATION.search(text) or _NEGATED.search(text[:100]):
        return False
    text = _QUOTED.sub("", text).strip()
    return bool(_SHUFFLE.search(text) or _DICE.search(text)
                or (_DRAW_ACTION.search(text) and _RANDOM.search(text)))


def is_randomization_repeat(trusted_text: str) -> bool:
    """A repeat hint only; the host must establish a prior successful draw."""
    return bool(_REPEAT.fullmatch(_without_prefix(_request_text(trusted_text))))


@dataclass
class RandomizerSession:
    """Own at most one operation and its exact output for one admitted request."""

    required: bool = False
    rng: object | None = None
    result: RandomizerResult | None = field(default=None, init=False)
    attempted: bool = field(default=False, init=False)
    _payload: str | None = field(default=None, init=False)
    _failure_text: str = field(default=FAILED_TEXT, init=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False, compare=False)

    @property
    def completed(self) -> bool:
        return self._payload is not None

    @property
    def literal_output(self) -> bool:
        return self.result is not None

    def execute(
        self,
        operation: str,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
        step: int = 1,
        count: int = 1,
        items: list[str] | None = None,
        replacement: bool | None = None,
    ) -> str:
        """Consume once, returning only bounded values or safe error codes."""
        # Synchronous SDK tools can run in parallel worker threads. Claim and
        # sampling must share the lock so a concurrent call cannot draw again.
        with self._lock:
            return self._execute_claimed(
                operation, minimum=minimum, maximum=maximum, step=step, count=count,
                items=items, replacement=replacement,
            )

    def _execute_claimed(
        self,
        operation: str,
        *,
        minimum: int | None,
        maximum: int | None,
        step: int,
        count: int,
        items: list[str] | None,
        replacement: bool | None,
    ) -> str:
        if self._payload is not None:
            return self._payload
        self.attempted = True
        if operation == "clarify" or (operation == "integers" and (minimum is None or maximum is None)):
            payload = {"status": "clarification", "code": "missing_parameters"}
            self._failure_text = CLARIFICATION_TEXT
        else:
            try:
                self.result = randomize(
                    operation, minimum=minimum, maximum=maximum, step=step, count=count,
                    items=items, replacement=replacement, rng=self.rng,
                )
                payload = self.result.to_dict()
            except RandomizerValidationError as exc:
                payload = {"status": "error", "code": exc.code}
                self._failure_text = INVALID_PARAMETERS_TEXT
            except Exception:
                payload = {"status": "error", "code": "randomization_failed"}
                self._failure_text = FAILED_TEXT
        self._payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return self._payload

    def final_output(self, candidate: str) -> str:
        """The host owns sampled output and failed required-operation claims."""
        if self.result is not None:
            return self.result.format_text()
        if self.required or self.attempted:
            return self._failure_text
        return candidate
