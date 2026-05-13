from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from bs4 import BeautifulSoup

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
HTML_DAY_FORMATS = ("%d %B %Y", "%B %d, %Y")


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
    user_map_path: Path | None = None
    interactive_user_map: str = "auto"
    write_user_map_path: Path | None = None
    require_resolved_users: bool = False
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
    unresolved_authors: dict[str, int] | None = None


class ExportParser(Protocol):
    def messages(self) -> list[dict[str, Any]]:
        ...


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


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def clean_text(value: str) -> str:
    lines = [line.strip() for line in (value or "").replace("\xa0", " ").splitlines()]
    return "\n".join(line for line in lines if line).strip()


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
    text = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if forward_origin(message) and not telegram_text_to_plain(message.get("source_text")):
        return ""
    return text


def message_source_text(message: dict[str, Any]) -> str:
    source = telegram_text_to_plain(message.get("source_text")).strip()
    if source:
        return source
    if forward_origin(message):
        parts = [
            telegram_text_to_plain(message.get("text")),
            telegram_text_to_plain(message.get("caption")),
        ]
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return ""


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
    normalized = normalize_name(label)
    username = bot_username.strip().lower().lstrip("@")
    candidates = {"aigan", "aigan ðŸ‘¾", "aigan 👾"}
    if username:
        candidates.add(username)
        candidates.add("@" + username)
    return normalized in {normalize_name(candidate) for candidate in candidates}


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


def export_base_dir(path: Path) -> Path:
    if path.is_dir():
        return path
    return path.parent


def html_page_sort_key(path: Path) -> tuple[int, str]:
    match = re.fullmatch(r"messages(\d*)\.html", path.name)
    if not match:
        return (999999, path.name)
    suffix = match.group(1)
    return (1 if suffix == "" else int(suffix), path.name)


def message_numeric_id(raw: str) -> int | None:
    match = re.search(r"(\d+)", raw or "")
    return int(match.group(1)) if match else None


def parse_html_message_datetime(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    for fmt in ("%d.%m.%Y %H:%M:%S UTC%z", "%d.%m.%Y %H:%M:%S %z"):
        try:
            return datetime.strptime(raw, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def parse_html_day_header(raw: str) -> datetime | None:
    raw = clean_text(raw)
    for fmt in HTML_DAY_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def direct_body(message_tag: Any) -> Any | None:
    for child in message_tag.find_all("div", recursive=False):
        classes = child.get("class") or []
        if "body" in classes:
            return child
    return None


def direct_child_text(node: Any, class_name: str) -> str:
    if node is None:
        return ""
    found = node.find("div", class_=class_name, recursive=False)
    return clean_text(found.get_text("\n")) if found else ""


def readable_text_from_node(node: Any) -> str:
    clone = BeautifulSoup(str(node), "html.parser")
    for br in clone.find_all("br"):
        br.replace_with("\n")
    for anchor in clone.find_all("a", href=True):
        label = clean_text(anchor.get_text(" "))
        href = str(anchor.get("href") or "").strip()
        if href.startswith("http") and href not in label:
            anchor.string = f"{label} {href}" if label else href
    return clean_text(clone.get_text("\n"))


def html_message_text(body: Any) -> str:
    if body is None:
        return ""
    texts: list[str] = []
    for node in body.find_all("div", class_="text", recursive=False):
        text = readable_text_from_node(node)
        if text:
            texts.append(text)
    return "\n\n".join(texts).strip()


def html_forwarded_body(body: Any) -> Any | None:
    if body is None:
        return None
    for node in body.find_all("div", recursive=False):
        classes = set(node.get("class") or [])
        if "forwarded" in classes and "body" in classes:
            return node
    return None


def html_forwarded_text(body: Any) -> str:
    forwarded = html_forwarded_body(body)
    return html_message_text(forwarded) if forwarded is not None else ""


def html_forward_origin(body: Any) -> str:
    if body is None:
        return ""
    forwarded = html_forwarded_body(body)
    if not forwarded:
        return ""
    name = forwarded.find("div", class_="from_name")
    if not name:
        return ""
    for date in name.find_all("span", class_="date"):
        date.extract()
    return clean_text(name.get_text(" "))


def html_reply_to_message_id(body: Any) -> int | None:
    if body is None:
        return None
    reply = body.find("div", class_="reply_to")
    if not reply:
        return None
    link = reply.find("a", href=True)
    if not link:
        return None
    return message_numeric_id(str(link.get("href") or ""))


def html_media_path(body: Any) -> tuple[str, str]:
    if body is None:
        return "", ""
    photo = body.find("a", class_="photo_wrap", href=True)
    if photo and str(photo.get("href") or "").strip():
        return str(photo["href"]).strip(), "photo"
    file_link = body.find("a", class_="media_file", href=True) or body.find("a", class_="document_wrap", href=True)
    if file_link and str(file_link.get("href") or "").strip():
        return str(file_link["href"]).strip(), "file"
    return "", ""


def user_map_entry(value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"user_id": value, "username": ""}
    if isinstance(value, str):
        return {"user_id": parse_user_id(value), "username": ""}
    if isinstance(value, dict):
        return {
            "user_id": parse_user_id(value.get("user_id") or value.get("id")),
            "username": str(value.get("username") or "").lstrip("@"),
        }
    return {"user_id": None, "username": ""}


def load_user_map(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("--user-map must be a JSON object keyed by exported display name")
    return {normalize_name(str(key)): user_map_entry(value) for key, value in data.items()}


def infer_existing_user_map(store: MemoryStore | None, chat_id: int) -> dict[str, dict[str, Any]]:
    if store is None:
        return {}
    rows = store._conn.execute(
        """
        SELECT sender_label, user_id, username, COUNT(*) AS count
        FROM messages
        WHERE chat_id = ? AND is_bot = 0 AND (user_id IS NOT NULL OR username != '')
        GROUP BY sender_label, user_id, username
        ORDER BY count DESC
        """,
        (chat_id,),
    ).fetchall()
    mapping: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = str(row["sender_label"] or "").strip()
        if not label:
            continue
        entry = {"user_id": row["user_id"], "username": str(row["username"] or "").lstrip("@")}
        aliases = {label}
        aliases.add(re.sub(r"\s*\(@.*$", "", label).strip())
        aliases.add(re.sub(r"\s*\(image request\)\s*$", "", label).strip())
        for alias in aliases:
            if alias:
                mapping.setdefault(normalize_name(alias), entry)
    return mapping


class JsonExportParser:
    def __init__(self, path: Path) -> None:
        self.path = path

    def messages(self) -> list[dict[str, Any]]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ValueError("Telegram export JSON must contain a top-level 'messages' list")
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict):
                item = dict(message)
                item.setdefault("_export_dir", str(self.path.parent))
                item.setdefault("_format", "json")
                normalized.append(item)
        return normalized


class HtmlExportParser:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_dir():
            self.export_dir = path
            self.pages = sorted(path.glob("messages*.html"), key=html_page_sort_key)
        else:
            self.export_dir = path.parent
            self.pages = [path]
        if not self.pages:
            raise ValueError(f"No messages*.html files found in {path}")

    def messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        current_day: datetime | None = None
        current_sender = ""
        for page in self.pages:
            soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
            for message_tag in soup.select("div.message"):
                item = self.parse_message(message_tag, current_day, current_sender)
                if item is None:
                    continue
                if item.get("_day_marker"):
                    current_day = parse_export_datetime(item)
                elif item.get("type") == "message":
                    sender = sender_label(item)
                    if sender and sender != "Telegram export":
                        current_sender = sender
                messages.append(item)
        messages.sort(key=lambda item: int(item.get("id") or 0))
        return messages

    def parse_message(self, message_tag: Any, current_day: datetime | None, current_sender: str = "") -> dict[str, Any] | None:
        message_id = message_numeric_id(str(message_tag.get("id") or ""))
        if message_id is None:
            return None
        classes = message_tag.get("class") or []
        body = direct_body(message_tag)
        if "service" in classes:
            text = clean_text(body.get_text("\n")) if body else ""
            day = parse_html_day_header(text)
            created = day or current_day
            return {
                "id": message_id,
                "type": "service",
                "date": created.isoformat() if created else "",
                "from": "Telegram service",
                "text": text,
                "_day_marker": bool(day),
                "_export_dir": str(self.export_dir),
                "_format": "html",
            }

        date_node = body.find("div", class_="date") if body else None
        created = parse_html_message_datetime(str(date_node.get("title") or "")) if date_node else None
        if created is None:
            return None
        sender = direct_child_text(body, "from_name") or current_sender or "Telegram export"
        media_path, media_kind = html_media_path(body)
        source_text = html_forwarded_text(body)
        item: dict[str, Any] = {
            "id": message_id,
            "type": "message",
            "date": created.isoformat(),
            "date_unixtime": str(int(created.timestamp())),
            "from": sender,
            "text": html_message_text(body),
            "source_text": source_text,
            "_export_dir": str(self.export_dir),
            "_format": "html",
        }
        reply_id = html_reply_to_message_id(body)
        if reply_id is not None:
            item["reply_to_message_id"] = reply_id
        origin = html_forward_origin(body)
        if origin:
            item["forwarded_from"] = origin
        if media_path:
            item[media_kind] = media_path
            item["_attachment_type"] = "photo" if media_kind == "photo" else "document"
        return item


def build_parser(path: Path) -> ExportParser:
    if path.is_dir() or path.suffix.lower() in {".html", ".htm"}:
        return HtmlExportParser(path)
    return JsonExportParser(path)


def media_source_path(message: dict[str, Any], default_export_dir: Path) -> Path | None:
    export_dir = Path(str(message.get("_export_dir") or default_export_dir))
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
    source = media_source_path(message, export_base_dir(options.file))
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
    has_media_ref = media_source_path(message, export_base_dir(options.file)) is not None or bool(message.get("photo"))
    if not message_text(message) and not message_source_text(message) and not has_media_ref:
        return False, "empty"
    return True, ""


def apply_user_map(message: dict[str, Any], user_map: dict[str, dict[str, Any]]) -> None:
    label = sender_label(message)
    entry = user_map.get(normalize_name(label))
    if not entry:
        return
    if entry.get("user_id") is not None and not message.get("from_id"):
        message["from_id"] = f"user{entry['user_id']}"
    if entry.get("username") and not message.get("username"):
        message["username"] = str(entry["username"]).lstrip("@")


def unresolved_author_counts(
    messages: list[dict[str, Any]],
    *,
    options: ImportOptions,
    cutoff: datetime | None,
    user_map: dict[str, dict[str, Any]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for original in messages:
        if not isinstance(original, dict):
            continue
        message = dict(original)
        apply_user_map(message, user_map)
        should_import, _reason = should_import_message(message, options, cutoff)
        if not should_import:
            continue
        label = sender_label(message)
        if not label or label in {"Telegram export", "Telegram service"}:
            continue
        if is_bot_sender(label, options.bot_username):
            continue
        if parse_user_id(message.get("from_id") or message.get("actor_id")) is not None:
            continue
        if str(message.get("username") or "").strip():
            continue
        counts[label] += 1
    return counts


def parse_user_map_answer(raw: str) -> dict[str, Any] | None:
    value = raw.strip()
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    user_id = parse_user_id(parts[0] if parts else "")
    if user_id is None:
        return None
    username = parts[1].lstrip("@") if len(parts) > 1 else ""
    return {"user_id": user_id, "username": username}


def maybe_prompt_for_unresolved_authors(
    counts: Counter[str],
    *,
    options: ImportOptions,
    user_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    mode = (options.interactive_user_map or "auto").strip().lower()
    if mode not in {"auto", "always", "never"}:
        raise ValueError("--interactive-user-map must be one of: auto, always, never")
    if not counts:
        return {}
    interactive = mode == "always" or (mode == "auto" and sys.stdin.isatty() and sys.stdout.isatty())
    if not interactive:
        return {}

    additions: dict[str, dict[str, Any]] = {}
    print("Unresolved Telegram export authors. Enter user_id[,username] or blank to keep unresolved.", file=sys.stderr)
    for label, count in counts.most_common():
        try:
            answer = input(f"{label} ({count} messages): ").strip()
        except EOFError:
            break
        entry = parse_user_map_answer(answer)
        if entry is None:
            continue
        key = normalize_name(label)
        user_map[key] = entry
        additions[label] = entry
    return additions


def write_user_map(path: Path, additions: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        existing_data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing_data, dict):
            existing = existing_data
    existing.update(additions)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def import_export(options: ImportOptions) -> ImportSummary:
    summary = ImportSummary()
    messages = build_parser(options.file).messages()
    cutoff = None
    if options.days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, options.days))

    store: MemoryStore | None = None if options.dry_run else MemoryStore(options.db_path, options.retention_days)
    user_map = infer_existing_user_map(store, options.chat_id)
    user_map.update(load_user_map(options.user_map_path))
    unresolved = unresolved_author_counts(messages, options=options, cutoff=cutoff, user_map=user_map)
    additions = maybe_prompt_for_unresolved_authors(unresolved, options=options, user_map=user_map)
    if additions and options.write_user_map_path is not None:
        write_user_map(options.write_user_map_path, additions)
    unresolved = unresolved_author_counts(messages, options=options, cutoff=cutoff, user_map=user_map)
    summary.unresolved_authors = dict(unresolved)
    if options.require_resolved_users and unresolved:
        names = ", ".join(f"{label} ({count})" for label, count in unresolved.most_common(10))
        raise ValueError(f"Unresolved Telegram export authors remain: {names}")
    try:
        for message in messages:
            if not isinstance(message, dict):
                summary.skipped_invalid += 1
                continue
            apply_user_map(message, user_map)
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
            source_text = message_source_text(message)
            content_kind = "text"
            attachment_type = str(message.get("_attachment_type") or "")
            local_media_path = ""
            mime_type = ""
            raw_note = f"imported from Telegram Desktop {message.get('_format', 'export')} export"

            media_ref_present = media_source_path(message, export_base_dir(options.file)) is not None or bool(message.get("photo"))
            if message.get("photo"):
                content_kind = "image"
                attachment_type = attachment_type or ("photo" if message.get("photo") else "image_document")
            elif media_ref_present:
                content_kind = "attachment"
                attachment_type = attachment_type or "document"
            elif attachment_type:
                content_kind = "attachment"

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
                username=str(message.get("username") or "").lstrip("@"),
                is_bot=bot_message,
                text=text,
                source_text=source_text,
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
        user_map_path=Path(args.user_map) if args.user_map else None,
        interactive_user_map=str(args.interactive_user_map),
        write_user_map_path=Path(args.write_user_map) if args.write_user_map else None,
        require_resolved_users=bool(args.require_resolved_users),
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
    unresolved = summary.unresolved_authors or {}
    if unresolved:
        print("- unresolved_authors:")
        for label, count in sorted(unresolved.items(), key=lambda item: (-item[1], item[0].casefold()))[:20]:
            print(f"  - {label}: {count}")
        print("- user_map_hint: add entries like {\"Display Name\": {\"user_id\": 123456, \"username\": \"name\"}}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Telegram Desktop JSON or HTML export into Aigan SQLite memory.")
    parser.add_argument(
        "--file",
        default=DEFAULT_IMPORT_FILE,
        help="Path to result.json, messages.html, or Telegram Desktop export directory",
    )
    parser.add_argument("--chat-id", required=True, type=int, help="Target Telegram chat id, e.g. -1002546271665")
    parser.add_argument("--db", default="", help="SQLite DB path; defaults to MEMORY_DB_PATH or /app/data/aigan.sqlite3")
    parser.add_argument("--days", type=int, default=30, help="Import only messages from the last N days; use 0 for all")
    parser.add_argument("--include-service", action="store_true", help="Also import Telegram service/action messages")
    parser.add_argument("--copy-media", action="store_true", help="Copy exported local photo/image files into memory media cache")
    parser.add_argument("--dry-run", action="store_true", help="Parse and count without writing to SQLite or media cache")
    parser.add_argument("--embed-missing", action="store_true", help="Create embeddings for missing imported/searchable messages")
    parser.add_argument("--embedding-limit", type=int, default=10000, help="Maximum embeddings to create in this run")
    parser.add_argument("--user-map", default="", help="Optional JSON map from HTML display names to user_id/username")
    parser.add_argument(
        "--interactive-user-map",
        choices=("auto", "always", "never"),
        default="auto",
        help="Ask for user_id[,username] for unresolved export authors when running in an interactive TTY",
    )
    parser.add_argument("--write-user-map", default="", help="Write interactive user-map answers to this JSON file")
    parser.add_argument(
        "--require-resolved-users",
        action="store_true",
        help="Fail if any imported non-bot author still lacks user_id/username after mapping",
    )
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
