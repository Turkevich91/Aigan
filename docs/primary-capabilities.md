# Primary-agent capabilities

The primary model can inspect original retained conversation evidence and propose a public-web image delivery when initial classification misses the user's request. `PRIMARY_CAPABILITY_RECOVERY_ENABLED` defaults to `false`. Disabling it restores the previous routing and agent-tool set; it does not change existing media settings or model assignments.

## Ownership and scope

`chat_history.py` owns request-local retrieval budgets over `MemoryStore.bounded_history_rows`. `image_capability.py` owns bounded image proposal validation and one execution claim. `agent_capabilities.py` exposes thin SDK function adapters. Telegram admission, actual reply/provenance resolution, and dispatch remain in `main.py`. The existing image pipeline owns searching, image review, sending, ambiguous outcomes and persistence.

The model can request a capability. It cannot choose the chat, execution identity, recipient, database, arbitrary tool name or private-media source. Existing invocation admission and feature settings remain application-owned. Classifier labels can be reconsidered by the primary model; they are not an additional permission source.

| Actor/path | History | Image recovery | Enforcement |
| --- | --- | --- | --- |
| Admitted member of an allowed group | Current group's retained messages | New public-web delivery in current group | Invocation gate, fixed chat/cutoff, typed proposal, host delivery |
| Admitted private-chat participant | Current DM only | Current DM only | Existing allowlist and fixed chat |
| Another participant replying to a public album | Same group evidence | May continue the verified public request | Actual reply and successful delivery provenance; prior author need not match |
| Ordinary uninvoked group chatter | No agent tool call | No delivery | Passive ingress path |
| Denied chat or bot-authored invocation | No capability context | No capability context | Existing admission plus context construction guard |
| Model-supplied foreign anchor or participant override | No foreign-chat rows; participant only narrows current chat | No recipient parameter | SQL scope and adapter validation |

```mermaid
sequenceDiagram
    participant Member
    participant App
    participant Primary
    participant History
    participant Delivery
    Member->>App: Admitted request or reply to bot
    App->>App: Check allowed chat and invocation
    App->>Primary: Request, context and bounded capability catalog
    opt Insufficient memory evidence
        Primary->>History: Read current-chat window or lexical search
        History-->>Primary: Capped untrusted records and coverage
    end
    opt Public-image request missed by classifier
        Primary->>App: Typed delivery proposal
        App->>App: Validate current spans, scope and verified antecedent
        App->>App: Claim exactly one delivery
        App->>Delivery: Existing media pipeline
        Delivery-->>Member: Confirmed media or truthful failure result
    end
```

```mermaid
sequenceDiagram
    participant Chatter
    participant App
    participant History
    Chatter->>App: Ordinary group text or denied chat
    App->>App: Preserve passive/denied path
    Note over App,History: No primary capability context or history disclosure
```

## History contract

`read_chat_history` offers `recent`, lexical `search`, and `around` a memory evidence id. Optional participant and ISO date/time filters only narrow the current chat. Search works without embeddings and preserves bot answers, authored invocations and separately labeled quoted/forwarded source material. A fixed target mode is available for separately authorized authored-only analysis; it excludes source material and other authors.

Hard ceilings are 20 messages per call, 1,000 serialized characters per row, 12,000 characters per response, four reads and 30,000 returned characters per model run. SQL reads bounded pages and projects only bounded safe fields. Concurrent tool calls reserve their budget before awaiting retrieval. The persisted current-request row and future messages are excluded. Results expose truncation and selected date coverage; no match does not prove absence from the full conversation. Date-only filters use UTC; precise local-day questions should supply timezone-bearing timestamps.

The current store has no forum-topic field. Retrieval is chat-scoped, not a claim of topic-scoped storage. No media bytes, paths, file tokens, raw notes or system logs are returned.

## Image continuation contract

An admitted primary run receives the typed image request tool when public-image search is enabled. Soft classifier clarification/unavailable responses can reach that run. Supported direct image routes retain their existing fast path; referenced visual analysis retains its existing pipeline.

For a contextual continuation, the application resolves the actual same-chat replied message through successful public-image delivery provenance to authored source requests. It follows at most four ordered delivery/request links and exposes at most 2,500 source-request characters. The model selects unchanged subject words from that evidence and a replacement modifier from the current request. For the synthetic sequence `red flowers` then `yellow ones`, the query uses `flowers yellow`. Unspecified plural inherits the latest confirmed album count; explicit counts and the existing five-image delivery ceiling remain.

History provides grounding, not fresh authority to act. Quoted, negated and unsupported private/external operations remain excluded. The model's semantic selection is checked against literal current and antecedent spans; no cheaper classifier is asked to veto it again. Proposals are bounded to three attempts, with one accepted plan and one locked execution claim. A rejected proposal returns to the model for clarification. An accepted proposal ends that SDK run before generic final prose, then the host dispatches the existing media pipeline once.

A failed or ambiguous delivery does not reopen the claim. A post-claim exception reports uncertainty without inviting a duplicate retry. Tool history, usage and hooks stay in one SDK run; no recursive model restart or prompt-only replay is used.

## Verification and limits

Automated tests cover source scope, exact row/text/call bounds, concurrent reservation, Unicode search without embeddings, multi-participant and multi-hop album replies, real SDK tool-output continuity, rejected proposal continuation, once-only host dispatch and ambiguous delivery. Provider-backed synthetic probes and their measured outcomes are recorded separately when completed. These checks do not mark the operator's manual Telegram field quests complete.

No model role, embedding dimension, active tier routing, database schema or live index is changed by this feature. Character analysis and Telegram burst assembly are separately tracked follow-ups.
