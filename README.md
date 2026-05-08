# aigan

Telegram group assistant backed by OpenAI Agents SDK and local MCP tools.

Default model: `gpt-5.4-mini`. The public model catalog currently lists `gpt-5.4-mini` and `gpt-5.4-nano`, not `GPT-5.4-micro`, so this project uses the mini model as the practical chat default.

## What is included

- Telegram bot that replies in groups on trigger, mention, reply, or `/ai`.
- Passive group context capture when Telegram bot privacy/chat access allows all messages.
- Optional scheduled proactive messages and cautious auto-reactions, disabled by default.
- OpenAI Agents SDK with local stdio MCP servers.
- `web` MCP server: web search and URL fetching.
- `youtube_transcript` MCP server: YouTube captions/transcripts, with optional audio transcription fallback.
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
 /context
 /proactive_now
!m explain this briefly
/ai summarize this https://www.youtube.com/watch?v=...
```

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

When privacy/chat access is disabled in BotFather, Telegram can deliver ordinary group messages to the bot. The bot uses that in three ways:

- it passively remembers recent text so replies to Telegram quotes have better context;
- it can respond to `@bot_username ...` and `!m ...` without slash commands;
- optional auto-reactions can be enabled with strict cooldowns.

Test passive reading:

```text
send a normal group message
/context
```

`/context` is admin-only and shows the recent text the bot observed in that chat.

## Forwarding And Images

Most reliable pattern:

```text
forward the message/photo/link first
reply to that forwarded item with @bot_username your question
```

In a private chat with the bot, simply forwarding a message/photo/link is treated as an implicit request to analyze it. No `поясни` prefix is required.

In a group chat, replying to or quoting a message with only `@bot_username` is also treated as an implicit request. The bot should use the replied-to/quoted/forwarded item as the main context even when you do not add words like `поясни`.

If the user asks first and forwards content immediately after, the bot keeps a short pending request window:

```env
PENDING_REQUEST_SECONDS=180
```

Image analysis is enabled by default for photos and image documents that Telegram actually delivers to the bot:

```env
IMAGE_ANALYSIS_ENABLED=true
VISION_MODEL=gpt-5.4-mini
IMAGE_MAX_BYTES=6000000
```

Telegram link previews are not always delivered as images. If the bot says it did not receive the image, resend the picture as a photo/file or reply directly to the forwarded photo.

## Tone

The default prompt is professional and concise. Dry humor, irony, and mild sarcasm are allowed only when they fit the moment. The bot should avoid clowning, forced jokes, and personal mockery.

## Language And Source Policy

The bot is configured to answer in Ukrainian by default. English is allowed only when explicitly requested or when the context is clearly English-first. Russian output is disabled: if a message, quote, or YouTube transcript is Russian, the bot should understand it as source material and summarize/explain it in Ukrainian.

Web search is also constrained: the assistant is instructed to search in Ukrainian or English, and the web MCP filters Russian domains/services such as `.ru`, `.su`, Yandex, VK, Mail.ru, RT, RIA, TASS, and similar sources.

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
