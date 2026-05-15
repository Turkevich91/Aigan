# Transcript Memory Integration Plan

Status: research/design note for issue `#22`.

## Scope

Design how future transcript outputs from universal media URLs, native Telegram media, YouTube audio fallback, and the shared transcription backend become searchable Aigan memory.

This note does not implement transcription, add a media adapter, change routing semantics, add document/OCR/fact-check tools, alter production configuration, or deploy anything.

## Current Baseline

- `MemoryStore` already separates user-authored `text` from `source_text`.
- FTS, keyword search, embeddings, `/memory_search`, and natural recall already search `source_text`, `source_title`, `source_url`, and image `vision_summary`.
- `/stat` and `/character` use user-authored text only; source/repost items are counted separately and excluded from personal writing analysis.
- `ToolRuntime` is the required boundary for optional tool failures, health, and cleanup.
- Universal media, Telegram native transcription, and transcription backend research all defer transcript memory writes to this issue.

## Design Goal

When Aigan transcribes media, the transcript should become searchable source context, so a later prompt can recall old videos, voice notes, clips, or summaries by meaning or keyword.

The transcript must not become evidence that the forwarding or requesting user personally wrote those words.

## Memory Contract

Transcript memory should be stored as source-only context:

- `text=""` for transcript-only rows unless there is a real user-authored caption/request to preserve separately.
- `source_text` contains the transcript summary and bounded transcript snippets.
- `content_kind="transcript"`.
- `attachment_type` names the source family, such as `telegram_voice`, `telegram_video`, `media_url`, or `youtube_audio`.
- `source_url` stores a sanitized public URL only when the source has one.
- `source_title` stores a short sanitized title or source label.
- `reply_to_message_id` points to the original Telegram message when available.
- `forward_origin` and `raw_note` may contain compact sanitized provenance, never raw private text or paths.

The row should use the original source message/user identity when that is useful for chat context, but personal stats/profile code must continue to read only authored `text`.

## Metadata And Provenance

The first implementation can reuse the existing `messages` table for search, but it should add a small companion metadata layer if dedupe or richer display is needed.

Suggested future table:

```text
transcript_artifacts(
    id,
    memory_item_id,
    artifact_key,
    source_family,
    platform,
    media_kind,
    backend,
    model,
    language,
    duration_seconds,
    source_url,
    source_title,
    transcript_hash,
    segment_count,
    truncated,
    created_at
)
```

`artifact_key` should be deterministic enough to avoid duplicate memory rows when a user retries the same transcription. For Telegram media this can include chat id, original message id or file unique id, backend, and transcript hash. For public URLs it can include sanitized canonical URL, backend, and transcript hash.

`memory_item_id` should point at the searchable source-only row in `messages`, so existing FTS, embeddings, cleanup, and recall behavior keep working.

## Transcript Text Shape

Do not dump unbounded raw transcripts into one memory row.

Recommended stored source text shape:

```text
Transcript summary:
...

Transcript snippets:
[00:00] ...
[00:32] ...
...
```

Recommended limits:

- keep a concise generated or extractive summary when the transcript is long;
- store timestamped snippets up to a configured character cap;
- split long transcripts into deterministic chunks only when later recall quality requires it;
- include language and truncation markers in metadata, not raw logs;
- keep the full media file out of durable storage unless a later private cache issue explicitly approves it.

If chunking is added, each chunk should be source-only and linked by the same artifact key plus a part index.

## Write Flow

1. A media or transcription adapter returns a structured transcript result with sanitized provenance.
2. The caller verifies that the request was explicit under existing routing rules.
3. A transcript-memory helper builds a source-only memory payload and metadata.
4. The helper deduplicates by artifact key or transcript hash.
5. The helper writes the source-only row through `MemoryStore`.
6. The row is indexed by FTS immediately.
7. The row is queued for embeddings when vector memory is enabled.
8. Embedding failure is logged as a sanitized system event and does not roll back the memory write or user reply.

Suggested helper:

```text
save_transcript_source_memory(
    chat_id,
    source_message_id,
    source_identity,
    transcript_result,
    provenance
) -> TranscriptMemoryWriteResult
```

The helper should be a small tested module rather than new orchestration in `main.py`.

## Recall And Display Behavior

`/memory_search` and natural recall should find transcript rows through the existing hybrid search path.

Search result formatting should show enough metadata to explain the source:

- source type, such as transcript or video transcript;
- platform or source family when known;
- backend/model when useful for diagnostics;
- date stored or source date when available;
- title and sanitized URL when safe.

Normal answers should treat transcripts as untrusted source material. If a transcript is Russian, Aigan should still answer in Ukrainian according to the existing language policy.

## Stats And Profile Isolation

Transcript text must not pollute `/stat`, `/character`, social profile extraction, proactive personal topics, or complaint/self-analysis text classification.

Default policy:

- forwarded/public media transcripts are always source context;
- Telegram voice notes are still source context in v1, even when spoken by the user;
- counting a user's own voice note as authored speech requires a separate explicit policy issue;
- bot-generated summaries of transcripts are not user-authored text.

This keeps the implementation aligned with the existing source-text split and avoids silently changing what "user writing style" means.

## Safety Rules

- Do not log raw transcript text, raw captions, private media paths, Telegram file paths, provider request bodies, tokens, cookies, or full private URLs.
- Do not put raw transcripts in GitHub issues, PRs, system-log messages, or health summaries.
- Strip credentials, tracking parameters, and token-like query values before saving `source_url`.
- Keep downloaded media and temp audio under the owning adapter cleanup contract.
- Store provider/backend failures as sanitized categories.
- Keep transcript content out of ordinary group responses unless the bot was explicitly invoked or the request is in a private DM.

## Configuration Defaults

Suggested future settings:

```env
TRANSCRIPT_MEMORY_ENABLED=true
TRANSCRIPT_MEMORY_MAX_CHARS=20000
TRANSCRIPT_MEMORY_SUMMARY_CHARS=1200
TRANSCRIPT_MEMORY_CHUNK_CHARS=3500
TRANSCRIPT_MEMORY_INCLUDE_TIMESTAMPS=true
TRANSCRIPT_MEMORY_STORE_FULL=false
```

`TRANSCRIPT_MEMORY_STORE_FULL=false` means the first implementation should prefer summaries plus bounded snippets. A later issue can decide whether full transcripts are worth storing for specific private deployments.

## Health And Diagnostics

Tool health should expose only counts and sanitized categories:

- transcript memory enabled/disabled;
- recent writes;
- dedupe hits;
- truncation count;
- embedding queued/stored/failure counts;
- last sanitized failure category.

Do not include transcript text, URLs with private query strings, local paths, usernames, or raw provider errors.

## Future Test Plan

- Saving a transcript creates a source-only searchable row.
- `/memory_search` finds transcript text through FTS when embeddings are unavailable.
- Natural recall can retrieve transcript context after explicit invocation.
- Transcript words do not affect `/stat` top words or `/character` profile samples.
- Source item counts may mention transcript/source items without counting them as authored text.
- Metadata display includes source family/platform/backend/date without raw transcript leakage.
- Duplicate transcription retries do not create duplicate memory rows.
- Long transcripts are truncated or chunked deterministically.
- URL sanitization strips credentials and token-like query values.
- Embedding enqueue happens after save, and embedding failure is non-fatal.
- Disabled transcript memory returns a no-op result and does not break the user-facing transcription reply.
- Existing forwarded/source-text tests continue to pass.

## Recommended Implementation Sequence

1. Add transcript-memory dataclasses and a null/no-op helper.
2. Add source-only memory write tests using the existing `MemoryStore`.
3. Add metadata/dedupe storage only if source-only rows alone are not enough.
4. Add formatting support for transcript metadata in memory search results.
5. Wire universal media, Telegram native transcription, and YouTube fallback callers one at a time.
6. Add regression tests for `/stat`, `/character`, recall, and embedding failure isolation.

## Acceptance Mapping For Issue #22

- Transcript snippets are searchable through the existing hybrid memory path.
- Metadata display is covered by the proposed transcript artifact fields and recall formatting.
- `/stat` and `/character` isolation is preserved by storing transcript words in source-only memory, not authored text.

## Related Notes

- [`universal-media-transcript-mcp.md`](universal-media-transcript-mcp.md)
- [`telegram-native-transcription.md`](telegram-native-transcription.md)
- [`transcription-backend-adapter.md`](transcription-backend-adapter.md)
- [`local-stt-vps-benchmark.md`](local-stt-vps-benchmark.md)
