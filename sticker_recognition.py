"""Demand-driven sticker recognition; durable reuse precedes all pixel I/O."""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Awaitable, Callable

from sticker_cache import StickerCache, make_cache_key
from sticker_media import STICKER_RECOGNITION_PROMPT_VERSION, normalize_sticker_image

RECOGNITION_VERSION = STICKER_RECOGNITION_PROMPT_VERSION
_operations: set[asyncio.Task] = set()


def _operation_finished(task: asyncio.Task) -> None:
    _operations.discard(task)
    if not task.cancelled():
        task.exception()  # Retrieve failures even when the original waiter left.


class StickerUnavailable(ValueError):
    """Only a bounded reason code crosses the adapter boundary."""


async def recognize_sticker(
    cache: StickerCache, *, file_unique_id: str, representation: str,
    load_image: Callable[[], Awaitable[bytes]],
    recognize: Callable[[str, str], Awaitable[dict]],
    max_bytes: int = 6_000_000, wait_seconds: float = 125, refresh_reason: str = "",
) -> dict:
    if not file_unique_id:
        raise StickerUnavailable("sticker_identity_unavailable")
    if representation not in {"static_original", "thumbnail"}:
        raise StickerUnavailable("sticker_preview_unavailable")
    key = make_cache_key(file_unique_id, representation, RECOGNITION_VERSION)
    if refresh_reason not in {"", "missing_detail", "correction"}:
        raise StickerUnavailable("sticker_refresh_reason_invalid")
    refresh_image = None
    if refresh_reason:
        # A warm occurrence may have no retained pixels. Do not destroy shared
        # successful evidence until this authorized source can actually be read.
        refresh_image = normalize_sticker_image(
            await asyncio.wait_for(load_image(), timeout=30), max_bytes=max_bytes)
        cache.request_refresh(key)
    deadline = time.monotonic() + wait_seconds
    while True:
        cached = cache.get(key)
        if cached is not None:
            return cached
        owner = cache.claim(key, lease_seconds=180)
        if owner is not None:
            break
        state = cache.state(key)
        if state == "ready":
            continue
        if state != "busy" or time.monotonic() >= deadline:
            raise StickerUnavailable("sticker_recognition_retry_later")
        await asyncio.sleep(0.05)
    async def run() -> dict:
        try:
            image = refresh_image or normalize_sticker_image(
                await asyncio.wait_for(load_image(), timeout=30), max_bytes=max_bytes)
            data_url = "data:image/png;base64," + base64.b64encode(image.data).decode("ascii")
            cache.record_recognition_call()
            result = dict(await recognize(data_url, representation))
            result.update(representation=representation, prompt_version=RECOGNITION_VERSION,
                          content_sha256=image.sha256)
            if not cache.complete(key, owner, result):
                raise StickerUnavailable("sticker_result_not_persisted")
            return result
        except BaseException:
            cache.fail(key, owner, retry_seconds=30)
            raise

    # A cancelled HTTP waiter does not terminate a blocking provider worker.
    # Keep the claim and persistence task alive until that operation completes.
    # The production provider has a shorter explicit timeout and no SDK retries.
    task = asyncio.create_task(asyncio.wait_for(run(), timeout=150))
    _operations.add(task)
    task.add_done_callback(_operation_finished)
    return await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
