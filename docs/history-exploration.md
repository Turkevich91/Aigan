# Verifiable bounded history exploration

The primary capability path can cite original retained messages with checked
Telegram source links and explain the limits of a search. This extends #183
under #189/#190 and uses the existing `PRIMARY_CAPABILITY_RECOVERY_ENABLED`
switch. Disabling that capability path disables its model-callable history and
image-recovery tools. No schema, retention, embedding, or model-role change is
required for citations and coverage.

## Evidence and citations

Host-formatted memory lines receive request-local, content-version-bound
references. Only lines actually present in the memory blocks supplied to the
model are admitted. Candidates removed during context trimming are not sources.
This reuses already selected evidence without another provider request.

Original-history tool results receive equivalent versioned references, counted
inside the tool's row and response budgets. The host resolves at most three
references in the final answer and rechecks the original full-content digest.
Edited or deleted sources do not silently become links to different evidence.
Forwarded or quoted material is labeled separately from the sender's own text.

For supported supergroups/channels, the application constructs links from the
fixed chat and original Telegram message ID. Private dialogs and basic groups
receive dated source references without invented permalinks. Forwarded material
links to its retained copy in the current chat. Forum topic identity is not
reconstructed from missing historical fields.

Generated private Telegram message URLs cannot substitute for checked source
references. On history/citation replies, public Telegram message links are also
resolved through the citation path. Unrelated ordinary public-web answers retain
their existing URL behavior. Sources and coverage are fitted against the actual
Telegram chunk splitter, not merely the sum of maximum chunk lengths.

Reference validity proves source identity and freshness, not that every model
interpretation of it is correct. History and worker output remain untrusted
evidence, never instructions or authorization.

## Coverage and continuation

Recent and literal-search pages retain stable ordering by timestamp plus memory
row ID. An opaque cursor belongs to one session and restores its original
filters and page size. Changing filters starts a new search; a cursor with
conflicting selectors is rejected. New rows and late imports after the original
request cutoff are excluded from all pages.

Coverage distinguishes the requested date/participant filters, returned count
and date span, cumulative unique exposed records, further database matches,
rows omitted for the response budget, and truncated text. The final reply calls
this a selection from retained history. It must not call date endpoints proof
of exhaustively reading that period, or a subjective favorite the best item in
the entire archive.

The limits remain 20 records/read, 1,000 serialized characters/record,
12,000 response characters, four reads/run and 30,000 total response characters.
Cursor/reference/coverage metadata counts toward those limits. Around reads
remain bounded windows without chronological pagination. A cancelled async read
consumes its call attempt but publishes no unseen evidence and releases unused
response capacity.

## Validation and follow-ups

Offline regressions cover source version changes, cancellation, equal-time page
boundaries, budget omission, unexposed references, author/source separation,
actual SDK function calls and Telegram chunk delivery. Synthetic transports do
not send Telegram messages or establish field acceptance.

#191 and #192 extend this evidence interface with semantic retrieval and
connected reply branches. #193 evaluates cheaper research workers for history
and character coverage before choosing any runtime role. Existing pricing
snapshots used by older evaluations remain immutable.
