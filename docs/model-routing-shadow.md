# Model responsibility routing: shadow contract

## Decision

Aigan may classify explicit text requests into model-responsibility tiers in
background shadow mode. Shadow decisions never change the answer model,
reasoning effort, tools, memory retrieval, or Telegram response. The existing
Agents SDK `RunConfig.model` remains the unconditional primary-model seam.

An active tier canary is not part of this change. It requires a separate
reviewed provider/settings seam and a new `GO` decision from frozen answer
quality, end-to-end cost, and latency evidence.

## Tiers

- `economy`: candidate for narrow, static, exact utility work.
- `balanced`: candidate for bounded source/tool synthesis after evaluation.
- `premium`: the configured primary answer model and unconditional fallback.

Concrete model IDs live only in deployment configuration. The premium alias
must equal `OPENAI_MODEL` in `off` and `shadow` modes.

## Deterministic safety floor

The classifier proposes a tier. Application policy may only raise it.
Premium is mandatory for memory recall, time-sensitive work, high-risk task
classification, mutation intent even when tools are unavailable, risk above
low, ambiguity above low, high complexity, live freshness, creative/persona
work, and short unanchored follow-ups. Source-linked
work is at least balanced. Low confidence, invalid output, timeout, or provider
failure falls back to premium.

Only a normal-route, low-risk, low-ambiguity, low-complexity,
freshness-independent `simple_utility` decision with no URL, attachment,
reference, mutation request, or short follow-up is marked as eligible for a
future canary. That marker is measurement only.

The opaque assignment handle is stable only for one Telegram forum thread.
Every non-thread message, including a reply, is deliberately marked
`single_turn`; this implementation does not pretend that it has inferred a
durable social episode. Any future follow-up canary needs a separately persisted
episode anchor before assignment can be called sticky across changing replies.

## Privacy and observability

The provider receives the trusted current request and bounded flags, not the
assembled Agents prompt or retained memory. URLs are replaced by a `[url]`
marker before that provider call. The SQLite decision ledger contains
only allowlisted enums, configured model aliases, confidence, an opaque keyed
episode handle, and terminal outcome metadata. It contains no prompt, response,
URL, Telegram identity, message ID, tool argument, exception text, or path.

The classifier call has its own `model_policy_router` telemetry stage. The
decision row stores policy, schema, and prompt versions alongside the
recommended/effective tier and applied tier. In shadow, applied is always
premium; provider-confirmed actual answer-model data continues to come from the
existing final model stages joined by opaque run ID.

## Frozen evaluation v1

The sanitized fixture is
`tests/fixtures/model_routing_v1.jsonl`: 120 cases, 20 for each task class. The
evaluation runner emits aggregate metrics only and does not persist model
outputs.

Fixture SHA-256:

```text
9c38f0b033889b67b4ba869b3224a094abc3b18155b4f3c6f9fb1262103ec8a5
```

Initial one-pass API result with `gpt-5.4-nano`, reasoning `none`:

- structured validity: 120/120;
- unsafe downgrades after deterministic policy: 0;
- task-class macro-F1: 0.814;
- low-confidence premium fallbacks at threshold 0.75: 59/120;
- router latency: p50 1.080 s, p95 1.846 s;
- estimated router cost for 120 decisions: USD 0.025956;
- provider model mismatches: 0; effort mismatches: 0 when effort was reported.

A later three-repeat measurement of the same already-observed fixture was used
only to estimate stability, not as a fresh holdout:

- structured validity: 360/360; unsafe downgrades: 0;
- task-class macro-F1: 0.798;
- identical task class across all three repeats: 95/120 cases (79.2%);
- low-confidence premium fallbacks: 164/360;
- exact-utility economy precision: 100%; recall: 83.3% (50/60);
- selected tiers: premium 300, economy 50, balanced 10;
- router latency: p50 1.088 s, p95 1.750 s, max 3.069 s;
- estimated router cost: USD 0.077897 for 360 decisions;
- provider model mismatches: 0; effort mismatches: 0.

Router classification verdict: **NO-GO for active routing**. The structured and
safety gates passed, but the pre-registered macro-F1 target of 0.90 did not.
The repeated measurement also missed the classification and stability gates,
and no candidate answer-quality or production-weighted net-cost gate has been
run. Shadow collection is permitted because it cannot alter answers; a canary
remains prohibited.

Do not tune against this frozen set and then reuse it as holdout evidence. Any
prompt, policy, threshold, or taxonomy change needs a new versioned holdout
block and reported uncertainty.

## Active-routing evidence still required

- real API smoke for every configured tier alias;
- repeated router evaluation on a new holdout;
- exact-utility correctness with no loss;
- blinded candidate-vs-Sol answer quality;
- production-weighted router + worker + retry cost reduction of at least 30%;
- p95 latency within the issue gate;
- seven-day canary with at least 30 eligible episodes per arm;
- zero privacy, silence, provenance, mutation, or actual-model mismatch.

Official model references used for the initial candidates:

- https://developers.openai.com/api/docs/models/gpt-5.4-nano
- https://developers.openai.com/api/docs/models/gpt-5.6-terra
- https://developers.openai.com/api/docs/models/gpt-5.6-sol
