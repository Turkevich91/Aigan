# Universal Media Transcript MCP Research

Status: research/design note for issue `#18`.

## Scope

Design a future universal media transcript tool for public video/audio URLs such as YouTube Shorts, TikTok, Instagram Reels, X/Twitter video, Reddit video, Vimeo, and similar platforms.

This note does not implement the tool. It does not add STT backends, cookies, proxy handling, native Telegram voice/video transcription, OCR, document ingest, fact-check routing, or memory integration.

## Current Baseline

- Aigan already has `mcp_servers/youtube_transcript.py` for YouTube captions with optional OpenAI audio fallback.
- `yt-dlp` and `ffmpeg` are installed in the Docker image.
- Optional tools should use the shared adapter/runtime boundary documented in `README.md`.
- Tool outputs that reach memory must be source context, not user-authored text.
- Raw media and temporary files must be deleted after processing.

## Research Findings

- `yt-dlp` is the right first extraction layer, but its own supported-sites page says support is best-effort and the only reliable test is to try the URL. Future UI must report degraded failures honestly rather than promising universal support.
- Captions/subtitles should be preferred over audio transcription. They avoid downloads, cost less, and reduce temp-file risk.
- OpenAI speech-to-text model names and accepted audio formats should be read from the current API reference during implementation; the current docs include `gpt-4o` transcription variants and `whisper-1`, and file uploads are limited to 25 MB.
- Long audio needs chunking below the file limit. Chunk boundaries should avoid cutting mid-sentence when possible.
- `whisper.cpp` is a plausible local-STT candidate because it supports quantized models and CPU/OpenVINO paths, but local STT should stay behind the separate benchmark/backend tasks before production use.
- TikTok and Instagram should be treated as unreliable in v1 because extractor breakage, auth gates, rate limits, cookies, or bot protection can fail independently of Aigan.

Sources:

- https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
- https://github.com/ggml-org/whisper.cpp

## Proposed Tool Shape

Add a future local MCP server, likely `mcp_servers/media_transcript.py`, backed by a testable Python module instead of embedding logic in `main.py`.

Suggested MCP tool:

```text
get_media_transcript(
    url: str,
    languages: str = "uk,en,ru",
    include_timestamps: bool = True,
    max_chars: int = 16000
)
```

Recommended internal layers:

- `MediaTranscriptAdapter`: optional adapter registered through `ToolRuntime`.
- `NullMediaTranscriptAdapter`: disabled fallback with `health_summary()`.
- `MediaTranscriptResult`: structured result with success state, transcript text, metadata, backend, and sanitized failure.
- MCP wrapper: thin stdio server that calls the adapter/core module and formats a model-readable response.

## Extraction Ladder

1. Validate that the URL is public `http` or `https`; reject local, private, file, credentialed, and non-network targets before passing anything to `yt-dlp`.
2. Use a `yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True})` instance and call `ydl.extract_info(url, download=False)` for metadata and extractor capability.
3. Re-check the resolved/canonical URL and any redirects for private IP ranges, URL credentials, unsupported schemes, and token-like query parameters.
4. Prefer manually provided captions in configured languages.
5. Fall back to automatic captions when available and marked as such.
6. If captions are missing and audio fallback is enabled, enforce duration and byte limits before downloading.
7. Download audio into a temporary directory, transcode to a supported compact format, transcribe through the configured STT adapter, and delete temp files.
8. Return a structured failure if any layer cannot proceed.

## Output Contract

A successful result should include:

- sanitized source URL and canonical URL when available;
- platform or extractor name;
- sanitized title/uploader metadata when available;
- duration in seconds when available;
- backend used: `captions`, `automatic_captions`, or `audio_transcription`;
- transcript language when known;
- timestamped transcript when requested;
- truncation marker when `max_chars` is reached.

A failed result should include:

- `ok=false`;
- sanitized failure category;
- sanitized user-facing reason;
- backend stage that failed;
- whether retry may help.

Suggested failure categories:

- `unsupported_url`;
- `metadata_failed`;
- `captions_unavailable`;
- `audio_fallback_disabled`;
- `duration_limit`;
- `file_too_large`;
- `download_failed`;
- `transcription_failed`;
- `auth_or_rate_limited`;
- `drm_or_private`;
- `cleanup_failed`.

## Configuration Defaults

Recommended future defaults:

```env
MEDIA_TRANSCRIPT_ENABLED=false
MEDIA_TRANSCRIPT_AUDIO_FALLBACK=false
MEDIA_TRANSCRIPT_MAX_DURATION_SECONDS=1200
MEDIA_TRANSCRIPT_MAX_AUDIO_BYTES=24000000
MEDIA_TRANSCRIPT_MAX_CHARS=16000
MEDIA_TRANSCRIPT_LANGUAGES=uk,en,ru
MEDIA_TRANSCRIPT_MODEL=gpt-4o-mini-transcribe
```

Cookie or proxy support should not be part of v1. If ever added, it must live in private operator configuration, never in tracked docs, issues, or logs.

## Safety And Memory Rules

- Do not keep downloaded media after the request completes.
- Strip credentials, tracking parameters, and token-like query values before storing or returning source URLs.
- Do not log raw URLs when they may contain private tokens or tracking parameters; log host/extractor/failure category instead.
- Do not put raw transcripts in system logs or GitHub issues.
- If transcripts become memory in a later issue, store them as source context with provenance, not as the forwarding user's authored text.
- Do not count transcript text in `/stat` or `/character`.
- Preserve Aigan's Ukrainian output policy: Russian transcripts are source material and should be summarized/explained in Ukrainian.

## Health Contract

The adapter `health_summary()` should expose:

- `name=media_transcript`;
- `enabled`;
- `adapter`;
- `status`;
- `error_count`;
- `yt_dlp_available`;
- `ffmpeg_available`;
- `caption_backend`;
- `stt_backend`;
- recent sanitized failure counts by category.

## Future Test Plan

- URL safety rejects local/private/file targets.
- Metadata extraction is mocked and does not hit the network in unit tests.
- Manual captions are preferred over automatic captions.
- Audio fallback is skipped when disabled.
- Duration and byte limits prevent large downloads/transcriptions.
- Temp files are removed after success and failure.
- Failure categories are sanitized and do not contain raw tokens, cookies, private paths, or full URLs.
- Adapter failures go through `ToolRuntime.safe_call` and do not break memory, embeddings, routing, `/stat`, `/character`, recall, or normal replies.
- Transcript output intended for memory is marked as source context.

## Recommended Implementation Sequence

1. Build the adapter/core module with mocked `yt-dlp` metadata and caption parsing tests.
2. Add the local MCP wrapper and wire it into agent tools only when `MEDIA_TRANSCRIPT_ENABLED=true`.
3. Add OpenAI STT through the transcription backend adapter task rather than duplicating YouTube fallback logic.
4. Add memory integration in the transcript memory task.
5. Add health/diagnostics in the tool health task.
