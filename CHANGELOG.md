# Aigan Changelog

## 2026-07-11 - Provider-neutral heavy local inference connector

- Added a disabled-by-default `heavy_model` adapter boundary with null and OpenAI-compatible Chat Completions backends for bounded text, image, and video inference.
- Kept the configured endpoint, model id, credential, media URLs, prompts, and generated text out of health output and telemetry-safe result metadata.
- Added fixed-model requests, strict data-URL parsing with actual decoded-byte limits, rejection of all remote media references pending a trusted staging layer, output limits, fail-fast concurrency control, safe error categories, cleanup, and a model-list probe that performs no inference.
- Left Telegram media routing, durable background jobs, media persistence, memory integration, provider fallback, deployment, and model lifecycle control unchanged.

## 2026-07-11 - Preregistered bounded-memory extraction v2

- Added a separate offline v2 schema with mutually exclusive candidate/no-candidate branches and strict rejection of model-owned values that require normalization.
- Added text-, template-, and identity-disjoint 160-case development/holdout fixtures plus a frozen balanced 48-case multilingual screen; regular tests keep holdout labels closed.
- Added cache-write-aware cost accounting, complete transitive artifact hashes, matrix-wide least-cost selection, clean-commit binding, and pre-API validation.
- Replaced the caller-selectable evaluator-v5 holdout receipt with an evaluator-v6 canonical content-keyed claim in private persistent POSIX-user state; CI, detected common container markers, unsafe directories, and repeated claims fail before provider access.
- Converted evaluator input and attestation-output filesystem failures into bounded CLI errors instead of tracebacks, without changing inference, scoring, or the measured result.
- Kept the measured evaluator-v5/prompt-v7 development matrix as `NO_GO` with no selected model and no v2 holdout request; evaluator v6 made no model calls and does not reinterpret that result.
- Left v1 immutable and permanently closed and left runtime memory, SQLite, vector/FTS retrieval, Telegram behavior, and the Sol answer path unchanged.

## 2026-07-11 - Frozen bounded-memory extraction evaluation

- Added separate 120-case development and 120-case holdout public-synthetic multilingual extraction blocks with explicit source, speaker, scope, correction/conflict, uncertainty, validity, forwarded, prior-bot, and tool-anchor labels.
- Added a strict candidate-only schema, fail-closed provenance validator, deterministic high-precision baseline, repeated stability measurement, frozen prompt/schema/evaluator hashes, Wilson-bound gates, and an aggregate-only API evaluator.
- Kept evaluator pricing on the existing shared runtime snapshot so this offline slice does not change live telemetry or authorize additional runtime models.
- Recorded the one locked Luna-low holdout as `NO_GO`: quality was perfect on 120 unique cases, but two of 360 repeat outputs failed the strict conditional schema-validity gate; no runtime worker or live shadow is authorized.
- Kept runtime memory, SQLite schema, vector/FTS retrieval, Telegram behavior, and the Sol-low answer path unchanged; a separate runtime-shadow PR remains gated on model results.

## 2026-07-10 - Privacy-bounded at-most-once self-reporting

- Added a durable SQLite report-claim ledger so concurrent threshold crossings, restarts, timeouts, and local finalization failures cannot automatically duplicate a GitHub issue.
- Added a scrubbed compatibility tombstone so rollback to the previous temperature-growth logic remains duplicate-safe; outbound reporting must still be disabled before a pre-ledger rollback.
- Removed chat samples, identities, internal fingerprints, paths, links, and exact timestamps from public self-report bodies; public correlation uses an opaque keyed marker.
- Decoupled core Issues reporting from optional GitHub Project access and made Project-add failures non-blocking after issue creation.
- Documented the minimal fine-grained token scope: repository `Issues: Read and write` plus GitHub-required read-only metadata.

## 2026-07-09 - Persistent outbound identity and provenance

- Persist Telegram bot text chunks and web images only after successful delivery, using the real returned message ids and ordered per-run chunk mappings.
- Reconstruct reply chains through complete chunk groups after restart and repair pending reaction targets as soon as an outbound message is stored.
- Add privacy-bounded route/tool provenance with keyed allowlisted source fingerprints, low-cardinality result digests, and sensitive Agents SDK tracing disabled.
- Isolate post-delivery SQLite/provenance failures from Telegram retries so an already delivered reply is never duplicated.
- Add durable pre-send reminder attempt markers and terminal unknown-outcome handling so timeouts, crashes, or simultaneous persistence/finalization failures cannot requeue a possibly delivered reminder.

## 2026-05-25 - Living reminders

- Added durable SQLite living reminders with idempotent due-fire claiming, misfire handling, and yearly birthday recurrence.
- Added explicit `/remind`, `/reminders`, and `/remind_cancel` commands plus an Agents SDK reminder tool for clear model-detected reminder requests.
- Added a scheduled model wake-up path that uses chat memory/context, asks for missing context instead of silently skipping, and surfaces sanitized reminder health diagnostics.

## 2026-05-23 - Telegram video frame fallback rollback

- Removed chat-facing Telegram video frame analysis so video/animation/video-note/video-document messages no longer trigger downloads, frame extraction, vision calls, or the representative-frame failure reply.
- Kept video attachments visible to the normal agent path as safe attachment markers alongside available text, captions, and reply context.
- Left image/photo analysis and the YouTube transcript path unchanged.

## 2026-05-22 - Public media route rollback

- Rolled back the public media URL/TikTok context route so YouTube links return to the normal agent path with the YouTube transcript MCP.
- Removed public-media temp downloads and visual frame fallback from URL handling after the route over-triggered expensive media analysis and degraded transcript requests.
- Kept earlier reliable URL lookup, Telegram activity presence, and working-memory improvements intact.

## 2026-05-19 - Telegram activity presence

- Added a centralized Telegram activity presence helper for long-running text, web, memory, translation, and image routes.
- Changed internet image lookup to show `typing` while searching and `upload_photo` immediately before sending photos.
- Surfaced sanitized Telegram presence and private-chat draft capability rows in tool diagnostics.

## 2026-05-18 - Reliable public URL lookup

- Added explicit configurable timeouts for MCP tool sessions and DDGS web/image search.
- Made time-sensitive prefetch prefer URLs in the current trusted prompt before reply/reference context and include direct URL fetch evidence ahead of secondary search.
- Normalized timeout/fetch/search failures into stable categories so Aigan can report incomplete validation instead of guessing from weak evidence.

## 2026-05-18 - Working-memory context compiler

- Added source-linked recall expansion so semantic/FTS hits become evidence windows with neighboring messages and reply-chain context.
- Added prompt-memory dedupe and char budgets to reduce repeated recent/semantic/reply-chain blocks and mark prior bot output as non-evidence.
- Added admin-only `/context_window` and `/memory_context` diagnostics with sanitized prompt-memory counts, limits, duplicate estimates, and embedding backlog state.

## 2026-05-16 - Reaction shadow eval diagnostics

- Added shadow/eval counters to outbound reaction decision summaries: score bands, context flags, candidate emotion classes, and future model-call gate buckets.
- Surfaced the new counters in compact `Reaction health` admin diagnostics without changing outbound reaction behavior or adding LLM calls.
- Added regressions for model-candidate, incomplete-context, and deterministic-gate buckets using sanitized decision records only.

## 2026-05-16 - Reaction diagnostics and health

- Added compact `Reaction health` summaries to admin diagnostics with outbound decision counts, emotion/reason counts, and reaction complaint temperatures.
- Surfaced bounded reaction-memory sent/skipped decision counts in `/tool_health reactions`.
- Added regressions proving reaction diagnostics stay compact and do not expose raw message text, usernames, paths, source URLs, OCR/frame text, transcripts, or token-like values.

## 2026-05-16 - Reaction misfire self-analysis

- Added reaction-specific complaint classification for insensitive reactions, rationale gaps, tone boundaries, fake empathy, and sycophancy.
- Linked reaction complaints to recent outbound reaction decision records using sanitized behavior metadata and keyed non-reversible target fingerprints.
- Kept reaction self-reports free of raw chat text while adding regressions for passive complaints, missing rationale challenges, and sanitized GitHub reports.

## 2026-05-16 - Outbound reaction empathy preflight

- Added a final empathy/perception preflight before Telegram outbound reactions are sent.
- Blocked candidate reactions that could be perceived as approval of harm, suffering, coercion, humiliation, violence, death, or injustice.
- Stored sanitized preflight skip reasons in outbound reaction decision records and added regressions for policy-escape, source-only sympathy, and positive framing of harm.

## 2026-05-16 - Universal media transcriptless fallback plan

- Updated the universal media research note with a transcriptless visual-frame fallback ladder.
- Documented reuse of the existing `media_frames` adapter and visual summary helper instead of adding a second extractor.
- Captured future tests for no-captions/no-audio fallback, disabled visual fallback, bounded public media acquisition, cleanup, and source-context memory isolation.

## 2026-05-15 - Tool health diagnostics plan

- Added a tool research note for future `/tools` and `/tool_health` diagnostics.
- Documented an admin-only capability matrix, stable status vocabulary, sanitized failure aggregation, and next-action hints.
- Captured future tests for non-admin denial, disabled/null capability rows, system-log failure counts, unsafe-field redaction, and non-regression of existing diagnostics.

## 2026-05-15 - Fact-check route v2 plan

- Added a tool research note for a future fact-check route v2.
- Documented explicit-only claim verification, evidence/source quality classes, verdict labels, citation/date requirements, and route precedence boundaries.
- Captured future tests for claim extraction, translation/image precedence, stale or missing evidence, ClaimReview lookup, sanitized logging, and `/stat`/`/character` isolation.

## 2026-05-15 - OCR and screenshot understanding plan

- Added a tool research note for future OCR and screenshot understanding.
- Documented explicit-only screenshot handling, optional local OCR, vision cache reuse, source-only memory storage, and prompt-injection fencing.
- Captured future tests for delivered-image routing, cached OCR reuse, searchable screenshot text, provider failures, cleanup, and `/stat`/`/character` isolation.

## 2026-05-15 - Document and PDF ingest plan

- Added a tool research note for future document/PDF ingest.
- Documented explicit-only Telegram document handling, PDF/plain-text extraction, source-only memory storage, and temp-file cleanup.
- Captured future tests for file limits, extraction failures, OCR-required PDFs, source attribution, and `/stat`/`/character` isolation.

## 2026-05-15 - Chat digest commands plan

- Added a tool research note for future chat digest commands.
- Documented explicit-only digest routing, bounded memory selection, topic/time-window modes, unresolved-thread handling, and source attribution rules.
- Captured future tests for parser behavior, timezone windows, prompt caps, safety, and non-regression of memory/stat/profile flows.

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
