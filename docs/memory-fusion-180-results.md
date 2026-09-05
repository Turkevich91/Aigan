# Hybrid memory fusion results

The frozen #180 comparison selected **reciprocal-rank fusion (RRF)**. Both RRF
and normalized-score fusion passed the predeclared development gates; the
protocol preferred RRF if both passed. This repairs the score-scale merge while
retaining `text-embedding-3-small` at 512 dimensions. It adds no model calls,
index migration or document-vector storage.

These are source-retrieval results from the already observed #176 corpus, not
human answer-quality acceptance or natural Telegram field queries. The protocol
is [memory-fusion-180-eval.md](memory-fusion-180-eval.md). Labels, parameters and
acceptance bounds stayed fixed throughout the one experiment.

## Measured source retrieval

The private copied index retained all 5,420 messages and 4,776 embedding rows.
There were 84 frozen queries: 48 controlled positives in 24 families, 12
source-derived probes, 12 constructed no-answer probes and 12 isolation probes.
Each policy used the same actual filtered keyword, semantic and FTS batches.

| Cohort / route | Policy | Hit@1 | Hit@6 | Hit@12 | MRR |
| --- | --- | ---: | ---: | ---: | ---: |
| Controlled / direct | Legacy | 2/48 | 7/48 | — | 0.0701 |
| Controlled / direct | RRF | 9/48 | 30/48 | — | 0.3715 |
| Controlled / direct | Normalized | 9/48 | 32/48 | — | 0.3944 |
| Controlled / recall | Legacy | 2/48 | 7/48 | 13/48 | 0.0827 |
| Controlled / recall | RRF | 11/48 | 31/48 | 35/48 | 0.3979 |
| Controlled / recall | Normalized | 11/48 | 34/48 | 38/48 | 0.4317 |
| Source-derived / direct | Legacy | 8/12 | 10/12 | — | 0.7500 |
| Source-derived / direct | RRF | 10/12 | 10/12 | — | 0.8333 |
| Source-derived / direct | Normalized | 10/12 | 10/12 | — | 0.8333 |
| Source-derived / recall | Legacy | 9/12 | 10/12 | 10/12 | 0.7917 |
| Source-derived / recall | RRF | 10/12 | 10/12 | 10/12 | 0.8333 |
| Source-derived / recall | Normalized | 10/12 | 10/12 | 11/12 | 0.8417 |

Hit@k means at least one labeled target source is among the first k returned
anchors. MRR is reciprocal rank of the first target within the route's returned
window. Direct retrieval returns at most six anchors, so direct Hit@12 is not a
separate measured endpoint.

The primary paired RRF result is **+23/48 controlled direct Hit@6**, or
**+47.92 percentage points** (family-bootstrap 95% interval **+31.25 to +64.58
points**). There were 24 query wins, one loss and 23 ties. The nonregression gate
was cohort-level; it does not mean every query improved. Controlled recall
Hit@6 gained 24/48 (+50.00 points; interval +33.33 to +66.67), and Hit@12 gained
22/48 (+45.83 points; interval +31.25 to +60.42). The small source-derived cohort
had no Hit@6 gain; its two direct Hit@1 wins have an interval including zero.

Normalized fusion's primary gain was +25/48 (+52.08 points; interval +35.42 to
+66.67). Its larger point estimate does not override the frozen RRF preference
or establish superiority to RRF on independent data.

## Cost, latency and validity

All 1,512 timed retrievals completed: 84 queries x two routes x three policies
x three repetitions, following six fixed warmups. Rankings and retriever-batch
hashes were identical across the three repetitions. No timing observations were
discarded or rerun. Timings include local retrieval and fusion on the deployment
runtime with one CPU and 2 GiB RAM; provider/network latency is excluded.

| Route | Legacy p95 | RRF p95 | Normalized p95 | RRF delta |
| --- | ---: | ---: | ---: | ---: |
| Direct | 1,217.42 ms | 1,205.52 ms | 1,205.23 ms | -11.90 ms (-0.98%) |
| Recall | 1,213.46 ms | 1,212.70 ms | 1,207.17 ms | -0.76 ms (-0.06%) |

Both policies passed the predeclared p95 allowance of baseline plus
`max(2 ms, 10% of baseline p95)` on both routes. The small measured reductions
are not a claimed speedup. The fusion experiment made **zero provider calls**.
Preparing its 84 missing query vectors once cost approximately **USD 0.000058**
under the shared #179/#180 budget; existing document vectors were reused.
Runtime fusion adds zero API calls relative to the current search path.

All arms had zero forbidden-source, duplicate-row, provenance, unexpected-policy
or numeric-protection violations. Every query had semantic and FTS results;
only one controlled and one source-derived query per route had keyword hits.
No benchmark query exercised the numeric-protected fallback. Its exact ordered
legacy parity is covered by separate synthetic runtime tests, including matching
numbers in source text; this is not field coverage for all amount/date requests.

The 24 no-answer/isolation queries still returned six or twelve context anchors
according to route, as the current search does not abstain. Their absence of a
labeled answer is not converted into a relevance or false-answer score. No
answer generation or Telegram delivery was tested by this evaluator.

## Release contract and verification

`MEMORY_SEARCH_FUSION_POLICY` remains `legacy` by default. The accepted candidate
is `rrf`; `normalized` remains an evaluated alternative. The flag changes only
the merge. Invalid candidate input, unknown policy or a candidate exception
falls back to legacy. A standalone numeric query token together with keyword
hits deliberately retains legacy ordering. Returning the flag to `legacy`
restores the previous ranking without rewriting data. Activation requires the
usual reviewed release and private smoke; the offline report itself does not
record a deployment.

An independent provider-free audit reconstructed all 504 distinct ranked outputs
from the saved channel batches, checked metrics directly against frozen labels,
verified all 1,512 unique timing records and three-pass ranking stability, and
matched measured/current source hashes plus the original archive hashes.
The final authoritative isolated suite passed **947 tests, with one skipped**
(948 discovered). It includes actual collector/filter, numeric rescue, fallback,
deduplication and evaluator-validity checks.

Evidence fingerprints (private payloads remain outside Git):

| Artifact | SHA-256 |
| --- | --- |
| Source freeze | `d7dd7cf5cc643308b07a472876cff8c6728a901ae202c1f116578c324618f79a` |
| Frozen protocol | `c891a59c9f10f7b639e423155e6758be220768ee6e388700b84dbc3a22b6f3c6` |
| Evaluator | `abdfc08b44650299f737ff69d6b935532353f2e6da8bab7e219d92a01a3b17af` |
| Aggregate report | `ee730619e7a5ef1c7af855802674bfee57261ca20d8e8d8648ae8d9f884d580e` |
