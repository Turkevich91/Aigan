# Telegram turn assembly

Issue: [#185](https://github.com/Turkevich91/Aigan/issues/185).

With `TELEGRAM_TURN_ASSEMBLY_ENABLED=true`, an eligible incoming message opens a
fixed two-second collection window. `TELEGRAM_TURN_ASSEMBLY_SECONDS` can select a
window between one and two seconds; its default is two. The feature defaults off.
Appending another fragment never moves the first deadline.

An authored comment followed by a forwarded report in that window becomes one
request. Short authored text fragments are preserved in order as the request;
forwarded text and captions remain source material. Forwarded instructions are
never promoted into the trusted request. The adapter supports either arrival
order and existing photo/album dispatch.

Assembly is scoped to the same sender, chat, topic, reply target and business
connection, with compatible message timestamps so an old update backlog is not
mistaken for a burst. Private messages can start a turn. Groups require an explicit bot
invocation to open a window; only that sender's compatible fragments can join it.
Ordinary messages outside such a window remain passive. A new topic or reply
target opens a separate turn only when it independently has invocation admission.
After the deadline, an unmentioned group fragment also stays passive; a following
group turn needs a fresh invocation. Private messages independently admit the
following turn. An already-correlated legacy pending request retains ownership
of its source, so the new collector cannot create a duplicate answer.

At the deadline the adapter seals an immutable turn. A later message starts a
following window; it cannot modify the active generation. Persistence completes
before dispatch, and assembled turns retain their order within each chat even
when an earlier media download is slow. Ingress schedules application-owned
tasks and returns without awaiting generation. The Telegram application retains
sequential update ingestion; no global concurrency switch is enabled.

Accepted assembled turns bypass the legacy prompt-similarity and cooldown drops:
a later clarification is an admitted following request, including when the
previous answer finished quickly. Replayed Telegram message IDs are suppressed
within a bounded 2,048-message identity cache. Each turn is bounded to ten parts,
ten media messages, the configured maximum request text and 12,000 source-text
characters. A full turn keeps its deadline; another independently invoked part
starts a following turn. At most four turns per cohort and 64 total can remain
outstanding. Oversized single messages and a full queue receive a concise notice.

Edited updates refresh their stored observation and complaint signals without
changing the admitted request or starting another answer. Complaint observation
also runs for collected messages before ingress returns.
Delayed messages arriving out of order are also observed without starting a
generation. Unadmitted group sources are observed in the adapter, so their text
cannot fall through to the legacy invocation parser.

`MemoryStore` records a versioned host-only relation in the existing `raw_note`
field of the delivery trigger. It contains at most ten canonical authored row
IDs and text digests, preserving the original message text and ordinary note.
Generic note writes cannot create the reserved relation. Later note/media writes
preserve it. The reader checks actor, chat, reply target, order, turn cutoff and
current digests; edits, deletion or invalid metadata disable continuation
evidence instead of falling back to the first fragment. Only original authored
text rows qualify: bot/forward/source rows, media rows and generated placeholders
are excluded. An authored text comment plus a separate forwarded source remains
supported, including when the source is the delivery trigger. These relations
never enter model input. A later participant replying to the same chat's verified
image album can therefore continue the full assembled subject through #183.

`AiganApplication.stop` closes admission and cancels pending resolvers before
python-telegram-bot drains its tracked tasks. Already admitted persistence and
dispatched operations finish normally. Updates already fetched during shutdown
retain their text observation without creating more generations. Shutdown does
not retry Telegram sends or infer that cancellation means nothing was sent.
Disabling the feature
restores the existing pending-context and duplicate/cooldown behavior. There are
no model, index, schema, provider-budget or SDK changes.

The acceptance tests use synthetic messages, a controlled clock and mocked model,
persistence and delivery boundaries. They cover the 1.9/2.1-second split, unchanged
deadline, reversed forward/comment order, text and photo sources, authored bursts,
group admission, cohort separation, duplicate updates, queue/size caps, persistence
failure, cancellation before task start, ordered dispatch and trust boundaries.
Integration regressions also exercise the actual Application stop hook, edited
updates, complaint observation and persisted burst-to-album continuation by a
different participant. Ordinary group participants remain passive outside an
invoked cohort; administrators have no special assembly admission. Forwarded
or generated messages cannot invoke the bot on their own in a group.

The Bot API exposes text, media captions, forwarding metadata and media-group
identity separately. It does not promise one universal client ordering or a
shared message for an authored comment and forwarded post:
[Telegram Message](https://core.telegram.org/bots/api#message).
The application task behavior follows
[python-telegram-bot Application.create_task](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html#telegram.ext.Application.create_task).
