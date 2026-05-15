# Local STT VPS Benchmark Spike

Status: benchmark/spike note for issue `#21`.

## Scope

Benchmark whether the current CPU-only deployment class can run useful local speech-to-text for Aigan without enabling local STT in production.

This spike did not change runtime configuration, deploy code, enable local transcription, use private chat audio, or store benchmark artifacts in the repository.

## Environment

Benchmark date: 2026-05-15.

Sanitized hardware profile:

- CPU: Intel N150.
- Logical CPUs: 4.
- RAM: about 15 GiB.
- GPU: none.
- Runtime isolation: separate Docker benchmark image and temporary workspace, not the live Aigan container or live data volume.

Toolchain:

- `whisper.cpp` commit `968eebe`.
- Build: Release, CPU backend, OpenMP, `-march=native`, 4 threads.
- Container base: Ubuntu 24.04 with build tools, `ffmpeg`, `espeak-ng`, and `/usr/bin/time`.
- Models tested: `tiny`, `base`, `small`.

`faster-whisper` was not benchmarked in this spike. The goal was to get a clean first CPU baseline without adding Python/native ML dependencies, model cache behavior, or CTranslate2 packaging to the result. It remains a future candidate only if whisper.cpp is not enough or if its API shape is materially easier for the adapter.

## Install And Storage Footprint

The isolated benchmark image was about `944,659,042` bytes. That includes Ubuntu, build tools, `ffmpeg`, `espeak-ng`, and other benchmark-only dependencies; it is not a recommended production image shape.

Temporary benchmark workspace after build, models, samples, and results:

| Component | Size |
| --- | ---: |
| Total workspace | 755 MB |
| Models directory | 681 MB |
| Samples | 15 MB |
| Results | 312 KB |
| `whisper.cpp` source/build checkout | 60 MB |
| `whisper.cpp` build directory | 15 MB |

Model files:

| Model | Bytes | Approx size |
| --- | ---: | ---: |
| `tiny` | 77,691,713 | 74 MB |
| `base` | 147,951,465 | 141 MB |
| `small` | 487,601,967 | 465 MB |

## Benchmark Inputs

No private Telegram audio or production media was used.

Inputs:

- Synthetic English speech generated with `espeak-ng`, looped to 30 seconds, 60 seconds, and 5 minutes.
- Synthetic Ukrainian and Russian speech generated with `espeak-ng`, each looped to 30 seconds for a coarse language spot-check.
- Public `whisper.cpp` JFK sample, 11 seconds, for a real English speech sanity check.

Important quality caveat:
`espeak-ng` synthetic Ukrainian/Russian is not a substitute for real user voice notes. It is useful for repeatable timing and a rough smoke test, but not a reliable production-quality estimate.

## Wall Time And Memory

English synthetic duration matrix:

| Model | 30s wall | 60s wall | 5m wall | Peak RSS range | Approx speed on 5m |
| --- | ---: | ---: | ---: | ---: | ---: |
| `tiny` | 3.00s | 6.75s | 47.22s | 178-293 MiB | 6.4x realtime |
| `base` | 5.48s | 11.33s | 1:02.07 | 285-401 MiB | 4.8x realtime |
| `small` | 22.06s | 41.12s | 3:32.75 | 753-868 MiB | 1.4x realtime |

JFK public English sample, 11 seconds:

| Model | Wall time | Peak RSS | Quality note |
| --- | ---: | ---: | --- |
| `tiny` | 2.46s | 177 MiB | Correct sentence, minor punctuation differences. |
| `base` | 5.02s | 284 MiB | Correct sentence. |
| `small` | 18.29s | 751 MiB | Correct sentence. |

Synthetic Ukrainian/Russian 30s spot-check:

| Case | Wall time | Peak RSS | Observed output |
| --- | ---: | ---: | --- |
| `uk` + `base` | 4.51s | 286 MiB | Repeated a bot-name-like token, not usable. |
| `uk` + `small`, fixed language | 16.62s | 753 MiB | Returned a music/noise-style marker, not usable. |
| `uk` + `small`, auto language | 38.18s | 753 MiB | Empty output. |
| `ru` + `base` | 24.41s | 287 MiB | Only a bot-name-like token, not usable. |
| `ru` + `small`, fixed language | 24.55s | 753 MiB | Only a bot-name-like token, not usable. |
| `ru` + `small`, auto language | 39.96s | 753 MiB | Repeated English-looking token, not usable. |

## Findings

- CPU speed is not the blocker for short clips. `tiny` and `base` are comfortably faster than realtime for synthetic English, including 5-minute audio.
- `small` is still faster than realtime on the 5-minute synthetic clip, but the latency and memory jump are large enough that it is not attractive as a default on this hardware.
- `base` looks like the best speed/memory middle ground for a local experimental backend.
- `tiny` is fast and low-memory but visibly lower quality on synthetic English, especially names and domain terms.
- The synthetic Ukrainian/Russian spot-check was poor across `base` and `small`. Because the input was synthetic TTS, this does not prove real Ukrainian/Russian voice notes will fail, but it is enough to avoid enabling local STT for multilingual production use without real sample validation.
- A benchmark-only image with build tools is too large to copy into the production Aigan image as-is. A production local-STT path should use a small runtime-only image, sidecar, or prebuilt binary/model cache.

## Recommendation

Keep OpenAI transcription as the production default.

Do not enable local STT for user-facing Telegram/media transcription yet.

The reasonable next local-STT path is:

- implement the transcription backend adapter with `openai` default and `disabled` fallback;
- keep local backend modes behind explicit env flags;
- if local mode is added, start with `whisper.cpp` `base` as an experimental/admin-only or diagnostics backend;
- require real Ukrainian, Russian, and English voice-note samples before any production local backend is advertised to users;
- do not add `small` as the default on this hardware unless quality gains are proven on real samples.

## Future Test Plan

- Re-run with real, consented short clips in Ukrainian, Russian, and English.
- Add quantized model variants if quality/speed tradeoffs matter.
- Test VAD/chunking behavior on mixed silence and speech.
- Compare a runtime-only whisper.cpp sidecar image against bundling local STT into the Aigan image.
- Benchmark `faster-whisper` only if there is a concrete reason to prefer its Python/CTranslate2 API or accuracy profile.
- Measure cold-start latency separately from warmed repeated runs.

## Acceptance Mapping For Issue #21

- Install/storage footprint was measured for the benchmark image, workspace, and models.
- RAM and wall time were measured for 30s, 60s, and 5m clips across `tiny`, `base`, and `small`.
- Ukrainian/Russian/English output was spot-checked without private audio.
- Recommendation: keep cloud-only for production, with `whisper.cpp base` as a possible future diagnostics/offline fallback after real multilingual validation.

## Sources

- https://github.com/ggml-org/whisper.cpp
- https://github.com/SYSTRAN/faster-whisper
- https://developers.openai.com/api/docs/guides/speech-to-text
