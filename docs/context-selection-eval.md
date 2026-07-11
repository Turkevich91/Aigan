# Context-selection evaluation for issue #119

## Status

This bundle starts the B0/B1/C1 research comparison. It is offline and
aggregate-only. It does not change Telegram routing, the delivered prompt,
SQLite schema, retrieval, model configuration, or runtime behavior.

The first committed artifacts are a strict evaluator contract, a
public-synthetic regression corpus, and a guarded local review-pool builder.
The synthetic corpus proves that the evaluator is deterministic and
privacy-safe; it is not evidence that any arm improves Aigan. The review pool
is private candidate intake, not a labeled or frozen efficacy corpus. The
efficacy decision requires a separately frozen private replay corpus from real
retained interactions.

Every report produced by this bundle has:

- `answer_evaluation_complete=false`;
- `timing_comparable=false`;
- `cost_evaluation_complete=false`;
- `research_decision=INCONCLUSIVE`;
- `runtime_authorized=false`.

No source-selection result can silently authorize implementation or deploy.

## Research question

Does a narrow persistent event anchor materially improve context selection over
both the deployed compiler and a strong query-time stateless compiler?

The comparison is deliberately asymmetric: C1 must justify new durable state,
construction calls, stale-anchor risk, and operational complexity. Matching B1
is not enough.

## Frozen arms

### B0 — deployed snapshot

B0 is not a new implementation in this module. A replay packet materializes the
source IDs selected by the exact deployed source/config snapshot, along with its
context characters, compile duration, and recommended action. The evaluator
then scores that immutable result.

This avoids a fake baseline that only approximates production after the code or
environment changes. A private replay builder must freeze candidate rows,
timestamps, reply/provenance edges, query embedding and retrieval scores,
effective config hash, source commit, and B0 output before evaluation.

The audited B0 behavior includes:

- structured current payload and reply/reference blocks;
- recent memory;
- short-follow-up expansion and reply-chain;
- embedding, keyword, and FTS retrieval;
- current deduplication and per-block character budgets.

Current measurement targets include raw cross-retriever score fusion,
speaker-insensitive payload dedupe, a short-follow-up query that omits the
structured reply, independent block budgets, and tail-first packing of a
best-first recall result. They are hypotheses to measure, not assumed causes.

### B1 — simple stateless compiler

B1 creates no event, episode, graph, fact, summary, or correction state. For
each request it:

1. prioritizes current payload and structured reply;
2. follows explicit reply-chain evidence;
3. combines semantic, lexical, FTS, and recency ranks with deterministic
   reciprocal-rank fusion;
4. deduplicates by source identity, speaker, source kind, payload, and reply
   target;
5. packs best-first into one global source budget and records drop reasons.

Only provenance-linked raw rows are selected. B1 is the minimum credible
alternative to persistent state, not a deliberately weak control.

### C1 — event anchor to raw evidence

C1 adds one capability to B1: it can rank append-only event summaries and use a
selected event to open a bounded set of immutable source rows. The event summary
is navigation metadata, not answer evidence. Source metrics score only the raw
rows.

An event packet is valid only when:

- it was constructed from history available before the target request;
- it was constructed without expected labels or the target query;
- its anchor and every linked row exist in the frozen candidate snapshot;
- construction calls, tokens, and latency are amortized into C1 cost;
- no prior source is overwritten or hidden by the summary.

No graph, autonomous reflection, broad active-topic state machine, or automatic
correction truth is part of C1.

The event summary is retrieval/navigation metadata and is not delivered to the
answer model. Its size is recorded separately as `navigation_chars`; it is not
subtracted from the raw-evidence prompt budget.

## Why B1 is required by the evidence

[LongMemEval (ICLR 2025)](https://proceedings.iclr.cc/paper_files/paper/2025/hash/d813d324dbf0598bbdc9c8e79740ed01-Abstract-Conference.html)
separates indexing, retrieval, and reading. Fact-augmented keys improved both
retrieval and QA, time-aware query expansion improved temporal recall, and
reader formatting changed accuracy by up to ten points even with oracle
retrieval. A persistent architecture cannot receive credit for gains caused by
a better query, ranking rule, or reader format.

[LoCoMo (ACL 2024)](https://aclanthology.org/2024.acl-long.747/) and
[Lost in the Middle (TACL 2024)](https://aclanthology.org/2024.tacl-1.9/)
show that more context is not a reliable fix. Retrieval can help, but distractor
volume, evidence position, and summary information loss remain important.

[HiGMem (Findings ACL 2026)](https://aclanthology.org/2026.findings-acl.1690/)
and [TiMem (Findings ACL 2026)](https://aclanthology.org/2026.findings-acl.1091/)
support selective temporal/event navigation followed by lower-level evidence.
They do not establish a benefit in asynchronous multi-party group chat.

[APEX-MEM (ACL 2026)](https://aclanthology.org/2026.acl-long.749/) is an
important counterweight. Its matched GPT-4o result improved temporal questions
but did not beat the full-context comparator on the non-adversarial aggregate;
construction and agent-loop costs were also substantial. The plausible outcome
is therefore route-specific benefit, not necessarily global benefit.

## Corpus separation

### Public-synthetic contract corpus

`tests/fixtures/context_selection_contract_v1.jsonl` contains 60 generated
cases, ten for each primary class:

- explicit reply;
- short follow-up;
- same-question transformation;
- topic shift with strong distractors;
- knowledge update;
- correction and stale-hypothesis guardrail.

It contains synthetic identities, topics, timestamps, text, retrieval ranks,
and event anchors. It is intentionally capable of exercising known failure
shapes. For that reason it must never be reported as an efficacy result.

Two variants share a case-family identifier so the bootstrap code is tested
against clustered perturbations rather than pretending every generated variant
is independent. These families are test scaffolding, not empirical clusters.
The first corpus exercises `expected_action=answer`; clarify/abstain, missing
event, and event-construction-failure branches remain required before the
contract can support the answer-stage study.

### Private replay corpus

The actual decision uses local ignored artifacts. Raw requests, source text,
identities, Telegram identifiers, URLs, media, and database locations never
enter Git, GitHub, reports, stdout, or exception messages.

Development and holdout are separate:

- development may be inspected and used to tune B1/C1;
- holdout labels remain sealed from selector code;
- holdout has at least 60 eligible cases and at least ten in each primary
  class;
- cases are not appended after looking at a holdout result;
- repeated model calls measure stability and are not independent samples.

The generic evaluator cannot score a private holdout. It validates the fixture
contract and returns `HOLDOUT_NOT_AUTHORIZED` without running B0, B1, or C1 and
without returning arm or comparison outcomes. A future holdout runner needs a
separate one-time authorization manifest; no fixture field can self-authorize a
run.

The first private replay tooling must fail closed if an output target is not
inside an ignored private directory or cannot be made owner-only.

The review-pool builder uses exact input-memory linkage where outbound
provenance exists. Older route events may be matched to the nearest preceding
non-bot row from the same retained user within a bounded interval; these
packets remain explicitly marked
`approximate_route_event` and can be rejected during human review. It includes
only history available before the target, plus a bounded reply-chain, and
replaces message, speaker, and packet identities with keyed opaque values.

Every exact provenance run receives a distinct opaque run and packet key while
retries of the same target share one opaque case key. This preserves run-level
B0 evidence without pretending retries are independent cases. A deterministic
one-run-per-case rule must be frozen before efficacy analysis; the remaining
runs are stability evidence only.

The current opaque cluster key sessionizes retained messages by a 30-minute
inactivity gap, so a wall-clock boundary does not split an active exchange. It
is safe for grouping review intake but is not yet a validated independence
model: development data must estimate dependence and choose a session or block
bootstrap rule, and that rule plus the required independent-cluster count must
be frozen before holdout analysis.

When neither modern provenance nor a retained route event exists, a retained
outbound reply edge is preferred and marked `outbound_reply_link`. A separate
lower-confidence intake may correlate a historical bot output with a nearby
private-chat, explicit-invocation, or reply-to-bot row. These packets are marked
`approximate_bot_output`, have a frozen gap, and cannot enter an eval split
without human confirmation. This recovers review candidates; it does not turn a
timing guess into ground truth.

The local packet deliberately retains raw text for human labeling, so the text
may itself contain identifying material or URLs. It omits dedicated structured
fields for raw Telegram/chat/user/message IDs, usernames, source URLs, local
paths, file IDs, and run IDs. Its output directory is outside the repository or
in the already ignored runtime-data tree, and is owner-only; the JSONL,
manifest, and HMAC key are owner-only files. Existing outputs are never
overwritten implicitly. The CLI uses an owner data directory automatically when
the runtime-data tree is not writable by the operator account.

## Fixture contract

Each JSONL record has four distinct sections:

- request metadata and a fixed global budget;
- B0's already-materialized deployed result;
- candidate raw sources and optional event anchors with frozen retrieval ranks;
- human labels used only by the evaluator after selection.

Selectors receive `SelectionInput`; that type has no expected labels. The
loader rejects unknown fields, unknown provenance links, duplicate IDs, future
sources/events, malformed ranks, and overlapping relevant/forbidden labels.
Validation failures identify only the line and contract class, never payload
content.

## Metrics and uncertainty

The current source-selection stage records raw counts for:

- correct top-1 anchor;
- source precision and required-evidence recall;
- mean reciprocal anchor rank;
- forbidden, stale, and wrong-speaker selections;
- preliminary compiler-action accuracy (final answer abstention is evaluated
  later by the blinded reader/answer stage);
- selected-source count, context characters, compile time, explicit drop
  reasons, and amortized C1 construction use.

Top-1 comparisons are paired. The primary interval is a deterministic,
equal-class estimand with a stratified cluster bootstrap over replay
window/case family. A family-level exact sign test is secondary evidence. Raw
counts are retained alongside rates; the bundle does not report an
independence-assuming per-message interval.

The minimum useful effect remains ten percentage points for C1 over both B0 and
B1. This bundle records comparisons but does not implement that decision gate.
It emits only `CONTRACT_ONLY` for synthetic data, `DEVELOPMENT_ONLY` for private
development data, or `HOLDOUT_NOT_AUTHORIZED` for a private holdout. Before any
future decision, the preregistration must freeze class-level safety/quality
checks and conditional temporal/update intervals in addition to both global
ten-point comparisons.

The current compile durations are not comparable: B0 is a frozen live duration,
while B1/C1 are local selector loops. Declared event construction values can be
exercised by the contract but are not deduplicated across cases and have no
frozen USD price snapshot. They appear under
`declared_event_construction_not_deduplicated` with
`decision_usable=false`. A final economic comparison requires unique
construction keys, actual prompt/output token ledgers, a dated price snapshot,
and end-to-end latency measured in the same environment.

A final `GO`, `NO-GO`, or `INCONCLUSIVE` still requires the separate one-time
holdout runner, blinded answer quality, model stability, tokens, cost, model
latency, and the full issue #119 gates.

## Privacy properties

The aggregate report contains no case ID, family ID, source/event ID, query,
message text, model output, path, or per-case prediction. It contains only
corpus kind, split, artifact/config hashes, aggregate counts, distributions,
intervals, and non-authorizing status.

Public reports must still be reviewed before GitHub publication. A private
fixture hash is an audit handle, not permission to publish any input artifact.

## Contract commands

Regenerate the public-synthetic fixture and manifest:

```bash
python3 -m scripts.build_context_selection_fixture
```

Run the aggregate-only contract evaluation:

```bash
python3 scripts/eval_context_selection.py \
  --fixture tests/fixtures/context_selection_contract_v1.jsonl
```

Run focused tests:

```bash
python3 -m unittest tests.test_context_selection_eval
```

Build the local-only human-review pool on the configured runtime host:

```bash
python3 scripts/build_context_selection_replay.py
```

The command prints an aggregate manifest only. It never prints packet content
or the private output location. A second invocation fails instead of replacing
the review pool.

The next research slice is human development labeling, deterministic
development/holdout freezing, and retrieval/B0 snapshot enrichment. An API
answer runner and one-time holdout gate are not yet present and must not be
inferred from this bundle.
