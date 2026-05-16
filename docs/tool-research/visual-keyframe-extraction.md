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

Benchmark environment:

- Date: 2026-05-16.
- Runtime: sanitized local workstation, not the deployment VPS and not a Docker
  production-like benchmark.
- OS: Windows 11 Pro.
- Shell: PowerShell with `uv` invoking Python 3.12.
- Logical CPUs: 24.
- RAM: about 95 GiB.
- Python: 3.12.13 through `uv`.
- `ffmpeg`/`ffprobe`: 8.1.
- PySceneDetect: 0.7.
- OpenCV: 4.13.0.
- Package cache was warmed before measuring command runtimes.
- First warm fetch observed package archive sizes around NumPy 11.8 MiB,
  OpenCV 38 MiB, and OpenCV headless 38 MiB, plus smaller PySceneDetect CLI
  dependencies. These are package archive observations, not installed disk size
  measurements.
- Process-level peak RSS was sampled from the command process tree. It is good
  enough for a directional comparison, but it is still not a Docker or VPS
  capacity benchmark.

Warm dependency check:

```text
uv run --python 3.12 --with scenedetect --with opencv-python-headless \
  python -c "import cv2, scenedetect"
```

Sanitized benchmark recipe:

```text
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("aigan-keyframe-spike-" + [guid]::NewGuid().ToString("N"))
$video = Join-Path $tempRoot "synthetic-scenes.mp4"
$ffmpegDir = Join-Path $tempRoot "ffmpeg-sample"
$sceneDir = Join-Path $tempRoot "scenedetect"
$opencvDir = Join-Path $tempRoot "opencv-diff"

try {
  New-Item -ItemType Directory -Force -Path $ffmpegDir, $sceneDir, $opencvDir | Out-Null

  ffmpeg -hide_banner -loglevel error -y `
    -f lavfi -i "testsrc2=size=640x360:rate=24:duration=2" `
    -f lavfi -i "smptebars=size=640x360:rate=24:duration=2" `
    -f lavfi -i "color=c=black:size=640x360:rate=24:duration=2" `
    -f lavfi -i "testsrc=size=640x360:rate=24:duration=2" `
    -f lavfi -i "color=c=white:size=640x360:rate=24:duration=2" `
    -filter_complex "[0:v][1:v][2:v][3:v][4:v]concat=n=5:v=1:a=0,format=yuv420p[v]" `
    -map "[v]" -c:v libx264 -preset veryfast -crf 23 $video

  ffprobe -v error -select_streams v:0 `
    -show_entries stream=width,height,avg_frame_rate,duration,nb_frames `
    -of json $video

  ffmpeg -hide_banner -loglevel error -y -i $video `
    -vf "fps=1,scale=320:-1:flags=lanczos" `
    (Join-Path $ffmpegDir "frame_%03d.jpg")

  uv run --python 3.12 --with scenedetect --with opencv-python-headless `
    scenedetect -i $video -o $sceneDir `
    detect-content list-scenes save-images --num-images 1 --width 320

  $opencvProbe = @'
import cv2
import json
import os
import sys
import time
import tracemalloc
import numpy as np

video_path, output_dir = sys.argv[1], sys.argv[2]
os.makedirs(output_dir, exist_ok=True)
cap = cv2.VideoCapture(video_path)
fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
sample_every = max(1, int(round(fps * 0.5)))
selected = []
duplicates = 0
last_gray = None
idx = 0
start = time.perf_counter()
tracemalloc.start()
while True:
    ok, frame = cap.read()
    if not ok:
        break
    if idx % sample_every == 0:
        h, w = frame.shape[:2]
        new_w = 320
        new_h = max(1, int(round(h * (new_w / w))))
        small = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        luma = float(gray.mean())
        diff = 999.0 if last_gray is None else float(np.mean(cv2.absdiff(gray, last_gray)))
        if last_gray is None or diff >= 12.0:
            out_path = os.path.join(output_dir, f"frame_{len(selected) + 1:03d}.jpg")
            cv2.imwrite(out_path, small, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            selected.append({"frame": idx, "time_s": round(idx / fps, 3), "diff": round(diff, 2), "blur": round(blur, 2), "luma": round(luma, 2)})
            last_gray = gray
        else:
            duplicates += 1
    idx += 1
cap.release()
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
print(json.dumps({"frames_read": idx, "selected_count": len(selected), "duplicates_skipped": duplicates, "elapsed_ms": elapsed_ms, "peak_tracemalloc_kb": round(peak / 1024, 1)}, sort_keys=True))
'@

  $opencvProbe | uv run --python 3.12 --with opencv-python-headless python - $video $opencvDir
}
finally {
  Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
```

Measurement wrapper:

```text
Each timed command was launched from Python and sampled with psutil:
  - wall time: time.perf_counter()
  - CPU time: sum(user + system) for the command process tree
  - peak RSS: max(sum(memory_info().rss) for the command process tree)
```

Results:

| Method | Command shape | Wall time | CPU time | Peak RSS | Frames output | Duplicate count/rate | Qualitative signal | Dependency footprint |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| `ffprobe` metadata | stream duration, fps, size, frame count | n/a | n/a | n/a | n/a | n/a | Correctly reported 10.0s, 640x360, 24 fps, 240 frames. | Existing FFmpeg binary; no Python packages. |
| `ffmpeg` interval sampling | `fps=1,scale=320:-1` | 82.0 ms | 0.047s | 44.9 MB | 10 | 5 duplicate-in-scene frames; 50% of output was redundant against the five intended scenes. | Fastest and simplest, but wastes vision budget inside unchanged scenes. | Existing FFmpeg binary; no Python packages. |
| PySceneDetect | `detect-content list-scenes save-images --num-images 1 --width 320` | 798.6 ms | 0.750s | 83.8 MB | 5 | 0 obvious duplicate scene frames on the synthetic fixture; 0% redundant against intended scenes. | Detected four cuts and produced five middle-scene frames plus a CSV. | PySceneDetect plus OpenCV backend; warm `uv` fetch included OpenCV wheels and NumPy. |
| OpenCV diff/dedupe | sample every 0.5s, resize, grayscale `absdiff`, Laplacian blur, luma | 260.5 ms process wall; 80.3 ms inner loop | 0.328s | 66.4 MB | 5 | Skipped 15 of 20 sampled candidates as near-duplicates; 75% candidate duplicate/drop rate. | Selected one representative frame per intended scene. | `opencv-python-headless` plus NumPy. |

Memory caveat:

- Peak RSS was sampled from the local process tree, including native allocations
  visible to the OS sampler.
- The OpenCV probe also reported Python `tracemalloc` peak near 1.8 MB, but that
  excludes native OpenCV/FFmpeg allocations and is not used as the memory
  comparison metric.
- The numbers are useful for candidate comparison on a tiny synthetic clip. They
  do not replace Docker or VPS smoke validation for implementation issue `#48`.

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
- This spike records wall-clock behavior, CPU time, directional process RSS, and
  qualitative dependency footprint. Implementation issue `#48` should repeat
  the same measurement shape during local/Docker/VPS validation before enabling
  any production route.

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
   pass the final selected frames to the existing vision path using the default
   count of 5 and hard cap of 8.

Default recommendation for implementation issue `#48`:

- Add `MediaFrameAdapter` around `ffprobe`, `ffmpeg`, optional PySceneDetect, and
  optional OpenCV post-processing.
- Register the real `MediaFrameAdapter` through `ToolRuntime` when enabled, with
  `NullMediaFrameAdapter` as the disabled or unavailable fallback.
- Keep PySceneDetect/OpenCV behind adapter health so missing packages degrade to
  `disabled` or `unconfigured`, not routing failure.
- Defer PyAV until keyframe-aware decoding or pure-Python container access is
  needed.
- Defer Decord until random/batched frame reads become a real bottleneck.
- Defer TransNetV2 until there is a quality benchmark that justifies model
  weight, install, memory, and CPU cost.

## Frame Policy

Suggested production caps for v1:

- Default max duration: 90 seconds for explicit requests.
- Hard duration cap: 180 seconds.
- Default max input bytes: 50 MB before local processing.
- Hard input byte cap: 100 MB before local processing. Telegram or downloader
  layers may impose stricter caps, and the adapter must honor the stricter
  upstream limit.
- Hard input resolution cap: reject streams above 3840x2160, above 8.3
  megapixels, or with either dimension above 4096 px before frame extraction.
- Working decode/selection resolution: downscale candidate frames to at most
  1280 px on the long side; use 320-512 px thumbnails for scene selection and
  duplicate scoring.
- Candidate frame cap before dedupe: 24.
- Default selected frame count after dedupe: 5.
- Hard selected frame cap after dedupe: 8.
- Minimum useful selected frames for a visual summary: 3 when available; if fewer
  are available, proceed with the smaller set and include a sanitized degraded
  diagnostic.
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
- Re-check any downloader-resolved URL, canonical URL, redirect target, and DNS
  resolution result before download or frame extraction; abort if any resolved
  target lands on local, loopback, link-local, private, multicast, or otherwise
  non-public network ranges.
- Apply that validation at the actual fetch boundary as well: every redirect and
  connection target must be checked immediately before use, or the download must
  be constrained to a previously validated public address to avoid DNS rebinding
  and time-of-check/time-of-use gaps.
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
- selected frames as scoped temp handles owned by the adapter;
- cleanup status;
- sanitized diagnostic details.

Temp handle lifetime rule:

- The adapter owns the temp directory and all extracted frame files.
- Callers must not persist frame paths in memory, logs, GitHub issues, or user
  replies.
- The preferred implementation shape is `with adapter.frame_session(...) as
  result:`, where selected frame handles remain valid only inside the context
  manager.
- Vision handoff must complete inside that context; cleanup runs in `finally`
  after vision succeeds, fails, times out, or is skipped.
- If the adapter returns a plain result object instead of a context manager, it
  must expose an explicit `cleanup()` hook and callers must invoke it in
  `finally` before returning from the routed operation.

Suggested failure categories:

```text
disabled
unconfigured
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
- All temp media, extracted frames, thumbnails, CSVs, and probe artifacts are
  cleaned on every exit path, including success, timeout, metadata failure,
  input rejection after temp download, decode failure, scene failure,
  `no_frames_selected`, vision failure, and unexpected exceptions.
- System-log events use stable low-cardinality categories and never include raw
  media paths, URLs, tokens, OCR text, transcript text, or usernames.
- Visual summaries that reach memory are source context only.
