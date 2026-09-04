# Auxiliary model comparison: 2026-09-04

This is development evidence for issue #176. No model setting, routing mode,
production database, or Telegram delivery was changed by the experiment.
Neither candidate is approved for promotion by these results.

## Protocol and coverage

The isolated deployed dependency image ran the matching production adapters,
normalizers, application retries, and image routing policy. The model-policy
corpus contained 120 existing synthetic cases, repeated three times per model.
The image-intent corpus contained 55 existing synthetic cases, repeated twice
per classifier, retaining the Terra authorizer. All 940 decisions completed:
1,072 provider attempts, zero evaluator errors, zero administrative aborts,
and zero provider model or reasoning mismatches.

Both candidate families used reasoning `none`, the actual eight-second deadline,
zero SDK retries, the original prompts and schemas, and concurrency two. The
image classifier and authorizer retained their actual application retry limit
of two attempts. Provider storage and tracing were disabled. Memory lookup and
Telegram transport were excluded. Model-policy inputs reused the fixture's
synthetic metadata; every image fixture was converted to a synthetic message
whose production-generated metadata matched the fixture.

These previously observed corpora are not fresh holdouts. No prompt, taxonomy,
threshold, or fixture was tuned after seeing the results. Bootstrap intervals
use 2,000 paired case-cluster samples, retaining all repeats of each case.

## Model-policy classification

| Metric | GPT-5.4 nano | GPT-5.6 Luna |
| --- | ---: | ---: |
| Decisions | 360 | 360 |
| Macro-F1 | 0.781917 | 0.844355 |
| Completed, structured-valid decisions | 360/360 | 360/360 |
| Unsafe downgrade after deterministic policy | 0 | 0 |
| Stable class and tier across all three repeats | 90/120 (75.00%) | 112/120 (93.33%) |
| Low-confidence premium fallbacks | 169 | 0 |
| Mean self-reported confidence | 0.778194 | 0.980389 |
| Latency p50 / p95 | 1,362 / 2,049 ms | 1,375 / 2,043 ms |
| Estimated known cost | $0.0781860 | $0.0774888 |
| Existing classification gate | NO_GO | NO_GO |

Luna's paired macro-F1 improvement was **+0.062439**, with 95% interval
**[+0.022024, +0.109128]**. Its per-repeat scores were 0.836806, 0.855927,
and 0.839915; nano's were 0.836289, 0.746581, and 0.758784.
Both miss the existing 0.90 macro-F1 threshold.

Luna's high self-reported confidence, despite a macro-F1 of 0.844355, is a
calibration concern. Preserving threshold 0.75 does not establish equivalent
fallback behavior: nano escalated 169 low-confidence decisions, whereas Luna
escalated none. No threshold adjustment was tested. This balanced synthetic
corpus is not the production request mixture, and shadow routing does not
change the model producing live answers. The measurements therefore cannot
establish total Aigan savings or active-routing readiness.

## Image-intent classification and routing

| Metric | GPT-5.4 mini + Terra | Luna + Terra |
| --- | ---: | ---: |
| Decisions | 110 | 110 |
| First-attempt semantic passes | 107/110 | 106/110 |
| Final classifier semantic passes after application retry | 108/110 | 106/110 |
| Completed, structured-valid final classifier decisions | 110/110 | 110/110 |
| Frozen end-to-end checks passed | 104/110 | 104/110 |
| Semantic and end-to-end conjunction passed | 103/110 | 104/110 |
| Stable intent and route across both repeats | 53/55 (96.36%) | 54/55 (98.18%) |
| Whole-decision latency p50 / p95 | 2,552 / 3,893 ms | 3,098 / 4,505 ms |
| Estimated known pipeline cost | $0.1998478 | $0.1435435 |
| Additional reserve for an attempt with unknown usage | $0 | $0.0307600 |

The paired final-classifier semantic difference was **-0.018182**, with 95%
interval **[-0.081818, +0.027273]**. These data do not demonstrate semantic
improvement or establish non-inferiority. One Terra authorization attempt in
the Luna pipeline hit the eight-second deadline; the application retry
recovered. The Luna classifier itself had no provider failure in this run.

The frozen scorer reports two unexpected public-delivery routes per model.
All four flags are the same case, `ua_limit`, across its repeats. Post-run
inspection found a stale expected result: the evaluator expects clarification
for a request exceeding five images, while `image_intent.py` deliberately caps
the delivery plan at five, and
`test_exact_count_above_transport_limit_is_naturally_bounded` explicitly verifies
that behavior. This is a fixture-versus-contract disagreement shared by the
baseline and candidate, not evidence of a new Luna safety regression or of an
actual unsolicited Telegram send. **The original scores and flags remain
unchanged.** A separately versioned fixture correction and fresh acceptance
evidence are required before using this corpus as a promotion gate.

Other frozen end-to-end failures remain distinct from that disagreement:
mini also failed cases `ua_mixed_private_negative_public_positive`,
`ua_reply_elliptical_analysis`, and `ua_unresolved_reply_pronoun`; Luna also
failed `ua_unresolved_reply_pronoun` and `ua_your_photos`.

## Cost and attempt attribution

| Decision family | Provider stage | Attempts | Known estimated cost | Unknown-usage attempts |
| --- | --- | ---: | ---: | ---: |
| nano model policy | nano classifier | 360 | $0.0781860 | 0 |
| Luna model policy | Luna classifier | 360 | $0.0774888 | 0 |
| mini image intent | mini classifier | 111 | $0.0885804 | 0 |
| mini image intent | Terra authorizer | 60 | $0.1112674 | 0 |
| Luna image intent | Luna classifier | 110 | $0.0187565 | 0 |
| Luna image intent | Terra authorizer | 71 | $0.1247870 | 1 |

Total known estimated cost was **$0.4990661**. The full conservative reservation
for the one unpriced attempt remains **$0.0307600**, producing an accounted
upper bound of **$0.5298261**, within the $4.50 family budget. Unknown usage is
not counted as free. Successful attempts release only unused reservations;
the cumulative sum of reservations is not a spend total.

These estimates use the current runtime pricing module and observed cache
usage, not invoices or uncached normalized prices. For the image classifier,
mini reported 245,480 input tokens including 198,912 cached, while Luna reported
243,270 including 228,710 cached. Both model-policy arms reported 222,480 input
tokens with none cached. Terra call counts differ because classifier decisions
change authorization admission and retry behavior. Cheap classifier tokens
alone therefore do not describe whole-pipeline value.

## Provenance and limits

The exact measured harness is preserved in commit
`08b73f82e9b8f50f9519ba28057a5b72f9d07e8e`, with SHA-256
`389153f24cb1a1b5af88744a986b3c8697e76eda67fbfd8a72ce9a197715ea19`.
The manifest binds production functions, source files, helper scorers, fixtures,
settings, and randomized job order. Its canonical logical hash is
`fda84aaa61d43a1032e61008cde55bd8247f89dffa257103a04ed27042a2351d`.
The original summary file SHA-256 is
`02a072f11cffe0beba1c81842d9d690fd754f84801ab93e51cdd157a8634aeb2`.

A subsequent reporting-only correction includes all dispatched attempts in
per-family cost ledgers even if a later administrative abort excludes the
decision from scoring, and marks evaluator failures incomplete. Its harness
SHA-256 is `4e065d5f87b048f767cf8f48ddea8c728412782f1634bc9456480104aa76c392`.
Recomputing from the retained records produced an identical summary: this run
had no administrative abort or evaluator failure. No provider calls were
replayed. Nine focused offline tests passed, including actual retry behavior,
unknown-cost retention, billed invalid/incomplete output, complete paired
clusters, and accounting after an administrative abort.

No conclusion is established here for the separate reminder tool-router,
background vision, image-candidate review, or memory extraction. Those roles
need their own acceptance examples and evaluation.
