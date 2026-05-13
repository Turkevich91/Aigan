from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from memory import EmbeddingCandidate, MemoryStore

DEFAULT_DB_PATH = "/app/data/aigan.sqlite3"
DEFAULT_IMPORT_FILE = "/app/imports/result.json"
IMAGE_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass
class ImportOptions:
    file: Path
    chat_id: int
    db_path: Path
    days: int | None = None
    include_service: bool = False
    copy_media: bool = False
    dry_run: bool = False
    embed_missing: bool = False
    embedding_limit: int = 10000
    retention_days: int = 30
    image_max_bytes: int = 6_000_000
    bot_username: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 512
    embedding_batch_size: int = 64
    semantic_lookback_days: int = 30


@dataclass
class ImportSummary:
    scanned: int = 0
    skipped_service: int = 0
    skipped_old: int = 0
    skipped_empty: int = 0
    skipped_invalid: int = 0
    imported: int = 0
    inserted: int = 0
    updated: int = 0
    media_copied: int = 0
    media_skipped: int = 0
    bot_messages: int = 0
    fts_indexed: int = 0
    embeddings_stored: int = 0
    embedding_error: str = ""


def load_dotenv_defaults(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        os.environ.setdefault(key, value.strip().strip("'\""))


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def telegram_text_to_plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


def message_text(message: dict[str, Any]) -> str:
    parts = [
        telegram_text_to_plain(message.get("text")),
        telegram_text_to_plain(message.get("caption")),
    ]
    return "\n".join(part.strip() for part in parts if part and part.strip()).strip()


def parse_user_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d+)", value)
    return int(match.group(1)) if match else None


def parse_export_datetime(message: dict[str, Any]) -> datetime | None:
    unix_time = message.get("date_unixtime")
    if unix_time not in (None, ""):
        try:
            return datetime.fromtimestamp(int(str(unix_time)), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass

    raw_date = message.get("date")
    if isinstance(raw_date, str) and raw_date:
        try:
            parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def is_service_message(message: dict[str, Any]) -> bool:
    return str(message.get("type") or "").lower() != "message"


def sender_label(message: dict[str, Any]) -> str:
    for key in ("from", "actor", "author"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Telegram export"


def forward_origin(message: dict[str, Any]) -> str:
    for key in ("forwarded_from", "saved_from", "via_bot"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_bot_sender(label: str, bot_username: str) -> bool:
    normalized = re.sub(r"\s+", " ", label.strip().lower())
    username = bot_username.strip().lower().lstrip("@")
    candidates = {"aigan", "aigan 👾"}
    if username:
        candidates.add(username)
        candidates.add("@" + username)
    return normalized in candidates


def detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def media_source_path(message: dict[str, Any], export_dir: Path) -> Path | None:
    for key in ("photo", "file"):
        raw = message.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        if raw.startswith("("):
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = export_dir / path
        if path.is_file():
            return path
    return None


def copy_export_media(
    *,
    store: MemoryStore,
    message: dict[str, Any],
    options: ImportOptions,
    message_id: int,
) -> tuple[str, str, str] | None:
    source = media_source_path(message, options.file.parent)
    if source is None:
        return None
    try:
        if source.stat().st_size <= 0 or source.stat().st_size > options.image_max_bytes:
            return None
        data = source.read_bytes()
    except OSError:
        return None
    mime_type = detect_image_mime(data)
    if mime_type is None:
        return None

    media_dir = store.media_dir / str(options.chat_id)
    media_dir.mkdir(parents=True, exist_ok=True)
    suffix = IMAGE_SUFFIXES[mime_type]
    destination = media_dir / f"imported-{message_id}{suffix}"
    shutil.copyfile(source, destination)
    return str(destination), mime_type, "photo" if message.get("photo") else "image_document"


def should_import_message(message: dict[str, Any], options: ImportOptions, cutoff: datetime | None) -> tuple[bool, str]:
    if is_service_message(message) and not options.include_service:
        return False, "service"
    created_at = parse_export_datetime(message)
    if created_at is None:
        return False, "invalid"
    if cutoff is not None and created_at < cutoff:
        return False, "old"
    has_media_ref = media_source_path(message, options.file.parent) is not None or bool(message.get("photo"))
    if not message_text(message) and not has_media_ref:
        return False, "empty"
    return True, ""


def import_export(options: ImportOptions) -> ImportSummary:
    summary = ImportSummary()
    data = json.loads(options.file.read_text(encoding="utf-8"))
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ValueError("Telegram export JSON must contain a top-level 'messages' list")

    cutoff = None
    if options.days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, options.days))

    store: MemoryStore | None = None if options.dry_run else MemoryStore(options.db_path, options.retention_days)
    try:
        for message in messages:
            if not isinstance(message, dict):
                summary.skipped_invalid += 1
                continue
            summary.scanned += 1
            should_import, reason = should_import_message(message, options, cutoff)
            if not should_import:
                if reason == "service":
                    summary.skipped_service += 1
                elif reason == "old":
                    summary.skipped_old += 1
                elif reason == "empty":
                    summary.skipped_empty += 1
                else:
                    summary.skipped_invalid += 1
                continue

            try:
                message_id = int(message["id"])
            except (KeyError, TypeError, ValueError):
                summary.skipped_invalid += 1
                continue

            created_at = parse_export_datetime(message)
            if created_at is None:
                summary.skipped_invalid += 1
                continue

            label = sender_label(message)
            bot_message = is_bot_sender(label, options.bot_username)
            if bot_message:
                summary.bot_messages += 1

            text = message_text(message)
            content_kind = "text"
            attachment_type = ""
            local_media_path = ""
            mime_type = ""
            raw_note = "imported from Telegram Desktop export"

            media_ref_present = media_source_path(message, options.file.parent) is not None or bool(message.get("photo"))
            if media_ref_present:
                content_kind = "image"
                attachment_type = "photo" if message.get("photo") else "image_document"

            if store is not None and options.copy_media:
                copied = copy_export_media(store=store, message=message, options=options, message_id=message_id)
                if copied is None and media_ref_present:
                    summary.media_skipped += 1
                elif copied is not None:
                    local_media_path, mime_type, attachment_type = copied
                    content_kind = "image"
                    summary.media_copied += 1

            if options.dry_run:
                summary.imported += 1
                continue

            assert store is not None
            existed = store.message_by_message_id(options.chat_id, message_id) is not None
            store.save_message(
                chat_id=options.chat_id,
                message_id=message_id,
                chat_type="supergroup",
                created_at=created_at,
                sender_label=label,
                user_id=parse_user_id(message.get("from_id") or message.get("actor_id")),
                username="",
                is_bot=bot_message,
                text=text,
                content_kind=content_kind,
                attachment_type=attachment_type,
                local_media_path=local_media_path,
                mime_type=mime_type,
                reply_to_message_id=parse_user_id(message.get("reply_to_message_id")),
                forward_origin=forward_origin(message),
                raw_note=raw_note,
            )
            summary.imported += 1
            if existed:
                summary.updated += 1
            else:
                summary.inserted += 1

        if store is not None:
            summary.fts_indexed = store.rebuild_search_index()
            if options.embed_missing:
                try:
                    summary.embeddings_stored = backfill_embeddings(store, options)
                except Exception as exc:
                    summary.embedding_error = f"{type(exc).__name__}: {exc}"
    finally:
        if store is not None:
            store.close()
    return summary


def normalize_embedding(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if norm <= 0:
        return [0.0 for _ in vector]
    return [float(value) / norm for value in vector]


def create_embeddings(texts: list[str], options: ImportOptions) -> list[list[float]]:
    from openai import OpenAI

    kwargs: dict[str, Any] = {
        "model": options.embedding_model,
        "input": [text[:8000] for text in texts],
    }
    if options.embedding_dimensions > 0:
        kwargs["dimensions"] = options.embedding_dimensions
    response = OpenAI().embeddings.create(**kwargs)
    return [normalize_embedding(list(item.embedding)) for item in response.data]


def backfill_embeddings(store: MemoryStore, options: ImportOptions) -> int:
    stored = 0
    remaining = max(0, int(options.embedding_limit))
    batch_size = max(1, int(options.embedding_batch_size))
    while remaining > 0:
        candidates = store.pending_embedding_candidates(
            model=options.embedding_model,
            dimensions=options.embedding_dimensions,
            lookback_days=options.semantic_lookback_days,
            limit=min(batch_size, remaining),
        )
        if not candidates:
            break
        stored += store_embeddings(store, candidates, options)
        remaining -= len(candidates)
    return stored


def store_embeddings(store: MemoryStore, candidates: list[EmbeddingCandidate], options: ImportOptions) -> int:
    vectors = create_embeddings([candidate.search_text for candidate in candidates], options)
    stored = 0
    for candidate, vector in zip(candidates, vectors):
        if options.embedding_dimensions > 0 and len(vector) != options.embedding_dimensions:
            raise ValueError(f"embedding dimensions mismatch: expected {options.embedding_dimensions}, got {len(vector)}")
        store.upsert_embedding(
            message_id=candidate.item.id,
            chat_id=candidate.item.chat_id,
            model=options.embedding_model,
            dimensions=len(vector),
            content_hash=candidate.content_hash,
            embedding=vector,
        )
        stored += 1
    return stored


def build_options(args: argparse.Namespace) -> ImportOptions:
    load_dotenv_defaults(REPO_ROOT / ".env")
    return ImportOptions(
        file=Path(args.file),
        chat_id=int(args.chat_id),
        db_path=Path(args.db or os.getenv("MEMORY_DB_PATH", DEFAULT_DB_PATH)),
        days=args.days,
        include_service=bool(args.include_service),
        copy_media=bool(args.copy_media),
        dry_run=bool(args.dry_run),
        embed_missing=bool(args.embed_missing),
        embedding_limit=int(args.embedding_limit),
        retention_days=env_int("MEMORY_RETENTION_DAYS", 30),
        image_max_bytes=env_int("IMAGE_MAX_BYTES", 6_000_000),
        bot_username=os.getenv("BOT_USERNAME", ""),
        embedding_model=os.getenv("MEMORY_EMBEDDING_MODEL", "text-embedding-3-small").strip(),
        embedding_dimensions=env_int("MEMORY_EMBEDDING_DIMENSIONS", 512),
        embedding_batch_size=env_int("MEMORY_EMBEDDING_BATCH_SIZE", 64),
        semantic_lookback_days=env_int("MEMORY_SEMANTIC_LOOKBACK_DAYS", 30),
    )


def print_summary(summary: ImportSummary, *, dry_run: bool) -> None:
    mode = "dry-run" if dry_run else "import"
    print(f"Telegram export {mode} summary:")
    print(f"- scanned: {summary.scanned}")
    print(f"- imported: {summary.imported}")
    if not dry_run:
        print(f"- inserted: {summary.inserted}")
        print(f"- updated: {summary.updated}")
        print(f"- fts_indexed: {summary.fts_indexed}")
        print(f"- embeddings_stored: {summary.embeddings_stored}")
    print(f"- bot_messages: {summary.bot_messages}")
    print(f"- media_copied: {summary.media_copied}")
    print(f"- media_skipped: {summary.media_skipped}")
    print(f"- skipped_service: {summary.skipped_service}")
    print(f"- skipped_old: {summary.skipped_old}")
    print(f"- skipped_empty: {summary.skipped_empty}")
    print(f"- skipped_invalid: {summary.skipped_invalid}")
    if summary.embedding_error:
        print(f"- embedding_error: {summary.embedding_error}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Telegram Desktop JSON export into Aigan SQLite memory.")
    parser.add_argument("--file", default=DEFAULT_IMPORT_FILE, help="Path to Telegram Desktop result.json")
    parser.add_argument("--chat-id", required=True, type=int, help="Target Telegram chat id, e.g. -1002546271665")
    parser.add_argument("--db", default="", help="SQLite DB path; defaults to MEMORY_DB_PATH or /app/data/aigan.sqlite3")
    parser.add_argument("--days", type=int, default=30, help="Import only messages from the last N days; use 0 for all")
    parser.add_argument("--include-service", action="store_true", help="Also import Telegram service/action messages")
    parser.add_argument("--copy-media", action="store_true", help="Copy exported local photo/image files into memory media cache")
    parser.add_argument("--dry-run", action="store_true", help="Parse and count without writing to SQLite or media cache")
    parser.add_argument("--embed-missing", action="store_true", help="Create embeddings for missing imported/searchable messages")
    parser.add_argument("--embedding-limit", type=int, default=10000, help="Maximum embeddings to create in this run")
    args = parser.parse_args(argv)
    if args.days == 0:
        args.days = None
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = build_options(args)
    summary = import_export(options)
    print_summary(summary, dry_run=options.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
