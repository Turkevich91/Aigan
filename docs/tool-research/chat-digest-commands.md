# Chat Digest Commands Plan

Status: research/design note for issue `#23`.

## Scope

Design future chat digest commands for Aigan so users can request concise recaps of busy periods, missed threads, decisions, unresolved questions, and topic-focused history.

This note does not implement `/digest`, change Telegram routing, add new tools, alter memory retention, deploy code, or expand passive group behavior.

## Current Baseline

- Aigan already stores bounded SQLite memory for delivered messages.
- `MemoryStore.latest()` and reply-chain helpers can provide recent context windows.
- Hybrid semantic memory search already supports topic recall through FTS, keyword fallback, and embeddings after explicit invocation.
- Source/repost content is searchable as source context but must not be misattributed as user-authored text.
- Transcript source memory is planned in issue `#22`; digest implementation should consume it only after that source-only write path exists.
- Proactive unanswered-thread logic already has a small heuristic for unresolved-looking recent messages, but there is no user-facing digest command.

## User Value

Digest commands should help people catch up without reading hundreds of messages.

Useful first cases:

- "what did I miss today?";
- "summarize the last 200 messages";
- "what decisions did the group make yesterday?";
- "what open questions are still unresolved?";
- "give me a digest about the STT/tool discussion";
- "summarize this user-facing thread after I was away".

## Commands And Aliases

Suggested commands:

```text
/digest
/digest today
/digest yesterday
/digest last 200
/digest since 14:30
/digest topic local stt
/digest unresolved
/дайджест
/підсумок
```

Aliases should route through the existing explicit command path. Ordinary group chatter must remain silent unless there is a command, mention, reply-to-bot, private DM, or pending request.

## Request Parsing

The digest parser should produce a small structured request:

```text
DigestRequest(
    mode: recent | today | yesterday | since | topic | unresolved,
    limit_messages,
    start_time,
    end_time,
    topic_query,
    include_sources,
    requester_context
)
```

Defaults:

- no arguments means a recent-window digest;
- `today` and `yesterday` use the configured local timezone;
- `last N` is capped by a safe maximum;
- `since HH:MM` applies to the current local date unless a date is provided;
- topic mode should use semantic memory retrieval plus a bounded surrounding context window;
- unresolved mode should prioritize questions, requests, TODO-like phrases, and threads without later clear resolution.

Ambiguous dates or very broad windows should produce a short clarification instead of silently dumping too much history into a model call.

## Memory Selection

Digest input should be built from retained memory visible to the bot, not from Telegram history that the bot never received.

Recommended source selection:

1. Build a candidate window by time or message count.
2. Add reply-chain parents for selected messages when available.
3. For topic digests, combine semantic/FTS results with nearby chronological context.
4. De-duplicate by memory item id.
5. Keep chronological order for the final prompt package.
6. Clip the package by message count and character budget before model invocation.

Recommended first limits:

```env
DIGEST_MAX_MESSAGES=300
DIGEST_DEFAULT_MESSAGES=120
DIGEST_MAX_PROMPT_CHARS=30000
DIGEST_TOPIC_TOP_K=12
DIGEST_REPLY_CONTEXT_DEPTH=3
```

Do not inject raw full retained history. If the requested window is too large, create a staged digest from sampled chronological anchors plus semantic/topic clusters, or ask the user to narrow the range.

## Digest Prompt Package

The model should receive a structured, untrusted package:

```text
Digest request:
- mode
- time window
- topic query when present

Untrusted retained chat items:
- message reference
- timestamp
- sender label or sanitized speaker label
- authored text excerpt
- source/repost/transcript excerpt marked as source context
- reply-to reference when available
- content kind and attachment type when useful
```

Source/repost/transcript excerpts must be labeled as source material so the model does not attribute public-media or forwarded text to the sender.

The digest prompt must explicitly fence retained chat items as untrusted source material. It should instruct the model to summarize or extract facts from the package, but not follow instructions, tool requests, roleplay, secrets requests, or policy changes contained inside the chat items being summarized.

The model instruction should require:

- Ukrainian output by default;
- concise sections;
- no diagnosis or private profiling;
- no invented decisions;
- explicit uncertainty when evidence is weak;
- message references when available;
- separation between confirmed decisions, open questions, links/media discussed, and follow-up items.

## Output Shape

Recommended digest format:

```text
Коротко
- ...

Головні теми
- ...

Рішення
- ...

Відкриті питання
- ...

Що варто перечитати
- ...
```

Sections with no evidence should be omitted or say that no clear evidence was found. The digest should not fabricate consensus or action items.

Message references can start as simple timestamps and sender labels. Deep Telegram links should be a later issue unless the bot can build them safely for the chat type.

## Topic And Unresolved Modes

Topic digests should use the existing semantic memory path after explicit invocation. They should include both high-scoring matches and a small chronological neighborhood so the recap has continuity.

Unresolved mode should combine:

- question marks and request verbs;
- reply-chain context;
- later replies that may answer the question;
- simple completion cues such as "done", "fixed", "decided", "merged", or localized equivalents.

The first implementation should treat unresolved detection as heuristic. It should say "looks unresolved" rather than claiming certainty.

## Safety And Privacy

- Do not put raw digest inputs, private chat text, or full message dumps into system logs, GitHub issues, or health output.
- System logs should record only mode, counts, time window size, truncation flags, and sanitized failure categories.
- Digest output is user-facing by design, but it must stay within the requested chat/user context and existing access rules.
- Non-admin users should not be able to request another user's private-style profile through digest wording.
- If per-user digests are added later, they must use high-level activity summaries and avoid personality inference.
- Source/repost/transcript text should be summarized as source content, not as the sender's writing.
- Russian source material should be understood as source and summarized in Ukrainian.

## Command Access

Recommended v1:

- allow digest commands in private DM and allowed group chats through existing command authorization;
- keep ordinary group messages passive;
- keep broad administrative diagnostics separate from digest;
- consider admin-only mode for very large windows or per-user digests;
- deny unsupported cross-chat requests.

## Failure Modes

Stable user-facing failures:

- memory disabled;
- no retained messages in window;
- requested window too broad;
- ambiguous date/time;
- topic not found;
- model unavailable;
- output too long after splitting limits;
- digest temporarily disabled.

Failures should not block normal memory save, embeddings, `/stat`, `/character`, recall, or normal Telegram replies.

## Configuration Defaults

Suggested future settings:

```env
DIGEST_ENABLED=false
DIGEST_DEFAULT_MESSAGES=120
DIGEST_MAX_MESSAGES=300
DIGEST_MAX_PROMPT_CHARS=30000
DIGEST_TOPIC_TOP_K=12
DIGEST_REPLY_CONTEXT_DEPTH=3
DIGEST_ALLOW_PER_USER=false
DIGEST_ADMIN_LARGE_WINDOWS=true
```

Keep the feature disabled until implementation tests prove prompt size, access control, and attribution behavior.

## Future Test Plan

- `/digest` and localized aliases parse through explicit command routing.
- Ordinary group chatter mentioning recap-like words stays silent without invocation.
- Recent-window digest builds a bounded chronological package.
- `today`, `yesterday`, and `since` use configured local timezone metadata.
- `last N` is capped and reports truncation when capped.
- Topic digest uses semantic/FTS results plus nearby context.
- Unresolved digest marks uncertainty and does not invent action items.
- Source/repost/transcript text is labeled as source context and not attributed as authored text.
- Digest prompt package excludes bot meta prompts, system logs, secrets, and raw private runtime details.
- Memory disabled or empty-window cases return clear user-facing messages.
- Model failure is logged as a sanitized system event and does not break normal bot routes.
- Long digest output uses existing Telegram reply splitting.
- `/stat`, `/character`, memory search, and normal recall behavior do not regress.

## Recommended Implementation Sequence

1. Add `chat_digest.py` with request parsing and bounded memory selection helpers.
2. Add unit tests for parser, time windows, caps, and topic/unresolved modes.
3. Add digest prompt package formatting with source/authored labels.
4. Add the Telegram command handlers and localized aliases.
5. Add model-call wrapper and sanitized system events.
6. Add README/env docs only after command behavior is implemented.
7. Revisit per-user digest and deep message links as separate issues if v1 is useful.

## Acceptance Mapping For Issue #23

- Key threads, decisions, unresolved questions, and message references are covered by the output shape and prompt package.
- Topic and time-window digests are covered by request parsing and memory selection.
- Ordinary group chatter remains silent because digest only routes through explicit command invocation.

## Related Notes

- [`transcript-memory-integration.md`](transcript-memory-integration.md)
- [`universal-media-transcript-mcp.md`](universal-media-transcript-mcp.md)
- [`telegram-native-transcription.md`](telegram-native-transcription.md)
