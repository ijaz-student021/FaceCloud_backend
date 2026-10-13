# Features 10–12: Image Enhancement

## 1. Overview

This feature enhances standardized images while preserving the uploaded original.
It provides Low, Medium, High, and Auto enhancement levels and records every
single-image or batch operation so progress and failures can be reported safely.

Current status: **Complete backend**. Enhancement levels, adaptive OpenCV processing,
optional EDSR restoration, private FaceCloud curve-model inference, Celery execution,
progress, owner-only APIs, comparison metadata, secure downloads, and adaptive
natural-output protection are implemented. Full-resolution source preservation and
the non-destructive Auto/Manual gallery editor are also implemented. A private,
checksum-pinned GFPGAN v1.4 face-restoration stage is implemented and verified in
an isolated Python 3.11 worker. Portable defaults remain disabled because private
model artifacts are not distributed with the repository; an authorized deployment
enables the stage only after its startup preflight passes.

## 2. Implemented foundation

- Dedicated Django `enhancement` application
- Low, Medium, High, and Auto enhancement-level choices
- Pending, queued, processing, completed, partial, failed, and cancelled states
- Batch-level progress counters and processing metadata
- Per-image enhancement result records
- Original image-to-enhanced output relationship
- Owner relationship on jobs and results
- One active enhancement operation per source image
- Output format, dimensions, size, parameters, error, and timing fields
- Enhanced-output cleanup when its database record is deleted
- Django administration support
- PostgreSQL migration and model tests
- Luminance-only OpenCV correction in LAB colour space
- Automatic level selection using input luminance
- Gamma correction, CLAHE, optional denoising, and controlled sharpening
- Separate enhanced-file storage without modifying the original
- Celery batch enhancement with progress and success/failure counters
- Authenticated single-image and batch creation APIs
- Asynchronous single-image enhancement with HTTP 202 queued responses
- Per-result Celery task identifiers and superseded-task protection
- Safe `missing` results for stale Celery identifiers after deletion, rollback,
  or queue recovery instead of uncaught database lookup failures
- Owner-only result, job, comparison-link, and download APIs
- Automated processing, batch-progress, auto-level, and cross-user privacy tests
- Image-quality analysis for exposure, contrast, blur, noise, clipping, and resolution
- Source-resolution-normalized before/after quality comparisons
- Separate native-output quality metadata for full-resolution inspection
- Adaptive defect routing and conservative over-enhancement fallback
- Exposure-aware routing for dark, normally exposed, and overexposed images
- Adaptive natural-output guard that limits brightness gain, contrast loss,
  highlight/shadow clipping, and colour drift
- Source-chroma preservation for learned curve-model output
- Automatic blending toward the original whenever a candidate is too aggressive
- Auditable guard metadata with raw-candidate and final-output measurements
- Named, independently testable pipeline stages with per-stage timings
- Separate post-exposure Guard A and final-output Guard B safety decisions
- Post-Guard-A noise measurement and feature-flagged adaptive denoising
- EDSR ordering after denoise so enlargement does not amplify avoidable noise
- AI-restoration warning for severe blur and low-resolution inputs
- Optional CPU EDSR 2x super-resolution routing for low-resolution inputs
- EDSR activation limited to genuinely small inputs (shortest edge at or below
  480 pixels by default), avoiding unnecessary enlargement of medium-size images
- Bounded output-pixel safety limit and automatic OpenCV fallback
- Auditable restoration provider, dimensions, outcome, and failure metadata
- Full-resolution original selected for final enhancement rendering
- Bounded proxy used only for learned curve prediction; curve maps are applied
  to the complete source pixel grid
- Full-resolution curve application runs in bounded tiles to avoid constructing
  a full-size floating-point curve tensor for very large uploads
- JPEG quality 95 with 4:4:4 chroma sampling when supported by OpenCV

## 3. Intended workflow

```text
Standardized image
  -> choose Low, Medium, High, or Auto
  -> create an owner-scoped enhancement job
  -> queue processing through Celery and Redis
  -> analyse the original and apply exposure enhancement
  -> apply output Guard A
  -> measure revealed noise and optionally denoise
  -> route still-low-resolution output through optional EDSR 2x restoration
  -> restore eligible degraded faces through guarded private GFPGAN
  -> apply bounded sharpening and output Guard B
  -> validate the final datatype and pixel-grid contract
  -> save the enhanced output separately
  -> update progress and terminal status
  -> expose secure comparison and download data
```

## 4. Database design

### `EnhancementBatchJob`

Tracks the requesting owner, source upload batch, requested enhancement level,
task identifier, progress counters, status, error summary, and timestamps.

### `ImageEnhancementResult`

Links a source image to its job and enhanced output. It records requested and
applied levels, processing status, output metadata, parameters, failure details,
processing duration, and the Celery task identifier used for single-image work.

Migration `0002_imageenhancementresult_task_id.py` adds the indexed `task_id`
field. The identifier supports queue tracing and prevents an old or duplicate
task from processing a result superseded by a newer task.

## 5. Security requirements

- Only the source-image owner may create or inspect a personal enhancement job.
- Organization access must follow explicit content ownership and review-sharing
  rules; membership alone must not expose personal images.
- Enhanced files require the same ownership checks as original files.
- User filenames are never trusted as storage paths.
- Failed or deleted operations must clean up partial output files.
- Processing errors must not expose server paths or internal secrets.

## 6. API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/enhancements/images/{image_id}/` | Queue enhancement for one preprocessed image; returns HTTP 202 |
| POST | `/api/enhancements/batches/{batch_id}/` | Queue a complete batch |
| GET | `/api/enhancements/jobs/` | List the user's batch jobs |
| GET | `/api/enhancements/jobs/{id}/` | Read progress and results |
| GET | `/api/enhancements/results/` | List the user's results |
| GET | `/api/enhancements/results/{id}/` | Read one result and comparison URLs |
| GET | `/api/enhancements/results/{id}/comparison/` | Read the completed result's secure before/after comparison contract |
| GET | `/api/enhancements/results/{id}/download/` | Download an enhanced image securely |

Request body:

```json
{"level": "auto"}
```

Allowed levels are `low`, `medium`, `high`, and `auto`.

## 7. Single-image asynchronous contract

The single-image endpoint creates the result, assigns a task identifier, changes
the result to `queued`, submits `enhancement.process_image` to Redis, and returns
HTTP 202 without waiting for EDSR or OpenCV processing.

Example initial response fields:

```json
{
  "id": 25,
  "image": 22,
  "requested_level": "auto",
  "applied_level": "",
  "status": "queued",
  "task_id": "opaque-celery-task-id",
  "download_url": null
}
```

The frontend should poll `GET /api/enhancements/results/{id}/` until the result
reaches `completed`, `failed`, or `cancelled`. A completed result includes its
applied level, output metadata, quality metadata, and secure download URL.

Recommended frontend polling behavior:

1. Poll every two seconds while the status is `queued` or `processing`.
2. Stop polling at a terminal status.
3. Enable comparison and download controls only for `completed` results.
4. Display the safe `failure_reason` for failed results.
5. Do not expose the task identifier as a user-editable value.

## 8. Quality metadata

`quality_before` describes the standardized source. `quality_after` describes the
native enhanced output at its real output dimensions. These native values are
useful for inspecting the stored result but must not be directly compared when
EDSR changed the resolution.

`quality_comparison` provides a fair before/after comparison by measuring both
images on the original source pixel grid:

```json
{
  "method": "source_resolution_normalized_v1",
  "reference_dimensions": [335, 597],
  "before": {
    "laplacian_sharpness": 69.99
  },
  "after": {
    "laplacian_sharpness": 63.1
  },
  "deltas": {
    "laplacian_sharpness": -6.89
  }
}
```

This separation prevents a 2x output from being marked severely blurred only
because Laplacian variance changes with the pixel scale. Frontend comparison
cards should use `quality_comparison`; technical details may additionally show
the native `quality_after` values.

### Comparison API contract

`GET /api/enhancements/results/{id}/comparison/` returns only an authenticated
owner's completed result. It includes original, standardized, and enhanced
metadata and protected download URLs, the applied enhancement level and
algorithm, processing time, warnings, and the normalized metrics needed by the
frontend comparison page.

The public metric names are `brightness`, `contrast`, `sharpness`, `noise`,
`shadow_clipping`, and `highlight_clipping`. Every metric contains `before`,
`after`, and `change`. The response never contains storage paths, model paths,
tokens, or internal task data.

State and privacy behavior:

- A result owned by another user returns HTTP 404 to avoid resource discovery.
- An unauthenticated request is rejected by JWT authentication.
- A queued, processing, failed, cancelled, or partial result returns HTTP 409.
- A completed result whose stored files are gone returns HTTP 410.
- A completed legacy result without normalized measurements returns HTTP 409.

## 9. EDSR restoration behavior

- EDSR is requested only when the quality router identifies low resolution.
- EDSR executes after post-exposure noise measurement and denoising.
- The configured model is OpenCV DNN EDSR at 2x scale.
- EDSR runs only when the source shortest edge is at or below the configured
  `AI_RESTORATION_MAX_INPUT_EDGE` (480 pixels by default).
- A larger image classified as low resolution keeps its original grid and records
  a safe skip reason instead of being enlarged merely to satisfy the classifier.
- The model is cached in each Celery worker process after its first load.
- The maximum output-pixel setting prevents unsafe memory growth.
- Missing or failed restoration falls back to the OpenCV enhancement path.
- Restoration metadata records whether EDSR was requested and applied, its
  provider, scale, dimensions, and safe fallback reason.
- EDSR improves enlargement quality but does not guarantee recovery of facial
  information absent from the source.

### Natural-output protection

Every learned-model result passes through `adaptive_natural_output_guard_v2`
immediately after exposure enhancement (Guard A). A second guard runs after
denoising, optional EDSR, and sharpening (Guard B). The guards are designed to
improve an image without turning a
normally exposed face unnaturally bright or washing out skin colour and contrast.

- Dark inputs use a controlled brightening policy.
- Normally exposed inputs use a balanced policy with a smaller model blend.
- Overexposed inputs use a darkening policy and cannot receive additional
  brightening from the learned model.
- Low, Medium, and High levels use progressively larger—but still bounded—model
  strengths. Auto selects a level from the source measurements.
- The learned luminance is combined with the source LAB chroma channels to reduce
  unwanted skin-tone and colour shifts.
- Candidate strength is reduced iteratively when luminance gain, luminance loss,
  contrast retention, clipping, or chroma shift exceeds the selected level's
  safety limits.
- Model strength is additionally scaled from normalized scene mean and median
  luminance. Inputs at or above the configured 0.45 threshold receive no learned
  brightening; genuinely dark inputs receive only the strength justified by the
  distance from the 0.60 target.
- A configurable absolute 5% highlight-clipping ceiling provides a final
  production safeguard and is recorded in result metadata.
- The saved processing parameters include the raw candidate measurements, final
  applied strength, guard reasons, final contrast retention, chroma shift, and
  applicable limits for auditing and frontend technical details.

This guard improves realism and consistency, but it cannot reconstruct facial
detail that does not exist in a severely blurred or extremely compressed source.
Such restoration remains a separate model and quality-review responsibility.

## 10. Operational verification

The real non-eager Celery pipeline and natural-output guard were verified on
`image6.jpg`. The latest natural-output verification was performed on July 22,
2026 with the private FaceCloud ONNX curve model.

| Check | Result |
|---|---|
| Source dimensions | 335 x 597 |
| EDSR output dimensions | 670 x 1194 |
| Queue response | HTTP 202 in 324 ms |
| Initial status | `queued` |
| Terminal status | `completed` |
| CPU processing duration | approximately 143.7 seconds |
| EDSR requested/applied | yes/yes |
| Normalized comparison method | `source_resolution_normalized_v1` |
| Normalized sharpness | 69.99 before, 63.1 after |
| PostgreSQL migration | applied successfully |
| Guarded source luminance | 76.99 |
| Raw model candidate gain | +73.35 luminance points |
| Final guarded luminance | 89.88 (+12.89) |
| Final contrast retention | 83.5% |
| Final chroma shift | 0.265 |
| Final applied model strength | 17.67% |
| Guard actions | luminance gain limited; contrast preserved |
| Automated test suite | 146 tests passing |
| Django system check | no issues |
| Redis and Celery | connected and ready |

The 324 ms queue response confirms that the user request is no longer blocked by
the approximately 144-second CPU inference. The current CPU duration remains a
known performance limitation and must be represented as background progress in
the frontend.

## 11. Fairness and skin-tone preservation

The enhancement pipeline does not classify skin colour. The OpenCV baseline
changes LAB lightness while preserving chroma, and the learned-model output is
also recombined with the source chroma before adaptive guarding. This reduces
unintended colour shifts between people with different skin tones.
Face-aware local exposure correction and diverse evaluation images remain required
before claiming demographic fairness in production.

The EDSR stage improves spatial resolution for eligible small images but cannot
guarantee recovery of facial detail absent from the source. Severe blur remains
flagged for a future face-restoration model and quality review.

## 12. Failure and recovery behavior

- A queue submission failure marks the result `failed` with a safe reason.
- A missing, disabled, or incompatible EDSR model falls back without exposing a
  server path to the API user.
- Completed and cancelled results are idempotent when a task is delivered again.
- A task whose identifier no longer matches the result returns `superseded`.
- Only one pending, queued, or processing enhancement is allowed per source image.
- The uploaded original and standardized processing copy are never overwritten.

### Task failure behaviour (2026-10)

- Every unexpected failure is logged with its traceback; the user still sees one
  safe sentence. Model-runtime errors (`RuntimeError`, `MemoryError`) mark the
  result failed and remove any output file already written.
- Dropped database connections are retried (three times, with backoff); only the
  last attempt records a failure.
- When the Celery soft time limit fires, a batch stops instead of starting more
  images: unfinished results are failed with a reason that says to start again,
  and the job is recorded as partial with that reason.

## 13. Curve-model implementation

- The private FaceCloud curve model is trained independently and exported to ONNX.
- Production inference uses OpenCV DNN and does not require PyTorch.
- Low-light inputs can request the private model; normally exposed and bright
  inputs retain conservative, exposure-aware processing.
- Zero-DCE or the OpenCV exposure fallback runs first. Guarded output is
  measured and denoised before EDSR is considered for low-resolution input.
- Eight learned RGB curve maps are applied at the original output dimensions.
- Source chroma is restored and adaptive output guarding is applied before save.
- Missing, disabled, corrupt, or incompatible models fall back safely to adaptive OpenCV.
- Result metadata records whether Zero-DCE was requested and applied, its provider, inference dimensions, model filename, curve iterations, and fallback reason.

The local runtime model is stored under `.runtime/models/`. Runtime model assets
are intentionally excluded from Git and must be deployed through controlled,
private model storage.

## 14. Remaining implementation

- Evaluate an identity-preserving face-restoration model for severe blur.
- Add advanced retry and user-requested cancellation.
- Add face-aware local exposure correction after multi-face detection exists.
- Evaluate colour preservation across diverse, consented test images.
- Connect the frontend to the real APIs and job states.

## 15. Non-destructive gallery editing

Completed results can now be adjusted using Auto or Manual mode. Manual mode
supports brightness, contrast, shadows, saturation, and highlights. Each final
render creates an `ImageEditVersion`; it does not rewrite the source upload,
standardized file, or enhanced result.

The frontend should preview slider movement locally for responsiveness, then
submit the final values to the backend. The backend renders the authoritative
full-resolution file through Celery and returns a pollable version record.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/enhancements/editor/configuration/` | Read modes, controls, ranges, and preview strategy |
| GET | `/api/enhancements/results/{result_id}/edits/` | List edit versions for one owned result |
| POST | `/api/enhancements/results/{result_id}/edits/` | Queue an Auto or Manual full-resolution render |
| GET | `/api/enhancements/edits/{edit_id}/` | Poll or inspect one version |
| DELETE | `/api/enhancements/edits/{edit_id}/` | Delete an owned version and its file |
| POST | `/api/enhancements/edits/{edit_id}/activate/` | Make an older completed version current |
| GET | `/api/enhancements/edits/{edit_id}/download/` | Download an owned completed version |

Manual request example:

```json
{
  "mode": "manual",
  "adjustments": {
    "brightness": 8,
    "contrast": 12,
    "shadows": 10,
    "saturation": 4,
    "highlights": -8
  }
}
```

Auto request example:

```json
{"mode": "auto"}
```

## 16. Resolution contract

- The original upload remains the authoritative pixel source.
- The 4096-pixel preprocessing copy remains useful for validation and analysis,
  but it is no longer the final enhancement source when the original is available.
- EXIF orientation and transparency flattening are handled without resizing.
- The private model may predict curve maps on a bounded proxy; only the curve maps
  are interpolated per tile and applied to the full-resolution original.
- Output dimensions equal the oriented original dimensions unless the explicitly
  requested EDSR restoration stage produces a larger 2x result.
- Enhancement cannot invent unlimited detail. Severe blur or compression damage
  still requires a validated restoration model and may need moderator review.

Sources above `ENHANCEMENT_MAX_INPUT_PIXELS` (default 24 MP) are reduced to that
budget before enhancement and the result says so (`input_pixel_budget`,
`source_resolution_strategy = original_reduced_to_pixel_budget`). See ADR-014.

## 17. Main implementation files

- `enhancement/models.py`
- `enhancement/admin.py`
- `enhancement/apps.py`
- `enhancement/services.py`
- `enhancement/zero_dce.py`
- `scripts/export_zero_dce_onnx.py`
- `requirements-model-export.txt`
- `enhancement/tasks.py`
- `enhancement/serializers.py`
- `enhancement/views.py`
- `enhancement/urls.py`
- `enhancement/tests.py`
- `enhancement/migrations/0001_initial.py`
- `enhancement/migrations/0002_imageenhancementresult_task_id.py`
- `enhancement/migrations/0003_imageeditversion.py`
- `config/settings.py`
- `config/urls.py`

## 18. Staged pipeline checkpoint (Parts 1-4)

The first four commercially structured hardening parts are implemented without
starting face restoration or GFPGAN integration:

1. Rollout flags were added for `ADAPTIVE_DENOISE_ENABLED`,
   `GFPGAN_ENABLED`, and `GFPGAN_QUEUE_ENABLED`. Adaptive denoise defaults to
   `True`; the two private-model flags default to `False` in portable source
   configuration.
2. Enhancement orchestration now uses an `EnhancementPipelineContext` and
   named stages. Stored parameters include `stage_order` and
   `stage_timings_ms`.
3. Guard A protects the exposure result before noise is measured. Optional
   adaptive denoising maps the measured post-exposure noise score to a bounded
   OpenCV strength using configurable breakpoints and strengths.
4. EDSR now executes only after denoising and rechecks the current image for
   low resolution at that point. Final sharpening is protected by Guard B.

The original `quality_guard` metadata key remains an alias of Guard A for
existing clients. New clients should use `quality_guards.guard_a` and
`quality_guards.guard_b`.

Adaptive denoise is enabled by default. A deployment can still disable it to use
the legacy denoise policy during rollback or compatibility testing.

### Night-photo grain fixes (2026-09)

A review on 60 real, very dark images from `Dataset/image` (default `auto`
settings, Zero-DCE on) found two defects in the denoise stage; both are fixed.

- **Edges were found on the noisy image.** The edge-aware blend protects edges
  and denoises flat areas, but it located edges by measuring gradients on the
  noisy input. Grain creates gradients everywhere, so only ~14% of a brightened
  night photo counted as flat and about 80% of the grain survived at *any*
  strength (raising the strength from 7 to 14 changed nothing). Edges are now
  measured on the denoised candidate. Result on the 60 images: mean measured
  noise 5.53 to 4.88, images still flagged `visible_noise` 24 to 22, edge
  strength retained 1.000 and a mean pixel change of 0.41/255 (detail is not
  lost). On a synthetic block image with grain, grain in flat areas fell 49%
  where the old logic removed 11%.
- **Dark frames counted as "severe blur".** "Severe blur" is a Laplacian
  variance below 35, which any near-black frame has, and it switched denoising
  off for the grainiest night photos. It is now judged on the brightened image
  (`post_exposure_noise.quality`), so a genuinely blurry photo is still
  protected. In the sample this affected 1 image in 60.

What was **not** changed by the grain fixes: raising the strength table barely
helped without the edge fix (mean noise 5.42 and 5.38 at stronger settings).

### Colour in very dark photos (`ENHANCEMENT_DARK_COLOUR_GAIN_MAX`)

When the learned brightening runs, the pipeline keeps the source's LAB colour
channels (an identity and skin-tone protection). A near-black source has almost
no colour, so a photo brightened many times over came out grey (mean
colourfulness 2.9 in, 2.86 out on the 60-image sample).

- **Rejected: using the model's colours.** The raw Zero-DCE output has strong
  green and magenta casts on pavements and trees, and smoothing or blending it
  keeps them. Near-black 8-bit pixels do not contain that colour information,
  so the model invents it.
- **Shipped: bounded gain on the photo's own colour.** After the brightening,
  the source's colour channels are smoothed (in near-black pixels they are mostly
  sensor noise) and scaled by the square root of how much each area's lightness
  grew, capped by `ENHANCEMENT_DARK_COLOUR_GAIN_MAX` (default 9, so at most 3x).
  Lightness is not touched, and no colour is introduced that the photo did not
  already hold. `1` or less switches it off and restores the old output exactly.
  The applied gain is recorded in `zero_dce.chroma_gain`.
- **Result (60 dark photos, full pipeline):** mean colourfulness 2.86 to 5.04,
  largest chroma shift 2.54 (the guard limits are 2.5 to 5.0 and cut it back on
  0 of 60), brightness unchanged, about 0.55 s more per image. Visible effect:
  warm shop lights, green courts and coloured clothing return; a few very dark
  scenes gain a faint tint.
- **Not verified:** the identity guard judged none of the 60 photos (no face
  large enough), so its interaction with the gain has not been measured on real
  faces. The gain only shifts chroma by about 2 to 3 units and the guard still
  runs afterwards; test with consented portraits before relying on it.

Automated regression coverage includes extremely dark, normally exposed,
bright, low-resolution, and multi-face scene fixtures. It verifies bounded
highlight clipping, contrast retention, normalized comparisons, stage ordering,
feature-flag behavior, and denoise-before-EDSR execution.

Synthetic checkpoint measurements with adaptive denoise enabled and AI model
files disabled are shown below. These fixtures make the routing deterministic;
they are regression evidence, not a substitute for the later consented
photographic evaluation set.

| Fixture | Auto level | Luminance before -> after | Contrast before -> after | Highlight clipping |
|---|---:|---:|---:|---:|
| Extremely dark | High | 14.56 -> 24.33 | 5.03 -> 8.94 | 0% |
| Normal portrait | Low | 144.67 -> 147.67 | 5.97 -> 5.98 | 0% |
| Bright portrait | Low | 225.50 -> 225.50 | 3.36 -> 3.36 | 0% |
| Low-resolution portrait | Low | 144.68 -> 151.11 | 5.92 -> 6.87 | 0% |
| Multi-face group | Medium | 101.69 -> 107.88 | 25.27 -> 25.74 | 0% |

## 18a. Staged candidate pipeline (2026-10)

`pipeline_version` is `staged_candidate_pipeline_v4`. Stage order:

```text
analyze_original          required   measure defects, choose level, write the plan
exposure_enhancement      optional   Zero-DCE curves, else OpenCV gamma/CLAHE
output_guard_a            required   judge exposure against the source
measure_post_exposure_noise required
adaptive_denoise          optional   strength from the measured noise
purevision_restoration    optional   1x artifact removal
low_resolution_restoration optional  EDSR (only with the HD pipeline off)
hd_restoration            optional   NAFNet deblur, then Real-ESRGAN
purescale_super_resolution optional  only if nothing above changed the grid
face_restoration          optional   GFPGAN, gated on a verifiable identity
sharpen                   optional
output_guard_b            required   judge the result against the pre-restoration image
face_fidelity             optional   each face against the pre-restoration image
colorization              optional
dynamic_style             optional
identity_guard            optional   embedding similarity against the source
validate_output           required   pixel grid and datatype contract
```

An optional stage proposes a candidate. If it raises, the image it was given is
kept and `stage_reports` shows `failed`; the pipeline continues. A required
stage that cannot run fails the enhancement (ADR-015).

Three references are kept apart: the original source (guard A, identity guard),
the last accepted image before any restoration model (guard B, face fidelity),
and that image resized bicubically to the output grid, used only to compare.

New keys in `applied_parameters`:

| Key | Meaning |
|---|---|
| `plan` | Defects found, whether the image gets minimal processing, and which stages have work, including the single planned upscaler |
| `stage_reports` | Per stage: `status` (`completed`, `accepted`, `partially_accepted`, `rejected`, `fallback`, `skipped`, `failed`), `changed_pixels`, `duration_ms`, and where relevant `provider`, `device`, `tile_size`, `memory_retries`, `fallback_reason` |
| `identity_status` | `verified`, `inconclusive`, `unavailable` or `failed` |
| `face_fidelity` | Faces checked and adjusted, with each face's measurements (no coordinates) |
| `encoded_output` | The stored file decoded again: size, channels, mean absolute error, PSNR |
| `transparency` | Whether the source had an alpha channel and whether it was kept (PNG output only) |
| `quality_after_measurement_point` | Always `final_output` |

### Evaluation (2026-10-13)

`scripts/evaluate_pipeline_cases.py` degrades clean photographs in known ways
and measures whether the output moves toward the clean original. Run on the four
scikit-image sample photographs (0.14-0.27 MP, one portrait), CPU only, **no model
checkpoints installed** (Zero-DCE, EDSR, NAFNet, Real-ESRGAN, GFPGAN and SFace all
fell back). Mean SSIM to the clean photo, before and after enhancement:

| Case | Before | After | Reading |
|---|---|---|---|
| Already good | 1.000 | 0.976 | Small, bounded change; the portrait stays at 0.994 |
| Dark | 0.520 | 0.712 | Better, but well short of the original without Zero-DCE |
| Noisy | 0.720 | 0.790 | Before the ladder was recalibrated the night photo went 0.747 to 0.515 (grainier than its input); it now ends at 0.751, and the portrait at 0.776 from 0.638 |
| Blurred | 0.791 | 0.792 | No deblurring model ran; effectively unchanged |
| Compressed | 0.857 | 0.832 | Slightly worse: nothing removes JPEG artifacts on this path |
| Overexposed | 0.744 | 0.739 | Clipped highlights cannot be recovered |
| Transparent cut-out | 0.671 | 0.856 | Alpha channel preserved exactly in all four |

Three of the sample photographs are themselves dim, so their "clean" reference
is not a correctly exposed image and their scores understate the result.

What this does not cover: any learned model, real sensor noise, real motion
blur, real group photographs, or skin tones beyond one subject.

## 19. Private GFPGAN v1.4 rollout checkpoint

The company has confirmed that private commercial permission for the supplied
GFPGAN v1.4 checkpoint is recorded outside this repository. The agreement,
checkpoint, and commercial terms must never be committed or returned through an
API.

Implemented controls:

1. The private checkpoint is stored at
   `.runtime/models/gfpgan/GFPGANv1.4.pth`; `.runtime/` is Git-ignored.
2. Its complete SHA-256 is configured and verified before the optional GFPGAN
   package is imported or the checkpoint is loaded.
3. No model is downloaded automatically. A missing or modified artifact safely
   falls back to the existing enhancement pipeline.
4. Loading is lazy and process-cached. Inference is serialized within a worker
   to avoid concurrent use of one model instance.
5. Restoration is eligible only for severe blur, optionally mild blur, or low
   resolution. Normally exposed, sharp, sufficiently large images skip the model.
6. GFPGAN uses `upscale=1`, the `clean` architecture, no background upsampler,
   restoration weight `0.30`, and an initial output blend of `0.25`.
7. Candidates that contain no face, exceed five faces or the configured pixel
   limit, change dimensions, contain invalid pixels, or fail inference are
   rejected without failing the overall enhancement.
8. A dedicated face-restoration guard measures structural similarity, mean and
   95th-percentile absolute change, chroma shift, and highlight-clipping growth.
   It reduces the blend step-by-step or rejects the candidate when any configured
   natural-image/change boundary is exceeded. This is explicitly recorded as a
   quality guard, not as embedding-based identity verification.
9. Stored metadata includes provider, checksum prefix, face count, dimensions,
   timing, every guard attempt, fallback reason, and identity-safeguard state. It
   excludes private paths and license terms.
10. `GFPGAN_ENABLED` and `GFPGAN_QUEUE_ENABLED` default to `False`.
11. The detector, parser, and GFPGAN checkpoints are all checksum-pinned and must
    already exist locally; runtime downloads are not permitted.
12. Final output validation requires a contiguous `uint8` BGR result on the
    expected original or bounded-EDSR grid. The enhanced output is never silently
    downscaled.

### Isolated worker setup

Use the separate Python 3.11 environment. Install the authorized CPU or CUDA
PyTorch build matching `requirements-gfpgan-constraints.txt`, then run:

```powershell
python -m pip install -r requirements-gfpgan.txt
```

Verify the checkpoint:

```powershell
(Get-FileHash `
  -LiteralPath ".runtime\models\gfpgan\GFPGANv1.4.pth" `
  -Algorithm SHA256).Hash.ToLowerInvariant()
```

Expected SHA-256:

```text
e2cd4703ab14f4d01fd1383a8a8b266f9a5833dacee8e6a79d3bf21a1b6be5ad
```

Start the isolated worker:

```powershell
.\scripts\start_gfpgan_worker.ps1
```

The launcher validates all checkpoint checksums, loads the private model, and
starts a uniquely named solo Celery worker on the `gfpgan` queue. Only after that
preflight is healthy should the deployment set `GFPGAN_ENABLED=True` and
`GFPGAN_QUEUE_ENABLED=True`. Rollback sets both flags to `False`.

### Verified local pipeline evidence

The isolated CPU runtime passed dependency validation and loaded GFPGAN v1.4,
its face detector, and its face parser without downloading artifacts. A real
full-pipeline smoke test completed with these results:

| Measurement | Verified result |
|---|---:|
| Source and output dimensions | 670 x 1194 |
| End-to-end CPU time | 36.259 seconds |
| Faces restored | 1 |
| GFPGAN output blend | 0.25 |
| Structural similarity | 0.99878 |
| Mean absolute pixel change | 0.235 |
| 95th-percentile absolute change | 2.0 |
| Chroma shift | 0.106 |
| Highlight-clipping growth | 0.0% |
| Final pixel-grid validation | Passed |

The same input was marked low resolution by the quality classifier, but EDSR
correctly skipped it because its shortest edge exceeded 480 pixels. This reduced
CPU processing time and preserved the original pixel grid.

### Remaining production enablement gate

Before a broad production rollout, run a consented evaluation set containing
dark, dim, normal, bright, blurry, low-resolution, multi-face, different-skin-tone,
and occlusion cases. The current bounded guard is suitable for conservative
controlled use; aggressive restoration must remain disabled until identity
fidelity is validated against that evaluation set.
