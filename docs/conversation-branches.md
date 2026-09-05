# Bounded connected conversation reads

`read_conversation_branch` lets the conversational primary inspect related original
messages after `read_chat_history` has exposed an anchor in the same run. It
follows retained Telegram reply references: closest ancestors first, then direct
replies and descendants breadth-first. Results render chronologically, with each
row's actual relation and depth in coverage metadata. Up to two nearby messages
can be requested explicitly; these remain labeled `neighbor`, never a reply.

This is a connected branch of retained reply edges. It does not reconstruct a
complete topic, historical forum, deleted conversation, or archive. A missing
parent, filtered node, cycle, depth cap, node cap, or character cap remains
explicit; the final citation footer marks partial branches. No thread IDs are
inferred from chronology.

## Authority and budgets

The host fixes the chat, current-request row/date cutoff, and any target identity.
Only an already emitted, unchanged evidence ID can anchor a branch. The session
revalidates its full original digest; the storage reader checks that digest again
under the same lock as selection. Edits outside the displayed text prefix also
invalidate old evidence. New references bind to the original content version.

Optional participant/date filters can only narrow the host scope. A fixed
character identity excludes other participants, bot text, commands, forwarded
bodies, and media-only rows; a filtered speaker cannot bridge into hidden context.
Conversational reads retain bot replies and forwarded copies with explicit source
attribution. All text remains untrusted evidence. No private paths, tokens, notes,
or raw forward-origin metadata enter the tool result.

All history tools share four reads and 30,000 returned characters per run. Each
branch has at most 20 messages (default 10), six ancestor/descendant levels, 1,000
serialized characters per row, and 12,000 characters including relations,
citations and coverage. Configuration can lower these ceilings. When space runs
out, optional neighbors and later admitted descendants are removed before nearer
ancestors and the anchor; all metadata and citation eligibility track only emitted
rows. A cancelled asynchronous read may finish its bounded SQL work but cannot
publish evidence or authorize a citation. Its claimed call remains spent.

Branches are not pageable. Use another exposed anchor when its context is needed,
within the same remaining budget. Existing chronological `around` and literal
search/cursor behavior remain available. The branch tool inherits the existing
primary-capability feature gate and admitted-message path, and performs no model
calls, Telegram sends, schema changes, embedding work, or writes.

## Offline evidence

Synthetic storage and session tests cover interleaved discussions, transport-ID
edges, missing/foreign/future anchors, cycles, edits between authorization and
selection, fan-out/depth bounds, shared concurrent/cancelled reads, character
attribution, exact serialized limits, citations and unchanged literal cursors.
A real installed Agents SDK test finds an anchor, reads its related context and
resolves a newly exposed source without provider or Telegram transport.

A local isolated benchmark used 5,000 synthetic retained messages, five warm-up
pairs and 40 measured pairs. The one-row anchor read took median/p95 5.373/5.525 ms;
its subsequent validated branch read took 10.636/11.092 ms. The bounded branch
returned 19 messages and 11,726 characters, with truncation disclosed. This is one
warm local CPU/SQLite fixture, not a production latency guarantee or quality
benchmark. Added evidence consumes primary-model input tokens; the tool adds no
provider call of its own and never expands the existing session character budget.
