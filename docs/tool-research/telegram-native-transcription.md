# Telegram Native Transcription Research

Status: research/design note for issue `#19`.

## Scope

Design a future native Telegram media transcription path for voice notes, audio files, videos, and video notes that Telegram delivers to Aigan through the Bot API.

This note does not implement transcription. It does not add an STT backend, local Whisper, memory integration, digest commands, document ingest, OCR, fact-check routing, or broader passive group routing.

## Current Baseline

- Aigan stores text, captions, forwarded/source text, image attachment metadata, and optional cached Telegram images.
- `main.py` already detects Telegram attachment kinds such as `voice`, `audio`, `video`, `video_note`, and `document`, but media-only audio/video is currently represented as an attachment placeholder.
- Existing image caching downloads delivered Telegram files through `get_file()` and stores bounded image bytes for memory/vision.
- The shared `ToolRuntime` boundary is the required integration point for optional tools.
- Universal public URL transcript research lives in `docs/tool-research/universal-media-transcript-mcp.md`; native Telegram media is a separate source path.

## Research Findings

- Telegram Bot API `getFile` is the correct download primitive for media already delivered to the bot. The cloud Bot API currently says bots can download files up to 20 MB, the returned file link is valid for at least 1 hour, and a new link can be requested after expiry.
- Telegram message objects expose `audio`, `voice`, `video`, `video_note`, and `document` attachments with `file_id`, optional `file_unique_id`, duration, MIME type, filename, and file size depending on the object.
- Voice notes are distinct from audio files. For outgoing voice, Telegram expects OGG/Opus, but incoming transcription should not rely on one exact codec; it should inspect metadata and normalize with `ffmpeg`.
- OpenAI speech-to-text file uploads are currently limited to 25 MB. The current transcription API reference accepts `flac`, `mp3`, `mp4`, `mpeg`, `mpga`, `m4a`, `ogg`, `wav`, and `webm`, and lists `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `gpt-4o-transcribe-diarize`, and `whisper-1`.
- The effective first production byte limit should be the stricter Telegram cloud download limit, with a safety margin below 20 MB unless a private local Bot API server is explicitly introduced later.
- Native Telegram media is not a substitute for arbitrary Telegram history access. The bot can only process media that Telegram delivers in updates, replies, forwards, or other supported message surfaces.

Sources:

- https://core.telegram.org/bots/api#getfile
- https://core.telegram.org/bots/api#audio
- https://core.telegram.org/bots/api#voice
- https://core.telegram.org/bots/api#video
- https://core.telegram.org/bots/api#videonote
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create

## Proposed Tool Shape

Add a future adapter registered through `ToolRuntime`, for example:

```text
TelegramNativeTranscriptionAdapter
NullTelegramNativeTranscriptionAdapter
TelegramNativeTranscriptionResult
```

Suggested public operation:

```text
transcribe_telegram_media(
    source_message_ref,
    media_kind: str,
    max_chars: int = 16000,
    include_timestamps: bool = True
)
```

The adapter should own Telegram media download, temp-file lifecycle, metadata checks, and sanitized failure reporting. Actual speech-to-text should delegate to the transcription backend adapter planned in issue `#20`, not duplicate OpenAI-specific logic inside the Telegram adapter.

## Activation Rules

Keep routing conservative:

- Private DM with a delivered supported media attachment may request transcription directly.
- Group transcription should require explicit invocation, reply-to-bot context, reply to the media with a clear request, or another existing explicit route.
- Passive group messages with voice, audio, video, or video notes should continue to be saved as attachments only.
- Forwarded media may be processed only when Telegram delivered the actual file object to the bot.
- Client-side previews, private previews, or old history that the Bot API did not deliver are out of scope.

## Supported Media Detection

Initial supported kinds:

- `voice`;
- `audio`;
- `video`;
- `video_note`;
- `document` only when MIME type clearly starts with `audio/` or `video/`.

Recommended metadata captured before download:

- media kind;
- `file_id` presence;
- optional `file_unique_id`;
- duration seconds when present;
- file size bytes when present;
- MIME type and filename when present;
- source surface such as current message, replied message, or forwarded/external reply.

Logs and system events should use sanitized categories and internal references, not raw captions, usernames, tokenized download URLs, or full private message text.

## Processing Ladder

1. Resolve the target media from the current message, replied-to message, or explicit forward/external reply context.
2. Reject unsupported media kinds before download.
3. Check declared file size and duration against configured limits.
4. Request Telegram file metadata through `getFile`.
5. Reject missing file paths, expired links that cannot be refreshed, or Telegram size errors as structured failures.
6. Download into a dedicated temporary directory, never directly into durable memory storage.
7. Normalize or extract audio with `ffmpeg` into a transcription-backend-supported format under the effective byte limit.
8. Call the configured STT backend through issue `#20`'s adapter contract.
9. Return a structured transcript result with sanitized metadata and truncation flags.
10. Delete all temp files in a `finally`/cleanup path, including download and transcoded fragments.

## Output Contract

A successful result should include:

- `ok=true`;
- media kind and source surface;
- duration seconds when known;
- input byte count when known;
- normalized audio format;
- transcription backend and model;
- transcript language when known;
- transcript text or timestamped segments, bounded by `max_chars`;
- `truncated=true` when output is clipped;
- provenance that marks the transcript as Telegram media-derived source context.

A failed result should include:

- `ok=false`;
- sanitized failure category;
- user-facing reason;
- backend stage that failed;
- retryability hint;
- whether temp cleanup completed.

Suggested failure categories:

- `unsupported_media`;
- `missing_file_id`;
- `file_too_large_telegram`;
- `duration_limit`;
- `download_failed`;
- `telegram_file_expired`;
- `protected_or_unavailable`;
- `ffmpeg_missing`;
- `ffmpeg_failed`;
- `unsupported_codec`;
- `stt_disabled`;
- `stt_failed`;
- `transcript_empty`;
- `cleanup_failed`.

## Configuration Defaults

Recommended future defaults:

```env
TELEGRAM_TRANSCRIPTION_ENABLED=false
TELEGRAM_TRANSCRIPTION_MEDIA_KINDS=voice,audio,video,video_note
TELEGRAM_TRANSCRIPTION_MAX_DOWNLOAD_BYTES=19000000
TELEGRAM_TRANSCRIPTION_MAX_DURATION_SECONDS=900
TELEGRAM_TRANSCRIPTION_MAX_CHARS=16000
TELEGRAM_TRANSCRIPTION_INCLUDE_TIMESTAMPS=true
```

The STT model/provider should come from the shared transcription backend adapter rather than Telegram-specific settings.

## Safety And Memory Rules

- Do not save downloaded audio/video files after the request completes.
- Do not put raw transcripts, raw captions, usernames, Telegram file paths, Bot API download URLs, tokens, or private operator paths into logs, GitHub issues, or system events.
- Use sanitized system-log events with media kind, failure category, duration, byte limits, and adapter names.
- In the first implementation, treat transcripts as source context by default, not as user-authored text.
- Do not count transcript words in `/stat` or `/character`.
- If a later policy wants a user's own voice note to count as authored speech, require an explicit authored-spoken-text decision in the memory integration issue.
- Preserve the Ukrainian output policy: transcribed Russian is source material and should be summarized/explained in Ukrainian.

## Health Contract

`health_summary()` should expose:

- `name=telegram_native_transcription`;
- `enabled`;
- `adapter`;
- `status`;
- `error_count`;
- `telegram_download_available`;
- `ffmpeg_available`;
- `stt_backend`;
- `temp_dir_writable`;
- current byte and duration limits;
- recent sanitized failures by category.

Disabled or misconfigured adapters must not block Telegram routing, memory save, embeddings, recall, `/stat`, `/character`, or normal replies.

## Future Test Plan

- Null adapter returns disabled health and no-ops safely.
- Group media is ignored unless the existing explicit route rules are satisfied.
- Private DM voice note request resolves the current message media.
- Reply-to-media request resolves voice, audio, video, and video-note targets.
- Document media is accepted only for clear audio/video MIME types.
- File-size and duration limits reject work before download.
- Telegram `getFile` and download failures become sanitized failure categories.
- `ffmpeg` failures do not leak file paths or raw command output.
- STT backend disabled/failing returns a structured failure through `ToolRuntime.safe_call`.
- Temp files are deleted after success, download failure, `ffmpeg` failure, STT failure, and transcript truncation.
- Transcript output intended for memory is marked as source context and does not pollute `/stat` or `/character`.

## Recommended Implementation Sequence

1. Add adapter and null adapter with mocked Telegram media objects and health tests.
2. Add target-media resolution for explicit current-message and reply-to-message paths.
3. Add bounded Telegram download and temp cleanup tests.
4. Add `ffmpeg` normalization behind a small wrapper with mocked subprocess tests.
5. Wire the STT call through the shared transcription backend adapter from issue `#20`.
6. Add user-facing Telegram replies only for explicit routes.
7. Defer searchable transcript memory writes to issue `#22`.

## Acceptance Mapping For Issue #19

- Private forward and explicit group reply transcription is covered by the activation and target-resolution plan.
- Voice, audio, video, and video-note support is covered by the supported media detection plan.
- Searchable memory is deferred to issue `#22`, with provenance and source-context rules defined here.
- `/stat` and `/character` pollution is explicitly prohibited unless a later authored-spoken-text policy changes that behavior.
