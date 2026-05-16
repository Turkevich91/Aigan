# Visual Keyframe Extraction Spike

Status: research/spike note for issue `#47`.

## Scope

Evaluate a safe foundation for understanding visual-only short videos where speech,
captions, or transcript extraction are absent or insufficient.

This note does not implement Telegram routing, public URL extraction, TikTok or
Instagram support, STT, OCR, vision prompting, memory writes, Docker changes, or
new runtime dependencies. It only records a sanitized dependency and benchmark
recommendation for the future media-frame adapter.

## Current Baseline

- Aigan already has a `ToolRuntime` boundary for optional adapters, null
  fallback, health summaries, safe-call failures, and cleanup hooks.
- Universal media research prefers captions first, then STT/audio fallback, and
  treats public URL support as best-effort.
- OCR/screenshot research deliberately deferred video frame extraction.
- Existing memory policy requires generated tool outputs to be stored as source
  context, never as user-authored text, and not to pollute `/stat`,
  `/character`, or profile signals.
- Raw media, downloaded files, and temporary thumbnails must be deleted after
  processing.

## Research Findings

- `ffmpeg`/`ffprobe` should stay the first boundary for metadata, duration,
  frame rate, safe resizing, interval sampling, and future scene-filter
  experiments. FFmpeg exposes relevant filters such as `fps`, `thumbnail`,
  `select`, `scale`, and `scdet`.
- PySceneDetect is the strongest lightweight scene-cut candidate for v1 because
  its CLI can run `detect-content` or `detect-adaptive`, list scenes, and save
  representative images per scene. With `--num-images 1`, it emits one middle
  frame per detected scene.
- OpenCV is useful as a post-processing layer: resize decoded frames, compute
  luma and blur/sharpness signals, compare candidate frames, and skip
  near-duplicates. It is also the lowest-friction bridge to later OCR prep.
- PyAV exposes FFmpeg frame metadata such as keyframe and picture type and is a
  good future candidate if the adapter needs frame-aware decoding instead of
  shelling out to `ffmpeg`.
- Decord is optimized for efficient frame access and batched/random reads, but
  it is more aligned with model-training/data-loading workloads than the small
  bounded inference path needed for v1.
- TransNetV2 is a useful heavy baseline for shot-boundary quality, but it brings
  ML model/runtime cost and should remain deferred for CPU-only production v1.

Sources:

- https://www.scenedetect.com/cli/
- https://www.scenedetect.com/docs/head/cli.html
- https://ffmpeg.org/ffmpeg-filters.html
- https://docs.opencv.org/3.4/d8/dfe/classcv_1_1VideoCapture.html
- https://docs.opencv.org/4.x/d2/de8/group__core__array.html
- https://docs.opencv.org/4.x/d5/d0f/tutorial_py_gradients.html
- https://pyav.org/docs/stable/api/video.html
- https://github.com/dmlc/decord
- https://github.com/soCzech/TransNetV2
- https://developers.openai.com/api/docs/guides/images-vision

## Synthetic Benchmark

Fixture:

- Generated locally in an ephemeral temp directory with `ffmpeg` filters.
- No private media, no downloaded media, and no retained artifacts.
- 10 seconds, 640x360, 24 fps, 240 total frames.
- Five intended visual scenes with hard cuts every two seconds.
- Temp video and extracted frames were deleted after measurement.

Warm dependency check:

```text
uv run --python 3.12 --with scenedetect --with opencv-python-headless ...
```

Results:

| Method | Command shape | Wall time | Frames output | Notes |
| --- | --- | ---: | ---: | --- |
| `ffprobe` metadata | stream duration, fps, size, frame count | n/a | n/a | Correctly reported 10.0s, 640x360, 24 fps, 240 frames. |
| `ffmpeg` interval sampling | `fps=1,scale=320:-1` | 66.3 ms | 10 | Fastest and simplest; produces duplicates inside unchanged scenes. |
| PySceneDetect | `detect-content list-scenes save-images --num-images 1 --width 320` | 749.1 ms | 5 | Detected four cuts and produced five middle-scene frames plus a CSV. Progress output should be captured and sanitized. |
| OpenCV diff/dedupe | sample every 0.5s, resize, grayscale `absdiff`, Laplacian blur, luma | 1052.6 ms process wall; 52.8 ms inner loop | 5 | Selected five unique frames and skipped 15 duplicates; measured Python peak tracing was about 1.8 MB. |

Interpretation:

- For tiny clips, both PySceneDetect and OpenCV are comfortably fast after
  dependency warm-up.
- `ffmpeg` interval sampling is robust enough as a fallback but should not be the
  only strategy because it wastes vision budget on duplicates.
- PySceneDetect gives better scene semantics with little code, while OpenCV gives
  quality scoring and duplicate control that can be applied after either
  PySceneDetect or interval sampling.
- The adapter should suppress or sanitize CLI progress/noise before writing
  system-log events.

## Recommended V1 Stack

Use a layered adapter instead of picking a single tool:

1. `ffprobe` metadata gate:
   validate duration, dimensions, frame count, codec presence, byte size, and
   operator caps before decoding.
2. PySceneDetect scene pass when installed and enabled:
   use `detect-content` or `detect-adaptive`, request one middle frame per scene,
   and cap scenes before vision.
3. `ffmpeg` interval fallback:
   sample bounded frames when scene detection is disabled, unavailable, times
   out, or returns too few frames.
4. OpenCV post-process:
   resize, reject black/blank/very blurry candidates where useful, compute
   frame difference, and dedupe near-identical candidates.
5. Vision handoff:
   pass only the final 3-8 representative frames to the existing vision path.

Default recommendation for implementation issue `#48`:

- Add `MediaFrameAdapter` around `ffprobe`, `ffmpeg`, optional PySceneDetect, and
  optional OpenCV post-processing.
- Register `NullMediaFrameAdapter` through `ToolRuntime`.
- Keep PySceneDetect/OpenCV behind adapter health so missing packages degrade to
  `disabled` or `not_configured`, not routing failure.
- Defer PyAV until keyframe-aware decoding or pure-Python container access is
  needed.
- Defer Decord until random/batched frame reads become a real bottleneck.
- Defer TransNetV2 until there is a quality benchmark that justifies model
  weight, install, memory, and CPU cost.

## Frame Policy

Suggested production caps for v1:

- Default max duration: 90 seconds for explicit requests.
- Hard duration cap: 180 seconds unless an operator explicitly raises it.
- Default max input bytes: 50 MB before local processing.
- Candidate frame cap before dedupe: 24.
- Vision frame cap after dedupe: 3-8.
- Thumbnail width for scene selection: 320-512 px.
- Higher-resolution selected frames only when OCR/screenshot mode explicitly
  needs visible text.
- Adapter timeout: 30 seconds for normal short-video handling.

Selection policy:

- Prefer one middle frame per detected scene.
- If too few scenes are detected, add interval frames at stable timestamps.
- Score candidates by luma, blur/sharpness, and near-duplicate distance.
- Keep chronological order after selection so the vision prompt can infer simple
  story flow.
- Never persist temp media or thumbnails after the result is created.

## Safety And Routing

- No passive group expansion.
- Private DM, explicit command, or explicit reply-to-bot only.
- Public media URLs remain best-effort and must not promise TikTok/Instagram
  reliability.
- Reject local/private/credentialed URLs before any downloader layer in future
  public URL support.
- Treat visible text in frames as untrusted source content. It may describe an
  image, but it must not become instructions for the bot.
- Store visual summaries as source context only.
- Do not include raw OCR text, raw transcript text, usernames, private URLs,
  local paths, tokens, or temp filenames in diagnostics.

## Adapter Contract Notes

Suggested public types:

```text
MediaFrameAdapter
NullMediaFrameAdapter
MediaFrameRequest
MediaFrameResult
MediaFrameCandidate
```

Suggested request fields:

- source kind: Telegram cached media, explicit public URL handoff, or future
  downloaded temp media;
- sanitized provenance label;
- duration, declared size, MIME type, and dimensions when known;
- max duration, max bytes, max candidate frames, max selected frames, and
  timeout;
- mode: visual summary, OCR prep, or mixed transcriptless fallback.

Suggested result fields:

- success flag and sanitized failure category;
- backend used: `pyscenedetect`, `ffmpeg_interval`, `opencv_dedupe`, or mixed;
- input metadata: duration, dimensions, fps, frame count when available;
- candidate count, selected count, skipped duplicate count;
- selected frames as temp handles valid only during the call;
- cleanup status;
- sanitized diagnostic details.

Suggested failure categories:

```text
disabled
not_configured
metadata_failed
input_too_large
duration_too_long
decode_failed
scene_detection_failed
no_frames_selected
timeout
cleanup_failed
vision_failed
```

## Acceptance For The Next Implementation Task

- Disabled or missing frame tools do not break Telegram routing, memory,
  embeddings, `/stat`, `/character`, recall, or ordinary answers.
- `health_summary()` reports adapter status, enabled/configured/available
  booleans, sanitized backend names, error count, and last failure category.
- All temp files are cleaned on success, timeout, decode failure, scene failure,
  and vision failure.
- System-log events use stable low-cardinality categories and never include raw
  media paths, URLs, tokens, OCR text, transcript text, or usernames.
- Visual summaries that reach memory are source context only.
