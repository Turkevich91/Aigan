# Transcription Backend Adapter Research

Status: research/design note for issue `#20`.

## Scope

Design a shared speech-to-text backend boundary for Aigan so native Telegram media, universal media transcripts, YouTube audio fallback, and future document/video flows can call one transcription contract.

This note does not implement the adapter. It does not install local STT dependencies, change Docker images, change Telegram routing, add memory writes, benchmark local models, or deploy a new backend.

## Current Baseline

- `mcp_servers/youtube_transcript.py` has a direct OpenAI transcription fallback behind `YOUTUBE_AUDIO_FALLBACK`.
- Universal media transcript research and Telegram native transcription research both defer speech-to-text to issue `#20`.
- `ToolRuntime` already provides adapter registration, null fallback, safe-call failure handling, health summaries, and cleanup hooks.
- Future transcript memory integration belongs to issue `#22`; this backend should only produce structured transcription results.

## Research Findings

- OpenAI should remain the production default because it avoids bundling large local model dependencies and is already used by the existing YouTube fallback path.
- Current OpenAI transcription models include `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, `gpt-4o-transcribe-diarize`, and `whisper-1`. Model capabilities differ, so the adapter should centralize feature negotiation rather than letting each media tool hard-code model-specific parameters.
- OpenAI transcription input formats currently include `flac`, `mp3`, `mp4`, `mpeg`, `mpga`, `m4a`, `ogg`, `wav`, and `webm`. Existing media download layers should still normalize through `ffmpeg` when needed, because Telegram and public media sources may deliver inconsistent containers/codecs.
- Official OpenAI docs currently describe different output-format support by model. A safe first adapter should request the simplest supported JSON/text response, extract `text`, and expose timestamps/diarization only when the selected model supports them.
- `language` can improve OpenAI accuracy and latency when known. Prompt/context hints can improve transcription quality or preserve continuity across chunks for non-diarization models.
- Streaming transcription exists for some OpenAI paths, but batch transcription is enough for this tool expansion epic. Realtime streaming should be a later issue if needed.
- `whisper.cpp` is attractive for optional CPU-capable local STT because it has C/C++ binaries, quantized models, Docker images, `ffmpeg` examples, word/segment timestamp options, and VAD support.
- `faster-whisper` is a Python-friendly local candidate using CTranslate2 with CPU `int8` support, but it adds heavier Python/native dependencies and should stay behind the separate VPS/local benchmark task before production use.
- Local STT quality, latency, memory use, image size, model storage, and language behavior must be measured on the target deployment before enabling any local backend by default.

Sources:

- https://developers.openai.com/api/docs/guides/speech-to-text
- https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
- https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe
- https://github.com/ggml-org/whisper.cpp
- https://github.com/SYSTRAN/faster-whisper

## Proposed Backend Shape

Add a future module such as `transcription_backend.py` with:

```text
TranscriptionBackendAdapter
NullTranscriptionBackendAdapter
OpenAITranscriptionBackendAdapter
LocalWhisperCppTranscriptionBackendAdapter
LocalFasterWhisperTranscriptionBackendAdapter
TranscriptionRequest
TranscriptionResult
```

Suggested operation:

```text
transcribe_audio(request: TranscriptionRequest) -> TranscriptionResult
```

Suggested request fields:

- local temp audio path;
- original media kind or source family;
- MIME type or normalized format;
- duration seconds when known;
- byte size;
- requested language hints such as `uk,en,ru`;
- optional previous transcript context for chunk continuity;
- timestamp granularity request: none, segment, or word;
- diarization request flag;
- max output characters;
- sanitized provenance/source label.

Suggested result fields:

- `ok`;
- transcript text;
- language and language confidence when available;
- segment or word timestamps when available;
- diarization speaker labels when available;
- backend mode and model;
- duration and usage/cost metadata when available;
- `truncated`;
- sanitized failure category and retryability when failed.

## Backend Modes

Recommended modes:

- `disabled`: always returns a clear unavailable result and disabled health.
- `openai`: production default; uses the OpenAI transcription API through existing SDK configuration.
- `local_whisper_cpp`: optional future local subprocess or sidecar adapter.
- `local_faster_whisper`: optional future Python in-process or worker adapter.

Mode selection should happen once at startup from configuration. Media tools should ask the runtime for the registered transcription adapter and should not import OpenAI, `whisper.cpp`, or `faster-whisper` directly.

## Processing Contract

1. Caller normalizes/downloads media into a temp file and passes only a local temp path plus sanitized metadata.
2. Backend adapter checks enabled state, backend availability, file readability, size, duration, and format support.
3. Adapter selects model-specific request parameters from a capability table.
4. Adapter invokes the backend with bounded timeout and optional retry only for retryable provider errors.
5. Adapter returns a structured result with transcript text or sanitized failure.
6. Caller owns final temp cleanup, while backend must not persist copies unless explicitly configured for a future private cache.

## Capability Table

The adapter should expose a capability table like:

```text
backend=disabled
  max_bytes=0
  timestamps=false
  diarization=false
  streaming=false

backend=openai
  default_model=gpt-4o-mini-transcribe
  accepted_formats=flac,mp3,mp4,mpeg,mpga,m4a,ogg,wav,webm
  max_bytes=25000000
  timestamps=model_dependent
  diarization=model_dependent
  streaming=model_dependent

backend=local_whisper_cpp
  default_model=tiny_or_base_after_benchmark
  accepted_formats=wav_or_ffmpeg_normalized
  timestamps=true
  diarization=false
  streaming=not_v1

backend=local_faster_whisper
  default_model=tiny_or_base_after_benchmark
  accepted_formats=ffmpeg_normalized
  timestamps=true
  diarization=false
  streaming=not_v1
```

Implementation should re-check current provider docs before coding because model lists, limits, pricing, and supported parameters change.

## Failure Categories

Use stable sanitized categories rather than raw exceptions:

- `backend_disabled`;
- `backend_misconfigured`;
- `input_missing`;
- `input_too_large`;
- `duration_limit`;
- `unsupported_format`;
- `provider_rate_limited`;
- `provider_auth_failed`;
- `provider_quota_exceeded`;
- `provider_unavailable`;
- `provider_bad_request`;
- `local_binary_missing`;
- `local_model_missing`;
- `local_dependency_failed`;
- `timeout`;
- `transcript_empty`;
- `unexpected_error`.

Failures must go through `ToolRuntime.safe_call` or an equivalent sanitized system-event path and must not block Telegram routing, memory ingestion, embeddings, recall, `/stat`, `/character`, or normal replies.

## Configuration Defaults

Recommended future defaults:

```env
TRANSCRIPTION_BACKEND=openai
TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
TRANSCRIPTION_MAX_AUDIO_BYTES=24000000
TRANSCRIPTION_MAX_DURATION_SECONDS=1200
TRANSCRIPTION_TIMEOUT_SECONDS=90
TRANSCRIPTION_LANGUAGES=uk,en,ru
TRANSCRIPTION_TIMESTAMPS=segment
TRANSCRIPTION_DIARIZATION=false
TRANSCRIPTION_LOCAL_MODEL=
```

`TRANSCRIPTION_MAX_AUDIO_BYTES` should stay below the provider limit to leave room for container overhead and future chunking behavior. Media-specific adapters may apply stricter limits, such as Telegram's delivered-file download limit.

## Safety And Memory Rules

- Do not log raw transcript text, raw audio paths, provider request bodies, credentials, private file paths, or full media URLs.
- Do not store transcripts as user-authored text inside this backend.
- Return provenance metadata so issue `#22` can store transcript output as source context.
- Preserve Aigan's Ukrainian output policy: Russian speech is source material and should be summarized/explained in Ukrainian.
- Keep local model paths and deployment-specific commands in private operator notes, not tracked docs.
- Treat diarization speaker labels as technical labels unless a later issue provides explicit, privacy-safe speaker identity rules.

## Health Contract

`health_summary()` should expose:

- `name=transcription_backend`;
- `enabled`;
- `adapter`;
- `status`;
- `error_count`;
- `mode`;
- `model`;
- `max_audio_bytes`;
- `max_duration_seconds`;
- `timestamps_supported`;
- `diarization_supported`;
- `streaming_supported`;
- `local_backend_available` when relevant;
- recent sanitized failures by category.

## Future Test Plan

- Null/disabled backend returns disabled health and a clear unavailable result.
- Env mode selects `openai`, `local_whisper_cpp`, `local_faster_whisper`, or disabled without importing unavailable heavy dependencies unnecessarily.
- OpenAI adapter maps model capabilities and omits unsupported parameters.
- OpenAI adapter extracts transcript text from JSON/text-like responses.
- Provider auth, quota, rate-limit, bad-request, and timeout exceptions map to sanitized categories.
- Size and duration limits reject work before provider calls.
- Local backend modes report missing binary/model/dependency as structured failures.
- Backend failures are logged through sanitized system events and do not break media tools, memory save, embeddings, recall, `/stat`, `/character`, or normal replies.
- Chunking prompt continuity can be tested without sending raw private transcript text to logs.
- Result provenance is marked for source-context memory integration.

## Migration Plan For Existing YouTube Fallback

1. Add the backend adapter with unit tests and register it through `ToolRuntime`.
2. Keep `YOUTUBE_AUDIO_FALLBACK=false` behavior unchanged.
3. Replace direct OpenAI calls in `mcp_servers/youtube_transcript.py` with the shared backend only after the backend has parity tests.
4. Keep captions-first behavior for YouTube and universal media.
5. Reuse the same backend from Telegram native transcription and future universal media audio fallback.
6. Defer memory writes and `/stat` or `/character` behavior to issue `#22`.

## Acceptance Mapping For Issue #20

- Env-selectable backend is covered by backend modes and configuration defaults.
- OpenAI remains default through `TRANSCRIPTION_BACKEND=openai` and `TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe`.
- Disabled or missing backend returns structured unavailable failures.
- Backend errors become sanitized system events and do not block memory ingestion or normal bot replies.
