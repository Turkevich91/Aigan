# Randomization

`randomizer.py` owns bounded integer sampling, selection from supplied entries,
and shuffling. It uses Python `SystemRandom`; the model supplies parameters,
never the outcome. There is no seed, persistence, result cache across requests,
or rejection of a particular number. Independent draws may repeat.

`randomizer_capability.py` owns one operation and its result per request.
`agent_capabilities.py` supplies a thin SDK adapter. `main.py` owns admission,
current-request context and literal delivery. The tool is available independently
of history/image recovery and reminder feature flags.

## Conversation and result ownership

- Return the number or list directly. Explain the method only when asked.
- Missing bounds or entries produce a clarification. Invalid parameters and
  execution failures produce a short, truthful response, never a guessed value.
- Successful sampling stops the SDK run before a further model response.
  The host formats the actual result and sends strings as literal text.
- One request consumes at most one operation, including an unsuccessful one.
  A new admitted request creates a fresh session.

A conservative recognizer marks explicit authored draw/shuffle requests as
requiring the named tool. The host rejects a model-only outcome for these
requests even if a provider ignores tool choice. Negated requests, quotations,
translations and questions about how randomness works are outside this hint.
The recognizer is intentionally partial: other formulations can still use the
same tool through normal model selection. It does not parse numbers or lists.

An isolated repeat such as “again” uses a verified same-chat successful reply
and its original request, or the most recent successful randomization in the
existing short-lived answer context. A bare previous number is not evidence of
a randomizer call. The host does not replay truncated parameter lists. Fresh
randomization requests bypass content-similarity suppression; existing update
and in-flight duplicate protection still applies.

## Operations and limits

| Operation | Behavior |
| --- | --- |
| `integers` | Inclusive bounds and a positive integer step (default 1); replacement defaults to true. |
| `choose` | Exact supplied text; replacement defaults to false. Equal strings at different positions remain distinct entries. |
| `shuffle` | A permutation of the entire supplied list, preserving duplicates. |
| `clarify` | No sampling; ask for the missing range or list. |

Maximum 100 outputs, 200 input entries (100 for shuffle), 200 characters per
entry and 10,000 aggregate input/output characters. Integer endpoints are
bounded to the exact JSON integer range `[-(2**53-1), 2**53-1]`. Source lists
are not mutated. Arbitrary word generation and built-in dictionaries are not
part of this capability. Stepped ranges include only values on the progression:
for example, a step that does not reach the upper bound stops below it. Multiple
dice are independent integer draws with replacement and retain each roll.

## Actor contract

This adds no entitlement or administrative role. Existing chat and invocation
gates remain authoritative.

| Actor/path | Intended behavior | Enforcement |
| --- | --- | --- |
| Admitted private-chat participant | Draw or choose from their supplied parameters | Existing admission, request-local session, validated adapter |
| Admitted group invocation/reply | Same capability in the current group | Existing invocation gate; same-chat repeat evidence |
| Ordinary uninvoked group chatter | No response or tool execution | Existing passive ingress path |
| Denied chat or bot-authored ingress | No newly admitted capability | Existing ingress checks |

Organization catalogs and object permissions do not apply: this capability has
no organizational role, durable object or externally chosen destination.

## Verification

Core tests use injected random sources for exact boundary and repeated-value
checks; they do not assert that successive random draws differ. SDK tests cover
actual tool execution, missing calls, failures, literal output, recovery-disabled
availability and compatibility with existing tools. Provider-backed synthetic
probes separately verify selection and delivery with the configured model;
scripted responses alone do not prove model tool selection.
