"""Explicit Telegram submission of selected media for an authenticity check."""
import asyncio
from collections.abc import Callable

from telegram import Update
from telegram.ext import ContextTypes

from sensity_media import SENSITY_MAX_BYTES, SENSITY_MIME_TYPES, check_sensity_media


SENSITY_USAGE = (
    "Sensity — зовнішній сервіс перевірки медіа. Для перевірки Aigan надішле йому "
    "вміст одного вибраного файлу. Надішли в цей чат власне фото, відео або аудіо "
    "до 20 МБ, яке маєш право передавати, і відповідай на нього командою /sensity send."
)
_DISABLED = "Доступ до перевірки Sensity у цьому чаті вимкнено."
_SELECTION = (
    "Відповідай командою /sensity send на власне фото, відео або аудіо в цьому чаті. "
    "Для пересланих або захищених повідомлень перевірка недоступна."
)
_FILE_ERROR = "Не вдалося завантажити вибраний файл із Telegram для перевірки."
_SIZE_ERROR = "Для Sensity потрібен непорожній файл із відомим розміром до 20 МБ."
_SLOT = asyncio.Semaphore(1)


def _selected_media(message):
    source = message.reply_to_message
    author = message.from_user
    if (
        source is None or author is None or author.is_bot
        or getattr(source.from_user, "id", None) != author.id
        or source.chat_id != message.chat_id
        or any(getattr(item, "sender_chat", None) or getattr(item, "forward_origin", None)
               for item in (message, source))
        or getattr(source, "has_protected_content", False)
    ):
        return None, ""
    if source.photo:
        return source.photo[-1], "image/jpeg"
    for kind in ("video", "audio", "voice", "video_note", "animation", "document"):
        attachment = getattr(source, kind, None)
        if attachment:
            if kind == "video_note":
                return attachment, "video/mp4"
            mime = getattr(attachment, "mime_type", None) or ""
            return attachment, mime.partition(";")[0].strip().lower()
    return None, ""


async def handle_sensity_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, api_key: str,
    is_allowed: Callable[[], bool],
) -> None:
    message = update.message
    if message is None:
        return
    if context.args != ["send"]:
        await message.reply_text(SENSITY_USAGE)
        return
    if not is_allowed():
        await message.reply_text(_DISABLED)
        return
    if not api_key or not api_key.strip():
        await message.reply_text("Оператор ще не налаштував ключ Sensity.")
        return

    attachment, mime = _selected_media(message)
    if attachment is None:
        await message.reply_text(_SELECTION)
        return
    if mime not in SENSITY_MIME_TYPES:
        formats = ", ".join(sorted({ext.upper() for ext in SENSITY_MIME_TYPES.values()}))
        await message.reply_text(f"Цей формат файлу не підтримується. Доступні формати: {formats}.")
        return
    if _SLOT.locked():
        await message.reply_text("Sensity вже виконує перевірку. Спробуй ще раз трохи пізніше.")
        return
    async with _SLOT:
        result = await _inspect_attachment(attachment, mime, api_key, is_allowed)
    await message.reply_text(result if is_allowed() else _DISABLED, parse_mode=None)


async def _inspect_attachment(attachment, mime, api_key, is_allowed):
    declared_size = getattr(attachment, "file_size", None)
    if declared_size is not None and not 0 < declared_size <= SENSITY_MAX_BYTES:
        return _SIZE_ERROR

    try:
        remote = await attachment.get_file()
        remote_size = getattr(remote, "file_size", None)
        known_size = declared_size if remote_size is None else remote_size
        if not known_size or not 0 < known_size <= SENSITY_MAX_BYTES:
            return _SIZE_ERROR
        # Telegram's hosted Bot API caps downloads at 20 MB. Refuse unknown
        # sizes before asking its client to buffer anything in memory.
        if not is_allowed():
            return _DISABLED
        data = await remote.download_as_bytearray()
    except Exception:
        # Telegram exceptions may contain a file URL with bot credentials.
        return _FILE_ERROR
    if len(data) != known_size or (declared_size is not None and len(data) != declared_size):
        return _FILE_ERROR
    if not is_allowed():
        return _DISABLED
    try:
        return await check_sensity_media(bytes(data), mime, api_key, is_allowed=is_allowed)
    except Exception:
        return "Не вдалося отримати оцінку від Sensity."
