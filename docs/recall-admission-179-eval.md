# Recall-admission evaluation for issue 179

This protocol covers admission to expanded recall only. Normal requests already
use memory. Hybrid fusion belongs to issue 180; model-policy routing and its
acceptance threshold are outside this change.

## Frozen evidence and custody

The independent fixture custodian authored two new synthetic blocks before any
candidate comparison. Each contains 72 efficacy cases: 36 recall requests and
36 non-recall requests. Each block has 24 scenario families, with one Ukrainian,
Russian and English wording per family. Each language therefore has 12 positives
and 12 negatives. Translations stay in the same family and split. Scenarios were
authored separately across blocks; broad behavior categories necessarily recur.
There are no exact cross-split prompt duplicates and no copied issue-176 cases.

These are independent-agent synthetic labels, not human gold, natural traffic,
or an estimate of deployment prevalence. The custodian sees both blocks to
author and audit them. Implementers and the coordinator may read development
only until the final candidate is frozen.

- Development fixture: `tests/fixtures/memory_recall_development_v1.json`.
- Development SHA-256: `2109575596776ead7ec5e29abe05f8fd1dd11c8345b7209c52ccee809358991d`.
- Closed holdout SHA-256: `c77bf08c8e08a25f39c1fb65a3b3d2d35d3a66f71f324c89a432c95adf470888`.
- Public manifest: `tests/fixtures/memory_recall_179_manifest.json`.

The holdout payload and its private custody location are excluded from Git.
Owner-only private custody does not authorize an implementer to inspect it.
The original bytes are retained. Do not replace, relabel, or rewrite a failed
case after observing scores.

Each file also contains 12 separate invocation-boundary cases. They exercise
ordinary uninvoked group messages, explicit group invocation and replies to
the bot. These are not part of the 72-case efficacy denominator.

## Scope of the labels

A positive asks for prior conversation evidence: an earlier agreement, reason,
quantity, location, attribution, attachment, correction, or related exchange.
A negative asks for an ordinary present task, new fact, prospective reminder,
general knowledge, software or cognitive memory, or a short question without
an anchor. Quoted instructions, explicit negation, supplied-source-only tasks,
translation and public-image requests are included as confounders.

Fifteen negatives per block have `critical_negative=true`: quoted requests
that must not be executed, explicit history prohibitions, route exclusions and
source-only requests with an explicit history prohibition. The category and
label are fixed before measurement.

The efficacy labels concern recall intent in an eligible private request.
Invocation permission is a different decision: a recall-shaped ordinary group
message must still remain silent. Do not count that permission denial as a
negative example for the semantic intent detector itself.

## Schema and boundary execution

Each fixture is one JSON object, not JSONL:

```json
{
  "schema_version": "recall-admission-fixture-v1",
  "fixture_id": "recall-admission-179-development-v1",
  "split": "development",
  "baseline_sha": "<baseline commit>",
  "anchor_datetime_utc": "<fixed synthetic clock>",
  "label_authority": "independent_agent_synthetic",
  "human_gold": false,
  "cases": [
    {
      "case_id": "<unique language-specific case>",
      "family_id": "<scenario shared by three languages>",
      "language": "ua",
      "category": "<behavior category>",
      "prompt": "<synthetic request>",
      "context": {
        "chat_type": "private",
        "invoked": true,
        "has_reply_text": false,
        "has_reply_image": false,
        "reply_text": ""
      },
      "expected": {
        "is_recall": true,
        "critical_negative": false,
        "reason": "<label rationale>"
      }
    }
  ],
  "boundary_cases": []
}
```

Boundary cases additionally identify `boundary_kind` and
`context.reply_to_bot`. Their expectations are `invocation_eligible`,
`recall_stage_calls`, and `telegram_deliveries`. A null call/delivery
expectation is deliberately unscored, not zero. Eligible boundaries do not
authorize real provider or Telegram calls in the boundary test.

The boundary adapter constructs the configured trigger or reply-to-bot shape
and exercises the actual invocation seam. Efficacy calls exercise the actual
recall detector using the frozen request and reply shape. Additional offline
integration tests must preserve translation/image precedence, provider-failure
behavior, current-chat/source isolation, context budgets, and off/shadow/enforce
selection. Boundary outcomes and degraded-provider regressions are reported
separately from efficacy.

## Baseline, cache and candidate calibration

Baseline is commit `a0d5028bab91ba64daff379375b1f52a46c9dd0b`, with strong
threshold 0.62, ambiguous threshold 0.48, existing hints and existing archetypes.
Its entire `main.py` SHA-256 is
`8117c14b80df560a8df00acc4e4289945e1e17889738e764cb2ba8ff7a7c652a`.
The manifest separately binds detector, hint, fallback and archetype sources.

First measure the baseline as it is. Calibrate a bounded candidate on development
only. Changing thresholds, hints or archetypes is allowed only during that
development phase. Record attempted candidates and their development results;
do not tune on an aggregate or a failing case from holdout.

Keep `text-embedding-3-small` at 512 dimensions. Share identical query vectors
between baseline and candidate. Cache keys must bind exact provider input,
model, dimensions and normalization version. Additional candidate archetypes
must be frozen before holdout; query caching must not accidentally substitute
archetype or context inputs. Freeze SDK/dependency versions and request settings,
and report any difference from production. No extra calls may be introduced
for an ordinary uninvoked message.

The shared provider ceiling is USD 5 for issues 179 and 180 combined. Reserve
before dispatch, charge known usage even if output parsing fails, retain a
conservative bound for unknown usage, and record retries and administrative
aborts. The custodian performs no provider calls.

## Metrics and predeclared holdout gates

Count TP, FN, FP and TN from the 72 efficacy cases. Report recall
`TP / (TP + FN)`, precision `TP / (TP + FP)`, specificity and false-positive
rate; precision with no admitted positives is undefined, not perfect.
Report per-language confusion counts, all critical-negative violations,
degraded outcomes, provider errors, administrative aborts and boundary results.
The 12 boundary cases remain a separate table.

The final candidate must satisfy all of these point-estimate engineering gates
on the untouched holdout:

1. At least 33 of 36 positives admitted (recall at least 0.90).
2. At least eight more positive admissions than the baseline on the same
   36 positives (material recall gain at least 0.20).
3. At most one of 36 negatives admitted.
4. Zero critical-negative or invocation-boundary violations.
5. Complete paired evidence with no unexplained missing case, administrative
   abort or provider failure in the efficacy measurement; all separate
   failure, precedence, scope and rollout-mode regressions pass.

Report paired bootstrap intervals even when the point gates pass. Use 10,000
replicates, fixed seed 179, and retain all three language variants when sampling
a family. For recall and recall difference, resample the 12 positive families.
For precision and its paired difference, resample positive and negative families
separately (12 each) to preserve this designed class balance. Report two-sided
percentile 95% intervals and the paired win/loss counts. Intervals are
descriptive and do not constitute an extra unannounced tuning target.

This is a small synthetic screen: there are only 12 independent positive and
12 negative scenario families per split. Zero false positives among 36 correlated
wordings does not establish a population false-positive rate below five percent.
Passing the point gates authorizes consideration of a guarded rollout, not a
claim of proven field efficacy or calibrated probability.

## One-time holdout authorization

After development, the coordinator submits a frozen candidate claim. Bind the
candidate source and configuration, evaluator/scorer sources, development report
and cache, this protocol, and the fixture manifest. Source hashes must represent
canonical repository-relative file-to-SHA256 maps, not just one convenient file.
Configuration binds thresholds, archetypes, hints, rollout mode and embedding
settings. The custodian then releases the holdout location and a private
authorization object with these fields:

```json
{
  "schema_version": "recall179-holdout-authorization-v1",
  "authorized": true,
  "fixture_sha256": "<closed holdout hash>",
  "fixture_manifest_sha256": "<public manifest hash>",
  "protocol_sha256": "<this document hash>",
  "evaluator_source_sha256": "<canonical evaluator source-map hash>",
  "candidate_source_sha256": "<canonical candidate source-map hash>",
  "candidate_config_sha256": "<frozen configuration hash>",
  "development_report_sha256": "<development report hash>",
  "development_cache_manifest_sha256": "<development cache manifest hash>",
  "nonce": "<unique authorization nonce>",
  "scope": "one_holdout_run_no_tuning"
}
```

Blind file hashing is allowed for integrity. Before parsing holdout cases or
dispatching holdout provider work, the runner validates every binding and
creates a private exclusive claim file using `O_EXCL`. One coherent paired
baseline/candidate evaluation consumes this authorization. A crash or budget
abort retains the claim and yields incomplete evidence; it must not silently
reopen or retry the holdout. Any further attempt needs an explicit custodian
and coordinator disposition, with the earlier exposure recorded.

After opening holdout, the original fixture remains preserved. If the candidate
fails and development resumes, the opened block becomes observed evidence;
a different fresh acceptance block is required for another acceptance claim.
