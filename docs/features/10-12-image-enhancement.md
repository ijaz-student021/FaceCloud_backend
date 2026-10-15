# Features 10–12: Image Enhancement

> **2026-10-15:** the learned enhancement models (Zero-DCE, EDSR, NAFNet,
> Real-ESRGAN, GFPGAN, PureVision, PureScale, colourisation) were retired.
> Enhancement is OpenCV and Pillow only, with an optional OpenAI advisor that only
> suggests slider values. What was retired, and how to recover it, is in
> `docs/enhancement/retired-capabilities.md`. The pipeline, controls, previews and
> advisor are described in `docs/enhancement/lightweight-pipeline.md` and
> `docs/enhancement/openai-advisor.md`. This page keeps the parts that still apply.

## 1. Overview

This feature enhances standardized images while preserving the uploaded original.
It provides Low, Medium, High, and Auto enhancement levels and records every
single-image or batch operation so progress and failures can be reported safely.

Current status: **Complete backend**. Enhancement levels, adaptive OpenCV
processing, Celery execution, progress, owner-only APIs, comparison metadata,
secure downloads, adaptive natural-output protection, the per-face fidelity check,
the identity guard (YuNet and SFace), full-resolution source preservation and the
non-destructive Auto/Manual gallery editor are implemented.

## 2. Implemented foundation

- Dedicated Django `enhancement` application
- Low, Medium, High, and Auto enhancement-level choices
- Pending, queued, processing, completed, partial, failed, and cancelled states
- Batch-level progress counters and processing metadata
- Per-image enhancement result records, linked to the original and owned by one user
- One active enhancement operation per source image
- Output format, dimensions, size, parameters, error, and timing fields
- Enhanced-output cleanup when its database record is deleted
- Luminance-only OpenCV correction in LAB colour space (gamma, CLAHE, bounded
  sharpening, measured denoising); colour channels are left alone
- Automatic level selection from measured defects; darkness alone never lifts a
  photo in automatic mode
- Separate enhanced-file storage without modifying the original
- Celery batch enhancement with progress and success/failure counters
- Asynchronous single-image enhancement with HTTP 202 queued responses
- Per-result Celery task identifiers and superseded-task protection; safe `missing`
  results for stale identifiers
- Owner-only result, job, comparison-link, and download APIs
- Image-quality analysis for exposure, contrast, blur, noise, clipping, and resolution
- Source-resolution-normalized before/after quality comparisons
- Defect-driven routing: a correction runs only for a measured defect
- Adaptive natural-output guard (Guard A after exposure, Guard B at the end) that
  limits brightness gain, contrast loss, highlight/shadow clipping, and colour drift
- Named, independently testable pipeline stages with per-stage timings and reports
- Full-resolution original selected for final enhancement rendering
- JPEG quality 95 with 4:4:4 chroma sampling when supported by OpenCV
- No upscaling: the saved image is on the source's pixel grid

## 3. Workflow

```text
Standardized image
  -> choose Low, Medium, High, or Auto
  -> create an owner-scoped enhancement job
  -> queue processing through Celery and Redis (default queue)
  -> analyse the original and apply exposure enhancement
  -> apply output Guard A
  -> measure revealed noise and optionally denoise
  -> apply bounded sharpening and output Guard B
  -> check each detected face against the pre-sharpening image
  -> optional dynamic style, then the identity guard (consent permitting)
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
HTTP 202 without waiting for processing.

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
useful for inspecting the stored result. The grid never changes now, but results made
before 2026-10-15 may be 2x larger (EDSR) and must not be compared natively.

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

This separation prevented a 2x output from being marked severely blurred only
because Laplacian variance changes with the pixel scale (it matters for results
made before the upscaling stages were retired). Frontend comparison
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

## 9. Natural-output protection

Every result passes through `adaptive_natural_output_guard_v2` immediately after
exposure enhancement (Guard A). A second guard runs after denoising and sharpening
(Guard B). The guards improve an image without turning a normally exposed face
unnaturally bright or washing out skin colour and contrast.

- Dark inputs are lifted only when a level or style is chosen (automatic mode
  withholds the lift and records `lift_withheld`); overexposed inputs use a
  darkening policy.
- Low, Medium and High levels use progressively larger, still bounded, corrections.
- Candidate strength is reduced iteratively when luminance gain, luminance loss,
  contrast retention, clipping, or chroma shift exceeds the selected level's limits.
- Photos darker than `EXTREME_LOW_LIGHT_LUMINANCE_THRESHOLD` are allowed a larger
  luminance gain (`EXTREME_LOW_LIGHT_MAX_LUMINANCE_GAIN`) when a lift was asked for.
- A configurable absolute 5% highlight-clipping ceiling is a final safeguard and
  is recorded in result metadata.
- The saved parameters include the raw candidate measurements, final applied
  strength, guard reasons, contrast retention, chroma shift and the limits.

This guard cannot reconstruct detail that is not in the source.

## 10. Fairness and skin-tone preservation

The enhancement pipeline does not classify skin colour. It changes LAB lightness
while preserving the chroma channels, and the per-face fidelity check and identity
guard compare each face with the source. Face-aware local exposure correction and
diverse evaluation images remain required before claiming demographic fairness in
production.

## 11. Failure and recovery behavior

- A queue submission failure marks the result `failed` with a safe reason.
- Completed and cancelled results are idempotent when a task is delivered again.
- A task whose identifier no longer matches the result returns `superseded`.
- Only one pending, queued, or processing enhancement is allowed per source image.
- The uploaded original and standardized processing copy are never overwritten.

### Task failure behaviour (2026-10)

- Every unexpected failure is logged with its traceback; the user still sees one
  safe sentence. Runtime errors (`RuntimeError`, `MemoryError`) mark the
  result failed and remove any output file already written.
- Dropped database connections are retried (three times, with backoff); only the
  last attempt records a failure.
- When the Celery soft time limit fires, a batch stops instead of starting more
  images: unfinished results are failed with a reason that says to start again,
  and the job is recorded as partial with that reason.

## 12. Remaining work

- Add advanced retry and user-requested cancellation.
- Add face-aware local exposure correction.
- Evaluate colour preservation across diverse, consented test images.

## 15. Non-destructive gallery editing

Completed results can be adjusted in Auto or Manual mode; each final render
creates an `ImageEditVersion` and never rewrites the source, the standardized file
or the enhanced result. The controls (brightness, contrast, saturation, shadows,
highlights, warmth, sharpness), their ranges, the bounded previews with revisions,
saving, and the optional advisor are specified in
`docs/enhancement/lightweight-pipeline.md` and `docs/enhancement/openai-advisor.md`.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/enhancements/editor/configuration/` | Read modes, controls, ranges, and preview strategy |
| GET | `/api/enhancements/results/{result_id}/edits/` | List edit versions for one owned result |
| POST | `/api/enhancements/results/{result_id}/edits/` | Queue an Auto or Manual full-resolution render |
| GET | `/api/enhancements/edits/{edit_id}/` | Poll or inspect one version |
| DELETE | `/api/enhancements/edits/{edit_id}/` | Delete an owned version and its file |
| POST | `/api/enhancements/edits/{edit_id}/activate/` | Make an older completed version current |
| GET | `/api/enhancements/edits/{edit_id}/download/` | Download an owned completed version |

## 16. Resolution contract

- The original upload remains the authoritative pixel source.
- The 4096-pixel preprocessing copy remains useful for validation and analysis,
  but it is no longer the final enhancement source when the original is available.
- EXIF orientation and transparency flattening are handled without resizing.
- Output dimensions equal the oriented original dimensions; no stage resizes the
  image. Severe blur and low resolution are reported as beyond the lightweight
  pipeline (a warning), not "fixed" by inventing detail.

Sources above `ENHANCEMENT_MAX_INPUT_PIXELS` (default 24 MP) are reduced to that
budget before enhancement and the result says so (`input_pixel_budget`,
`source_resolution_strategy = original_reduced_to_pixel_budget`). See ADR-014.

## 17. Main implementation files

- `enhancement/models.py`, `admin.py`, `apps.py`, `checks.py`, `policy.py`
- `enhancement/services.py` (pipeline stages, guards, editor rendering entry points)
- `enhancement/controls.py`, `tone.py` (editor controls and renderer)
- `enhancement/denoise_guard.py`, `face_fidelity.py`, `identity_guard.py`
- `enhancement/tasks.py`, `serializers.py`, `views.py`, `urls.py`
- `enhancement/tests.py` and the `test_*.py` files beside it
- `enhancement/migrations/`
- `config/settings.py`, `config/urls.py`

## 18. Staged pipeline

`pipeline_version` is `staged_candidate_pipeline_v5`. Enhancement orchestration
uses an `EnhancementPipelineContext` and named stages; stored parameters include
`stage_order` and `stage_timings_ms`. Guard A protects the exposure result before
noise is measured; optional adaptive denoising maps the measured post-exposure
noise score to a bounded OpenCV strength (breakpoints and strengths are
configurable); final sharpening is protected by Guard B. The original
`quality_guard` key remains an alias of Guard A; new clients should use
`quality_guards.guard_a` and `quality_guards.guard_b`.

Stage order:

```text
analyze_original            required   measure defects, choose level, write the plan
exposure_enhancement        optional   OpenCV gamma/CLAHE in LAB lightness
output_guard_a              required   judge exposure against the source
measure_post_exposure_noise required
adaptive_denoise            optional   strength from the measured noise, accepted or rejected by the denoise guard
sharpen                     optional
output_guard_b              required   judge the result against the pre-sharpening image
face_fidelity               optional   each face against the pre-sharpening image
dynamic_style               optional
identity_guard              optional   SFace embedding similarity against the source (consent permitting)
validate_output             required   pixel grid and datatype contract
```

An optional stage proposes a candidate. If it raises, the image it was given is
kept and `stage_reports` shows `failed`; the pipeline continues. A required stage
that cannot run fails the enhancement (ADR-015).

New keys in `applied_parameters`:

| Key | Meaning |
|---|---|
| `plan` | Defects found, whether the image gets minimal processing, and which of exposure, denoise, sharpen and style have work |
| `stage_reports` | Per stage: `status` (`completed`, `accepted`, `rejected`, `fallback`, `skipped`, `failed`), `changed_pixels`, `duration_ms`, and where relevant `provider`, `device`, `fallback_reason` |
| `identity_status` | `verified`, `inconclusive`, `unavailable` or `failed` |
| `face_fidelity` | Faces checked and adjusted, with each face's measurements (no coordinates) |
| `encoded_output` | The stored file decoded again: size, channels, mean absolute error, PSNR |
| `transparency` | Whether the source had an alpha channel and whether it was kept (PNG output only) |
| `render_policy` | `lightweight_v1`, the list of retired capabilities and a pointer to their documentation |
| `quality_after_measurement_point` | Always `final_output` |

Results stored before 2026-10-15 may also hold `zero_dce`, `restoration`,
`hd_restoration`, `purevision`, `purescale`, `face_restoration` and `colorization`
blocks; they are history, are returned unchanged, and are never written again.

### Night-photo grain fixes (2026-09)

A review on 60 real, very dark images from `Dataset/image` (default `auto`
settings) found two defects in the denoise stage; both are fixed.

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


Automated regression coverage includes extremely dark, normally exposed, bright,
low-resolution and multi-face scene fixtures. It verifies bounded highlight
clipping, contrast retention, normalized comparisons, stage ordering and
feature-flag behaviour.

### Evaluation (2026-10-13)

`scripts/evaluate_pipeline_cases.py` degrades clean photographs in known ways
and measures whether the output moves toward the clean original. Run on the four
scikit-image sample photographs (0.14-0.27 MP, one portrait), CPU only, **no model
checkpoints installed**, so only the classical path ran (which is now the only
path; the learned stages it lists were retired on 2026-10-15). Mean SSIM to the clean photo, before and after enhancement:

| Case | Before | After | Reading |
|---|---|---|---|
| Already good | 1.000 | 0.976 | Small, bounded change; the portrait stays at 0.994 |
| Dark | 0.520 | 0.712 | Better, but well short of the original (automatic mode now withholds the lift altogether; choose a level) |
| Noisy | 0.720 | 0.790 | Before the ladder was recalibrated the night photo went 0.747 to 0.515 (grainier than its input); it now ends at 0.751, and the portrait at 0.776 from 0.638 |
| Blurred | 0.791 | 0.792 | No deblurring model ran; effectively unchanged |
| Compressed | 0.857 | 0.832 | Slightly worse: nothing removes JPEG artifacts on this path |
| Overexposed | 0.744 | 0.739 | Clipped highlights cannot be recovered |
| Transparent cut-out | 0.671 | 0.856 | Alpha channel preserved exactly in all four |

Three of the sample photographs are themselves dim, so their "clean" reference
is not a correctly exposed image and their scores understate the result.

What this does not cover: real sensor noise, real motion
blur, real group photographs, or skin tones beyond one subject.

### Conservative routing (2026-10-13, ADR-020)

The table above showed two regressions: compressed photos came out worse than
they went in, and already-good photos were changed for no reason. Ablating the
stages on 22 photographs (the four above plus 18 not used to design the fix)
showed both came from sharpening and the local-contrast step being applied
without a defect to justify them. After routing on measured defects, the same
evaluation reads (mean SSIM to the clean photo):

| Case | Input | Before routing | After routing |
|---|---|---|---|
| Already good | 1.000 | 0.976 | 0.981 (the two well-lit photos are untouched, 1.000) |
| Compressed | 0.857 | 0.832 | 0.854 |
| Noisy | 0.720 | 0.790 | 0.805 |
| Dark | 0.520 | 0.712 | 0.715 |
| Blurred | 0.791 | 0.792 | 0.791 |

Still true: nothing here removes JPEG artifacts or blur. Those need a model.

### Denoise decision (2026-10-13, ADR-021)

`applied_parameters.adaptive_denoise` now reads:

| Key | Meaning |
|---|---|
| `eligibility` | `eligible`, the reason, the noise score, the noise measured in flat regions and the minimum; `depends_on_exposure_level` is always false |
| `benefit` | Noise before and after (grey levels, flat regions), amount and share removed, and how noise-like the removed part was |
| `detail` | Share of structured detail and of edges kept, for the frame and for each face; `null` where there was no structure to measure |
| `acceptance` | `accepted`, `strength_reduced`, `rejected` or `not_attempted`, with the reason and the blend applied |
| `candidate_guard` | Every strength tried, with its measurements and violations |
| `stage_duration_ms` | Time spent in the stage, including the check |

The stage report carries the same `benefit`, `detail` and `acceptance`, and its
status is `rejected` when a candidate was computed and dropped.

