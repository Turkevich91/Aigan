# Bounded memory extraction evaluation v2

## Status and decision

Issue #143 v2 is an offline evaluation bundle. It does not run from Telegram,
write SQLite, add a worker, alter vector/FTS retrieval, change prompt-memory
packing, or affect the Sol answer path.

The v1 prompt, fixtures, evaluator, hashes, and final `NO_GO` remain immutable.
Its holdout is permanently closed. V2 exists because v1 produced 358/360 valid
repeat outputs and exposed three contract gaps: candidate versus no-candidate
was not structurally exclusive, evidence whitespace was not always preserved,
and a correction could be linked to the wrong prior row.

The first v2 development screen matrix ran on source commit
`d3863098c01cd53909c21c1b524ab3e6df469837` on 2026-07-11. All three arms
completed without provider failures but failed the strict schema/safety gate.
No locked-development or holdout call followed; the v2 holdout remains closed.

The preregistered v5 prompt was superseded before any call by v6, which names
every strict-schema `candidate_type` with its exact enum token. The v6 screen
then exposed two missing transport instructions: canonical date-only expiry
encoding and unambiguous backward-only correction links. Prompt v7 restores
those rules without changing fixtures, labels, schema, validator, run matrix,
or holdout. Byte-identical v5 and v6 remain inactive audit artifacts at
SHA-256 `51ed7624b663001de77a0e219bde71e229d19b8cf953a479e5ebd07840e2af59`
and `f063b311df0ba62abd610ac58b62410db25fb1a6d1b3c423a800e9ae13d3aa63`.
The runner and manifest reference v7 exclusively.

## Ownership boundary

- `memory_extraction_v2.py` owns the v2 schema, strict adapter, deterministic
  baseline, frozen fixtures, phase gates, and aggregate report.
- `memory_extraction_selection_v2.py` recomputes the complete model matrix and
  selects the least expensive arm that passed every locked-development gate.
- `scripts/eval_memory_extraction_v2.py` is the explicit offline runner.
- `scripts/select_memory_extraction_v2_candidate.py` creates an aggregate-only
  development attestation from saved reports.
- Runtime `memory.py`, SQLite, FTS, embeddings, Telegram routing, and model
  configuration are outside this bundle and remain unchanged.

The evaluation bundle hash includes the v1 semantic validator, shared evaluator
helpers, shared pricing implementation, v2 schema/scorer, matrix selector, and
both v2 command-line runners. Prompt, fixtures, schema, baseline, price snapshot,
run matrix, and manifest are also frozen independently.

## V2 output contract

The root is an object with one required `result` field. `result` is a nested,
discriminated `anyOf`:

1. `kind="candidates"` and one through four candidates; or
2. `kind="no_candidate"` and one non-`none` rejection reason.

All objects are closed and every field is required. This follows OpenAI's
[Structured Outputs subset](https://developers.openai.com/api/docs/guides/structured-outputs):
the root is not an `anyOf`, while the nested union is supported.

The v2 adapter rejects values that the legacy validator would need to repair or
normalize. Evidence spans must match the cited raw field exactly, including
spaces, tabs, and newlines. Correction links may contain only the explicit,
older, same-chat `reply_to_row_key`; speaker identity determines whether that
link is a supersession or conflict.

## Frozen public-synthetic evidence

Development and holdout are separate 160-case files. Each has 40 Ukrainian,
40 Russian, 40 English, and 40 mixed-language cases; 112 are candidate-bearing
and 48 are expected negatives. Each split includes 24 exact-whitespace and 24
multi-prior correction cases.

The two splits use disjoint sentence templates, exact texts, case IDs, row IDs,
chat IDs, speaker IDs, and value pools. They contain no copied chat text, real
identities, URLs, secrets, private paths, or runtime artifacts.

Regular development tests parse only development labels. They treat holdout as
an opaque frozen byte file and verify its hash without loading its labels.

Frozen SHA-256 values:

```text
development file/cases  e00f9fccb134017c94a196121b1101eb70f5d7241a97dc0ce10a0c2c0af548b8
holdout file/cases      d9cc736f10e9a1196a500032532cc0cc76dadbf37dc2715199446a6e91cbc609
screen cases            938c07629cc3242e0b80e3339f30d4baaff549f7730dd1860a8e89b3e4cf6d18
prompt v7               22768ec62a69c0596af97e14a42960886d180bb06896d8402b197352c8d282c4
output schema v3        09838f4ccca1bf831c9ea7491e45a9d37a0a54b57b707904811d25c811f12cef
deterministic baseline  433994c6d44a8abcab7756f79794a0d8987ae874c8e5c5ceae94832eec55d80e
pricing snapshot        a8aebcd56703bf09c7f84cacf543d20c4bd3b7d532d6010e7db0ef9e9682a61c
run matrix              203ad336482d56dd3f1fe1995c24f8b089734dd85fc75da325a8aff330518673
evaluation bundle       26f4457465cf533d4b402f8290269954198975dfd505ade15fc2dd1813931852
whole manifest          a9de1e0f74bfef5e43d9858c3e3c70b9bf3939342d571b8527abe6a462bfda90
```

The manifest lists all 48 screen case IDs. That subset is exactly 12 cases per
language, 34 candidate-bearing and 14 expected-negative cases, with every
candidate/rejection class, 20 exact-whitespace cases, and 6 multi-prior
corrections.

### V6 screen result and v7 rationale

| Arm | Strict valid | Overall F1 | Hard violations | Estimated cost | p95 latency |
|---|---:|---:|---:|---:|---:|
| Luna / none | 45/48 | 0.8889 | 3 | $0.080292 | 9,884 ms |
| Luna / low | 44/48 | 0.8889 | 4 | $0.094266 | 11,970 ms |
| Terra / low | 44/48 | 0.9041 | 4 | $0.203850 | 10,012 ms |

All three had zero provider failures, zero expected-negative false positives,
complete model/effort/usage evidence, and `runtime_authorized=false`. Each
failed only `structured_valid_rate_100`,
`all_repeat_structured_valid_rate_100`, and `hard_safety_violations_zero`.
The two low-effort arms failed all four screen expiry cases solely because the
model returned a parseable but noncanonical `valid_until`. Luna/none also
produced correction-link direction errors.

V7 therefore makes the already-frozen representation explicit: a source-only
`YYYY-MM-DD` is encoded as `YYYY-MM-DDT00:00:00Z` for canonical transport and
does not claim that the source supplied a time. A later runtime design must
retain date precision or otherwise avoid treating this transport encoding as
an asserted midnight event. The complete three-arm screen matrix must be rerun
against v7; prior reports cannot admit a v7 locked-development run.

## Fixed model matrix

| Phase | Model | Effort |
|---|---|---|
| measurement only | deterministic v2 baseline | none |
| API arm | `gpt-5.6-luna` | none |
| API arm | `gpt-5.6-luna` | low |
| API arm | `gpt-5.6-terra` | low |

Luna is the lower-cost worker candidate and Terra is the higher-cost quality
ceiling. Sol remains the final-answer model and is only a same-usage cost
counterfactual. The frozen standard-processing rates are Luna $1/$0.10/$6,
Terra $2.50/$0.25/$15, and Sol $5/$0.50/$30 per million input/cached-input/output
tokens. Cache writes are captured and priced at 1.25 times ordinary input.
The evaluator tolerates both plural and singular input-detail field names and
still requires complete cached/cache-write metadata.
See the official [Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), and
[Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol) pages.

## Phases and gates

The deterministic baseline is diagnostic only. Its current development result
is 160/160 structurally valid with an exact candidate-set rate of 100%; twelve
negative rejection-reason mismatches remain diagnostic. Its verdict is always
`INCONCLUSIVE`.

The 48-case screen is a transport/schema/safety admission gate. Every request
uses one repeat, concurrency 6, a 45-second timeout, 1,200 maximum output
tokens, and `store=false`. It emits `PASS_FOR_LOCKED_DEVELOPMENT` only when all
48 outputs are terminal and strictly valid, artifact/config/model/effort/usage/
pricing evidence is complete and consistent, expected-negative false positives
and hard violations are zero, and measured cost is at most half of the Sol
counterfactual. It makes no small-sample Wilson quality claim.

Every screen-pass arm then runs 160 development cases three times with the same
configuration. Full development and holdout gates are conjunctive:

- 100% strict validity across all repeats;
- overall and durable precision and recall Wilson lower bounds at least 0.95;
- exact candidate-set rate at least 0.99;
- exact raw evidence and valid local attribution/provenance;
- zero expected-negative false positives and hard safety violations;
- candidate behavior, including rejection reason, stable on at least 99% of
  cases across repeats;
- exact frozen artifacts and run configuration;
- one actual provider model snapshot, matching effort, terminal calls, complete
  usage including cache writes, frozen rates, and exact cost recomputation;
- measured worker cost no more than half of the same-usage Sol counterfactual.

The first repeat supplies unique-case quality metrics. Later repeats measure
stability only and are not pooled as independent quality samples. Confidence is
reported through full-contract stability but is not candidate identity.

## Matrix selection and one-time holdout

A single development `GO_FOR_HOLDOUT_CANDIDATE` cannot open holdout. The
selection attestation recomputes all reports and requires exactly one screen
report for every arm plus one locked-development report for every screen-pass
arm. All reports must bind the same clean source commit and frozen artifacts.
Among complete passes, measured cost ranks first and p95 latency is only the
tie-breaker.

Holdout additionally requires the exact manifest and attestation hashes and the
selected model/effort/actual-model snapshot. Every artifact is rechecked before
an API client is created. Immediately before the first request, the runner
atomically creates a private external receipt with exclusive-create semantics.
On POSIX, both the receipt file and its parent directory are fsynced before any
provider request; a sync failure aborts the run and leaves the claim consumed.
An existing receipt, concurrent claim, crash, timeout, or provider failure
cannot be overridden and consumes v2 holdout. The aggregate result is written
to a different external file. Neither file belongs in Git, GitHub, or runtime
SQLite.

This document intentionally contains no runnable holdout command. Holdout is a
separate operator gate after matrix selection and source freeze.

Even a perfect holdout emits only `GO_FOR_RUNTIME_SHADOW_PR` with
`runtime_authorized=false`. A separate reviewed PR, fresh private backup, and
non-overlapping live observation gate are still required before any worker.

## Development commands

Regenerate only the development fixture:

```bash
python3 -m scripts.build_memory_extraction_fixture_v2
```

Run the offline baseline:

```bash
python3 scripts/eval_memory_extraction_v2.py \
  --mode baseline \
  --fixture tests/fixtures/memory_extraction_v2_development.jsonl
```

After committing a clean frozen source tree, run one screen per API arm and
save stdout outside the repository. Set `REPORT_DIR` to an existing absolute
directory outside the repository before running these commands:

```bash
: "${REPORT_DIR:?set REPORT_DIR to an external absolute directory}"
```

Example for Luna without reasoning:

```bash
python3 scripts/eval_memory_extraction_v2.py \
  --mode api \
  --fixture tests/fixtures/memory_extraction_v2_development.jsonl \
  --model gpt-5.6-luna \
  --reasoning-effort none \
  --limit 48 \
  > "$REPORT_DIR/luna-none-screen.json"
```

Only a screen-pass arm may run locked development:

```bash
SCREEN_REPORT="$REPORT_DIR/luna-none-screen.json"
SCREEN_SHA="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$SCREEN_REPORT")"

python3 scripts/eval_memory_extraction_v2.py \
  --mode api \
  --fixture tests/fixtures/memory_extraction_v2_development.jsonl \
  --model gpt-5.6-luna \
  --reasoning-effort none \
  --repeats 3 \
  --screen-admission "$SCREEN_REPORT" \
  --acknowledge-screen-admission-sha256 "$SCREEN_SHA" \
  --require-gates \
  > "$REPORT_DIR/luna-none-development.json"
```

Create the matrix attestation from all three screen reports and every required
development report:

```bash
shopt -s nullglob
REPORTS=("$REPORT_DIR"/*-screen.json "$REPORT_DIR"/*-development.json)

python3 scripts/select_memory_extraction_v2_candidate.py \
  "${REPORTS[@]}" \
  --output "$REPORT_DIR/selection-attestation.json"
```

The selector prints the canonical attestation SHA-256. Reports and attestations
contain aggregate metrics only; predictions, fixture rows, prompts, and model
outputs are never persisted by the evaluator.
