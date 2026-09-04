# Astra integration and auxiliary model assessment

Assessment date: 2026-09-04. This change preserves Aigan's existing model responsibilities and replaces Sol only in its three configured roles.

## Scope and current orchestration

The inspected deployment uses `MODEL_ROUTING_MODE=shadow`. The classifier records proposed economy, balanced, and premium assignments; it does not select the answer model. The existing Agents SDK `RunConfig.model` remains the primary answer-model seam. See [the shadow routing contract](model-routing-shadow.md).

| Configuration | Before | After |
| --- | --- | --- |
| `OPENAI_MODEL` | `gpt-5.6-sol` | `gpt-6-astra` |
| `MODEL_TIER_PREMIUM_MODEL` | `gpt-5.6-sol` | `gpt-6-astra` |
| `VISION_INTERACTIVE_MODEL` | `gpt-5.6-sol` | `gpt-6-astra` |

The existing OpenAI credential remains the authentication source. Routing mode, tool availability, auxiliary models, memory retrieval, and embedding configuration are unchanged. Astra supports `low` reasoning, but does not support `none`; primary and interactive vision requests retain compatible reasoning settings. [Astra model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra)

Reaction handling has two distinct paths. Custom emoji and sticker asset interpretation uses the background vision model. Ordinary automatic-response admission first uses deterministic rules, then calls `run_agent_for_outbound`, which uses the primary answer model. It is therefore incorrect to describe every reaction operation as a separate inexpensive model call.

## Current prices and role choices

The following are current Standard rates in USD per million tokens for short-context requests. They are token rates, not measured end-to-end task costs.

| Model | Input | Cached input | Cache write | Output |
| --- | ---: | ---: | ---: | ---: |
| GPT-6 Astra | 10.00 | 1.00 | 12.50 | 50.00 |
| GPT-5.6 Sol | 4.00 | 0.40 | 5.00 | 20.00 |
| GPT-5.6 Terra | 2.00 | 0.20 | 2.50 | 12.00 |
| GPT-5.6 Luna | 0.20 | 0.02 | 0.25 | 1.20 |
| GPT-5.4 mini | 0.75 | 0.075 | No additional write charge | 4.50 |
| GPT-5.4 nano | 0.20 | 0.02 | No additional write charge | 1.25 |

Sources: [current pricing](https://developers.openai.com/api/docs/pricing), [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra), [Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), [Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna), [mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini), and [nano](https://developers.openai.com/api/docs/models/gpt-5.4-nano).

Sol's listed promotional rates are available at least through November 21, 2026. The July evidence snapshot used Sol at $5/$0.50/$30, Terra at $2.50/$0.25/$15, and Luna at $1/$0.10/$6 for input/cached/output. Current input/output rates are lower by 20%/33.3%, 20%/20%, and 80%/80%, respectively. Astra's current input/output rates are 2.5 times Sol's current rates at equal token counts; an Aigan task-cost reduction has not been established.

For GPT-5.6 and Astra, requests above 272,000 input tokens use twice the input/cache rates and 1.5 times the output rate for the full request. Cache writes cost 1.25 times ordinary input. Changed cache matching and write charges mean Luna and nano are not automatically equal-cost per request despite equal headline input rates. Compare actual cached tokens, write tokens, reasoning/output tokens, and retries. [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)

| Responsibility | Existing model | Assessment |
| --- | --- | --- |
| Model-policy classifier, tool router, candidate economy tier | GPT-5.4 nano | Luna is the strongest near-price candidate; retain nano until role-specific evaluation passes. |
| Candidate balanced tier and image-operation authorizer | GPT-5.6 Terra | Keep Terra; current rates are already 20% below the July snapshot. |
| Image-intent classifier | GPT-5.4 mini | Compare Luna as a cheaper candidate; improved classification is unproven. |
| Background vision, reaction asset interpretation, candidate-image review | GPT-5.4 mini | Evaluate Luna on these visual outputs separately. Terra costs 2.67 times mini at equal token counts. |
| Memory-extraction v2 research | Luna/low candidate | Existing development evidence remains `NO_GO`; this is not an approved runtime worker. |
| Memory embeddings | `text-embedding-3-small`, 512 dimensions | Evaluate the same model at 1536 dimensions in a separate complete index before considering a model change. |
| YouTube transcription | `gpt-4o-mini-transcribe` | Keep current model; consider GPT-Transcribe only through a measured transcription comparison. |

Visual comparisons must fix image dimensions and `detail`: GPT-5.4 and GPT-5.6 use different detail/resizing behavior, so changing only the model with `auto` can change both quality and input size. [Images and vision](https://developers.openai.com/api/docs/guides/images-vision)

Embedding-small costs $0.02 per million input tokens at either 512 or 1536 dimensions; 1536 dimensions use three times the vector storage. Embedding-large costs $0.13, or 6.5 times as much. Changing dimensions or models requires compatible query/document vectors and retrieval evaluation; relabeling existing vectors is invalid. A quality gain from increasing dimensions has not been demonstrated for Aigan. [Small](https://developers.openai.com/api/docs/models/text-embedding-3-small), [large](https://developers.openai.com/api/docs/models/text-embedding-3-large), [dimensions](https://developers.openai.com/api/docs/guides/embeddings)

Mini-transcribe is approximately $0.003 per minute; GPT-Transcribe is $0.0045 per minute, a 50% increase. GPT-Transcribe supports context, keyword hints, and multiple language hints, making it a candidate for multilingual or domain-specific audio, but it is not a same-price replacement. [Pricing](https://developers.openai.com/api/docs/pricing), [GPT-Transcribe](https://developers.openai.com/api/docs/models/gpt-transcribe)

## Development comparison: nano and Luna

A single development pass used the first six cases from each of six task classes in the existing public 120-case routing fixture: 36 cases per model. This already-observed fixture was not a new holdout. Requests were interleaved with concurrency three, reasoning `none`, output cap 240 tokens, confidence threshold 0.75, timeout 40 seconds, no SDK retries, and provider storage disabled.

- Source fixture: `tests/fixtures/model_routing_v1.jsonl`.
- Source SHA-256: `9c38f0b033889b67b4ba869b3224a094abc3b18155b4f3c6f9fb1262103ec8a5`.
- Canonical selection SHA-256: `107998279e863677bc75570588a397ea837e0ca9aacff33cb801223f7a284cad`.

| Metric | GPT-5.4 nano | GPT-5.6 Luna |
| --- | ---: | ---: |
| Task-class macro-F1 | 0.821978 | 0.880037 |
| Valid structured responses | 36/36 | 35/36 |
| Provider timeouts | 0 | 1 |
| Unsafe downgrades after application policy | 0 | 0 |
| Low-confidence premium fallbacks | 14 | 0 |
| Observed latency p50 | 1429 ms | 1431 ms |
| Observed latency p95 | 2156 ms | 3993 ms |
| Known estimated cost | $0.0078382 for 36 responses | $0.0075146 for 35 responses |

Luna's failed attempt raised `APITimeoutError` after approximately 40.05 seconds and forced a premium fallback because the router failed. Its provider cost is unknown and excluded from the known-cost subtotal; this comparison does not establish a lower total cost for Luna. The timeout's synthetic confidence of zero is excluded from the low-confidence fallback count; none of Luna's 35 valid responses triggered that fallback.

Both candidates remain **NO_GO**: macro-F1 was below 0.90, and Luna also missed 0.99 structured validity. Higher observed F1 and fewer low-confidence fallbacks make Luna worth further investigation, but one pass provides neither stability evidence nor production-quality assurance. The next useful step is a full repeated development comparison, followed by a new untouched holdout only after candidate selection. No auxiliary assignment changes are included here.

The earlier [memory-extraction v2 evaluation](memory-extraction-eval-v2.md) also remains authoritative for its own workload: Luna/none failed its screen, while Luna/low and Terra/low later failed development gates. Price changes do not reopen those quality conclusions by themselves.

## Pricing evidence and acceptance boundary

`model_pricing.py` remains the frozen July price catalog used by historical evaluation evidence. `runtime_model_pricing.py` contains the current catalog for runtime estimates and new measurements. Stored historical estimates and published evaluation results are not rewritten using current prices.

Synthetic API checks passed for an Astra direct response, an Agents SDK function-tool round trip, and vision input using the existing SDK. These checks establish basic account access and interface compatibility; they do not establish production answer quality, Telegram behavior, long-context cost, or live deployment acceptance.

The three Sol-role substitutions are prepared for review. Live application deployment and acceptance remain pending. Shadow routing and the auxiliary model assignments remain unchanged.
