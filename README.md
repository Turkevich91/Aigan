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
- Social taste memory for sanitized group/user interests and reactions, used by proactive thought seeds.
- Sanitized system health logs, admin self-check commands, and optional GitHub self-reporting.
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
/interests
/interests @username
/health
 /logs 20
 /selfcheck
 /complaints
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
/інтереси
/інтереси @username
/проактив
/питай підсумуй це відео https://www.youtube.com/watch?v=...
/п коротко поясни попереднє
/а що тут важливо?
/память subnautica
/памʼять subnautica
/пошук_памяті subnautica
```

User commands use only retained SQLite memory for the current chat. `/stat` and `/стат` count saved text/caption messages, sentences, words, and top words across the full retained history; media-only placeholders, mentions, slash commands, the bot trigger, and pasted stat-output lines are ignored. `/character`, `/profile`, `/характер`, `/портрет`, and `/профіль` build a cautious non-clinical communication portrait from the full retained history by sending the model aggregate stats plus chronological anchors, recent tail, and an embedding-diverse sample, not thousands of raw messages. Users can request their own data; admins can request another user by `@username`. When a username can be resolved to a stored Telegram `user_id`, imported rows matched by base display-name aliases are included too.

`/interests`, `/likes`, `/інтереси`, and `/смаки` show a public sanitized summary of the room's recurring interests and reactions. With `@username`, they show the selected participant's high-level topic/reaction signals. This is not a private dossier or diagnosis: social memory stores condensed observations such as interest, dislike, irritation, amusement, recurring questions, and avoided topics. It uses only the user's own text, not repost bodies, quotes, previews, or bot replies. Admin-only maintenance commands are `/interest_evidence`, `/rebuild_social_memory`, and `/forget_interest`; users can remove one of their own stored interest topics with `/forget_interest topic`.

Telegram emoji reactions are stored as a separate relation layer, not as words in `/stat`. Custom emoji are cached by `custom_emoji_id`: Aigan fetches Telegram sticker metadata once, optionally downloads a safe thumbnail/static sticker, and lazily stores a vision summary after the emoji is used enough times. The global asset summary says what the emoji looks like; chat-local semantics track how this room tends to use it.

Forwarded/reposted bodies are stored as source context, not as the forwarding user's personal text. That means `/stat` and `/character` do not treat channel repost text, previews, quotes, external replies, or generated/via-bot content as the user's own writing, while `/memory_search` can still find that source material.

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

To avoid duplicate answers while a long model call is running, Aigan keeps one in-flight generation per chat and suppresses near-duplicate explicit prompts shortly after an answer:

```env
CHAT_INFLIGHT_GUARD_ENABLED=true
CHAT_DUPLICATE_SUPPRESS_SECONDS=45
CHAT_DUPLICATE_SIMILARITY_THRESHOLD=0.72
CHAT_INFLIGHT_SUPPRESS_ORDINARY_AUTO_REACT=true
```

This guard is separate from cooldowns: admins can still skip cooldowns, but they cannot accidentally cause two near-identical answers from concurrent updates.

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
MEMORY_FOLLOWUP_CONTEXT_MESSAGES=40
MEMORY_THREAD_CONTEXT_DEPTH=6
MEMORY_RETENTION_DAYS=30
MEMORY_IMAGE_SUMMARY_LIMIT=3
MEMORY_EAGER_IMAGE_SUMMARY=false
WEB_IMAGE_SEARCH_ENABLED=true
MEMORY_VECTOR_ENABLED=true
MEMORY_EMBEDDING_MODEL=text-embedding-3-small
MEMORY_EMBEDDING_DIMENSIONS=512
MEMORY_SEMANTIC_LOOKBACK_DAYS=30
MEMORY_SEMANTIC_TOP_K=6
MEMORY_EMBEDDING_BATCH_SIZE=64
MEMORY_VECTOR_BACKFILL_ON_START=true
MEMORY_VECTOR_BACKFILL_LIMIT=1000
MEMORY_RECALL_INTENT_THRESHOLD=0.62
MEMORY_RECALL_INTENT_AMBIGUOUS_THRESHOLD=0.48
SOCIAL_MEMORY_ENABLED=true
SOCIAL_MEMORY_EXTRACT_EVERY_MESSAGES=20
SOCIAL_MEMORY_CONFIDENCE_THRESHOLD=0.65
SOCIAL_PROFILE_RETENTION_DAYS=180
```

`MEMORY_CONTEXT_MESSAGES` controls how many recent delivered messages are added to normal model requests. For explicit short follow-ups such as `@bot скільки?`, `що?`, or `how many?`, Aigan also injects up to `MEMORY_FOLLOWUP_CONTEXT_MESSAGES` recent messages plus reply-chain parents up to `MEMORY_THREAD_CONTEXT_DEPTH`. If the referent is still unclear, it should ask one concise clarification instead of guessing. This expanded retrieval only runs after an explicit bot invocation, private DM, reply-to-bot, or pending consume; ordinary group chatter stays passive memory only.

`MEMORY_RETENTION_DAYS` deletes older rows and cached media. `MEMORY_IMAGE_SUMMARY_LIMIT` limits how many recent unsummarized images can be lazily sent to vision for one answer.

Semantic memory adds a local SQLite hybrid index: FTS5 for keyword fallback plus OpenAI embeddings for meaning-based retrieval over the retained lookback window. It is only used after explicit bot invocation, never for ordinary group chatter. `/memory_search query` is admin-only and shows the retrieved snippets without asking the main model to answer; aliases `/память`, `/памʼять`, and `/пошук_памяті` call the same hybrid search. The command automatically uses embeddings when indexed, always attempts FTS fallback, and reports `embeddings_used`, fallback status, and per-result sources. Natural prompts that semantically ask for old chat context, such as `@bot що ми казали про Pragmata?` or `@bot а про 170 тис в казино ми щось обговорювали?`, route to the same recall backend automatically. `MEMORY_RECALL_INTENT_THRESHOLD` and `MEMORY_RECALL_INTENT_AMBIGUOUS_THRESHOLD` control that intent detector. Recall search also runs exact rescue with numeric terms such as `170`, `5к`, `4070`, or `$250`, and excludes the current prompt so it does not become its own top memory result.

The bot can only remember messages Telegram delivered to it after memory is enabled. It cannot fetch arbitrary older group history. If a forwarded post contains a real `photo` or image `document`, the bot can cache and analyze it. If Telegram only shows a client-side link preview and does not deliver the image file, Aigan keeps the text/link and may fetch public page images, but it cannot inspect a private preview that was never sent through Bot API.

For web image requests such as `покажи картинку ...` or `знайди 3 фотки ...`, Aigan searches safe public image results, filters Russian/private hosts, downloads valid image bytes, and sends Telegram photo attachments with source captions instead of replying with raw image links. When it finds 2-10 valid images for one request, it sends them as a Telegram album/media group; if Telegram rejects the album, it falls back to individual photo messages.

To backfill older chat history, export the chat from Telegram Desktop as either HTML or JSON and import it into the same SQLite memory. HTML export directories such as `ChatExport_2026-05-13` are supported directly; no manual conversion is needed. Keep exports in `imports/`; that folder is ignored by git.

```bash
cd ~/Projects/aigan
mkdir -p imports
# Put Telegram Desktop export directory or result.json under imports/.

docker compose run --rm \
  -v "$PWD/imports:/app/imports:ro" \
  aigan python scripts/import_telegram_export.py \
  --file /app/imports/ChatExport_2026-05-13 \
  --chat-id -1002546271665 \
  --days 30 \
  --copy-media \
  --embed-missing \
  --embedding-limit 10000
```

`--file` may point to `result.json`, a single `messages.html`, or an export directory containing `messages.html`, `messages2.html`, and later pages. Run with `--dry-run` first to count what would be imported without writing to SQLite. Re-running the importer is safe: messages are keyed by `(chat_id, message_id)`, so repeated imports update existing rows instead of duplicating history. HTML joined messages that omit `from_name` inherit the previous sender across export pages. `--copy-media` only copies valid exported JPEG/PNG/WebP/GIF files within `IMAGE_MAX_BYTES`; text and captions are imported either way.

HTML exports do not reliably contain Telegram user ids. If you need `/stat @name` and `/характер @name` to attach old HTML messages to exact users, pass `--user-map /app/imports/users.json` with entries like `{"Display Name": {"user_id": 123, "username": "handle"}}`. Without a map, Aigan keeps the exported display name and only infers ids when it can match existing memory safely.

When the importer sees unresolved export authors, it reports them with counts. In an interactive terminal, `--interactive-user-map auto` can ask for `user_id[,username]`; in Docker/non-TTY it will not hang. Use `--write-user-map /app/imports/users.json` to save answers, or `--require-resolved-users` to fail the import if any authors remain unmapped.

Forwarded/source bodies from Telegram exports are stored separately from the sender's own text. They remain searchable through FTS/embeddings, but they do not affect `/stat` top words or `/character` profiles.

Social taste memory is also separate from raw chat memory. It keeps compact sanitized observations for topic selection, `/interests`, and proactive messages. Proactive messages choose a weighted direction from group taste, personal ping, current hook, or an unanswered thread:

```env
REACTIONS_ENABLED=true
REACTION_ASSET_ANALYSIS_ENABLED=true
REACTION_ASSET_MIN_USES_FOR_VISION=3
REACTION_ANALYSIS_PROMPT_VERSION=1
REACTION_ASSET_MAX_BYTES=2000000
OUTBOUND_REACTIONS_ENABLED=false
OUTBOUND_REACTION_EVERY_N_MESSAGES=10
OUTBOUND_REACTION_COOLDOWN_SECONDS=1800
OUTBOUND_REACTION_MIN_SCORE=0.72
OUTBOUND_REACTION_ALLOWED_EMOJI=fire,eyes,thumbs_up,thinking,laugh,sad,broken_heart,shock,fear,angry
OUTBOUND_REACTION_USE_CUSTOM_EMOJI=true
OUTBOUND_REACTION_BIG=false
PROACTIVE_DIRECTION_WEIGHTS=group_taste:0.25,personal_ping:0.25,current_hook:0.25,unanswered_thread:0.25
PROACTIVE_SELF_REFERENCE_GUARD=true
PROACTIVE_RECENT_SEED_COOLDOWN_DAYS=14
```

Inbound reaction memory and outbound reactions are separate. `REACTIONS_ENABLED=true` lets Aigan store reactions from the chat and learn lightweight taste signals. `OUTBOUND_REACTIONS_ENABLED=true` lets Aigan set real Telegram reactions on selected live group messages through an optional adapter hook. `OUTBOUND_REACTION_ALLOWED_EMOJI` accepts ASCII aliases such as `fire`, `eyes`, `thumbs_up`, `thinking`, `laugh`, `sad`, `broken_heart`, `shock`, `fear`, and `angry` so `.env` files do not depend on editor Unicode handling; literal UTF-8 emoji also work. Outbound reaction selection is emotion-aware: positive reactions are reserved for safely positive direct content, while sensitive or ambiguous content defaults to no reaction unless a configured emotion class is high-confidence. Possible stored sent classes are `positive_celebratory`, `grief_sympathy`, `horror_shock`, `condemnation_outrage`, `despair_heavy_news`, and `uncertainty_doubt`. A final empathy preflight runs after emoji selection and before Telegram send; it skips if the candidate could be perceived as approval of harm, suffering, coercion, humiliation, violence, death, or injustice, and stores the sanitized skip reason in the reaction decision record. If the outbound adapter is disabled, missing, or Telegram rejects a reaction, memory saving and embeddings continue normally. The older `maybe_auto_react()` behavior is text-based; outbound reactions use Telegram's native message reaction API.

The self-reference guard rejects proactive drafts that talk about being a bot/AI, announce capabilities, or ask users to tag/contact Aigan. Direct answers to explicit user requests are not blocked by this proactive-only guard.

## Tool Adapter Boundary

Optional tools should plug into the shared tool runtime instead of adding one-off hooks directly to `main.py`.
Each tool family should provide a small adapter with:

- a null/no-op fallback for disabled or unavailable dependencies;
- `health_summary()` with adapter name, enabled state, status, and error counts;
- best-effort execution through the runtime safe-call boundary;
- sanitized system log events for failures and degraded states;
- a cleanup hook for temporary downloads, audio/video fragments, OCR images, or document extracts when needed.

Tool failures must not block message persistence, embeddings, memory recall, `/stat`, `/character`, Telegram routing, or normal replies. Tool outputs that are later saved to memory must be stored as source context, not as user-authored text.

The media frame extraction adapter is registered as `media_frames` and is disabled by default. When `MEDIA_FRAME_EXTRACTION_ENABLED=true`, explicit Telegram video/animation/video-document routes can use bounded ffprobe/ffmpeg frame extraction for visual summaries. These summaries are saved only as source-context vision summaries, not user-authored text, and do not enable TikTok/Instagram support or passive group handling.

Universal media plans should keep the same ladder: captions first, optional audio transcription second, optional visual-frame summary only for explicit transcriptless cases, and a structured unavailable result when the media cannot be handled safely.

Future tool research notes:

- [`docs/tool-research/universal-media-transcript-mcp.md`](docs/tool-research/universal-media-transcript-mcp.md)
- [`docs/tool-research/visual-keyframe-extraction.md`](docs/tool-research/visual-keyframe-extraction.md)
- [`docs/tool-research/telegram-native-transcription.md`](docs/tool-research/telegram-native-transcription.md)
- [`docs/tool-research/transcription-backend-adapter.md`](docs/tool-research/transcription-backend-adapter.md)
- [`docs/tool-research/local-stt-vps-benchmark.md`](docs/tool-research/local-stt-vps-benchmark.md)
- [`docs/tool-research/transcript-memory-integration.md`](docs/tool-research/transcript-memory-integration.md)
- [`docs/tool-research/chat-digest-commands.md`](docs/tool-research/chat-digest-commands.md)
- [`docs/tool-research/document-pdf-ingest.md`](docs/tool-research/document-pdf-ingest.md)
- [`docs/tool-research/ocr-screenshot-understanding.md`](docs/tool-research/ocr-screenshot-understanding.md)
- [`docs/tool-research/fact-check-route-v2.md`](docs/tool-research/fact-check-route-v2.md)
- [`docs/tool-research/tool-health-capability-diagnostics.md`](docs/tool-research/tool-health-capability-diagnostics.md)

Long text replies are split by the delivery layer instead of being truncated at the first Telegram limit:

```env
MAX_REPLY_CHARS=12000
TELEGRAM_TEXT_CHUNK_CHARS=3500
MAX_REPLY_CHUNKS=4
```

## System Health And Self-Analysis

Aigan keeps a separate sanitized operational journal in SQLite. These records are not normal chat memory and are not used by `/stat`, `/character`, or ordinary answers.

Admin-only commands:

```text
/health
/logs 20
/selfcheck
/complaints
```

Ukrainian aliases:

```text
/самопочуття
/здоровя
/логи 20
/самоаналіз
/скарги
/температура
```

The system log stores metadata such as route decisions, tool failures, Telegram delivery fallbacks, image-search failures, pending/debounce events, and command usage. It must not store secrets, raw prompts, `.env`, private chat dumps, or full user messages.

User complaints about the bot are treated as temperature signals, not confirmed bugs. A first similar complaint starts at `temperature=1`; repeated similar complaints inside `COMPLAINT_LOOKBACK_SECONDS` raise the temperature. Reaction-specific criticism is classified into psychological-health categories such as `insensitive_reaction`, `reaction_reasoning_gap`, `tone_boundary`, `fake_empathy`, and `sycophancy`, with only behavior-level reaction decision metadata and a keyed non-reversible target fingerprint stored in self-reports when a reaction target is linked. `/health` and `/complaints` include a compact `Reaction health` summary with sent/skipped decision counts, emotion/reason counts, and reaction complaint temperatures; `/tool_health reactions` includes bounded reaction-memory decision counts. These diagnostics intentionally avoid raw message text, usernames, paths, source URLs, OCR/frame text, transcripts, and reaction rationale bodies. When `COMPLAINT_REPORT_TEMPERATURE` is reached and GitHub reporting is enabled, Aigan creates a sanitized `[Aigan] self-report: ...` issue and adds it to the configured GitHub Project.

```env
SYSTEM_LOG_ENABLED=true
SYSTEM_LOG_RETENTION_DAYS=14
GITHUB_REPORTING_ENABLED=false
GITHUB_TOKEN=
GITHUB_REPOSITORY=Turkevich91/Aigan
GITHUB_PROJECT_OWNER=Turkevich91
GITHUB_PROJECT_NUMBER=4
COMPLAINT_LOOKBACK_SECONDS=86400
COMPLAINT_REPORT_TEMPERATURE=3
```

Health reports to an admin/private chat are opt-in:

```env
HEALTH_REPORT_ENABLED=false
HEALTH_REPORT_ADMIN_CHAT_ID=
HEALTH_REPORT_INTERVAL_SECONDS=21600
HEALTH_REPORT_LOOKBACK_SECONDS=21600
HEALTH_REPORT_MIN_LEVEL=warning
HEALTH_REPORT_COOLDOWN_SECONDS=3600
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

Internal prompts, hidden instructions, env values, secrets, tool wiring, and private logs are not chat material. If someone asks Aigan to expose them, it gives a short boundary answer and can discuss only observable behavior or a concrete bug.

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
PROACTIVE_PROMPT=Write one short thought seed that can restart the room: an observation, paradox, or safe provocation, not a helpdesk offer.
PROACTIVE_PERSONA_MODE=thought_seed
PROACTIVE_REGENERATE_ON_PERSONA_REJECT=true
PROACTIVE_IDLE_ONLY=true
PROACTIVE_IDLE_SECONDS=21600
PROACTIVE_MIN_SECONDS_BETWEEN_POSTS=21600
PROACTIVE_PERSONAL_PING_ENABLED=true
PROACTIVE_PERSONAL_PING_PROBABILITY=0.35
PROACTIVE_PERSONAL_PING_MIN_USER_IDLE_SECONDS=86400
PROACTIVE_PERSONAL_PING_COOLDOWN_SECONDS=259200
PROACTIVE_PERSONAL_PING_MAX_CANDIDATES=5
PROACTIVE_META_TOPIC_GUARD=true
PROACTIVE_META_TOPIC_STRICT=true
```

With `PROACTIVE_IDLE_ONLY=true`, the loop checks on `PROACTIVE_INTERVAL_SECONDS` but calls the model only after the latest non-bot chat message is older than `PROACTIVE_IDLE_SECONDS`. Bot proactive posts do not reset that idle timer, and `PROACTIVE_MIN_SECONDS_BETWEEN_POSTS` prevents repeated self-posting.

With `PROACTIVE_PERSONA_MODE=thought_seed`, proactive messages should behave like short conversation catalysts: an observation, paradox, or safe provocation from the chat's actual topics, not a report about what the bot can do. `PROACTIVE_REGENERATE_ON_PERSONA_REJECT=true` rejects helper/service-style drafts once and asks the model to rewrite before skipping. `PROACTIVE_META_TOPIC_GUARD=true` removes self/prompt/internal-setup topics from proactive context and rejects drafts that still talk about them; strict mode skips generation when there is no non-meta topic to use.

Personal pings are optional. When enabled, Aigan may choose one participant who has been quiet for `PROACTIVE_PERSONAL_PING_MIN_USER_IDLE_SECONDS`, use only that user's own recent cleaned text as topic material, and respect a per-user cooldown. It should use `@username` when available, avoid sensitive topics, and return `SKIP` instead of forcing a bad nudge.

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
