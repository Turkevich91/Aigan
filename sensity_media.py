"""Authenticity checks for an explicitly supplied attachment using Sensity v2."""

import asyncio
import json
from collections.abc import Callable
from uuid import UUID

import httpx

from public_media_http import PublicMediaTransferError, bounded_chunks


SENSITY_MAX_BYTES = 20_000_000
SENSITY_MIME_TYPES = {
    "image/jpeg": "jpg", "image/png": "png", "image/tiff": "tiff",
    "image/gif": "gif", "image/webp": "webp",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    "video/x-matroska": "mkv", "video/x-msvideo": "avi", "video/x-flv": "flv",
    "video/3gpp": "3gpp", "video/x-m4v": "m4v",
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/ogg": "ogg", "audio/aac": "aac", "audio/flac": "flac",
    "audio/x-flac": "flac", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/vnd.wave": "wav", "audio/x-matroska": "mka",
}
_SUBMISSIONS = "https://api.sensity.ai/v2/submissions"
_DEADLINE_SECONDS = 90
_RESULTS = {
    "suspicious": "Sensity виявив можливі ознаки маніпуляції у файлі. Це автоматична оцінка.",
    "valid": "Sensity не виявив ознак маніпуляції під час виконаних перевірок. "
             "Ця оцінка не підтверджує походження файлу.",
}


class _CheckError(Exception):
    """Carries only local messages suitable for the requester."""


async def _request(transport, method, url, *, read_json=False, **kwargs):
    request = httpx.Request(method, url, **kwargs)
    # Use the transport directly so the HTTP client never logs a signed upload URL.
    response = await transport.handle_async_request(request)
    try:
        if response.status_code == 429:
            raise _CheckError("Sensity тимчасово обмежив кількість запитів.")
        if not response.is_success:
            raise _CheckError("Сервіс Sensity не виконав запит. Результат перевірки недоступний.")
        if read_json:
            body = bytearray()
            async for block in bounded_chunks(response, 2_000_000):
                body.extend(block)
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise ValueError("invalid response")
            return parsed
    finally:
        await response.aclose()


async def check_sensity_media(
    data: bytes, mime_type: str, api_key: str, *, is_allowed: Callable[[], bool],
) -> str:
    """Check one attachment. The caller supplies a live chat-permission check."""
    if not api_key:
        return "Для Sensity не налаштовано ключ API."
    extension = SENSITY_MIME_TYPES.get(mime_type.partition(";")[0].strip().lower())
    if not extension or not isinstance(data, bytes) or not data:
        return "Надішліть непорожнє зображення, аудіо або відео у підтримуваному форматі."
    if len(data) > SENSITY_MAX_BYTES:
        return "Файл перевищує обмеження у 20 МБ."

    def require_permission():
        if not is_allowed():
            raise _CheckError("Перевірку зупинено. Sensity вимкнено для цього чату.")

    headers = {"apikey": api_key, "Accept-Encoding": "identity"}
    try:
        async with asyncio.timeout(_DEADLINE_SECONDS), httpx.AsyncHTTPTransport(trust_env=False) as transport:
            require_permission()
            slot = await _request(
                transport, "POST", f"{_SUBMISSIONS}/upload/", headers=headers, read_json=True,
                json={"filename": f"attachment.{extension}", "product_group": "deepfake_detection"},
            )
            if not isinstance(slot.get("submission_id"), str) or not isinstance(slot.get("upload_url"), str):
                raise ValueError("invalid upload slot")
            submission = f"{_SUBMISSIONS}/{UUID(slot['submission_id'])}/"
            upload_url = httpx.URL(slot["upload_url"])
            if (upload_url.scheme != "https" or upload_url.host != "storage.googleapis.com"
                    or upload_url.port not in (None, 443) or upload_url.userinfo or upload_url.fragment):
                raise ValueError("invalid storage URL")
            require_permission()
            await _request(transport, "PUT", upload_url, content=data,
                           headers={"Content-Type": "application/octet-stream"})
            require_permission()
            await _request(transport, "POST", submission + "complete/", headers=headers)
            delay = 3
            while True:
                require_permission()
                detail = await _request(transport, "GET", submission, headers=headers, read_json=True)
                require_permission()
                status = detail.get("status")
                if status == "completed":
                    return _RESULTS[detail["result"]]
                if status == "failed":
                    return "Sensity не зміг проаналізувати файл."
                if status == "skipped":
                    return "Sensity пропустив перевірку цього файлу. Оцінка відсутня."
                if status not in ("pending_upload", "uploaded", "ingesting", "pending", "in_progress"):
                    raise ValueError("unknown status")
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)
    except _CheckError as exc:
        return str(exc)
    except TimeoutError:
        return ("Час очікування відповіді Sensity минув. Перевірка могла продовжитися на сервісі. "
                "Перед повторним надсиланням перевірте стан у Sensity.")
    except (httpx.HTTPError, httpx.InvalidURL, PublicMediaTransferError, ValueError, TypeError, KeyError):
        return "Не вдалося отримати результат від Sensity."
