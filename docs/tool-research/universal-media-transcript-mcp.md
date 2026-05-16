# Universal Media Transcript And Visual Fallback Research

Status: research/design note for issues `#18` and `#50`.

## Scope

Design a future universal media understanding tool for public video/audio URLs
such as YouTube Shorts, TikTok, Instagram Reels, X/Twitter video, Reddit video,
Vimeo, and similar platforms.

The first goal remains caption/transcript extraction. When captions and speech
transcription are absent, disabled, or unhelpful, the future tool should be able
to fall back to a bounded visual-frame summary for transcriptless/silent shorts
where useful context lives in frames.

This note does not implement public TikTok/Instagram/Reels support. It does not
add cookies, proxy handling, native Telegram voice/video transcription, OCR,
document ingest, fact-check routing, or passive group routing expansion.

## Current Baseline

- Aigan already has `mcp_servers/youtube_transcript.py` for YouTube captions with optional OpenAI audio fallback.
- `yt-dlp` and `ffmpeg` are installed in the Docker image.
- Optional tools should use the shared adapter/runtime boundary documented in `README.md`.
- Tool outputs that reach memory must be source context, not user-authored text.
- Raw media and temporary files must be deleted after processing.
- `MediaFrameAdapter` is registered through `ToolRuntime` as `media_frames` with
  a disabled null fallback, bounded ffprobe/ffmpeg frame extraction, health, and
  cleanup.
- Explicit Telegram video/animation/video-document routes can already summarize
  representative frames through the existing vision path when media-frame
  extraction and image analysis are enabled. That route is explicit-only and
  stores the summary as source-context `vision_summary`.

## Research Findings

- `yt-dlp` is the right first extraction layer, but its own supported-sites page says support is best-effort and the only reliable test is to try the URL. Future UI must report degraded failures honestly rather than promising universal support.
- Captions/subtitles should be preferred over audio transcription. They avoid downloads, cost less, and reduce temp-file risk.
- OpenAI speech-to-text model names and accepted audio formats should be read from the current API reference during implementation; the current docs include `gpt-4o` transcription variants and `whisper-1`, and file uploads are limited to 25 MB.
- Long audio needs chunking below the file limit. Chunk boundaries should avoid cutting mid-sentence when possible.
- `whisper.cpp` is a plausible local-STT candidate because it supports quantized models and CPU/OpenVINO paths, but local STT should stay behind the separate benchmark/backend tasks before production use.
- TikTok and Instagram should be treated as unreliable in v1 because extractor breakage, auth gates, rate limits, cookies, or bot protection can fail independently of Aigan.
- Some short videos carry their meaning mostly in visible actions, captions,
  screenshots, or on-screen UI rather than spoken words. A transcript-only tool
  would report "no transcript" even though a visual summary could still be
  useful.
- Visual fallback costs vision tokens and touches media bytes, so it must be
  opt-in, capped, explicit-route only, and routed through the existing
  `MediaFrameAdapter` rather than duplicating frame extraction logic.

Sources:

- https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
- https://github.com/ggml-org/whisper.cpp
- https://www.scenedetect.com/cli/
- https://ffmpeg.org/ffmpeg-filters.html
- https://developers.openai.com/api/docs/guides/images-vision

## Proposed Tool Shape

Add a future local MCP server, likely `mcp_servers/media_context.py`, backed by a
testable Python module instead of embedding public URL logic in `main.py`.

Suggested MCP tool:

```text
get_media_context(
    url: str,
    languages: str = "uk,en,ru",
    include_timestamps: bool = True,
    allow_visual_fallback: bool = False,
    max_chars: int = 16000
)
```

Recommended internal layers:

- `MediaTranscriptAdapter`: optional adapter registered through `ToolRuntime`.
- `NullMediaTranscriptAdapter`: disabled fallback with `health_summary()`.
- `MediaContextResult`: structured result with success state, modality
  (`captions`, `automatic_captions`, `audio_transcription`, `visual_summary`, or
  `unavailable`), transcript or visual summary text, metadata, backend, and
  sanitized failure.
- `MediaFrameAdapter`: reused for visual fallback. The media-context tool should
  call the registered adapter instead of owning a second frame extractor.
- MCP wrapper: thin stdio server that calls the adapter/core module and formats a model-readable response.

## Extraction Ladder

1. Validate that the URL is public `http` or `https`; reject local, private, file, credentialed, and non-network targets before passing anything to `yt-dlp`.
2. Use a `yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True})` instance and call `ydl.extract_info(url, download=False)` for metadata and extractor capability.
3. Re-check the resolved/canonical URL and any redirects for private IP ranges, URL credentials, unsupported schemes, and token-like query parameters.
4. Prefer manually provided captions in configured languages.
5. Fall back to automatic captions when available and marked as such.
6. If captions are missing and audio fallback is enabled, enforce duration and byte limits before downloading.
7. Download audio into a temporary directory, transcode to a supported compact format, transcribe through the configured STT adapter, and delete temp files.
8. If captions and STT are absent, disabled, failed safely, or return an
   unhelpful empty result, and visual fallback is explicitly enabled for this
   route, enforce media byte/duration caps before any full-file download.
9. For visual fallback, acquire media through a bounded download/copy boundary,
   pass the temp media to the registered `media_frames` adapter, extract 3-8
   representative frames, summarize them through the existing vision path, and
   delete temp media and frame files in `finally`.
10. Return a structured unavailable result if no layer can proceed.

Summary ladder:

```text
safe URL validation
-> metadata and captions
-> optional audio transcription
-> optional visual-frame summary
-> structured unavailable result
```

## Output Contract

A successful result should include:

- sanitized source URL and canonical URL when available;
- platform or extractor name;
- sanitized title/uploader metadata when available;
- duration in seconds when available;
- modality and backend used: `captions`, `automatic_captions`,
  `audio_transcription`, or `visual_summary`;
- transcript language when known;
- timestamped transcript when requested;
- visual summary frame count when visual fallback was used;
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
- `audio_unavailable`;
- `duration_limit`;
- `file_too_large`;
- `download_failed`;
- `transcription_failed`;
- `visual_fallback_disabled`;
- `visual_extraction_unavailable`;
- `visual_summary_failed`;
- `provider_failed`;
- `auth_or_rate_limited`;
- `drm_or_private`;
- `cleanup_failed`.

## Configuration Defaults

Recommended future defaults:

```env
MEDIA_CONTEXT_ENABLED=false
MEDIA_TRANSCRIPT_ENABLED=false
MEDIA_TRANSCRIPT_AUDIO_FALLBACK=false
MEDIA_TRANSCRIPT_VISUAL_FALLBACK=false
MEDIA_TRANSCRIPT_MAX_DURATION_SECONDS=1200
MEDIA_TRANSCRIPT_MAX_AUDIO_BYTES=24000000
MEDIA_TRANSCRIPT_MAX_VISUAL_MEDIA_BYTES=50000000
MEDIA_TRANSCRIPT_MAX_CHARS=16000
MEDIA_TRANSCRIPT_LANGUAGES=uk,en,ru
MEDIA_TRANSCRIPT_MODEL=gpt-4o-mini-transcribe
```

Cookie or proxy support should not be part of v1. If ever added, it must live in private operator configuration, never in tracked docs, issues, or logs.

## Safety And Memory Rules

- Do not keep downloaded media after the request completes.
- Reuse `MediaFrameAdapter` and its scoped cleanup contract for visual fallback.
- Strip credentials, tracking parameters, and token-like query values before storing or returning source URLs.
- Do not log raw URLs when they may contain private tokens or tracking parameters; log host/extractor/failure category instead.
- Do not put raw transcripts, frame-visible text, OCR-like text, file paths, or
  provider errors in system logs or GitHub issues.
- If transcripts or visual summaries become memory in a later issue, store them
  as source context with provenance, not as the forwarding user's authored text.
- Do not count transcript text or visual summaries in `/stat` or `/character`.
- Visual fallback is feature-flagged and explicit-route only: private DM,
  explicit command, explicit reply-to-bot, or pending context. It must not add
  passive group media handling.
- Preserve Aigan's Ukrainian output policy: Russian transcripts are source material and should be summarized/explained in Ukrainian.

## Health Contract

The adapter `health_summary()` should expose:

- `name=media_context` or `name=media_transcript`;
- `enabled`;
- `adapter`;
- `status`;
- `error_count`;
- `yt_dlp_available`;
- `ffmpeg_available`;
- `caption_backend`;
- `stt_backend`;
- `visual_fallback_enabled`;
- `media_frame_backend`;
- recent sanitized failure counts by category.

## Future Test Plan

- URL safety rejects local/private/file targets.
- Metadata extraction is mocked and does not hit the network in unit tests.
- Manual captions are preferred over automatic captions.
- Audio fallback is skipped when disabled.
- Duration and byte limits prevent large downloads/transcriptions.
- Visual fallback is skipped when disabled and returns a safe unavailable reason.
- Mocked no-captions/no-audio cases fall through to visual summary when the
  route is explicit and the feature flag is enabled.
- Public-media acquisition requires safe metadata and bounded download/copy
  before frame extraction.
- Temp files are removed after success, unavailable, timeout, decode failure,
  transcription failure, vision failure, and unexpected exceptions.
- Failure categories are sanitized and do not contain raw tokens, cookies, private paths, or full URLs.
- Adapter failures go through `ToolRuntime.safe_call` and do not break memory, embeddings, routing, `/stat`, `/character`, recall, or normal replies.
- Transcript and visual-summary output intended for memory is marked as source context.
- Visual summaries do not create user-authored text, `/stat`, `/character`, or
  profile pollution.

## Recommended Implementation Sequence

1. Build the adapter/core module with mocked `yt-dlp` metadata and caption parsing tests.
2. Keep the public URL/media-context adapter behind `ToolRuntime` with null fallback and admin diagnostics.
3. Add OpenAI STT through the transcription backend adapter task rather than duplicating YouTube fallback logic.
4. Add memory integration in the transcript memory task.
5. Reuse the existing `media_frames` adapter and visual-summary helper for transcriptless fallback instead of adding a second frame extractor.
6. Add the local MCP wrapper and wire it into agent tools only when `MEDIA_CONTEXT_ENABLED=true` or `MEDIA_TRANSCRIPT_ENABLED=true`.
7. Keep TikTok/Instagram/Reels support best-effort and report unavailable states honestly.
