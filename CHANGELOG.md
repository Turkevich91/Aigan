# Aigan Changelog

## 2026-05-13 - Self-analysis health reporting

- Added sanitized system health logs for routing, tool, delivery, pending, and command events.
- Added admin health commands: `/health`, `/logs`, `/selfcheck`, and `/complaints` with Ukrainian aliases.
- Added complaint-temperature tracking and optional `[Aigan]` GitHub self-report issues.
- Added `AGENTS.md` rules for Codex, Google Antigravity, Claude Code, and other coding agents.

## 2026-05-09 - Cleaner stat tokenization

- Removed Telegram mentions, slash commands, bot trigger tokens, and pasted top-word rows from `/stat` token counts.
- Reused the same cleaned user text for `/характер`, so bot mentions and pasted stat snippets do not shape profiles.
- Kept normal text and media captions countable while preserving raw messages in SQLite memory.

## 2026-05-09 - Cleaner user stats filtering

- Excluded media-only memory placeholders like `[message has attachment(s): sticker]` from `/stat` and `/характер`.
- Kept captions and real text countable while preserving media placeholders for chat memory context.

## 2026-05-09 - User profile and stats commands

- Added `/character`, `/profile`, `/характер`, `/портрет`, and `/профіль` for cautious communication-style portraits from the last 100 saved user messages.
- Added `/stat`, `/stats`, `/стат`, `/стата`, and `/статистика` for saved-message counts, sentence/word counts, and top words.
- Limited other-user profile/stat requests to admins; users can still inspect their own saved data.

## 2026-05-09 - Short Ukrainian ask aliases

- Added `/п` and `/а` as short Ukrainian aliases for `/питай`.
- Updated help and README so users can ask the bot without switching keyboard layout.
- Kept the aliases as explicit slash commands, so ordinary group messages still do not trigger the bot.

## 2026-05-09 - Ukrainian command aliases

- Added Ukrainian aliases for common commands: `/версія`, `/довідка`, `/айді`, `/пінг`, `/контекст`, `/проактив`, and `/питай`.
- Kept aliases as explicit commands, so they do not make the bot respond to ordinary group messages.
- Updated help and README so Ukrainian users can use commands without switching keyboard layout.

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
