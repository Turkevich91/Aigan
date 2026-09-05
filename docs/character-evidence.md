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

The structured result contains at most five observable behavioral facets, short
supporting quotations, optional counterexamples and explicit uncertainty. Each
reference must identify a target-authored record actually exposed to this run;
the exact quotation must occur in the exposed portion. Repeated observations
require examples on two dates. If some retained records remain unread, at least
one additional history inspection is required before publishing observations.
The renderer provides dates and the unique examined/available record count.
It does not print internal row identifiers or claim complete-history coverage.
Exact quotations remain internal validation evidence. The user receives Ukrainian
observations and counter-observations with dated references, without raw quotes
that could conflict with the existing response-language policy.

Sparse or invalid evidence produces a qualified abstention. No free-form generic
profile is published after a reference-validation failure. Reference validation
does not prove semantic entailment: the primary model still owns interpretation,
counterexample selection and uncertainty. Clinical diagnoses and psychometric
scoring are outside the prompt contract.

Validation uses synthetic temporary stores and scripted SDK model responses with
network access disabled. It covers identity/permission isolation, date diversity,
deduplication, actual-exposure references and quotations, unread tails,
counterexamples, concurrency/budgets, structured output and command rollback.
Scripted contrasting profiles test evidence isolation, not measured model quality.
Actual-adapter provider probes and release review are separate activation gates;
provider probes share the existing combined evaluation ceiling with #183.

## Initial actual-adapter screen

Three fresh synthetic development cases ran once through the actual primary
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
