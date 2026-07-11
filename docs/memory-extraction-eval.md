# Bounded memory extraction evaluation

## Decision

Issue #143 starts with an offline extraction contract and frozen evaluation, not
with a runtime worker. This slice does not add a queue, migrate SQLite, call a
model during normal bot operation, alter retrieval, or change the context sent
to GPT-5.6 Sol.

The next runtime-shadow pull request is allowed only after a model candidate
passes the extraction gates below. Even then, the worker may create private
source-linked candidates only; it cannot promote trusted facts or change a
user-visible answer without a later reviewed gate.

## Ownership boundary

- `memory.py` remains the source of truth for retained messages, FTS, embeddings,
  reply links, and existing run/tool provenance.
- `memory_extraction.py` owns the versioned experimental output schema,
  fail-closed provenance checks, deterministic baseline, and aggregate scoring.
- `scripts/eval_memory_extraction.py` is an explicit offline/API evaluator. It
  never runs from the Telegram lifecycle.
- `main.py` and the live SQLite schema are intentionally unchanged by this
  slice.

The current prompt-memory payload hash deliberately deduplicates repeated text.
That behavior must not be reused as candidate identity: the frozen suite
contains identical text from different chat/speaker scopes and requires both
identities to remain distinct.

## Frozen fixture

`tests/fixtures/memory_extraction_v1.jsonl` contains two separate, text-disjoint
blocks generated from the same taxonomy and templates: 120 public-synthetic
development/calibration cases and 120 frozen holdout cases with different
wording and values.
It has no copied chat text, Telegram IDs, usernames, URLs, paths, deployment
aliases, secrets, or runtime artifacts.

Frozen SHA-256 values:

```text
fixture file  4737869e8b36a5ae16b38c98b91adbc9774c580933f932f5d75116d612666d44
development   bbd9ebcf31a68b41d9bf09a679411b5378752d23ee6787fecd95f8087d6b2c02
holdout       1d4bc0ddde8f5d6a6df41266e1448de2d366ab9d6daa903a4314fc62799fc687
prompt v4     8f5b599f904d9017f2ead6bb1d084cdcff767f0768992bcb9c374e5f700dc4e2
schema v2     192ba5c4e1b5a08ae64b5c2ac9636f30f63863ae98d39825ef6919c105d37cfe
eval bundle   e08dc80589f440b95020d1b293a4c8d22be46793eb1f3329b88a8a9c35d8413c
```

Each 120-case block has the following distribution:

| Class | Count |
|---|---:|
| fact claim | 14 |
| preference | 10 |
| decision | 10 |
| relationship | 10 |
| correction/conflict | 14 |
| uncertainty | 10 |
| validity/expiry | 10 |
| opinion (no durable candidate) | 8 |
| joke/sarcasm | 8 |
| question/hypothetical | 6 |
| transient acknowledgement | 6 |
| forwarded quote without endorsement | 6 |
| prior-bot echo | 4 |
| cross-scope bait | 4 |

Each block has 40 Ukrainian, 30 Russian, 30 English, and 20
mixed/transliterated cases. Each also includes 12 identical-text identity
pairs, six cross-speaker conflicts, eight same-speaker corrections, three
synthetic raw tool-evidence anchors, and four cross-chat distractors.

Prompt/schema iteration and capability screening use only `development`. Once
the contract is frozen, the full gate uses `holdout`; a post-holdout prompt or
policy change requires a new holdout version rather than retuning and reusing
the same evidence. The checked-in holdout is frozen and process-blinded, not an
external secret benchmark: it can support an engineering decision to build an
off-by-default shadow worker, not a claim of generalization to private chat.
External or privately adjudicated evidence is required before extracted memory
can influence retrieval, promotion, or user-visible answers.

The checked-in builder derives expected labels from scenario semantics, not
from the deterministic rule baseline. This keeps the baseline independent of
the ground truth and prevents a parser gap from silently becoming a negative
label. Regeneration is deterministic and must produce the same bytes and hash:

```bash
python3 -m scripts.build_memory_extraction_fixture
```

Any label, taxonomy, prompt, policy, or fixture change requires a new version
and hash. Do not tune against v1 and then describe it as a fresh holdout.

## Candidate contract

The strict model output can contain at most four candidates. Every candidate
must preserve:

- one allowlisted candidate type;
- one or two existing evidence row/field references;
- the complete cited field copied case-exactly into the evidence span;
- epistemic and durability state;
- same-speaker supersession or cross-speaker conflict links;
- explicit validity bounds when present;
- confidence from 0.5 through 1.0 and exactly one type-matched reason code.

The validator, not the model, derives subject, source role, and candidate-only
lifecycle from the cited row. Chat scope and observation time remain
recoverable through mandatory source references and are validated for local
scope; a future runtime adapter must derive them from those rows rather than
accepting model-owned identity, authority, or freshness fields.

Application validation rejects unknown fields, missing/non-local evidence,
cross-chat references, speaker/source-role changes, non-finite confidence,
short or case-changed evidence spans, invalid/expired validity, wrong reason
codes, tool facts without a raw synthetic tool anchor, any forwarded/prior-bot
candidate, uncertain promotion, non-prior links, overlapping links, and
same-speaker conflict or cross-speaker supersession.

Neither the model nor the deterministic baseline can create active or trusted
memory. A future authority/promotion policy is a separate decision.

## Gates

A full 120-case frozen-holdout run requires all of the following:

- exact fixture, split, prompt-v4, schema-v2, and evaluator/scorer bundle hashes;
- exactly three calls per case with at least 99% candidate-behavior stability;
- 120/120 quality outputs and 360/360 repeat outputs structurally valid;
- overall and durable precision at least 95%, with Wilson 95% lower bounds at
  least 95%;
- durable recall at least 80%, with its Wilson lower bound at least 80%;
- recall at least 60% for every positive type, including each Wilson lower
  bound;
- zero candidates on every expected-negative case;
- zero cross-scope, missing-source, attribution, tool-anchor, uncertainty,
  forwarded-source, prior-bot, or lifecycle violations;
- zero provider failures, missing actual model metadata, missing actual effort
  when reasoning was requested, or requested/actual mismatches;
- complete price status, basis, snapshot, and token rates;
- aggregate-only output.

Quality intervals use the 120 unique cases once. The two additional repeats
measure stability only and are never pooled as 360 independent observations.
The 99% gate covers candidate behavior, including evidence span and reason
code. The report separately exposes stricter full-contract stability, including
self-reported confidence and the diagnostic rejection reason; those fields do
not define candidate identity or authorize promotion.

A full development run can demonstrate that the point gates pass, but it always
returns `INCONCLUSIVE`. Only the frozen `holdout` split can emit
`GO_FOR_RUNTIME_SHADOW_PR`.

A no-op extractor therefore fails recall instead of receiving a misleading
perfect-precision pass.

`GO_FOR_RUNTIME_SHADOW_PR` means only that a separate, off-by-default
extraction-shadow implementation may be proposed. The report always emits
`runtime_authorized=false`; it cannot authorize deployment, environment
changes, retrieval packing, or active memory.

## Model screening

The final prompt-v4 development matrix intentionally has no hard-coded winner:

| Role | Model / effort |
|---|---|
| deterministic floor | no model |
| cost challenger | `gpt-5.4-nano-2026-03-17`, `low` |
| quality challenger | `gpt-5.4-mini-2026-03-17`, `low` |
| new-family ceiling | `gpt-5.6-luna`, `low` |

OpenAI documents GPT-5.4 nano for classification, extraction, ranking, and
sub-agents. Lower-cost families were considered during exploratory development,
but this artifact makes no prompt-v4 aggregate claim about them and does not
add them to the shared runtime price registry. Luna remains a quality ceiling
rather than the presumed worker.

Prompt-v4 development results use 120 unique cases and three calls per case:

| Model | Durable P / Wilson lower | Durable R / Wilson lower | Negative FP | All-repeat valid | Measured cost | Decision |
|---|---:|---:|---:|---:|---:|---|
| GPT-5.4 nano low | 96.55% / 90.35% | 97.67% / 91.91% | 1 | 97.78% | $0.140955 | reject |
| GPT-5.4 mini low | 96.47% / 90.13% | 95.35% / 88.64% | 0 | 99.72% | $0.502448 | reject |
| GPT-5.6 Luna low | 100% / 95.72% | 100% / 95.72% | 0 | 100% | $0.339432 | holdout candidate |

Luna's candidate-behavior stability was 100%; stricter full-contract stability
was 74.17% because confidence and diagnostic rejection reasons varied. Its run
used 342,427 cached input tokens, so measured run costs are reported rather
than treated as an equal-cache price comparison. Luna is selected because it
is the only candidate that passed every development quality/safety gate, not
because it was the cheapest call.

## Frozen holdout result

The one locked prompt-v4/schema-v2/bundle-`e08dc805` holdout run used Luna low,
three repeats, concurrency 6, a 45-second per-call timeout, 1,200 maximum output
tokens, and `store=false`. It returned **`NO_GO`** with
`runtime_authorized=false`.

| Measure | Holdout result |
|---|---:|
| unique candidate-set exact match | 120/120 (100%) |
| overall precision / recall | 100% / 100% |
| overall precision Wilson lower | 96.15% |
| durable precision / recall | 100% / 100% |
| durable precision/recall Wilson lower | 95.72% / 95.72% |
| expected-negative false positives | 0 |
| candidate-behavior stability | 119/120 (99.17%) |
| all-repeat structured validity | 358/360 (99.44%) |
| full-contract stability | 90/120 (75%) |
| provider/model/effort/usage/pricing failures | 0 |
| repeat validation errors | `schema_missing_reason`: 2 |
| measured cost | $0.645807 |
| latency p95 including queue time | 76.005 seconds |

The only failed gate was the pre-registered requirement for 360/360 valid
repeat outputs. The first quality observation remained perfect, but two later
repeats violated the conditional `candidates`/`no_candidate_reason` invariant.
The holdout is not rerun or used for prompt tuning. Any future contract change
requires a new development cycle and a new text-disjoint holdout version. This
result does not permit a runtime worker, candidate-table migration, live shadow,
or retrieval change.

Official model references:

- https://developers.openai.com/api/docs/models/gpt-5.4-nano
- https://developers.openai.com/api/docs/models/gpt-5.4-mini
- https://developers.openai.com/api/docs/models/gpt-5.6-luna

The evaluator price snapshot uses standard processing. Batch API can reduce
offline extraction evaluation cost, but its completion window is unsuitable
for query-time reranking:

- https://developers.openai.com/api/docs/guides/batch
- https://developers.openai.com/api/docs/guides/structured-outputs

## Commands

Regenerate the stdlib-only fixture builder output:

```bash
python3 -m scripts.build_memory_extraction_fixture
```

Build a disposable evaluator image from the candidate branch. Do not reuse or
retag the live Compose image:

```bash
docker build --tag aigan-memory-eval:local .
```

Run the deterministic floor without network. It always reports
`INCONCLUSIVE`; it can verify the evaluator but cannot authorize a model:

```bash
docker run --rm --network none --entrypoint python \
  aigan-memory-eval:local -m scripts.eval_memory_extraction \
  --mode baseline \
  --split holdout \
  --repeats 3
```

For API runs, expose only `OPENAI_API_KEY` in the operator shell; do not pass
the deployment `.env` or unrelated bot secrets into the evaluator container.

Stratified 20-case capability screen:

```bash
docker run --rm --env OPENAI_API_KEY --entrypoint python \
  aigan-memory-eval:local -m scripts.eval_memory_extraction \
  --mode api \
  --split development \
  --model gpt-5.4-nano-2026-03-17 \
  --reasoning-effort low \
  --limit 20
```

Full repeated API evaluation:

```bash
docker run --rm --env OPENAI_API_KEY --entrypoint python \
  aigan-memory-eval:local -m scripts.eval_memory_extraction \
  --mode api \
  --split development \
  --model gpt-5.6-luna \
  --reasoning-effort low \
  --repeats 3 \
  --concurrency 6 \
  --timeout-seconds 45 \
  --max-output-tokens 1200
```

For audit only, the one locked frozen-holdout command that was run was the
following. Do not rerun holdout v1:

```bash
docker run --rm --env OPENAI_API_KEY --entrypoint python \
  aigan-memory-eval:local -m scripts.eval_memory_extraction \
  --mode api \
  --split holdout \
  --model gpt-5.6-luna \
  --reasoning-effort low \
  --repeats 3 \
  --concurrency 6 \
  --timeout-seconds 45 \
  --max-output-tokens 1200 \
  --require-gates
```

The evaluator sends `store=false`, keeps predictions only in process memory,
and writes no local payload. Its JSON output is an allowlisted aggregate with
fixture/prompt/schema/evaluation-bundle hashes, unique cases versus evaluation instances,
TP/FP/FN, Wilson intervals, negative-case accuracy, per-type metrics, repeat
stability, requested and actual provider metadata, request limits, failures,
latency, token usage, price snapshot/status/rates, gates, and verdict. It
contains no case IDs, text, evidence spans, row/chat/speaker aliases, prompt,
or model output.

## Known blockers after this slice

- Issue #119 specifies a frozen B0/B1/C1 context suite, but that fixture and
  evaluator do not yet exist. Retrieval decomposition/reranking/packing cannot
  receive a credible GO until they do.
- Current tool provenance stores bounded digests, not a raw evidence row.
  Runtime v1 must reject `verified_tool` candidates until a private
  source-evidence boundary is implemented and reviewed.
- A runtime extraction queue, additive candidate tables, telemetry buckets,
  backup/restore check, and seven-day shadow are a separate pull request after
  the model gate.
- The frozen synthetic holdout measures this contract, not natural-chat
  generalization. Private adjudication or a new external blind set is required
  before candidate data can affect retrieval or fact promotion.
- Existing vector/FTS retrieval and Sol-low answer behavior remain the mandatory
  fallback throughout the experiment.
