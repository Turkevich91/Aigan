# Specialist model evaluation

[Issue 242](https://github.com/Turkevich91/Aigan/issues/242) covers a single release of qualified specialist assignments. Synthetic requests used the current Responses adapters, request schemas, application normalization and provider-reported model IDs. No fixture used conversation history from real chats.

| Role | Comparison | Result |
| --- | --- | --- |
| Reminder tool selection | GPT-5.4 nano and GPT-6 Luna | Both matched all 16 expected intents and tool permissions. Median provider time was 0.940 and 0.825 seconds respectively. Estimated costs were $0.00210 and $0.00098 for 16 decisions. Luna used explicit `none` reasoning within the existing 120-token allowance. |
| Image intent | GPT-5.4 mini and GPT-6 Luna | Both matched all 18 delivery decisions. Luna invoked the separate authorizer 14 times versus nine with mini and raised the median pipeline time from 2.06 to 2.80 seconds. Keep mini. |
| Memory recall intent | GPT-5.6 Luna and GPT-6 Luna | Existing Luna matched the history-access expectation 64/64 times. New Luna matched it 57/64 times and unnecessarily requested history on questions about the current reply. Keep the existing model. |
| Image-operation authorization | GPT-5.6 Terra and GPT-6.1 Terra | Both matched 28/28 decisions. Median latency rose from 1.60 to 2.55 seconds. Keep GPT-5.6 Terra. |
| Reaction descriptions | GPT-5.4 mini and GPT-6 Luna | Both recognized all eight controls and four target emotions in five additional stickers. Luna avoided unsupported emotion tags on neutral images and misstated two colors. Its median time on the eight controls was 1.25 seconds versus 1.75 seconds. |
| Candidate image review | GPT-5.4 mini and GPT-6 Luna | Correct decisions across eleven batches were 41/42 and 42/42. Batch medians were 2.74 and 2.82 seconds. Estimated cost fell from $0.02347 to $0.00299–0.00310. The slowest Luna batch took 5.15 seconds. Select Luna with this tail-latency tradeoff. |

These are small paired samples. Cost estimates use observed token usage and short-context Standard rates. The catalog preserves the rate version on new records. Historical evaluation snapshots keep their original rates.

The rollout reuses the existing provider, endpoints, structured outputs, tool gates, modality-specific budgets and admin model selectors. Separate per-chat and per-tool choices retain precedence. Data storage, cache policy, delivery, embedding dimensions, streaming, conversation state, transports and processing tiers have no migration in this release.

[OpenAI model rates](https://developers.openai.com/api/docs/models/gpt-6-luna)
