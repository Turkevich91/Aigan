# Grounded character observations

`CHARACTER_EVIDENCE_ENABLED=false` retains the existing `/character` implementation.
When enabled, the self-or-administrator command uses the same primary model with
one bounded, read-only history tool. The flag is an immediate rollback; there are
no schema, embedding or model-role changes.

The host fixes the current chat, resolved target author and persisted command
cutoff before the model runs. Ambiguous or unresolved usernames abstain; display
name aliases cannot expand the identity. Bot portraits, forwarded messages,
service commands, source bodies, future records and other authors are excluded.

The initial sample deduplicates text and interleaves dates across chronological
quartiles. It contains at most 20 messages and 10,000 serialized characters.
Follow-up reads use `ChatHistorySession`: at most four calls, 20 messages per call
and 20,000 further serialized characters. The total evidence ceiling is 30,000
characters. The SDK run has at most six turns, 1,800 output tokens per turn and a
120-second command timeout. These are runtime bounds, not a dollar-price promise.

The structured result contains at most three observable behavioral facets, short
supporting quotations, optional counterexamples and internal uncertainty. Each
reference must identify a target-authored record actually exposed to this run;
the exact quotation must occur in the exposed portion. Repeated observations
require examples on two dates. If some retained records remain unread, at least
one additional history inspection is required before publishing observations.
The prompt prioritizes two or three distinctive observations in direct, natural
Ukrainian, each developed in two to four sentences. Short evidence excerpts and
brief internal uncertainty reduce output demand without changing the generation
allowance; necessary negations and qualifications must remain intact.
The renderer joins independently supported paragraphs into a Ukrainian portrait,
without displaying category names or repetitive evidence-log labels. Each
observation allows up to 650 characters; a cited contrasting observation allows
300. The prompt asks for meaningful context and qualifications within the prose.
Internal uncertainty is not mechanically appended to every paragraph.

Up to three distinct source excerpts are copied by the host from validated,
actually examined text, with their dates. Multiline quotations remain valid
internal evidence but are skipped as visible examples: cropping a line could
remove its qualifying context, and paragraph packing could change it. Repeated
references do not fill the example allowance. Sources retain their original
language; the surrounding model-written portrait is requested in Ukrainian.
The entire grounded-character reply uses literal delivery: HTML and Markdown
inside either a source or generated prose cannot become Telegram formatting.
Other reply paths keep their existing formatting behavior. Before delivery the
host checks that complete paragraphs, examples and the coverage note survive the
actual reply chunker. If needed, it omits whole final observations and reports
that length limitation rather than cutting a quotation or losing the note.

One final note gives the unique examined/available count, the examined period and
the sample limitation. Internal row IDs are not printed. The prose remains
model-generated: contextual interpretation and Ukrainian paraphrasing are prompt
requirements, not hard language or semantic-entailment validators.

An invalid source, quotation, repetition claim or counterexample discards that
entire observation while retaining independently valid observations. No text or
examples from a discarded observation enter the renderer. Report-wide invalid
structure, missing required additional inspection, sparse evidence or no valid
observations still produces a qualified abstention. There is no automatic repair
model call and no unvalidated free-form narrative fallback. Reference validation
does not prove semantic entailment: the primary model still owns interpretation,
counterexample selection and uncertainty. Clinical diagnoses and psychometric
scoring are outside the prompt contract.
The session retains aggregate rejected-observation counts and fixed validation
reason codes through rendering. Partial rejection is recorded without rejected
prose, quotations or source identifiers.

Validation uses synthetic temporary stores and scripted SDK model responses with
network access disabled. It covers identity/permission isolation, date diversity,
deduplication, actual-exposure references and quotations, unread tails,
counterexamples, concurrency/budgets, structured output and command rollback.
Scripted contrasting profiles test evidence isolation, not measured model quality.
Actual-adapter provider probes and release review are separate activation gates;
each evaluation freezes its own cases and provider budget before dispatch.

## Initial actual-adapter screen (previous presentation)

Before the narrative renderer change, three fresh synthetic development cases ran once through the actual primary
adapter with Astra, low reasoning and the configured medium verbosity. The
1,800-token output limit and application evidence/history limits were retained.
Provider storage, tracing and retries were disabled for these private probes.

| Case | Examined / available | History reads | Provider requests | Time | Result |
| --- | --- | --- | --- | --- | --- |
| Cautious collaboration | 21 / 24 | 1 | 2 | 26.61 s | Five contextual facets with qualifying counterexamples |
| Action-first collaboration | 22 / 24 | 1 | 2 | 21.24 s | Five distinct facets and four counter-observations |
| Sparse single-day conversation | 4 / 4 | 0 | 1 | 3.32 s | Explicit abstention; no invented profile |

All three passed examined-source and exact-quote validation, coverage/budget
checks and the predefined qualitative screen. The two substantial portraits
distinguished checking evidence before action from testing through action; they
qualified inferred behavior and separated stated intentions from completed work.
Rendered output remained Ukrainian with dated references, without raw quotation
blocks, internal row identifiers, clinical labels or complete-history claims.

Five actual provider requests cost **USD 0.2317685**. Combined with #183, the shared
ledger recorded **USD 0.3922325 of USD 3**, with no uncertain reservations and no
Telegram sends. These are observed probe costs, not predicted production costs.

This is a small development screen, not a held-out population benchmark. It does
not establish performance on real chat histories or psychological validity. No
failed-case rerun or post-result semantic-policy tuning was used. The measured character
module SHA-256 was
`3323ee9f7b8a8cf63136b1467bcb52e9df6c2b9066b102ba1ce1e92ea6586a4a`;
the subsequent base update removed only unused image guidance and added #183
probe documentation, leaving the character adapter and its dependencies intact.
After the screen, the coverage label received a grammar-only change to a
count-neutral form; source selection, prompts, report validation and model
settings are unchanged. Original measured reports were preserved.
