from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.constants import ChatType
from telegram.error import BadRequest
from PIL import Image


TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"aigan-test-{os.getpid()}.sqlite3")
_CONFIGURED = False

_TEST_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:test-token",
    "OPENAI_API_KEY": "sk-test",
    # Pin historical fixtures; default-model tests clear these explicit settings.
    "OPENAI_MODEL": "gpt-5.6-sol",
    "AGENTS_TRACING_MODE": "disabled",
    "LOG_LEVEL": "CRITICAL",
    "ALLOWED_CHAT_IDS": "-1001",
    "ADMIN_USER_IDS": "42424242",
    "AUTO_REACT_ENABLED": "false",
    "BOT_TIMEZONE": "America/New_York",
    "MAX_REPLY_CHARS": "12000",
    "TELEGRAM_TEXT_CHUNK_CHARS": "3500",
    "MAX_REPLY_CHUNKS": "4",
    "CHAT_INFLIGHT_GUARD_ENABLED": "true",
    "CHAT_DUPLICATE_SUPPRESS_SECONDS": "45",
    "CHAT_DUPLICATE_SIMILARITY_THRESHOLD": "0.72",
    "CHAT_INFLIGHT_SUPPRESS_ORDINARY_AUTO_REACT": "true",
    "FOLLOWUP_DEBOUNCE_SECONDS": "0.5",
    "PROACTIVE_ENABLED": "false",
    "PROACTIVE_IDLE_ONLY": "true",
    "PROACTIVE_IDLE_SECONDS": "21600",
    "PROACTIVE_MIN_SECONDS_BETWEEN_POSTS": "21600",
    "PROACTIVE_PERSONA_MODE": "thought_seed",
    "PROACTIVE_REGENERATE_ON_PERSONA_REJECT": "true",
    "PROACTIVE_PERSONAL_PING_ENABLED": "true",
    "PROACTIVE_PERSONAL_PING_PROBABILITY": "0.35",
    "PROACTIVE_PERSONAL_PING_MIN_USER_IDLE_SECONDS": "86400",
    "PROACTIVE_PERSONAL_PING_COOLDOWN_SECONDS": "259200",
    "PROACTIVE_PERSONAL_PING_MAX_CANDIDATES": "5",
    "PROACTIVE_DIRECTION_WEIGHTS": "group_taste:0.25,personal_ping:0.25,current_hook:0.25,unanswered_thread:0.25",
    "PROACTIVE_SELF_REFERENCE_GUARD": "true",
    "PROACTIVE_META_TOPIC_GUARD": "true",
    "PROACTIVE_META_TOPIC_STRICT": "true",
    "PROACTIVE_RECENT_SEED_COOLDOWN_DAYS": "14",
    "REMINDERS_ENABLED": "false",
    "REMINDER_TOOL_ENABLED": "true",
    "REMINDER_CRUD_TOOLS_ENABLED": "true",
    "REMINDER_POLL_SECONDS": "60",
    "REMINDER_MAX_DUE_PER_TICK": "5",
    "REMINDER_MISFIRE_GRACE_SECONDS": "86400",
    "REMINDER_CONTEXT_REQUEST_TTL_SECONDS": "86400",
    "TOOL_ROUTER_ENABLED": "false",
    "TOOL_ROUTER_MODEL": "gpt-5.4-nano",
    "TOOL_ROUTER_MAX_OUTPUT_TOKENS": "120",
    "TOOL_ROUTER_CONFIDENCE_THRESHOLD": "0.65",
    "MODEL_ROUTING_MODE": "off",
    "MODEL_ROUTING_POLICY_VERSION": "primary_sol_low_v1",
    "MODEL_ROUTER_MODEL": "gpt-5.4-nano",
    "MODEL_ROUTER_REASONING_EFFORT": "none",
    "MODEL_ROUTER_SCHEMA_VERSION": "model_policy_v1",
    "MODEL_ROUTER_PROMPT_VERSION": "model_policy_prompt_v1",
    "MODEL_TIER_ECONOMY_MODEL": "gpt-5.4-nano",
    "MODEL_TIER_BALANCED_MODEL": "gpt-5.6-terra",
    "MODEL_TIER_PREMIUM_MODEL": "gpt-5.6-sol",
    "PROMPT_PRIVACY_GUARD_ENABLED": "true",
    "VISION_MODEL": "",
    "VISION_INTERACTIVE_MODEL": "gpt-5.6-sol",
    "VISION_INTERACTIVE_REASONING_EFFORT": "low",
    "VISION_BACKGROUND_MODEL": "gpt-5.4-mini",
    "VISION_BACKGROUND_REASONING_EFFORT": "none",
    "IMAGE_CANDIDATE_REVIEW_MODEL": "gpt-5.4-mini",
    "MEMORY_ENABLED": "true",
    "MEMORY_DB_PATH": TEST_DB_PATH,
    "MEMORY_CONTEXT_MESSAGES": "10",
    "MEMORY_FOLLOWUP_CONTEXT_MESSAGES": "40",
    "MEMORY_THREAD_CONTEXT_DEPTH": "6",
    "MEMORY_RETENTION_DAYS": "30",
    "MEMORY_IMAGE_SUMMARY_LIMIT": "3",
    "MEMORY_VECTOR_ENABLED": "true",
    "MEMORY_EMBEDDING_MODEL": "text-embedding-3-small",
    "MEMORY_EMBEDDING_DIMENSIONS": "4",
    "MEMORY_SEMANTIC_LOOKBACK_DAYS": "30",
    "MEMORY_SEMANTIC_TOP_K": "3",
    "MEMORY_EMBEDDING_BATCH_SIZE": "2",
    "MEMORY_VECTOR_BACKFILL_ON_START": "false",
    "MEMORY_VECTOR_BACKFILL_LIMIT": "10",
    "MEMORY_RECALL_INTENT_THRESHOLD": "0.62",
    "MEMORY_RECALL_INTENT_AMBIGUOUS_THRESHOLD": "0.48",
    "MCP_TOOL_TIMEOUT_SECONDS": "30",
    "WEB_SEARCH_TIMEOUT_SECONDS": "15",
    "SYSTEM_LOG_ENABLED": "true",
    "SYSTEM_LOG_RETENTION_DAYS": "14",
    "GITHUB_REPORTING_ENABLED": "false",
    "GITHUB_PROJECT_ADD_ENABLED": "false",
    "COMPLAINT_LOOKBACK_SECONDS": "86400",
    "COMPLAINT_REPORT_TEMPERATURE": "3",
    "SOCIAL_MEMORY_ENABLED": "true",
    "SOCIAL_MEMORY_EXTRACT_EVERY_MESSAGES": "20",
    "SOCIAL_MEMORY_CONFIDENCE_THRESHOLD": "0.65",
    "SOCIAL_PROFILE_RETENTION_DAYS": "180",
    "REACTIONS_ENABLED": "true",
    "REACTION_ASSET_ANALYSIS_ENABLED": "true",
    "REACTION_ASSET_MIN_USES_FOR_VISION": "3",
    "REACTION_ANALYSIS_PROMPT_VERSION": "1",
    "REACTION_ASSET_MAX_BYTES": "2000000",
    "OUTBOUND_REACTIONS_ENABLED": "false",
    "OUTBOUND_REACTION_EVERY_N_MESSAGES": "10",
    "OUTBOUND_REACTION_COOLDOWN_SECONDS": "1800",
    "OUTBOUND_REACTION_MIN_SCORE": "0.72",
    "OUTBOUND_REACTION_ALLOWED_EMOJI": "fire,eyes,thumbs_up,thinking,laugh,sad,broken_heart,shock,fear,angry",
    "OUTBOUND_REACTION_USE_CUSTOM_EMOJI": "true",
    "OUTBOUND_REACTION_BIG": "false",
    "MEDIA_ACQUISITION_ENABLED": "false",
    "MEDIA_ACQUISITION_MAX_DURATION_SECONDS": "180",
    "MEDIA_ACQUISITION_MAX_DOWNLOAD_BYTES": "50000000",
    "MEDIA_ACQUISITION_SOCKET_TIMEOUT_SECONDS": "12",
    "HEAVY_MODEL_ENABLED": "false",
}


def configure_test_environment() -> None:
    global _CONFIGURED
    if not _CONFIGURED:
        try:
            os.remove(TEST_DB_PATH)
        except FileNotFoundError:
            pass
        _CONFIGURED = True
    os.environ.update(_TEST_ENV)


_jpeg_stream = io.BytesIO()
Image.new("RGB", (16, 16), (120, 80, 40)).save(_jpeg_stream, format="JPEG", quality=95)
VALID_JPEG = _jpeg_stream.getvalue()


class FakeUser:
    def __init__(self, user_id: int = 42424242, username: str = "tester") -> None:
        self.id = user_id
        self.is_bot = False
        self.full_name = "Test User"
        self.username = username


class FakeMessage:
    def __init__(
        self,
        text: str = "",
        chat_type: str = ChatType.SUPERGROUP,
        chat_id: int = -1001,
        message_id: int = 101,
    ) -> None:
        self.text = text
        self.caption = None
        self.chat_id = chat_id
        self.message_id = message_id
        self.date = datetime.now(timezone.utc)
        self.chat = SimpleNamespace(type=chat_type, title="Test Chat")
        self.from_user = FakeUser()
        self.photo = []
        self.document = None
        self.reply_to_message = None
        self.external_reply = None
        self.forward_origin = None
        self.forward_from = None
        self.forward_from_chat = None
        self.forward_sender_name = None
        self.forward_date = None
        self.is_automatic_forward = False
        self.quote = None
        self.entities = None
        self.media_group_id = None
        self.message_thread_id = None
        self.reply_calls = []
        self.photo_calls = []
        self.photo_failures = 0
        self.media_group_calls = []
        self.media_group_attempts = 0
        self.media_group_failures = 0
        self.bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
            set_message_reaction=AsyncMock(return_value=True),
        )

    async def reply_text(self, text: str, **kwargs):
        self.last_reply = text
        self.reply_calls.append({"text": text, **kwargs})
        return SimpleNamespace(
            message_id=self.message_id + 10_000 + len(self.reply_calls),
            chat=self.chat,
            date=datetime.now(timezone.utc),
            reply_to_message=self,
        )

    async def reply_photo(self, photo, **kwargs):
        if self.photo_failures > 0:
            self.photo_failures -= 1
            raise BadRequest("failed to send photo")
        self.photo_calls.append({"photo": photo, **kwargs})
        return SimpleNamespace(
            message_id=self.message_id + 20_000 + len(self.photo_calls),
            chat=self.chat,
            date=datetime.now(timezone.utc),
            reply_to_message=self,
        )

    async def reply_media_group(self, media, **kwargs):
        self.media_group_attempts += 1
        if self.media_group_failures > 0:
            self.media_group_failures -= 1
            raise BadRequest("failed to send media group")
        self.media_group_calls.append({"media": tuple(media), **kwargs})
        return tuple(
            SimpleNamespace(
                message_id=self.message_id + 30_000 + index,
                chat=self.chat,
                date=datetime.now(timezone.utc),
                reply_to_message=self,
            )
            for index, _ in enumerate(media, start=1)
        )

    def get_bot(self):
        return self.bot
