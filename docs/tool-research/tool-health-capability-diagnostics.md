# Tool Health And Capability Diagnostics Plan

Status: research/design note for issue `#27`.

## Scope

Design future admin diagnostics for Aigan's optional tools, backends, and capability matrix so operators can answer: what is enabled, what is disabled by design, what is misconfigured, what failed recently, and what the next likely action is.

This note does not implement `/tools`, change `/health`, add dependencies, change Docker images, deploy code, expose runtime secrets, or change Telegram routing.

## Current Baseline

- Aigan already has admin-only `/health`, `/logs`, `/selfcheck`, and `/complaints` with localized aliases.
- `SystemLogStore` records sanitized operational events with component, event type, level, route, duration, message, and details.
- `/health` already combines self-analysis health, semantic memory/vector status, and `ToolRuntime.health_summary()`.
- `ToolRuntime` already supports adapter registration, null adapters, safe-call failures, runtime error counts, cleanup hooks, and aggregated health summaries.
- Startup logs a sanitized `tool_runtime_ready` event and shutdown logs `tool_runtime_cleanup_finished`.
- Current registered runtime adapter coverage is still narrow; many future tool families are only planning notes.
- Non-admin access to diagnostics is denied through the same admin-command path used by `/health` and `/logs`.

## Research Findings

- Kubernetes separates liveness, readiness, and startup probes. The useful lesson for Aigan is to separate "process alive", "ready for this capability", and "starting or warming" instead of one vague healthy/unhealthy label.
- OpenTelemetry semantic conventions use stable attributes such as service name, operation name, status, error type, duration, and event/log fields. Aigan diagnostics should prefer stable low-cardinality fields over raw payloads.
- Prometheus metric and alerting practices emphasize naming, labels with bounded cardinality, rates/counts, and actionable alerts. Aigan's admin diagnostics should aggregate counts by stable component and failure category instead of dumping raw logs.
- Google SRE guidance distinguishes symptoms from causes and emphasizes alerts that require action. Aigan's tool diagnostics should show a compact "next likely action" and avoid paging the operator for disabled-by-design tools.

Sources:

- https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/
- https://opentelemetry.io/docs/specs/semconv/general/attributes/
- https://prometheus.io/docs/practices/instrumentation/
- https://sre.google/sre-book/monitoring-distributed-systems/

## User Value

Admins should be able to inspect tool health without reading container logs or guessing which backend failed.

Useful first cases:

- "why did TikTok/Instagram/YouTube transcript fail?";
- "is OpenAI STT configured or disabled?";
- "is local STT available?";
- "are embeddings falling behind?";
- "is OCR enabled?";
- "which tools are disabled intentionally?";
- "what broke in the last few hours?".

## Command Shape

Suggested admin-only commands:

```text
/tools
/tool_health
/tool_health media
/tool_health stt
/tool_health memory
/tool_health failures
/тулзи
/стан_тулзів
```

`/health` should remain the broad system snapshot. `/tools` or `/tool_health` should be a compact capability matrix focused on optional tools and backends. `/logs` remains the detailed recent event view.

Non-admin users should receive the same denial path as other admin diagnostics.

## Capability Matrix

Represent each capability as a sanitized row:

```text
CapabilityHealth(
    name,
    family,
    enabled,
    configured,
    available,
    status,
    adapter,
    mode,
    backend,
    error_count,
    warning_count,
    last_success_age_bucket,
    last_failure_age_bucket,
    recent_failure_categories,
    next_action
)
```

Recommended initial rows:

- `tool_runtime`;
- `system_log`;
- `web_search`;
- `web_image_search`;
- `youtube_captions`;
- `media_transcript`;
- `telegram_transcription`;
- `stt_openai`;
- `stt_local`;
- `document_ingest`;
- `image_understanding`;
- `ocr`;
- `fact_check`;
- `memory_store`;
- `memory_embeddings`;
- `chat_digest`;
- `outbound_reactions`;
- `reaction_memory`;
- `github_reporting`.

Rows for future tools may exist as disabled/null capabilities before implementation, as long as they are clearly marked `disabled` or `not_implemented` and do not imply support that is not present.

## Status Vocabulary

Use a small stable vocabulary:

- `ok`: enabled/configured and no recent relevant failures.
- `disabled`: intentionally off by config or feature flag.
- `not_implemented`: planned capability with no adapter yet.
- `unconfigured`: needs credentials, model, dependency, or endpoint before it can run.
- `degraded`: works but has recent warnings/failures or partial backend availability.
- `failing`: enabled but recent attempts are failing consistently.
- `error`: direct `ToolRuntime` adapter health failure, such as `health_summary()` raising; render as an urgent failing row unless a later mapper safely normalizes it.
- `unavailable`: dependency, provider, local binary, or network is unavailable.
- `warming`: startup, cache build, model load, or backfill is in progress.
- `unknown`: health could not be computed safely.

Do not mark disabled-by-design tools as failures.

## Data Sources

Build diagnostics from bounded sanitized sources:

1. `ToolRuntime.health_summary()` for registered adapters.
2. Adapter-specific `health_summary()` fields for tool capability state.
3. `SystemLogStore.health_summary()` and recent events aggregated by component, event type, level, and sanitized failure category.
4. Existing memory/vector health helpers for embedding backlog and last embedding failure.
5. Static feature flags and config booleans, represented as mode labels rather than raw values.
6. Optional startup/shutdown events for last-known adapter readiness and cleanup outcome.

Avoid direct probing that performs expensive work or calls paid/provider APIs just to render diagnostics. Active probes should be explicit admin actions later, for example `/tool_health probe stt`, and should have separate rate limits.

## Failure Aggregation

Aggregate recent failures by stable keys:

```text
component + event_type + failure_category + tool + operation
```

Recommended lookback windows:

```env
TOOL_HEALTH_LOOKBACK_SECONDS=21600
TOOL_HEALTH_MAX_FAILURE_ROWS=8
TOOL_HEALTH_MAX_CAPABILITY_ROWS=30
TOOL_HEALTH_SHOW_DISABLED=true
TOOL_HEALTH_ADMIN_ONLY=true
```

Output should show counts and age buckets such as `<5m`, `<1h`, `<6h`, `<24h`, or `none`, not raw timestamps when concise output is enough.

## Output Shape

Recommended default `/tools` output:

```text
Tool capabilities
Overall: degraded

ok:
- memory_store: enabled, rows/backlog summary
- web_search: enabled

degraded:
- memory_embeddings: 2 recent failures, backlog 14, next: check embedding provider

disabled:
- stt_local: disabled by config
- document_ingest: not implemented yet
```

Recommended `/tool_health failures` output:

```text
Recent tool failures
- stt_openai/transcribe timeout: 3 in <6h, next: check provider status and file size limits
- media_transcript/yt_dlp_unavailable: 1 in <24h, next: verify dependency image
```

Keep output short enough for Telegram and use existing reply splitting if needed.

## Next Likely Action

Each degraded/failing row may include one sanitized action hint:

- `check feature flag`;
- `check provider status`;
- `check credentials configured`;
- `check dependency image`;
- `check file size or duration limit`;
- `retry later`;
- `inspect /logs`;
- `run explicit probe`;
- `deployment-only check required`.

Do not include exact env var values, secret names beyond public config labels, local paths, hostnames, shell commands with private aliases, raw URLs, or raw exception text.

## Privacy And Safety

- Diagnostics are admin-only.
- Do not expose tokens, cookies, API keys, raw prompts, raw chat text, provider request bodies, Telegram file paths, local paths, media cache paths, database paths, or host aliases.
- Do not show raw user URLs if they may contain credentials or private query parameters.
- Do not show raw transcript, OCR, document, or fact-check source text.
- Do not use diagnostics as normal chat memory, `/stat`, `/character`, proactive context, or social taste input.
- Do not let capability rows imply a feature is available to users before routing and adapter tests exist.
- Keep logs and health output sanitized even when adapter `health_summary()` returns unsafe fields.

## Adapter Health Contract

Every future adapter should expose:

- `name`;
- `enabled`;
- `adapter`;
- `status`;
- `error_count`;
- `mode` or `backend` when useful;
- capability booleans such as `captions_enabled`, `audio_fallback_enabled`, `ocr_enabled`, or `local_backend_enabled`;
- limits such as max bytes, max duration, max pages, or max prompt chars;
- recent sanitized failure counts or categories;
- cleanup status when temporary files are involved.

Adapters must not expose:

- raw filenames, paths, URLs with tokens, provider response bodies, raw prompts, raw documents, raw transcripts, OCR text, private chat excerpts, or secret values.

`ToolRuntime` should continue to sanitize runtime exceptions and mark health as degraded when runtime error counts are nonzero.

## Relationship To Existing Commands

- `/health`: broad operator snapshot, including self-analysis, memory/vector health, and a compact tool runtime summary.
- `/tools`: capability matrix grouped by status/family.
- `/tool_health <family>`: focused diagnostic detail for one family.
- `/logs N`: recent sanitized event list for manual inspection.
- `/selfcheck`: model-assisted reflection over sanitized system health context.
- `/complaints`: complaint-temperature clusters.

If implementation scope is small, `/tools` can first reuse `/health`'s tool runtime block plus system-log failure aggregation before adding family filters.

## Future Test Plan

- `/tools`, `/tool_health`, and localized aliases are admin-only.
- Non-admin denial matches existing diagnostics behavior.
- Null/unimplemented capabilities render as `disabled` or `not_implemented`, not `failing`.
- Registered `ToolRuntime` adapters appear in the capability matrix.
- Adapter runtime errors mark the row and overall status as `degraded`.
- Adapter `health_summary()` exceptions produce `unknown` or `failing` row without crashing the command.
- System-log recent failures aggregate by component/event type/failure category without raw messages.
- Failure counts respect lookback windows and row caps.
- Enabled but missing credentials/dependencies renders `unconfigured` or `unavailable`.
- Disabled-by-config local STT/OCR/document ingest does not page as an error.
- Memory embeddings row includes backlog and last failure category without leaking provider payloads.
- Output does not contain raw prompts, private chat text, token-like values, local paths, media paths, database paths, or raw provider errors.
- `/health`, `/logs`, `/selfcheck`, `/complaints`, `/stat`, `/character`, memory search, and normal replies do not regress.
- Startup and shutdown tool runtime events remain sanitized.

## Recommended Implementation Sequence

1. Add a `tool_diagnostics.py` helper with capability row dataclasses, status vocabulary, sanitization, and rendering.
2. Add unit tests for row rendering, status grouping, disabled/null capabilities, and unsafe adapter field redaction.
3. Add system-log aggregation helpers for recent tool failures by stable category.
4. Add a capability registry that can merge static planned capabilities with live `ToolRuntime` adapter health.
5. Add `/tools` and `/tool_health` command handlers with localized aliases and admin-only checks.
6. Add focused family filters for media, STT, OCR, memory, web, reactions, fact-check, and documents.
7. Optionally extend `/health` with one compact "Tool capabilities: ok/degraded/failing" line after `/tools` exists.
8. Add explicit active probes only as a later issue if passive diagnostics are not enough.

## Acceptance Mapping For Issue #27

- Admin visibility for web, image search, YouTube captions, universal media, OpenAI STT, local STT, OCR, memory, and embeddings is covered by the capability matrix.
- Recent failure counts are covered by system-log aggregation.
- Non-admin denial is covered by command access and future regression tests.
- Sanitized output is covered by privacy rules, adapter health contract, and unsafe-field tests.

## Related Notes

- [`universal-media-transcript-mcp.md`](universal-media-transcript-mcp.md)
- [`telegram-native-transcription.md`](telegram-native-transcription.md)
- [`transcription-backend-adapter.md`](transcription-backend-adapter.md)
- [`local-stt-vps-benchmark.md`](local-stt-vps-benchmark.md)
- [`document-pdf-ingest.md`](document-pdf-ingest.md)
- [`ocr-screenshot-understanding.md`](ocr-screenshot-understanding.md)
- [`fact-check-route-v2.md`](fact-check-route-v2.md)
