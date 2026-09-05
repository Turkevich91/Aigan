# Explicit semantic history retrieval

Issue #191 extends `read_chat_history` with `semantic` and `hybrid`. Existing
`recent`, literal `search`, `around`, citations and chronological cursors remain
available. The primary model can request meaning search when literal wording
misses a candidate, then inspect its original evidence. Ordinary responses do
not automatically run this tool or request an extra embedding.

## Scope and ranking

The host fixes chat, current request row and request time. Optional participant,
authored-only and half-open date filters narrow that scope before ranking. An
explicit old date may search an old retained record; it does not change automatic
recall's rolling lookback or the retention policy. The retained archive is not
the complete Telegram history.

`history_retrieval.py` performs bounded reads and ranking without provider calls.
It uses the existing `text-embedding-3-small`, 512-dimensional index and the
existing RRF fusion owner, including deduplication and numeric keyword priority.
Semantic vectors must match the record's full canonical content hash, model,
dimensions, chat and request-time boundary. Invalid lengths, nonfinite values,
zero vectors and post-request index updates are excluded. No index is written.

The eligible scope is probed with a hard 8192-row cap plus one. An oversized
scope returns `scope_too_large`; it does not silently select the latest 8192.
Narrower dates or a participant can reduce the scope. Coverage reports retained
and indexed counts, unusable index entries, applied mode and fallback reason.

Unindexed bot replies remain available to literal search. Authored-only search
excludes forwarded rows, rejects vectors that mix authored and attached source
text, and does not use the mixed-field FTS channel. Original source text remains
separate from authored text; the safe forwarded flag survives citation rendering.

## Shared request budget

All history modes share four tool reads, at most 20 rows per result, 12000
serialized characters per result and 30000 characters per request. Metadata and
reference markers count toward those limits. Semantic candidates are trimmed
from weakest to strongest before surviving rows are displayed chronologically.
Only successfully emitted rows enter the existing immutable citation registry.

At most two embedding requests are reserved per session. Queries are limited to
256 characters. Completed successful query vectors can be reused within that
session; concurrent calls still reserve their own bounded request slots. The
awaiting timeout is ten seconds and the explicit tool's transport timeout is
eight seconds, with retries disabled. Automatic context and indexing retain
their existing transport policy.

Provider admission checks the complete scope cap and stops vector validation at
the first fully valid vector. This boolean check authorizes no source and claims
no complete index counts. The separate aggregate preflight API remains available
for diagnostics. Admission is skipped when the query vector is already cached,
no provider is configured, or the embedding budget is exhausted. Every path
still performs fresh final retrieval. After a provider await, retrieval
reads a fresh snapshot rather than caching source content across that await.
Fresh scope refusal or an unusable index takes precedence over a provider-budget
fallback; exhausted embedding calls remain reported in the usage count.
Cancellation cannot publish candidates from a continuing SQLite worker. Failed
or timed-out embeddings retain literal search with a specific fallback reason;
they do not expose provider exception text. Aggregate diagnostics contain no
query, source text, identities or vectors. Existing embedding telemetry records
actual provider usage when a query is made.

The message store has no edit-revision history. These boundaries govern retained
message identity/creation time and embedding freshness, not reconstruction of a
historical text revision. Canonical citation digests reject edits and deletion
after evidence was shown.

## Validation and limits of the evidence

Synthetic geometry tests cover multilingual development shapes, alias/typo
queries, misleading recent neighbors, numeric rescue, invalid indexes, scope
filters, shared budgets, cancellation, citations and SDK query refinement.
They verify retrieval contracts and do not establish linguistic embedding
quality. A scripted ordinary SDK response makes zero history or embedding calls.

Semantic neighbors have no calibrated answerability threshold. A nonempty
result is a candidate, not proof that it answers the question. The primary
model must inspect evidence and abstain or refine when appropriate. Frozen
previously observed retrieval cases are regression evidence, not a new untouched
holdout; independent task-success and token-cost probes remain a separate gate.

No model roles, embedding dimensions, database schema, index contents or global
lookback are changed by this feature. Rollback is compatible with the existing
index and database. Disabling the existing vector configuration makes these
explicit modes report literal fallback; ordinary history access remains usable.

## Cached regression replay

A provider-free replay reused the previously observed #176/#180 frozen archive:
5420 retained records, 4776 small/512 embeddings and 84 verified cached query
vectors. Original archive and vector hashes were verified before reading; the
historical files were mounted read-only and a disposable copy was used. There
were **zero new provider calls, tokens or dollars**, and no Telegram delivery.

The main replay included 81 queries with a fixed request cutoff and an explicit
date lower bound matching the automatic backend's 30-day filter. Three original
trigger-boundary controls used their actual retained trigger ID/time separately.
The 60 positive cases are paired below; this is observed regression evidence,
not a new holdout or a test of generated answer quality.

| Retrieval path | Controlled hit@6 (48) | Source-derived hit@6 (12) |
| --- | ---: | ---: |
| Existing automatic hybrid/RRF backend | 30 | 10 |
| Explicit hybrid history tool | 30 | 10 |
| Explicit semantic history tool | 34 | 10 |

Hybrid preserved each positive case's hit/miss result. Semantic gained 8.33
percentage points on controlled cases versus the hybrid baseline; the paired
case-bootstrap 95% interval was +2.08 to +16.67 points. This describes the frozen
cases, not independent families or production success. The encoder and its dimensions
did not change. The source-derived cohort was unchanged.

All three paths returned neighbors on all 12 constructed no-answer cases. A
nonempty result therefore remains insufficient evidence of answerability.
There were no duplicate results. Cross-chat and explicit date-boundary controls
returned no prohibited rows; the three separate trigger-cutoff controls also
returned no prohibited rows. The new hybrid tool returned the three bot replies
that the old automatic-prefetch fixture excluded. Focused checks confirmed all
three were in the permitted chat/date/cutoff scope: conversational history
intentionally allows bot replies. Those bot controls have different eligibility
rules and are not claimed as unchanged backend isolation behavior.

One cached pass on one CPU measured the original integrated implementation:

| Path before admission optimization | Median | p95 |
| --- | ---: | ---: |
| Existing hybrid backend | 863 ms | 1213 ms |
| Explicit hybrid tool, including publication | 2025 ms | 2081 ms |
| Explicit semantic tool, including publication | 2022 ms | 2142 ms |

A focused CPU profile identified full vector normalization in both aggregate
preflight and final retrieval as the dominant extra work. Provider admission now
uses the boolean early exit described above; full post-await validation remains.
One bounded six-case comparison, alternating arm order, preserved the entire
emitted payload in 6/6 cases except random reference tokens. Median latency fell
from 2058 to 1371 ms (33.4%); observed ranges were 1973–2135 and 1332–1421 ms.
This small comparison does not establish a new p95. The broad quality replay was
not repeated after the shared-validator refactor. Neither timing includes
provider network latency or primary-model generation cost.
