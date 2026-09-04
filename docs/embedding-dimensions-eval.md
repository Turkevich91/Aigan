# Embedding dimension development experiment

This experiment compares `text-embedding-3-small` at 512 and 1536 dimensions
using Aigan's current retrieval functions and an isolated copy of retained
memory. It never changes the running service, its environment, or its index.
The research decision is `INCONCLUSIVE_NO_RUNTIME_PROMOTION`.

## Corpus and evidence levels

The first frozen cohort contains 24 synthetic families, with two paraphrased
questions and one deliberately related distractor per family. Real retained
messages remain in the candidate pool. A separate cohort selects at most four
real sources from each of six mutually exclusive strata: image descriptions,
attachments, replies, mixed-script text, long text and short text. Selection
uses a deterministic hash before any embedding or ranking is inspected.

For these real sources, the existing Mini model proposes questions and exact
answer spans. A second, independent source-only call checks answerability.
Exact-span, answer-copy, title-copy and identifier checks reject invalid probes.
Rejected sources are not replaced. All accepted questions and labels are frozen
before either embedding arm runs. These are machine-checked development probes,
not naturally occurring user questions, independent human labels or a holdout.

An independent agent then reviews every generated source-question family without
seeing vectors or rankings. It can accept or reject the frozen question, but
cannot rewrite it or replace its source. A private `query-audit.json` binds these
decisions to the original query-freeze hash. The runner requires exactly one
Boolean acceptance decision for every generated family and writes a separate
reviewed-query freeze before embedding. Agent review does not create human gold
labels. The completed experiment's selection counts are recorded in the results
report, separately from the maximum planned cohort size.

Twelve constructed missing-answer questions measure exposure to retrieved
neighbors. The current retriever has no abstention threshold, so returning
neighbors does not establish a false answer or hallucination. Twelve isolation
cases exercise chat boundaries, bot rows, the lookback cutoff and current-message
exclusion. Deliberately indexed synthetic bot/old rows test filtering defensively;
normal indexing would exclude them.

A separate 24-case synthetic screen calls the actual recall-intent detector
with both dimensions and the current strong/ambiguous thresholds. It reports
changed decisions, correct decisions and degraded or administratively aborted
cases separately. This is a component screen, not full application routing or
field calibration, and no threshold is tuned.

## Execution and measurements

The private `prepare`, `generate` and `run` phases freeze source/config hashes,
snapshot bytes, selected sources, prompts and accepted labels. Two separate
databases receive freshly generated vectors for the same candidate rows.
Aigan's clipping, normalization, semantic search, keyword search, FTS and current
fusion remain unchanged. Both direct top-6 and memory-recall top-12 paths must
confirm they actually used embeddings and encountered no embedding error.

Results distinguish semantic ranking from the unchanged hybrid ranking. Metrics
are **known-source hit** at 1/6/12, reciprocal rank and known-source nDCG;
they are not exhaustive relevance recall. Preidentified duplicate real sources
share labels and bootstrap families. Synthetic paraphrase pairs share a family.
Paired family bootstrap intervals describe this development set only.

Single-query API wall time, batch indexing API time and local retrieval wall
time are separate. The evaluator alternates dimension order and warms each
index once. The benchmark container has a one-CPU quota and a 2 GiB memory
limit. The trial uses zero SDK retries and a 45-second API timeout, which differ
from the production adapter defaults; latency and failure results therefore do
not establish production end-to-end behavior.

The shared study ceiling is USD 0.50, including at most USD 0.20 for source
question construction/checking. UTF-8 byte bounds reserve input cost; generation
also reserves framing and maximum output. Provider usage settles known cost.
Failed calls keep their reserved unknown-cost bound. An incremental private
ledger preserves cost evidence if a run aborts. Runs do not retry or resume
silently, and existing freezes are never overwritten.

## Private operation

The CLI accepts an operator-provided SQLite snapshot and a new, owner-only
directory outside the repository. The source is opened read-only and copied
with SQLite backup; only the private copy is opened with `MemoryStore`.
Raw rows, generated questions, source identifiers, vectors and detailed results
remain private. Standard output contains hashes, counts and aggregate results.
No Telegram polling or delivery is started.

```sh
python scripts/eval_embedding_dimensions.py prepare --snapshot "$AIGAN_PRIVATE_SNAPSHOT" --output-dir "$AIGAN_PRIVATE_EVAL"
python scripts/eval_embedding_dimensions.py generate --output-dir "$AIGAN_PRIVATE_EVAL"
# Independent blind review writes query-audit.json in the private output directory.
python scripts/eval_embedding_dimensions.py run --output-dir "$AIGAN_PRIVATE_EVAL"
```

The audit must contain `query_freeze_sha256` (the SHA-256 of the unchanged
`query-freeze.json`) and a `decisions` array. Each entry contains the original
`family` and an `accepted` Boolean; private rejection reasons may also be retained.
Do not run the final command before this review or regenerate questions after
seeing rankings.

The embedding table stores only one vector per message. Increasing the
dimension in the live environment alone is not a migration: it can make older
vectors unavailable and overwrite the previous index during backfill. Any
future adoption needs complete coverage, a compatible index switch and a
verified fallback. Three times the vector bytes also does not imply three times
the entire database size or process memory.
