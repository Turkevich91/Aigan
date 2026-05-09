# Aigan Changelog

## 2026-05-09 - Explicit current-claim verification

- Added safe web verification for current-looking forwarded claims after explicit bot invocation or private DM.
- Kept ordinary group messages silent unless the bot is mentioned, triggered, replied to, or consuming a pending request.
- Improved time-sensitive routing for political/news claims while preserving translation and image-route precedence.

## 2026-05-09 - Albums and long replies

- Grouped 2-10 found internet images into Telegram albums instead of separate photo messages.
- Added fallback to individual photos if Telegram rejects an album.
- Added smart splitting for long text replies so answers are chunked instead of truncated at one message.

## 2026-05-09 - Internet images as photos

- Improved image-request routing for prompts like `знайди 3 фотки ... і запость сюди`.
- Added multi-image sending so Aigan uploads valid image bytes as Telegram photos instead of replying with image-link lists.
- Kept source attribution in photo captions while avoiding raw `<a href>` image-link answers.

## 2026-05-09 - Safe routing and version notes

- Added `/version` so the chat can view the latest local release notes.
- Added `CHANGELOG.md` as the human-editable source for version notes.
- Added safe routing for translation, image sending, and time-sensitive requests.
- Added image-byte validation before sending internet images to Telegram.
- Added fresh web context for current facts and recent/news-like questions.

## 2026-05-08 - Persistent multimodal memory

- Added SQLite-backed bounded chat memory with 30-day retention.
- Cached Telegram-delivered photos and image documents under `/app/data/media`.
- Added lazy vision summaries so recent images can be reused as context.
- Added safe public image search and Telegram photo sending for explicit image requests.
- Updated `/context` to show persistent recent memory.
