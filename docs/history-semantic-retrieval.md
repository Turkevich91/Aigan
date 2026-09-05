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

Preflight occurs before any provider call. After a provider await, retrieval
reads a fresh snapshot rather than caching source content across that await.
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
