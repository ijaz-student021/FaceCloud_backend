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
checksum-pinned GFPGAN v1.4 face-restoration stage is implemented behind disabled
rollout flags for isolated-worker evaluation.

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
  -> apply bounded sharpening and output Guard B
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
   `GFPGAN_ENABLED`, and `GFPGAN_QUEUE_ENABLED`. All default to `False`.
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

The adaptive denoise rollout is intentionally disabled by default. With it
disabled, the legacy denoise policy remains in effect.

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
5. Restoration is eligible only for severe blur, mild blur, or low resolution.
   Normally exposed, sharp, sufficiently large images skip the model.
6. GFPGAN uses `upscale=1`, the `clean` architecture, no background upsampler,
   restoration weight `0.35`, and a bounded final blend of `0.40`.
7. Candidates that contain no face, exceed five faces or the configured pixel
   limit, change dimensions, contain invalid pixels, or fail inference are
   rejected without failing the overall enhancement.
8. Guard B evaluates face restoration and sharpening against the pre-GFPGAN
   reference. The uploaded original remains unchanged.
9. Stored metadata includes provider, checksum prefix, face count, dimensions,
   timing, candidate quality, fallback reason, and identity-safeguard state. It
   excludes the private path and license terms.
10. `GFPGAN_ENABLED` and `GFPGAN_QUEUE_ENABLED` default to `False`.

### Isolated worker setup

Use a separate Python 3.10 or 3.11 environment. Install the correct CPU or CUDA
PyTorch build for the deployment host first, then run:

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
celery -A config worker --loglevel=info --pool=solo -Q gfpgan
```

Only after that worker is healthy should the deployment set
`GFPGAN_ENABLED=True` and `GFPGAN_QUEUE_ENABLED=True` and restart its processes.
The queue switch routes image and batch enhancement tasks to `gfpgan`. Rollback
sets both flags to `False`.

### Remaining production enablement gate

Run a consented evaluation set containing dark, dim, normal, bright, blurry,
low-resolution, multi-face, different-skin-tone, and occlusion cases. Review
identity fidelity manually and add embedding-based identity-drift protection
before aggressive restoration is allowed. Until that gate passes, the adapter
is ready for controlled QA but remains off for users.
