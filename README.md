# aigan

Telegram group entertainer bot backed by OpenAI Agents SDK and local MCP tools.

Default model: `gpt-5.4-mini`. The public model catalog currently lists `gpt-5.4-mini` and `gpt-5.4-nano`, not `GPT-5.4-micro`, so this project uses the mini model as the practical chat default.

## What is included

- Telegram bot that replies in groups only on trigger, mention, reply, or `/ai`.
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
!m say something cursed but legal
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

## YouTube

The YouTube MCP tries public captions first. If a video has no captions and you want real audio transcription, set:

```env
YOUTUBE_AUDIO_FALLBACK=true
```

That uses `yt-dlp`, `ffmpeg`, and OpenAI transcription, so it can cost money and should be used only for videos you are allowed to process.
