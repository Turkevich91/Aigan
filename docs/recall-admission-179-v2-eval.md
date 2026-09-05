# Recall admission: independent v2 acceptance protocol

This is the second, bounded iteration of issue 179. The v1 lexical candidate
failed its opened holdout: TP 20/36 and FP 2/36, including two critical false
positives; its paired baseline had TP 5/36 and FP 4/36. Preserve that failed
result and the original v1 fixture, manifest, protocol and measured sources.
Both v1 blocks are now observed development evidence. Their success or failure
cannot establish v2 acceptance. Fusion belongs to issue 180.

## Closed fixture

The independent custodian authored a fresh synthetic holdout before inspecting
any v2 classifier prompt or candidate code. It contains 72 efficacy cases:
36 positive and 36 negative, grouped in 24 scenario families with Ukrainian,
Russian and English variants. Each language has 12 positives and 12 negatives.
Fifteen negatives are predesignated critical. Scenario topics, exact wordings
and family identities differ from both v1 blocks. Linguistic forms include
indirect and elliptical requests, tense/person variation, reported commitments,
quoted material, negation scope and current-source versus prior-chat references.
Broad semantic behavior classes necessarily recur. These are independent-agent
synthetic labels, not human gold or sampled production traffic.

The closed fixture SHA-256 is
`d0711ee79acdb95d89680b94939bef7493164ab074cae09f9136a24e3e90af13`.
Its payload and custody location remain outside Git and unavailable to the
coordinator and implementers until the final candidate claim is accepted.
The public manifest is `tests/fixtures/memory_recall_179_v2_manifest.json`.
The JSON uses the existing `recall-admission-fixture-v1` schema, with a new v2
fixture ID. Do not edit, replace or relabel cases after observing results.

There are also 12 separate invocation-boundary cases, four scenario families
in three languages. Six are ordinary uninvoked group messages: actual recall
stage, classifier, other provider and Telegram delivery calls must all be zero.
The remaining six test explicit invocation or reply-to-bot eligibility with
mocked providers and deliveries. Null call expectations are unscored, not zero.
Run the boundary block with each repetition and report it separately; it never
enters the 72-case efficacy denominator.

## Candidate and execution freeze

Development may use the two observed v1 blocks only. Record every attempted
classifier/policy version and its development results. The final holdout must
exercise the actual `main.py` recall admission adapter and actual invocation
seam, after development justifies integration into that isolated candidate.
A stand-alone prompt test cannot establish acceptance of the integrated path.
Freeze candidate and evaluator source maps, classifier prompt and schema,
model/settings, dependency versions, effective configuration, baseline artifacts,
cache entries, protocol and manifest before releasing the holdout. The unchanged
baseline is commit `a0d5028bab91ba64daff379375b1f52a46c9dd0b`, thresholds
0.62/0.48 and its original hints/archetypes. Keep embeddings at
`text-embedding-3-small`, 512 dimensions.

Run exactly three fixed candidate repetitions on all 72 cases. Pair every
candidate decision with the same frozen baseline decision for that case. The
baseline may reuse verified deterministic cached results; do not inflate its
sample size by counting repetitions as independent cases. Use fresh classifier
requests for each repetition, retaining the same frozen request semantics.
No retries or additional repetitions are allowed to repair a failed gate.
The model timeout is 8 seconds and the explicit fallback remains observable.
Uninvoked group messages must not gain a model call.

## Fixed gates and uncertainty

Every repetition must independently meet every efficacy/validity gate:

- TP at least 33/36 and net positive gain at least eight versus paired baseline.
- FP at most 1/36; critical false positives and invocation violations both zero.
- All 72 paired cases complete, with zero degraded, invalid-provider,
  unexplained missing or administrative-abort outcomes.
- Classifier-call p95 at most 3,000 ms in that repetition, over actual attempted
  classifier requests. Use nearest-rank p95, retain slow/failed observations,
  and report the number of attempts and any zero-call decisions. An empty
  classifier latency population is incomplete evidence, not a latency pass.

In addition, at least 69 of 72 cases must have the same final decision in all
three repetitions (at least 95% stability). Report individual decision flips;
a majority vote does not rescue any failed repetition. The three repetitions
are the complete experiment, not three opportunities from which to pick a pass.

For each repetition report TP/FN/FP/TN, precision, recall, specificity, FPR,
per-language counts, critical errors, paired wins/losses, provider failures,
latency and cost. Undefined precision is null. Use 10,000 paired-family
percentile bootstrap samples with seed 179 and retain all three language
variants in each selected family. Recall intervals resample the 12 positive
families; precision intervals resample the 12 positive and 12 negative families
separately. Report per-arm and paired-difference 95% intervals. Repetitions
remain paired observations within each family, never 216 independent queries.

Separate offline regressions must pass for forced timeout, malformed/empty
output, provider error, safe fallback, off/shadow/enforce behavior, precedence,
source/current-chat isolation and context budgets. Forced failures are not
mixed into efficacy. These point gates screen a small balanced synthetic set;
they do not prove field prevalence, calibrated probabilities or a population
false-positive rate. Passing allows consideration of a guarded release only.

## Budget and one-time release

The provider ceiling remains USD 5 combined across issues 179 and 180, with a
single existing shared ledger. Bind both its persisted UUID and canonical state
mount identity from the runner; an alternate directory or copied ledger must
not reopen acceptance. Validate all previously frozen cache entries unchanged;
additive issue-180 entries are allowed. Reserve before dispatch, charge known
usage even on parse failure, retain unknown-usage reservations, and report all
attempts, timeouts and administrative aborts. Frozen Luna budgeting rates are
USD 0.20 input and USD 1.20 output per million tokens; bind this trial rate card
explicitly rather than using the older historical pricing table.

After the development and actual-adapter claim, the custodian issues one private
authorization binding the fixture, manifest, protocol, candidate/evaluator source
maps, configuration, development report/cache, state identity, fixed three-run
schedule and a unique nonce. Validate it and create the exclusive claim before
parsing any holdout payload or dispatching its provider work. One claim consumes
one complete three-repetition experiment. Retain the claim and all original
bytes after a crash, budget refusal or failed gate; never silently reopen it.
No holdout-driven prompt change, threshold change, relabeling or gate relaxation
is authorized. The custodian makes no provider requests.
