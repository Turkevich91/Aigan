# aigan

Telegram group assistant backed by OpenAI Agents SDK and local MCP tools.

Default model: `gpt-5.4-mini`. The public model catalog currently lists `gpt-5.4-mini` and `gpt-5.4-nano`, not `GPT-5.4-micro`, so this project uses the mini model as the practical chat default.

## What is included

- Telegram bot that replies in groups on trigger, mention, reply, `/ai`, `/питай`, `/п`, or `/а`.
- Passive group context capture when Telegram bot privacy/chat access allows all messages.
- Optional scheduled proactive messages and cautious auto-reactions, disabled by default.
- OpenAI Agents SDK with local stdio MCP servers.
- `web` MCP server: web search, image search, and URL fetching.
- `youtube_transcript` MCP server: YouTube captions/transcripts, with optional audio transcription fallback.
- Persistent bounded chat memory in SQLite, including cached Telegram image context.
- User profile and stats commands based on messages the bot has actually seen.
- Placeholder `.env` ready for secrets.

## Setup

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=...
OPENAI_API_KEY=...
BOT_USERNAME=
ALLOWED_CHAT_IDS=
BOT_TRIGGER=!m
```

Then run:

```bash
docker compose up -d --build
docker compose logs -f
```

In a group chat, use:

```text
 /ping
 /ids
 /version
 /context
 /stat @username
 /character me
 /proactive_now
!m explain this briefly
/ai summarize this https://www.youtube.com/watch?v=...
```

Ukrainian aliases are also available so users do not need to switch keyboard layout:

```text
/пінг
/айді
/версія
/контекст
/стат @username
/характер мій
/проактив
/питай підсумуй це відео https://www.youtube.com/watch?v=...
/п коротко поясни попереднє
/а що тут важливо?
```

User commands use only retained SQLite memory for the current chat. `/stat` and `/стат` count saved messages, sentences, words, and top words. `/character`, `/profile`, `/характер`, `/портрет`, and `/профіль` build a cautious non-clinical communication portrait from the last 100 saved text messages. Users can request their own data; admins can request another user by `@username`.

If Telegram privacy mode is on and the bot is not an admin, it may only receive commands, mentions, and replies. That is usually good for cost control.

For first group debugging, use `/ping@your_bot_username` or `/ids@your_bot_username`.
Plain `!m ...` and ordinary `@bot_username ...` mentions usually require either disabling privacy mode in BotFather or making the bot an admin so Telegram delivers non-command group messages to it.
`BOT_USERNAME` is optional; the bot normally discovers it via Telegram `getMe`. Set it only if mention detection needs an explicit hint, for example `BOT_USERNAME=thrd_ua_bot`.

`ALLOWED_CHAT_IDS` is a comma-separated list of Telegram chat IDs, for example:

```env
ALLOWED_CHAT_IDS=-1002546271665,890218886
ADMIN_USER_IDS=890218886
```

`ADMIN_USER_IDS` is also comma-separated. Admin users skip cooldowns and can DM the bot even when `ALLOWED_CHAT_IDS` is locked to group IDs.

## Full Chat Access

When privacy/chat access is disabled in BotFather, Telegram can deliver ordinary group messages to the bot. The bot uses that in four ways:

- it saves recent delivered text/media context so replies to Telegram quotes have better context;
- it can respond to `@bot_username ...` and `!m ...` without slash commands;
- it can summarize recently delivered images lazily when a later question needs them;
- optional auto-reactions can be enabled with strict cooldowns.

Test passive reading:

```text
send a normal group message
/context
```

`/context` is admin-only and shows the recent persistent memory the bot observed in that chat.

## Forwarding And Images

Most reliable pattern:

```text
forward the message/photo/link first
reply to that forwarded item with @bot_username your question
```

In a private chat with the bot, simply forwarding a message/photo/link is treated as an implicit request to analyze it. No `поясни` prefix is required.

In a group chat, replying to or quoting a message with only `@bot_username` is also treated as an implicit request. The bot should use the replied-to/quoted/forwarded item as the main context even when you do not add words like `поясни`.

For current-looking forwarded news or political claims, Aigan may run a safe web check automatically, but only after an explicit bot invocation or in private DM. Ordinary group messages without a mention, command, trigger, reply-to-bot, or pending request stay silent and are only stored as passive context.

If the user asks first and forwards content immediately after, the bot keeps a short pending request window:

```env
PENDING_REQUEST_SECONDS=180
FOLLOWUP_DEBOUNCE_SECONDS=0.5
```

`FOLLOWUP_DEBOUNCE_SECONDS` is a short input-lag buffer before the bot starts generating an answer for context-dependent requests. `PENDING_REQUEST_SECONDS` is the longer expiry window for a pending request that is waiting for follow-up context.

Image analysis is enabled by default for photos and image documents that Telegram actually delivers to the bot:

```env
IMAGE_ANALYSIS_ENABLED=true
VISION_MODEL=gpt-5.4-mini
IMAGE_MAX_BYTES=6000000
```

Telegram link previews are not always delivered as images. If the bot says it did not receive the image, resend the picture as a photo/file or reply directly to the forwarded photo.

## Persistent Memory

Aigan stores bounded chat memory in SQLite so it can use recent context after restarts:

```env
MEMORY_ENABLED=true
MEMORY_DB_PATH=/app/data/aigan.sqlite3
MEMORY_CONTEXT_MESSAGES=10
MEMORY_RETENTION_DAYS=30
MEMORY_IMAGE_SUMMARY_LIMIT=3
MEMORY_EAGER_IMAGE_SUMMARY=false
WEB_IMAGE_SEARCH_ENABLED=true
```

`MEMORY_CONTEXT_MESSAGES` controls how many recent delivered messages are added to each model request. `MEMORY_RETENTION_DAYS` deletes older rows and cached media. `MEMORY_IMAGE_SUMMARY_LIMIT` limits how many recent unsummarized images can be lazily sent to vision for one answer.

The bot can only remember messages Telegram delivered to it after memory is enabled. It cannot fetch arbitrary older group history. If a forwarded post contains a real `photo` or image `document`, the bot can cache and analyze it. If Telegram only shows a client-side link preview and does not deliver the image file, Aigan keeps the text/link and may fetch public page images, but it cannot inspect a private preview that was never sent through Bot API.

For web image requests such as `покажи картинку ...` or `знайди 3 фотки ...`, Aigan searches safe public image results, filters Russian/private hosts, downloads valid image bytes, and sends Telegram photo attachments with source captions instead of replying with raw image links. When it finds 2-10 valid images for one request, it sends them as a Telegram album/media group; if Telegram rejects the album, it falls back to individual photo messages.

Long text replies are split by the delivery layer instead of being truncated at the first Telegram limit:

```env
MAX_REPLY_CHARS=12000
TELEGRAM_TEXT_CHUNK_CHARS=3500
MAX_REPLY_CHUNKS=4
```

## Version Notes

`/version` shows the latest local release note from `CHANGELOG.md`. `/version 3` shows the latest three entries, capped at five.

Keep `CHANGELOG.md` newest-first. Add a new entry before each release:

```md
## YYYY-MM-DD - short title

- User-visible change.
- Operational note.
```

## Tone

The default prompt is professional and concise. Dry humor, irony, and mild sarcasm are allowed only when they fit the moment. The bot should avoid clowning, forced jokes, and personal mockery.

## Language And Source Policy

The bot is configured to answer in Ukrainian by default. English is allowed only when explicitly requested or when the context is clearly English-first. Russian output is disabled: if a message, quote, or YouTube transcript is Russian, the bot should understand it as source material and summarize/explain it in Ukrainian.

Web search is also constrained: the assistant is instructed to search in Ukrainian or English, and the web MCP filters Russian domains/services such as `.ru`, `.su`, Yandex, VK, Mail.ru, RT, RIA, TASS, and similar sources.

## Current Time

Every model request includes timezone-aware current time metadata so the bot does not rely on stale model memory for "today", "now", or whether a date is past/future. Configure the local timezone with an IANA timezone name:

```env
BOT_TIMEZONE=America/New_York
```

Use `Europe/Kyiv` instead if the group should reason in Kyiv time.

## Proactive Messages

Disabled by default. To let the bot post on its own, set:

```env
PROACTIVE_ENABLED=true
PROACTIVE_CHAT_ID=-1002546271665
PROACTIVE_INTERVAL_SECONDS=18000
PROACTIVE_START_DELAY_SECONDS=300
PROACTIVE_PROMPT=Write a brief, useful group check-in. Be professional, concise, and only lightly ironic if the context invites it.
```

Test without waiting:

```text
/proactive_now
```

## Auto-Reactions

Disabled by default. A conservative keyword-based setup:

```env
AUTO_REACT_ENABLED=true
AUTO_REACT_PROBABILITY=0
AUTO_REACT_KEYWORDS=поясни,что это,новость,ссылка
AUTO_REACT_COOLDOWN_SECONDS=1800
AUTO_REACT_MIN_CHARS=25
```

For occasional random reactions, set a low probability such as `0.02`. Keep a long cooldown unless the group explicitly wants a more active bot.

## YouTube

The YouTube MCP tries public captions first. If a video has no captions and you want real audio transcription, set:

```env
YOUTUBE_AUDIO_FALLBACK=true
```

That uses `yt-dlp`, `ffmpeg`, and OpenAI transcription, so it can cost money and should be used only for videos you are allowed to process.
