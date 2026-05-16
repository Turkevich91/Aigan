from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from telegram import ReactionTypeCustomEmoji, ReactionTypeEmoji
from telegram.constants import ChatType
from telegram.error import BadRequest

from memory import MemoryItem
from reaction_memory import ReactionMemoryStore, ReactionSpec, safe_code


EventCallback = Callable[..., None]
ValueProvider = Callable[[], int | str | None]
DECISION_POLICY_VERSION = "outbound_reaction_emotion_policy_v1"
EMOTION_POLICY_VERSION = DECISION_POLICY_VERSION
TERM_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


REACTION_EMOJI_BY_EMOTION: dict[str, tuple[str, ...]] = {
    "positive_celebratory": ("🔥", "👍", "❤️", "❤", "🎉", "😂"),
    "grief_sympathy": ("😢", "💔", "😭"),
    "horror_shock": ("😱", "😨", "🤯"),
    "condemnation_outrage": ("😡", "🤬"),
    "despair_heavy_news": ("😢", "💔", "😐"),
    "uncertainty_doubt": ("🤔", "👀"),
}

EMOTION_CONFIDENCE_MIN: dict[str, float] = {
    "positive_celebratory": 0.58,
    "grief_sympathy": 0.76,
    "horror_shock": 0.72,
    "condemnation_outrage": 0.84,
    "despair_heavy_news": 0.78,
    "uncertainty_doubt": 0.82,
}

POSITIVE_TERMS = (
    "achievement",
    "award",
    "birthday",
    "celebrat",
    "congrat",
    "fixed",
    "good news",
    "great news",
    "happy",
    "launch",
    "release",
    "released",
    "success",
    "victory",
    "виграв",
    "досяг",
    "перемог",
    "реліз",
    "свято",
    "успіх",
    "ура",
    "выпуст",
    "достиж",
    "побед",
    "празд",
    "успех",
)

GRIEF_TERMS = (
    "casualties",
    "dead",
    "death",
    "died",
    "funeral",
    "grief",
    "killed",
    "loss",
    "mourning",
    "victims",
    "вбит",
    "втрат",
    "жертв",
    "загин",
    "помер",
    "скорбот",
    "смерт",
    "убит",
    "погиб",
    "умер",
)

HORROR_TERMS = (
    "attack",
    "bomb",
    "catastrophe",
    "crash",
    "disaster",
    "explosion",
    "horror",
    "massacre",
    "missile",
    "shocking",
    "terror",
    "вибух",
    "жах",
    "катастроф",
    "обстріл",
    "ракета",
    "теракт",
    "удар",
    "шок",
    "авар",
    "взрыв",
    "обстрел",
)

CONDEMNATION_TERMS = (
    "abuse",
    "coercion",
    "condemn",
    "corruption",
    "crime",
    "criminal",
    "cruel",
    "injustice",
    "occup",
    "threat",
    "torture",
    "war crime",
    "засуд",
    "злочин",
    "катув",
    "коруп",
    "несправ",
    "окуп",
    "погроз",
    "примус",
    "насиль",
    "преступ",
    "угроз",
)

STEM_TERMS = {
    "celebrat",
    "congrat",
    "occup",
}

BENIGN_TERM_CONTEXTS: dict[str, tuple[str, ...]] = {
    "attack": ("attack surface",),
}

AMBIGUOUS_TERMS = (
    "alleged",
    "maybe",
    "rumor",
    "unconfirmed",
    "unclear",
    "ймовір",
    "можливо",
    "неясно",
    "чутк",
    "вероят",
    "слух",
)

SARCASM_MEME_TERMS = (
    "irony",
    "joke",
    "lol",
    "meme",
    "sarcasm",
    "ірон",
    "мем",
    "сарказ",
    "шутк",
)


@dataclass(frozen=True)
class OutboundReactionConfig:
    enabled: bool = False
    every_n_messages: int = 10
    cooldown_seconds: int = 1800
    min_score: float = 0.72
    allowed_emoji: tuple[str, ...] = ("🔥", "👀", "👍", "🤔", "😂")
    use_custom_emoji: bool = True
    is_big: bool = False
    bot_trigger: str = "!m"


@dataclass(frozen=True)
class EmotionPolicyDecision:
    emotion_class: str
    confidence: float
    allow_reaction: bool
    reason_code: str
    rationale: str
    severity_flags: tuple[str, ...] = ()


class ReactionAdapter(Protocol):
    async def on_message_ingested(self, message: Any, memory_item: MemoryItem | None, phase: str) -> None:
        ...

    async def on_reaction_update(self, update: Any, context: Any) -> None:
        ...

    def health_summary(self) -> dict[str, Any]:
        ...


class NullReactionAdapter:
    async def on_message_ingested(self, message: Any, memory_item: MemoryItem | None, phase: str) -> None:
        return None

    async def on_reaction_update(self, update: Any, context: Any) -> None:
        return None

    def health_summary(self) -> dict[str, Any]:
        return {"enabled": False, "adapter": "null"}


class OutboundReactionAdapter:
    def __init__(
        self,
        *,
        config: OutboundReactionConfig,
        reaction_memory: ReactionMemoryStore | None = None,
        event_callback: EventCallback | None = None,
        bot_id_provider: ValueProvider | None = None,
        bot_username_provider: ValueProvider | None = None,
    ) -> None:
        self.config = config
        self.reaction_memory = reaction_memory
        self.event_callback = event_callback or (lambda **_kwargs: None)
        self.bot_id_provider = bot_id_provider or (lambda: None)
        self.bot_username_provider = bot_username_provider or (lambda: None)
        self._eligible_since_sent: dict[int, int] = {}
        self._last_sent_at: dict[int, float] = {}
        self._sent_count = 0
        self._skip_count = 0
        self._error_count = 0

    async def on_message_ingested(self, message: Any, memory_item: MemoryItem | None, phase: str) -> None:
        if not self.config.enabled or phase != "pre_embedding":
            return
        try:
            await self._maybe_react(message, memory_item)
        except Exception as exc:
            self._error_count += 1
            self._emit(
                level="warning",
                event_type="outbound_reaction_adapter_error",
                message_obj=message,
                message=f"{type(exc).__name__}: {exc}",
            )

    async def on_reaction_update(self, update: Any, context: Any) -> None:
        return None

    def health_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "adapter": "outbound",
            "every_n_messages": self.config.every_n_messages,
            "cooldown_seconds": self.config.cooldown_seconds,
            "min_score": self.config.min_score,
            "sent_count": self._sent_count,
            "skip_count": self._skip_count,
            "error_count": self._error_count,
            "tracked_chats": len(self._eligible_since_sent),
        }

    async def _maybe_react(self, message: Any, item: MemoryItem | None) -> None:
        if item is None:
            self._skip_count += 1
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="missing_memory_item",
                rationale="Skipped because no persisted target message was available for a safe reaction decision.",
            )
            return

        skip_reason = self._eligibility_skip_reason(message, item)
        if skip_reason is not None:
            self._skip_count += 1
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code=skip_reason,
                rationale=f"Skipped because the target failed the outbound reaction eligibility gate: {skip_reason}.",
            )
            return

        chat_id = int(item.chat_id)
        self._eligible_since_sent[chat_id] = self._eligible_since_sent.get(chat_id, 0) + 1
        if self._eligible_since_sent[chat_id] < max(1, self.config.every_n_messages):
            self._emit(
                event_type="outbound_reaction_skipped_rate",
                message_obj=message,
                details={"eligible_since_sent": self._eligible_since_sent[chat_id]},
            )
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="rate_gate",
                rationale="Skipped because the per-chat eligible-message counter has not reached the configured reaction interval.",
                details={"eligible_since_sent": self._eligible_since_sent[chat_id]},
            )
            return

        now = time.time()
        last_sent_at = self._last_sent_at.get(chat_id, 0.0)
        if now - last_sent_at < max(0, self.config.cooldown_seconds):
            remaining = int(self.config.cooldown_seconds - (now - last_sent_at))
            self._emit(
                event_type="outbound_reaction_skipped_cooldown",
                message_obj=message,
                details={"remaining_seconds": remaining},
            )
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="cooldown",
                rationale="Skipped because the chat-level outbound reaction cooldown is still active.",
                details={"remaining_seconds": remaining},
            )
            return

        score = self.relevance_score(message, item)
        if score < self.config.min_score:
            self._emit(
                event_type="outbound_reaction_skipped_score",
                message_obj=message,
                details={"score": round(score, 3), "min_score": self.config.min_score},
            )
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="score_below_min",
                rationale="Skipped because the relevance score did not meet the configured reaction threshold.",
                score=score,
                details={"min_score": self.config.min_score},
            )
            return

        policy = self._classify_emotion_policy(item, score)
        if not policy.allow_reaction:
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code=policy.reason_code,
                rationale=policy.rationale,
                score=score,
                confidence=policy.confidence,
                emotion_class=policy.emotion_class,
                severity_flags=policy.severity_flags,
                details={"policy": EMOTION_POLICY_VERSION},
            )
            return

        primary = self._select_reaction(chat_id, item, policy.emotion_class)
        if primary is None:
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="no_allowed_reaction_for_emotion",
                rationale=f"Skipped because no configured reaction emoji is allowed for emotion class {policy.emotion_class}.",
                score=score,
                confidence=policy.confidence,
                emotion_class=policy.emotion_class,
                severity_flags=policy.severity_flags,
                details={"policy": EMOTION_POLICY_VERSION},
            )
            return

        send_rationale = self._send_attempt_rationale(item, score, policy)
        if not send_rationale.strip():
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="insufficient_rationale",
                rationale="Skipped because no sufficient outbound reaction rationale was available.",
                score=score,
                confidence=policy.confidence,
                candidate_spec=primary,
                candidate_reaction_class=policy.emotion_class,
                emotion_class=policy.emotion_class,
                severity_flags=policy.severity_flags,
                details={"policy": EMOTION_POLICY_VERSION},
            )
            return

        self._record_decision(
            message,
            item,
            action="send_attempt",
            reason_code="eligible_score",
            rationale=send_rationale,
            score=score,
            confidence=policy.confidence,
            candidate_spec=primary,
            candidate_reaction_class=policy.emotion_class,
            emotion_class=policy.emotion_class,
            severity_flags=policy.severity_flags,
            details={"policy": EMOTION_POLICY_VERSION},
        )
        sent_spec = await self._send_reaction(message, primary, fallback=True)
        if sent_spec is None:
            self._record_decision(
                message,
                item,
                action="skipped",
                reason_code="send_failed",
                rationale="Skipped because Telegram did not accept the candidate reaction and no fallback was sent.",
                score=score,
                confidence=policy.confidence,
                candidate_spec=primary,
                candidate_reaction_class=policy.emotion_class,
                emotion_class=policy.emotion_class,
                severity_flags=policy.severity_flags,
                details={"policy": EMOTION_POLICY_VERSION},
            )
            return

        self._last_sent_at[chat_id] = now
        self._eligible_since_sent[chat_id] = 0
        self._sent_count += 1
        self._record_outbound_reaction(message, item, sent_spec)
        self._record_decision(
            message,
            item,
            action="sent",
            reason_code="sent",
            rationale=send_rationale,
            score=score,
            confidence=policy.confidence,
            candidate_spec=primary,
            candidate_reaction_class=policy.emotion_class,
            sent_spec=sent_spec,
            emotion_class=policy.emotion_class,
            severity_flags=policy.severity_flags,
            details={"policy": EMOTION_POLICY_VERSION},
        )
        self._emit(
            event_type="outbound_reaction_sent",
            message_obj=message,
            details={"reaction_key": sent_spec.reaction_key, "score": round(score, 3)},
        )

    def _is_eligible_message(self, message: Any, item: MemoryItem) -> bool:
        return self._eligibility_skip_reason(message, item) is None

    def _eligibility_skip_reason(self, message: Any, item: MemoryItem) -> str | None:
        if item.is_bot or item.message_id is None:
            return "bot_or_missing_message_id"
        chat_type = str(getattr(getattr(message, "chat", None), "type", "") or item.chat_type or "")
        if chat_type not in {ChatType.GROUP, ChatType.SUPERGROUP, "group", "supergroup"}:
            return "unsupported_chat_type"
        user = getattr(message, "from_user", None)
        if user is None or bool(getattr(user, "is_bot", False)):
            return "missing_or_bot_sender"
        text = self._message_text(message)
        stripped = text.strip()
        if stripped.startswith("/"):
            return "command"
        trigger = (self.config.bot_trigger or "").strip()
        if trigger and stripped.startswith(trigger):
            return "bot_trigger"
        username = str(self.bot_username_provider() or "").strip().lstrip("@")
        if username and re.search(rf"@{re.escape(username)}\b", text, flags=re.IGNORECASE):
            return "bot_mention"
        bot_id = self.bot_id_provider()
        reply = getattr(message, "reply_to_message", None)
        reply_user = getattr(reply, "from_user", None)
        if bot_id is not None and reply_user is not None and getattr(reply_user, "id", None) == bot_id:
            return "reply_to_bot"
        if item.attachment_type in {"sticker", "dice", "poll", "location", "contact"} and not (item.text or item.source_text):
            return "unsupported_attachment"
        content = self._content_for_scoring(item).strip()
        if len(content) < 8 and not (item.source_text or item.vision_summary or item.source_url):
            return "insufficient_content"
        if not content:
            return "empty_content"
        return None

    def relevance_score(self, message: Any, item: MemoryItem) -> float:
        content = self._content_for_scoring(item)
        text = self._message_text(message)
        score = 0.0
        length = len(content)
        if length >= 50:
            score += 0.2
        if length >= 120:
            score += 0.16
        if length >= 260:
            score += 0.12
        if item.source_text or item.source_title or item.source_url:
            score += 0.18
        if item.vision_summary or item.content_kind == "image":
            score += 0.14
        if "?" in text or "?" in content:
            score += 0.08
        if re.search(r"\b\d{2,}\b|[$€₴%]", content):
            score += 0.08
        if re.search(r"[!?]{2,}|[“”\"']|:", content):
            score += 0.06
        if re.search(r"\b(новина|реліз|ціна|скандал|проблема|питання|анонс|release|price|issue)\b", content, re.I):
            score += 0.08
        if len((item.text or "").strip()) < 12 and not (item.source_text or item.vision_summary):
            score -= 0.12
        return max(0.0, min(1.0, score))

    async def _send_reaction(self, message: Any, spec: ReactionSpec, *, fallback: bool) -> ReactionSpec | None:
        bot = self._message_bot(message)
        if bot is None or not hasattr(bot, "set_message_reaction"):
            self._emit(event_type="outbound_reaction_unavailable", message_obj=message)
            return None

        reaction = self._telegram_reaction(spec)
        try:
            await bot.set_message_reaction(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reaction=[reaction],
                is_big=bool(self.config.is_big),
            )
            return spec
        except BadRequest as exc:
            self._emit(
                level="warning",
                event_type="outbound_reaction_rejected",
                message_obj=message,
                message=str(exc),
                details={"reaction_key": spec.reaction_key},
            )
            if fallback and spec.reaction_type == "custom_emoji":
                fallback_spec = self._standard_fallback_spec(message.chat_id, None)
                if fallback_spec is not None:
                    return await self._send_reaction(message, fallback_spec, fallback=False)
            return None

    def _telegram_reaction(self, spec: ReactionSpec) -> Any:
        if spec.reaction_type == "custom_emoji" and spec.custom_emoji_id:
            return ReactionTypeCustomEmoji(custom_emoji_id=spec.custom_emoji_id)
        return ReactionTypeEmoji(emoji=spec.base_emoji or self.config.allowed_emoji[0])

    def _select_reaction(self, chat_id: int, item: MemoryItem, emotion_class: str = "positive_celebratory") -> ReactionSpec | None:
        if emotion_class == "positive_celebratory" and self.config.use_custom_emoji and self.reaction_memory is not None:
            for preference in self.reaction_memory.group_preferences(chat_id, limit=10):
                if preference.reaction_type == "custom_emoji" and preference.reaction_key.startswith("custom:"):
                    custom_id = preference.reaction_key.split(":", 1)[1]
                    if custom_id:
                        return ReactionSpec(
                            reaction_type="custom_emoji",
                            reaction_key=preference.reaction_key,
                            custom_emoji_id=custom_id,
                        )
        return self._standard_fallback_spec(chat_id, item, emotion_class)

    def _standard_fallback_spec(self, chat_id: int, item: MemoryItem | None, emotion_class: str = "positive_celebratory") -> ReactionSpec | None:
        configured = set(self.config.allowed_emoji)
        class_candidates = REACTION_EMOJI_BY_EMOTION.get(emotion_class, ())
        allowed = tuple(emoji for emoji in class_candidates if emoji in configured)
        if not class_candidates:
            allowed = tuple(emoji for emoji in self.config.allowed_emoji if emoji)
        if not allowed:
            return None
        basis = f"{chat_id}:{getattr(item, 'id', '')}:{getattr(item, 'text', '')[:80]}"
        digest = hashlib.sha256(basis.encode("utf-8")).digest()
        emoji = allowed[digest[0] % len(allowed)]
        return ReactionSpec(reaction_type="emoji", reaction_key=f"emoji:{emoji}", base_emoji=emoji)

    def _record_outbound_reaction(self, message: Any, item: MemoryItem, spec: ReactionSpec) -> None:
        if self.reaction_memory is None:
            return
        bot_id_raw = self.bot_id_provider()
        bot_id = int(bot_id_raw) if isinstance(bot_id_raw, int) or str(bot_id_raw or "").isdigit() else None
        bot_username = str(self.bot_username_provider() or "").strip().lstrip("@")
        raw_json = json.dumps({"outbound": True, "phase": "pre_embedding"}, ensure_ascii=False)
        self.reaction_memory.record_message_reaction_update(
            update_id=None,
            chat_id=item.chat_id,
            target_message_id=int(item.message_id or message.message_id),
            target_memory_id=item.id,
            actor_key=f"bot:{bot_id or bot_username or 'self'}",
            actor_kind="bot",
            actor_user_id=bot_id,
            actor_username=bot_username,
            old_specs=[],
            new_specs=[spec],
            received_at=datetime.now(timezone.utc),
            raw_json=raw_json,
        )

    def _record_decision(
        self,
        message: Any,
        item: MemoryItem | None,
        *,
        action: str,
        reason_code: str,
        rationale: str,
        score: float | None = None,
        confidence: float = 0.0,
        candidate_spec: ReactionSpec | None = None,
        candidate_reaction_class: str = "",
        sent_spec: ReactionSpec | None = None,
        emotion_class: str = "unclassified",
        severity_flags: tuple[str, ...] = (),
        details: dict[str, object] | None = None,
    ) -> None:
        if self.reaction_memory is None:
            return
        try:
            target_message_id = getattr(item, "message_id", None) if item is not None else getattr(message, "message_id", None)
            target_memory_id = getattr(item, "id", None) if item is not None else None
            chat_id = int(getattr(item, "chat_id", None) if item is not None else getattr(message, "chat_id", 0) or 0)
            self.reaction_memory.record_outbound_decision(
                chat_id=chat_id,
                target_message_id=target_message_id,
                target_memory_id=target_memory_id,
                item=item,
                policy_version=DECISION_POLICY_VERSION,
                phase="pre_embedding",
                action=action,
                reason_code=reason_code,
                rationale=rationale,
                severity_flags=self._merge_severity_flags(self._decision_feature_flags(item), severity_flags),
                emotion_class=emotion_class,
                confidence=confidence,
                score=score,
                candidate_spec=candidate_spec,
                candidate_reaction_class=candidate_reaction_class,
                sent_spec=sent_spec,
                details=details or {},
            )
        except Exception as exc:
            self._error_count += 1
            self._emit(
                level="warning",
                event_type="outbound_reaction_decision_record_failed",
                message_obj=message,
                message=type(exc).__name__,
            )

    def _decision_feature_flags(self, item: MemoryItem | None) -> tuple[str, ...]:
        if item is None:
            return ("missing_memory_item",)
        flags: list[str] = []
        if item.text:
            flags.append("has_text")
        if item.source_text or item.source_title or item.source_url:
            flags.append("source_context")
        if item.vision_summary:
            flags.append("vision_summary")
        if item.forward_origin:
            flags.append("forwarded")
        if item.content_kind:
            flags.append(f"content:{safe_code(item.content_kind)}")
        if item.attachment_type:
            flags.append(f"attachment:{safe_code(item.attachment_type)}")
        return tuple(flags)

    def _reaction_class(self, spec: ReactionSpec) -> str:
        value = spec.base_emoji or spec.reaction_key
        for emotion_class, candidates in REACTION_EMOJI_BY_EMOTION.items():
            if value in candidates:
                return emotion_class
        return "custom_or_unknown"

    def _send_attempt_rationale(self, item: MemoryItem, score: float, policy: EmotionPolicyDecision) -> str:
        return policy.rationale

    def _classify_emotion_policy(self, item: MemoryItem, score: float) -> EmotionPolicyDecision:
        own_content = (item.text or "").casefold()
        source_content = self._source_context_for_policy(item).casefold()
        combined_content = f"{own_content} {source_content}".strip()
        own_tokens = self._term_tokens(own_content)
        source_tokens = self._term_tokens(source_content)
        combined_tokens = self._term_tokens(combined_content)
        flags: list[str] = []
        has_own_text = bool((item.text or "").strip())
        has_source_context = bool(
            (item.source_text or item.source_title or item.source_url or item.vision_summary or item.forward_origin or "").strip()
        )
        if not has_own_text and has_source_context:
            flags.append("source_only")
        if self._has_any(combined_content, SARCASM_MEME_TERMS, combined_tokens):
            return EmotionPolicyDecision(
                emotion_class="ambiguous_sensitive",
                confidence=0.35,
                allow_reaction=False,
                reason_code="emotion_sarcasm_or_meme",
                rationale="Skipped because sarcasm or meme-like context makes the intended reaction ambiguous.",
                severity_flags=tuple(flags + ["sarcasm_or_meme"]),
            )

        own_grief = self._hit_count(own_content, GRIEF_TERMS, own_tokens)
        own_horror = self._hit_count(own_content, HORROR_TERMS, own_tokens)
        own_condemnation = self._hit_count(own_content, CONDEMNATION_TERMS, own_tokens)
        own_ambiguous = self._hit_count(own_content, AMBIGUOUS_TERMS, own_tokens)
        own_positive = self._hit_count(own_content, POSITIVE_TERMS, own_tokens)
        own_policy_signal = own_grief + own_horror + own_condemnation + own_ambiguous + own_positive
        if item.attachment_type in {"video", "animation"} and not item.vision_summary and not item.source_text and not own_policy_signal:
            return EmotionPolicyDecision(
                emotion_class="ambiguous_sensitive",
                confidence=0.25,
                allow_reaction=False,
                reason_code="emotion_incomplete_media_context",
                rationale="Skipped because media context was incomplete and the reaction could be misread.",
                severity_flags=tuple(flags + ["incomplete_media_context"]),
            )
        source_grief = self._hit_count(source_content, GRIEF_TERMS, source_tokens)
        source_horror = self._hit_count(source_content, HORROR_TERMS, source_tokens)
        source_condemnation = self._hit_count(source_content, CONDEMNATION_TERMS, source_tokens)
        source_ambiguous = self._hit_count(source_content, AMBIGUOUS_TERMS, source_tokens)
        source_sensitive = source_grief + source_horror + source_condemnation
        if not has_own_text and source_sensitive:
            flags.append("source_sensitive")
        # Source, URL, and vision summaries are untrusted context: they can add caution
        # after direct-chat text establishes a class, but they cannot drive a reaction.
        grief = own_grief + (min(source_grief, 1) if own_grief else 0)
        horror = own_horror + (min(source_horror, 1) if own_horror else 0)
        condemnation = own_condemnation + (min(source_condemnation, 1) if own_condemnation else 0)
        ambiguous = own_ambiguous + source_ambiguous
        positive = own_positive
        sensitive = grief + horror + condemnation
        source_penalty = 0.08 if has_source_context else 0.0

        if sensitive and ambiguous:
            return EmotionPolicyDecision(
                emotion_class="ambiguous_sensitive",
                confidence=max(0.35, min(0.65, 0.46 + 0.04 * sensitive)),
                allow_reaction=False,
                reason_code="emotion_sensitive_ambiguous",
                rationale="Skipped because sensitive content was present but the stance or context was ambiguous.",
                severity_flags=tuple(flags + ["sensitive", "ambiguous"]),
            )
        if condemnation >= 2 and horror + grief >= 1:
            confidence = min(0.96, 0.84 + 0.03 * condemnation + 0.02 * (horror + grief) - source_penalty)
            return self._emotion_decision(
                "condemnation_outrage",
                confidence,
                "Detected high-confidence condemnation of harm or injustice.",
                flags + ["sensitive", "condemnation"],
            )
        if grief >= 1:
            confidence = min(0.94, 0.78 + 0.04 * grief + 0.01 * horror - source_penalty)
            return self._emotion_decision(
                "grief_sympathy",
                confidence,
                "Detected grief, loss, victims, or death; positive reactions are not allowed.",
                flags + ["sensitive", "grief"],
            )
        if horror >= 1:
            confidence = min(0.90, 0.72 + 0.04 * horror + 0.02 * condemnation - source_penalty)
            return self._emotion_decision(
                "horror_shock",
                confidence,
                "Detected shocking or frightening news; celebratory reactions are not allowed.",
                flags + ["sensitive", "shock"],
            )
        if sensitive:
            confidence = min(0.82, 0.62 + 0.04 * sensitive - source_penalty)
            return self._emotion_decision(
                "despair_heavy_news",
                confidence,
                "Detected heavy or sensitive news without enough confidence for a stronger class.",
                flags + ["sensitive", "heavy_news"],
            )
        if positive >= 1 and has_own_text and not has_source_context:
            confidence = min(0.88, 0.62 + 0.04 * positive + 0.08 * score)
            return self._emotion_decision(
                "positive_celebratory",
                confidence,
                "Detected safely positive direct-chat content.",
                flags + ["safe_positive"],
            )
        if positive >= 2 and has_own_text and not item.forward_origin:
            confidence = min(0.80, 0.58 + 0.04 * positive + 0.06 * score - source_penalty)
            return self._emotion_decision(
                "positive_celebratory",
                confidence,
                "Detected positive content with enough direct-chat context.",
                flags + ["safe_positive"],
            )
        if ambiguous and not sensitive and has_own_text and not has_source_context:
            confidence = min(0.84, 0.58 + 0.05 * ambiguous + 0.04 * score)
            return self._emotion_decision(
                "uncertainty_doubt",
                confidence,
                "Detected non-sensitive uncertainty where a doubt reaction cannot endorse harm.",
                flags + ["uncertainty"],
            )
        return EmotionPolicyDecision(
            emotion_class="ambiguous_low_confidence",
            confidence=max(0.2, min(0.55, score)),
            allow_reaction=False,
            reason_code="emotion_low_confidence",
            rationale="Skipped because the emotion policy did not find a safe high-confidence reaction class.",
            severity_flags=self._merge_severity_flags(flags, ("low_confidence",)),
        )

    def _emotion_decision(self, emotion_class: str, confidence: float, rationale: str, flags: list[str]) -> EmotionPolicyDecision:
        threshold = EMOTION_CONFIDENCE_MIN.get(emotion_class, 1.0)
        allow = confidence >= threshold
        reason = "emotion_policy_allowed" if allow else "emotion_policy_low_confidence"
        return EmotionPolicyDecision(
            emotion_class=emotion_class,
            confidence=max(0.0, min(1.0, confidence)),
            allow_reaction=allow,
            reason_code=reason,
            rationale=rationale if allow else f"Skipped because {emotion_class} confidence was below the policy threshold.",
            severity_flags=self._merge_severity_flags(flags),
        )

    @staticmethod
    def _hit_count(content: str, terms: tuple[str, ...], tokens: list[str] | None = None) -> int:
        if not content:
            return 0
        normalized = content.casefold()
        term_tokens = tokens if tokens is not None else OutboundReactionAdapter._term_tokens(normalized)
        return sum(1 for term in terms if OutboundReactionAdapter._term_matches(normalized, term_tokens, term))

    @staticmethod
    def _has_any(content: str, terms: tuple[str, ...], tokens: list[str] | None = None) -> bool:
        return OutboundReactionAdapter._hit_count(content, terms, tokens) > 0

    @staticmethod
    def _term_tokens(content: str) -> list[str]:
        if not content:
            return []
        return TERM_WORD_RE.findall(content.casefold())

    @staticmethod
    def _term_matches(content: str, tokens: list[str], term: str) -> bool:
        normalized = term.casefold().strip()
        if not normalized:
            return False
        benign_phrases = BENIGN_TERM_CONTEXTS.get(normalized, ())
        if benign_phrases:
            for phrase in benign_phrases:
                content = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", content)
            tokens = TERM_WORD_RE.findall(content)
        if " " in normalized:
            return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", content) is not None
        if normalized in STEM_TERMS or not normalized.isascii():
            return any(token.startswith(normalized) for token in tokens)
        return normalized in tokens

    @staticmethod
    def _merge_severity_flags(*flag_groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        merged: list[str] = []
        seen: set[str] = set()
        for flags in flag_groups:
            for flag in flags:
                if flag and flag not in seen:
                    seen.add(flag)
                    merged.append(flag)
        return tuple(merged)

    def _source_context_for_policy(self, item: MemoryItem) -> str:
        values = [
            item.source_title,
            item.source_text,
            item.vision_summary,
            item.source_url,
        ]
        return " ".join(value for value in values if value)

    def _content_for_scoring(self, item: MemoryItem) -> str:
        values = [
            item.text,
            item.source_title,
            item.source_text,
            item.vision_summary,
            item.source_url,
        ]
        return " ".join(value for value in values if value)

    def _message_text(self, message: Any) -> str:
        return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")

    def _message_bot(self, message: Any) -> Any:
        get_bot = getattr(message, "get_bot", None)
        if callable(get_bot):
            try:
                return get_bot()
            except Exception:
                return None
        return getattr(message, "bot", None)

    def _emit(
        self,
        *,
        event_type: str,
        message_obj: Any,
        level: str = "info",
        message: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.event_callback(
                level=level,
                event_type=event_type,
                chat_id=int(getattr(message_obj, "chat_id", 0) or 0),
                user_id=getattr(getattr(message_obj, "from_user", None), "id", None),
                message=message,
                details=details or {},
            )
        except Exception:
            return
