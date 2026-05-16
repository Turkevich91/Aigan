# Aigan Changelog

## 2026-05-15 - Transcript memory integration plan

- Added a tool research note for future transcript memory writes.
- Documented source-only transcript storage so media transcripts stay searchable without polluting `/stat` or `/character`.
- Captured future tests for transcript metadata, dedupe, truncation, recall, and embedding failure isolation.

## 2026-05-15 - Local STT VPS benchmark spike

- Added a benchmark/spike note for CPU-only local STT on the current deployment class.
- Documented whisper.cpp tiny/base/small wall time, peak RSS, storage footprint, multilingual quality caveats, and production recommendation.
- Kept OpenAI transcription as the recommended production default; local STT remains experimental/diagnostic only.

## 2026-05-15 - Transcription backend adapter research

- Added a research/design note for a future shared STT backend adapter.
- Documented OpenAI default behavior, local STT candidate boundaries, backend modes, health fields, failure categories, and migration from the direct YouTube fallback.
- Kept the task planning-only: no OpenAI/local Whisper implementation, Docker dependency change, routing change, or memory integration.

## 2026-05-15 - Telegram native transcription research

- Added a research/design note for future Telegram voice, audio, video, and video-note transcription.
- Documented Bot API download limits, activation rules, failure categories, temp cleanup, health fields, and source-memory boundaries.
- Kept the task planning-only: no STT backend, runtime routing, memory integration, or media download implementation.

## 2026-05-15 - Universal media transcript research

- Added a research/design note for a future universal media transcript MCP.
- Documented the caption-first extraction ladder, audio fallback boundaries, failure categories, health contract, and source-memory rules.
- Kept the task planning-only: no TikTok/Instagram/STT implementation or routing expansion.

## 2026-05-15 - Tool adapter runtime boundary

- Added a shared tool runtime boundary for optional adapters, including health summaries, safe failure handling, and cleanup hooks.
- Registered outbound reactions through the tool runtime while preserving the existing null-adapter fallback and behavior.
- Documented the adapter contract for future media, transcription, OCR, document, and fact-check tools.

## 2026-05-15 - Modular outbound reactions

- Added an optional outbound reaction adapter that can set real Telegram reactions on selected strong live group messages.
- Kept outbound reactions disabled by default and isolated from memory, social memory, digest, and embeddings through a null-adapter fallback.
- Added rate limits, cooldowns, deterministic relevance scoring, custom-emoji fallback handling, and local storage of bot-set reactions.
- Allowed ASCII reaction aliases such as `fire`, `eyes`, `thumbs_up`, `thinking`, and `laugh` to avoid `.env` Unicode corruption.

## 2026-05-15 - Cached reaction emoji analysis

- Added Telegram reaction memory as a separate relation layer, linked to stored messages when available.
- Added cached custom emoji assets with Telegram metadata, optional safe media caching, and lazy one-time vision summaries.
- Added chat-local reaction semantics so `/interests` and `/character` can use emoji reactions as lightweight taste signals without polluting `/stat`.

## 2026-05-15 - In-flight reply coalescing

- Added a per-chat in-flight generation guard so overlapping explicit prompts do not produce duplicate answers.
- Added short-lived duplicate suppression for near-identical prompts, including admin requests that bypass normal cooldowns.
- Suppressed ordinary auto-reactions while a chat answer is already being generated.

## 2026-05-15 - Prompt privacy and no-meta proactive

- Added a deterministic privacy boundary for direct requests to reveal system prompts, hidden instructions, env/secrets, tool wiring, or private logs.
- Added a minimal public identity response for “хто ти?” without exposing internal setup.
- Hardened proactive messages so bot/AI/prompt/internal-setup topics are filtered from context and rejected in drafts.

## 2026-05-15 - Social taste memory

- Added sanitized social taste memory for group/user interests, dislikes, irritation, amusement, recurring questions, and avoided topics.
- Added `/interests`, `/likes`, `/інтереси`, and `/смаки` for public high-level taste summaries, plus admin maintenance commands.
- Changed proactive messages to choose weighted directions from group taste, personal pings, current hooks, or unanswered threads while rejecting self-referential bot/capability framing.

## 2026-05-14 - Proactive thought-seed persona

- Changed proactive messages from service-style check-ins to short thought seeds from an equal AI participant.
- Added a proactive persona contract with few-shot examples for observations, paradoxes, safe provocations, and `SKIP`.
- Added a deterministic guard that rejects helper/capability-report phrasing and regenerates once before skipping.

## 2026-05-14 - Idle proactive personal pings

- Added idle-only proactive posting so Aigan only wakes the group after real user silence, not just on a fixed timer.
- Added optional soft personal pings that use a quiet participant's own recent topics, avoid sensitive subjects, and respect per-user cooldowns.
- Added proactive observability events for idle skips, cooldown skips, candidate selection, model skips, and sent pings.

## 2026-05-13 - Semantic direct memory recall

- Added semantic routing for natural prompts that ask Aigan to recall old chat context, without requiring `/memory_search`.
- Reused hybrid memory retrieval for direct recall prompts while excluding the current prompt from search results.
- Improved exact rescue for numeric phrases such as `170 тис`, `4070`, or `$250`, including matches stored as source/repost text.

## 2026-05-13 - Source content separated from user stats

- Added separate memory storage for forwarded/source text so repost bodies stay searchable without shaping `/stat` or `/характер`.
- Updated Telegram export import to split HTML forwarded bodies from the user's own comment and to treat JSON forwards as source-only when the author boundary is ambiguous.
- Added importer unresolved-author reporting and optional interactive user-map prompts for Telegram Desktop exports.

## 2026-05-13 - Full-history semantic profiles

- Fixed Telegram HTML import so joined messages without repeated sender names inherit the previous sender across pages.
- Expanded `/stat` and `/характер` identity matching to include `user_id`, username, and imported display-name aliases.
- Changed `/характер` to send aggregate stats, coverage, chronological anchors, recent tail, and embedding-diverse samples instead of raw full history.

## 2026-05-13 - Full-memory profiles and memory search

- Changed `/character` and `/характер` to analyze the full retained SQLite history through aggregate stats and representative samples instead of only the last 100 messages.
- Improved `/stat` and profile target resolution so `@username` can resolve to a stored `user_id` and include imported rows without usernames.
- Added Ukrainian memory-search aliases and richer `/memory_search` diagnostics showing embeddings, FTS fallback, and exact topic rescue sources.
- Added exact named-topic rescue for prompts like `згадай стару розмову про Pragmata`, so old indexed topics are included even when semantic retrieval is weak.

## 2026-05-13 - Native Telegram HTML export import

- Added direct Telegram Desktop HTML export import from `messages.html` files or full export directories.
- Preserved reply links, forwarded sources, text links, local photos, and optional user-map metadata during import.
- Kept JSON export import compatible while removing the manual HTML-to-JSON conversion step.

## 2026-05-13 - Telegram export memory import

- Added a one-off Telegram Desktop JSON importer for backfilling older chat history into SQLite memory.
- Kept imports idempotent by using Telegram `(chat_id, message_id)` keys and rebuilding the local FTS index.
- Added optional exported image copy and missing embedding backfill for imported messages.

## 2026-05-13 - Hybrid semantic memory search

- Added local SQLite semantic memory search with embeddings plus FTS5 fallback over retained chat history.
- Added background embedding backfill and admin `/memory_search` diagnostics.
- Kept ordinary group messages silent; long-term memory retrieval only runs after explicit bot invocation.

## 2026-05-13 - Short follow-up memory retrieval

- Added expanded persistent memory for explicit short follow-ups such as `@bot скільки?`, without changing ordinary group silence.
- Added reply-chain expansion so stored parent/grandparent messages can be used even when they are outside the normal recent window.
- Logged `memory_context_expanded` events for observability through health/log commands.

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
