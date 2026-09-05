# Primary capability development probes

This synthetic development screen checks the new primary tools with the actual installed Agents SDK and configured primary model. It is not a held-out quality estimate or a Telegram delivery acceptance test.

## Frozen run

- Model: `gpt-6-astra`, low reasoning, at most three model turns and 500 output tokens per request.
- SDK: Agents 0.18.1, OpenAI 2.45.0.
- Measured implementation: `9cb728d`; the subsequent removal of an unused guidance constant does not change the runtime prompt or tools.
- Fixture SHA-256: `43733f5605a77e86ee6b46da15d14900a784f5063af9d43220cadc47901ac47e`.
- Twelve fixed synthetic cases; temporary conversation store; real primary capability adapters. No live conversation data, Telegram sends or external agent channels.
- Provider requests reserve a conservative cost bound before dispatch against a shared USD 3 ceiling. Retries, provider storage and tracing were disabled.

## Observed results

| Cohort | Cases | Passed | Checked behavior |
| --- | ---: | ---: | --- |
| Another participant continues a public album | 2 | 2 | Changed adjective, retained subject and confirmed five-image count |
| Direct request missed by initial classification | 2 | 2 | Correct subject and explicit requested count |
| Original-history lookup | 3 | 3 | Exact number/date retrieval and search followed by a surrounding window |
| Quoted, negated, private or external operation | 4 | 4 | No accepted image proposal |
| Missing antecedent | 1 | 1 | Inspected history and asked for clarification |

All 12 cases passed across 18 provider requests. Cost was **USD 0.160464**, with zero provider errors, quarantined cases or uncertain reserved cost. Median case latency was 2.80 seconds and maximum was 6.28 seconds; these small-sample timings are descriptive, not a latency guarantee.

The accepted image plans were inspected before any delivery; this evaluation sent **zero Telegram messages**. Automated host-flow tests separately verify dispatch through the existing image pipeline and one execution claim. Organic conversations and the operator's manual field quests remain separate acceptance evidence.
