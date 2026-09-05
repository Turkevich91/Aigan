# Hybrid memory fusion development comparison

This protocol is fixed before the three-policy comparison for #180. The observed
#176 corpus is development evidence, not a new holdout, human gold, or a field
answer-quality study. Historical #176 reports and labels remain unchanged.

## Ownership and invariant

`memory.py` owns canonical memory-row identity and retrieval ranking. `main.py`
collects the existing filtered per-query batches and selects the configured
policy. Current raw maximum-score merging lets keyword scores around 100 dominate
cosine similarities around 1 and compares FTS scores across different queries.
The new helper changes fusion only: no schema, document vectors, source filters,
query generation, invocation admission, source-window expansion or context budget.

## Frozen arms

1. **legacy**: the current maximum raw score, first occurrence and source union;
   flatten batches in existing keyword, semantic, FTS order.
2. **rrf**: equal channel weights; contribution `1 / (60 + rank)`; keep only the
   best contribution for each canonical row and channel across query variants.
3. **normalized**: within each deduplicated batch, `(score-min)/(max-min)`;
   singleton/equal-score batches contribute 1. Keep each row's best contribution
   per channel and sum equal-weight channel contributions.

Deduplicate row IDs inside each batch before assigning one-based ranks. Negative
finite scores participate normally in min/max normalization. Missing channels
contribute zero. No clipping, score thresholds, tuned weights or extra query
votes are added. Candidate ties use best rank then canonical row ID. Candidate
provenance is a deterministic union of the original source labels.

Unknown policies, malformed channels or nonfinite scores fall back to the legacy
merge. The rollout flag `MEMORY_SEARCH_FUSION_POLICY` defaults to `legacy`.
Current numeric rescue has no independent rule: keyword AND-LIKE retrieval and
its score priority are the mechanism. For this first slice, a search query with
a standalone numeric token plus keyword hits retains legacy ordering under both
candidate policies. Count that protected path separately; it limits improvement
for amount/date queries. It must not be described as candidate-policy success.

## Corpus and pairing

Use the preserved #176 512-dimensional index: 4,776 embedded rows (4,716 real
rows and 60 constructed rows). Reconstruct exactly 84 accepted queries from the
original query freeze and independent-agent audit: 48 controlled positives in
24 two-query families, 12 source-derived probes in 12 families, 12 constructed
no-answer probes and 12 isolation probes. Keep all timestamps and the original
evaluation clock, labels, current-message exclusion and bot-invocation filters.
The archived SQLite message table contains 5,420 retained rows; keep the full
table for keyword/FTS parity rather than trimming it to the embedded subset.

Verify archival file hashes before copying the index into a new private store.
The original document vectors are reused. The missing query vectors are fetched
once through the separate shared-budget provider adapter, then all three arms
consume exactly that cache. The fusion evaluator itself has no provider client.
Before requests, bind the archive, query input, provider normalization, candidate
source, protocol, runtime package versions and effective retrieval configuration.
The shared provider ceiling remains USD 5 across #179 and #180.

## Measurement and fixed decision

Run actual `semantic_memory_search_outcome` for `direct` (top 6) and
`memory_recall` (top 12). Report target source-hit@1/6/12 where available, MRR,
duplicate/forbidden source counts, channel availability, protected/failure
fallback counts and applied policy. Source-hit is not exhaustive relevance recall.
No-answer probes only measure retrieval exposure; the runtime does not abstain,
so nonempty retrieval does not prove a false answer.

Pair candidates with the same-run legacy baseline. Use 10,000 seeded (180)
percentile bootstrap samples by family, preserving each family's queries.
Report paired wins/losses and 95% intervals for metric differences. Intervals
describe this development cohort; candidate selection is not a confirmatory
field-efficacy claim. Prefer RRF if both candidates pass; otherwise choose the
sole passing candidate. No parameter retuning or new acceptance bounds follow
the rankings. If both fail, retain legacy.

A passing candidate must satisfy every condition:

- Controlled direct source-hit@6 gains at least 4/48 and its paired 95% lower
  confidence bound for gain is strictly above zero.
- On each positive cohort and each route, point source-hit@1/6 and MRR do not
  decline; recall-route source-hit@12 also does not decline.
- Isolation has zero forbidden-source exposure; canonical-row duplicates,
  numeric protection and source provenance have zero regressions. All admitted
  nonnumeric cases actually exercise the requested fusion policy; unexpected
  legacy fallbacks are a failed validity check.
- For either route, local end-to-end retrieval p95 is no greater than the
  same-run baseline p95 plus `max(2 ms, 10% of baseline p95)`. This practical
  nonmaterial-latency bound is an engineering choice fixed before measurement;
  report both absolute and relative deltas without treating small noise as free.

For timing, cap the isolated container at one CPU and 2 GiB RAM. Warm each
arm/route once on the first query, then run exactly three timed repetitions of
every query/route/arm. Rotate the three-arm order by query and repetition; use
the same cached vectors, store and clock. Record local wall time (including
actual retrieval and fusion, excluding provider calls), all raw private timings,
and per-route p95. No repeats until passing or discarded slow observations.
Use the nearest-rank definition for reported timing percentiles.
Also save the filtered per-channel batches for future offline audit. Production
API/network latency and Telegram answer delivery remain outside this benchmark.

## Runtime checks

Focused tests cover scale separation, duplicate rows and variants, singleton
and missing channels, ties, source unions, nonfinite/unknown-policy fallback,
numeric protection, exact legacy parity and actual collector filters. Then run
the authoritative suite and build/review gates independently of #179. Enabling
a policy requires separate accepted evidence and guarded release; default legacy
does not change behavior merely because the new code is present.
