# Recall admission evaluation and rollout

This task changes admission to the expanded prior-conversation recall path.
Ordinary direct replies already have a memory-search path; missed recall
admission did not mean the bot had no memory at all. Hybrid result ranking is
the independent #180 change.

## Rejected lexical candidate

The original detector used embedding similarity at thresholds 0.62/0.48, with
its existing archetypes and context hints. Both language blocks used independent
synthetic UA/RU/EN labels, 36 positive and 36 negative cases each.

| Block | Policy | TP | FN | FP | TN | Recall | Precision |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Development | Legacy | 4 | 32 | 2 | 34 | 11.11% | 66.67% |
| Development | Lexical v1 | 36 | 0 | 0 | 36 | 100% | 100% |
| Fresh v1 holdout | Legacy | 5 | 31 | 4 | 32 | 13.89% | 55.56% |
| Fresh v1 holdout | Lexical v1 | 20 | 16 | 2 | 34 | 55.56% | 90.91% |

Both remaining lexical false positives were predesignated critical. The v1
candidate therefore **failed acceptance and was not activated**. Its development
success did not generalize to new linguistic forms and negation scope. The
original fixture, protocol and measured source archive remain preserved; the
opened v1 holdout became development evidence for the next candidate.

## Luna development trial

One frozen `gpt-5.6-luna` classifier was tested on the two now-observed blocks,
144 cases with two fresh calls per case. The strict result is one of six intent
labels. Only `prior_conversation` admits recall; the other labels cover current
input, general requests, future reminders, new memory and unclear instructions.
The provider receives current user instruction text and reply-presence flags,
without reply bodies, conversation history, identifiers, tools or output storage.

All **288 decisions were correct**, with 144/144 cases stable across both calls.
There were no invalid outputs, timeouts, retries or fallback decisions. The
paired legacy decisions had recall 12.5% and precision 60%; Luna had 100% on
this observed set. The family-bootstrap 95% interval for recall gain was
+76.39 to +95.83 percentage points. Repeated calls do not create 288 independent
linguistic examples, and perfect synthetic results are not a population guarantee.

The development classifier stage had p50 791 ms and p95 2,239 ms. Its total
known cost was USD 0.0346252, approximately USD 0.12 per 1,000 such classifier
requests, using the frozen current rate card. These are component costs and
latencies, not total response cost or Telegram response time. Development
fallback reused previously measured legacy decisions and did not price another
embedding transport call; no fallback occurred.

## Independent v2 acceptance

The new closed fixture and three-repetition gates are defined in
[recall-admission-179-v2-eval.md](recall-admission-179-v2-eval.md). Its author
created it before inspecting the candidate prompt or code. It tests the actual
application adapter, with the unchanged legacy detector as paired baseline.
The single frozen experiment **passed every predeclared gate in all three
repetitions**. Each repetition produced TP 36, FN 0, FP 0, TN 36, versus the
paired unchanged baseline TP 2, FN 34, FP 1, TN 35. Recall increased from 5.56%
to 100%; precision from 66.67% to 100%. Each language had 12/12 positive hits
and zero false positives. The paired-family 95% interval for recall gain was
+86.11 to +100 percentage points. This small synthetic result does not estimate
the natural prevalence or false-positive rate of Telegram traffic.

All 72 case decisions were identical across the three repetitions. Each
repetition made 69 actual Luna calls; three requests took an unchanged excluded
route before classification. Thus 216 integrated decisions used **207 provider
classifier calls**, not 216 calls. All completed with the frozen model and
settings, with no invalid, degraded, aborted or fallback outcome. Each of the
three 12-case invocation blocks passed, including zero classifier/provider or
Telegram calls for all six ordinary uninvoked messages.

| Repetition | Classifier p95 | Adapter p95 | Classifier cost |
| --- | ---: | ---: | ---: |
| 1 | 1,766 ms | 1,780 ms | USD 0.0085324 |
| 2 | 1,730 ms | 1,747 ms | USD 0.0085324 |
| 3 | 1,431 ms | 1,446 ms | USD 0.0085324 |

The classifier cost total was USD 0.0255972. Combined known provider spend for
the two memory tasks, including development and query embeddings, was
**USD 0.06045184 of the USD 5 ceiling**, with zero unknown-usage reservations.
No paid trial was replayed after observing results. A preflight-only price
assertion was corrected before freezing: a million-token test incorrectly
invoked the valid long-context surcharge; a 1,000-token test now verifies the
base rate. No provider price or measured candidate behavior was changed.

Evidence SHA-256 fingerprints:

| Artifact | SHA-256 |
| --- | --- |
| Observed development report | `481eff293e0794fe4dbe4e373648f157d32b058eedeca21a75ca9a5363650e2c` |
| V2 source freeze | `c4c8e18c03ff20844a0167e1c60457fd9af995350bec24ae685a23b350b588d0` |
| V2 measured source archive | `3cfae6a17baa272eda9b74ead2aff7d94455b178ec476ac9550c44684f7c3f31` |
| V2 aggregate report | `a4bb34d004cfcf81abc20154b6c8b1cd9f910285a83304c0abf767798ae74df0` |

An independent audit verified all labels, scheduled requests, model identities,
usage and ledger links, 36 boundary results, stability, metrics and the archived
39-file measured source map. The subsequent integration of #180 changed only
its approved retrieval functions and imports; AST comparison proved all recall
functions unchanged. The combined deployment image passed 1,036 tests with one
skipped. A private synthetic smoke exercised actual invocation, recall admission
and RRF retrieval in all rollout modes with mocked transport and zero Telegram
sends; it is distinct from the actual-provider holdout and from field answers.

## Runtime contract

`MEMORY_RECALL_POLICY_MODE=off` preserves the exact legacy path. `shadow` calls
both detectors, records a sanitized comparison and applies the original legacy
object. `enforce` calls Luna once and replaces the intent-embedding request when
successful; it calls the unchanged legacy detector only on classifier failure.
An overlong/invalid request, timeout or invalid provider response uses that same
fallback. There are no classifier retries; cancellation propagates.

The existing invocation gate runs first. Uninvoked ordinary group chatter gains
no recall-classifier call. Translation/image exclusions, current-chat scope,
query generation, search windows and context budgets remain owned by their
existing paths. The new structured intent has no invented probability or
self-reported confidence threshold. Usage is recorded by the existing model
telemetry stage, including provider usage from invalid completed responses.

The flag defaults to off. Rollback changes it to off without rewriting memory,
embeddings or schema. Astra and existing tier-shadow routing are separate model
roles. Provider evaluation uses the existing shared USD 5 ceiling and sends no
Telegram messages. ASTRA-Q1/Q2 remain operator-owned manual quests.
