# Auxiliary model evaluation: 4 September 2026

Issue: [#176](https://github.com/Turkevich91/Aigan/issues/176).

Luna improved model-policy classification over Nano at approximately the same
measured token cost, but neither passed the existing classification gate. Luna
did not establish a quality improvement over Mini for image-intent routing.
The embedding comparison and its resource measurements are recorded below.
No runtime model, routing mode, threshold, live index or schema was changed.

## Scope and reproducibility

The application baseline was `aa43d806610ef251a0fd660ae62da9a7a189ceb5`
(merged PR #175). The measured evaluator sources were preserved in commit
`08b73f82e9b8f50f9519ba28057a5b72f9d07e8e`. Experiments used the existing
provider credential in isolated processes with the deployed dependency set.
Responses API storage and trace export were disabled. No evaluator polled
Telegram or sent messages. Detailed private records, corpus rows, source IDs,
questions, vectors and provider payloads are excluded from this report and Git.

The issue froze scope, repetitions, thresholds and a USD 5 total ceiling before
provider execution: USD 4.50 for classifiers and USD 0.50 for embeddings,
including source-question construction. No results-based tuning or replacement
of failed cases occurred. Existing historical evaluations and unopened holdouts
remain unchanged. These are development comparisons, not field efficacy or
promotion evidence.

## Specialist models

The [detailed classifier report](auxiliary-model-eval-results-2026-09-04.md)
contains all repetition scores, paired intervals, validity, fallback, latency,
provider-stage costs and frozen-source provenance.

| Candidate | Environment-specific result | Decision |
|---|---|---|
| Luna for Nano model-policy classification | F1 0.781917 to 0.844355; paired difference +0.062439, 95% interval [+0.022024, +0.109128]; approximately equal cost | Stronger development candidate; both miss the existing 0.90 gate |
| Luna for Mini image-intent classification | Final semantic passes 108/110 to 106/110; frozen route passes 104/110 for both | Improvement and non-inferiority unproven; retain current model |

Model-policy F1 is not accuracy. The 120 cases have three paired repetitions,
with case clusters retained in uncertainty calculations. Luna's mean confidence
was 0.9804 despite remaining errors, with no low-confidence fallbacks versus
169 for Nano. An unchanged threshold does not establish equivalent calibration.
Current model-policy routing is shadow-only; this study does not measure live
answer-model savings or authorize active tier routing.

The image comparison used 55 cases twice per model, preserving the actual Terra
authorizer and application retries. Whole-pipeline known cost fell 28.2%, or
12.8% when charging the full reserved unknown usage, while observed p95 time
rose 15.7%. This is much less saving than the roughly 73% lower Luna-versus-Mini
input/output list rates. The unchanged authorizer was called more often, and
cache usage also differed. One Terra attempt timed out and recovered on retry;
it was not a timeout of the Luna classifier. This single run is not a long-term
latency estimate.

A shared stale expectation in `ua_limit` flags the current intentional cap of
five photos as a failure. Original scores remain preserved; this is not evidence
of a new Luna safety regression or an actual unsolicited Telegram send. A fresh,
separately versioned acceptance fixture must reconcile this contract. Other
failures remain. Separate reminder routing, background vision, candidate-image
review and memory extraction were not assessed by these classifier runs.

## Embedding dimensions

The frozen corpus contained **4,716 real eligible records**, plus 60 controlled
and adversarial records: **4,776 freshly indexed rows in each arm**. Both indexes
were rebuilt from identical rows with current provider embeddings. This isolates
dimension effects; it does not compare new coverage with the old live cache.

There were **84 queries**: 48 controlled positives (24 paraphrase families),
12 real-source-derived probes, 12 constructed missing-answer probes and
12 isolation checks. Of 24 selected real sources, 20 passed generation and
source-only checking; independent blind agent review accepted 12 before either
index existed. Rejected questions were not replaced or rewritten. All six
source strata remained represented. These are agent-reviewed development
labels, not human gold or naturally occurring field questions.

Each query ran pure semantic retrieval and the unchanged hybrid top-6 and
top-12 paths in both dimensions: 504 ranking passes, not 504 independent cases.
Metrics below are **known-source hit counts** and reciprocal rank, not exhaustive
relevance recall or answer accuracy. Top-6/top-12 hybrid rows describe their
actual returned cutoff; the semantic path retained 12 for both cutoff metrics.

| Cohort / retrieval | Hit@1, 512 / 1536 | Hit@6, 512 / 1536 | Hit@12, 512 / 1536 | MRR, 512 / 1536 |
|---|---:|---:|---:|---:|
| Controlled, semantic (48) | 26 / 25 | 35 / 36 | 40 / 39 | 0.61864 / 0.61652 |
| Controlled, hybrid top-6 (48) | 2 / 2 | 7 / 7 | n/a | 0.07014 / 0.07014 |
| Controlled, hybrid top-12 (48) | 2 / 2 | 7 / 7 | 13 / 13 | 0.08266 / 0.08266 |
| Real-source probes, semantic (12) | 9 / 9 | 10 / 11 | 11 / 11 | 0.80208 / 0.81250 |
| Real-source probes, hybrid top-6 (12) | 8 / 8 | 10 / 10 | n/a | 0.75000 / 0.75000 |
| Real-source probes, hybrid top-12 (12) | 9 / 9 | 10 / 10 | 10 / 10 | 0.79167 / 0.79167 |

For semantic hit@6, the paired family-bootstrap difference was +2.08 percentage
points on controlled probes, 95% interval **[-4.17, +10.42]**, and +8.33 points
on the 12 real-source probes, interval **[0, +25]**. These small development
intervals do not establish field improvement. Controlled semantic hit@1,
hit@12 and MRR slightly declined. All measured hybrid quality aggregates stayed
unchanged; this does not assert identical complete rankings or answer quality.

The pronounced controlled semantic-versus-hybrid gap warrants investigating
the existing raw-score fusion: keyword scores and vector dot products have
different scales, and the current maximum-score merge can favor lexical
matches. This is a source-backed explanation to test, not proof from a fusion
ablation or authorization to replace the ranking policy. Dimension alone did
not recover the measured hybrid gap.

All 12 isolation probes excluded their deliberately forbidden records in both
dimensions and all three retrieval paths. Missing-answer probes still returned
neighbors, as expected from a retriever without abstention; no answer-generation
or hallucination conclusion follows. Every hybrid path confirmed embeddings
were used, with zero embedding errors.

| Resource measurement | 512 | 1536 |
|---|---:|---:|
| Vector blob bytes | 9,781,248 | 29,343,744 |
| Complete copied SQLite file bytes | 58,810,368 | 67,305,472 |
| Controlled semantic median local wall time | 288 ms | 648 ms |
| Controlled hybrid top-6 median local wall time | 841 ms | 1,203 ms |
| Real-source hybrid top-6 median local wall time | 1,110 ms | 1,446 ms |
| Index input tokens / estimated API cost | 206,008 / USD 0.00412016 | 206,008 / USD 0.00412016 |
| Query input tokens (84 queries) | 2,898 | 2,898 |
| Single-query API median / p95 | 185 / 354 ms | 178 / 603 ms |

Local timings exclude query-vector API calls and measure the actual application
functions under a one-CPU, 2 GiB container limit, with one warmup per arm and
alternated dimension order. There was one retrieval repetition per query;
timings are this run's observations, not established production percentiles.
API trials used zero SDK retries and a 45-second deadline, differing from
production defaults. Three times the vector bytes did not triple the copied
database file; file allocation and existing free pages affect its size. No
per-arm process-memory claim is made.

The separate 24-case synthetic recall-intent screen produced **zero decision
changes**: both dimensions missed all 12 expected recall requests and correctly
rejected all 12 non-recall requests, with no provider degradation. Positive
cases failed the unchanged strong-threshold/context-hint admission rules.
This identifies a limited component-screen gap to investigate, not proof that
all live memory is broken or that dimensions caused a regression. Thresholds,
context hints and archetypes need separate, fresh calibration evidence before
any change; no threshold was tuned on these results.

**Decision: retain 512 dimensions.** Formal efficacy/promotion verdict remains
`INCONCLUSIVE_NO_RUNTIME_PROMOTION`: no measured hybrid source-hit gain, higher
local resource cost, and no field-query answer evaluation. The existing
single-vector-per-message storage and bounded backfill also require a complete
index migration and verified fallback before any future dimension switch.

The [methods document](embedding-dimensions-eval.md) describes private execution
and freeze/audit gates. SHA-256 evidence anchors:

- Source freeze: `9ff8e83ffd58925df335b8ef9ee56cc38e3acb762a8597d39c98fedde8154db7`.
- Measured embedding CLI: `a44b912a6a046084ce47c16d2158b106c0780f171880878567eb537ea5edd737`.
- Original query freeze: `5a3b48f2b565b10d0723f1e5c65700eaab7bbea22e639c931c9fbc8e89211401`.
- Independent audit: `e7e071f512f13752f7185a0763deea4657bc4a29e2c1af98765fdd5fcf5a9dea`.
- Reviewed query freeze: `87d6953b6f71fd77669723e0b5e0f463fb70e80c6cd6a19b2434ed8dbde74793`.
- Aggregate report: `3729080f355744a6d8334e2d30bec9de0b47aede901ccabfc446f954700c1be4`.

The exact private run and its source/query freezes were copied to durable
operator storage, with file hashes verified and owner-only access.

## Cost and operational evidence

Classifier evaluation completed **940 decisions and 1,072 provider attempts**,
with zero administrative aborts, internal evaluation errors or model/effort
mismatches. Known estimated cost was **USD 0.4990661**, plus the **USD 0.0307600**
unknown usage reservation. Reservations are conservative bounds, not invoices.
Completed response usage is priced before parsing, so invalid outputs remain
billable. A subsequent reporting-only fix includes attempts belonging to
administratively aborted decisions in per-role costs and marks internal-error
runs incomplete. This completed measurement had neither condition, so its
results are unchanged and no provider rerun was needed.

The embedding study completed **408 provider calls**, including 44 Mini
question-construction/checking calls, with zero failures or unknown usage.
Its total estimated cost was **USD 0.02192275**, including USD 0.01353375 for
question preparation. Across both studies, known estimated cost was
**USD 0.52098885**, with a conservative total bound of **USD 0.55174885** after
the unpriced Terra attempt reservation. Both family caps and the total ceiling
were respected.

Docker build passed. The isolated full suite ran 926 tests: **925 passed and one
was skipped**. Independent method and aggregate-evidence review covered case
denominators, clustered uncertainty, current contract conflicts, cost ledgers,
blind labels, dimension parity and the absence of runtime promotion. Public
diff privacy scanning and whitespace checks passed. Provider experiments were
not repeated for documentation or accounting-only changes.

## Independent context and limits

The [OpenAI embedding guide](https://developers.openai.com/api/docs/guides/embeddings)
documents the dimensions parameter and the default 1536 dimensions for
`text-embedding-3-small`; its current input rate is USD 0.02 per million tokens,
independent of requested dimension. Prices used here are the repository's
`openai-standard-2026-09-04` snapshot from the
[official pricing documentation](https://developers.openai.com/api/docs/pricing).
Provider invoices and tool/external-service fees are outside these estimates.

[Matryoshka Representation Learning](https://arxiv.org/abs/2205.13147) supports
the general principle of representations at different dimensionalities;
it does not prove that increasing dimensions improves Aigan.
The original [MTEB paper](https://arxiv.org/abs/2210.07316) compares embeddings
across diverse tasks and datasets. Such independent research motivates
task-specific testing; it does not replace this application's corpus, ranking
policy or eventual field-query answer evaluation.

Two manual Telegram quests were delivered privately: one exercises real-photo
delivery followed by analysis of that exact replied image; the other exercises
corrections and an explicitly unknown time. They contain five bot requests in
total. They are operator-run cards, not an implemented native quest system, and
do not prove an embedding improvement. No field pass is claimed here without
operator observations and matching private provenance.
