# Features 10–12: Image Enhancement

## 1. Overview

This feature enhances standardized images while preserving the uploaded original.
It provides Low, Medium, High, and Auto enhancement levels and records every
single-image or batch operation so progress and failures can be reported safely.

Current status: **Complete backend**. Enhancement levels, adaptive OpenCV processing,
optional EDSR restoration, official Zero-DCE Epoch99 inference, Celery execution,
progress, owner-only APIs, comparison metadata, and secure downloads are implemented.

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
- AI-restoration warning for severe blur and low-resolution inputs
- Optional CPU EDSR 2x super-resolution routing for low-resolution inputs
- Bounded output-pixel safety limit and automatic OpenCV fallback
- Auditable restoration provider, dimensions, outcome, and failure metadata
- 4096-pixel preprocessing boundary and JPEG quality 95 for detail preservation

## 3. Intended workflow

```text
Standardized image
  -> choose Low, Medium, High, or Auto
  -> create an owner-scoped enhancement job
  -> queue processing through Celery and Redis
  -> route low-resolution input through optional EDSR 2x restoration
  -> apply the OpenCV enhancement policy
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
- The configured model is OpenCV DNN EDSR at 2x scale.
- The model is cached in each Celery worker process after its first load.
- The maximum output-pixel setting prevents unsafe memory growth.
- Missing or failed restoration falls back to the OpenCV enhancement path.
- Restoration metadata records whether EDSR was requested and applied, its
  provider, scale, dimensions, and safe fallback reason.
- EDSR improves enlargement quality but does not guarantee recovery of facial
  information absent from the source.

## 10. Operational verification

The real non-eager Celery pipeline was verified on `image6.jpg` on July 20, 2026.

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
| Automated test suite | 85 tests passing |
| Django system check | no issues |
| Redis and Celery | connected and ready |

The 324 ms queue response confirms that the user request is no longer blocked by
the approximately 144-second CPU inference. The current CPU duration remains a
known performance limitation and must be represented as background progress in
the frontend.

## 11. Fairness and skin-tone preservation

The OpenCV baseline does not classify skin colour. It measures overall luminance
and changes only the LAB lightness channel while preserving the chroma channels.
This reduces unintended colour shifts between people with different skin tones.
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

## 13. Zero-DCE implementation

- The official academic `Epoch99.pth` checkpoint is exported once to ONNX.
- Production inference uses OpenCV DNN and does not require PyTorch.
- Only low-light inputs request Zero-DCE; well-lit inputs retain the conservative OpenCV route.
- EDSR can run first when low resolution is detected, followed by Zero-DCE for low light.
- Eight learned RGB curve maps are applied at the original output dimensions.
- Missing, disabled, corrupt, or incompatible models fall back safely to adaptive OpenCV.
- Result metadata records whether Zero-DCE was requested and applied, its provider, inference dimensions, model filename, curve iterations, and fallback reason.
- The official implementation is licensed for academic/non-commercial use. Commercial deployment requires a separately approved model/license decision.

The local model path is `.runtime/models/zero_dce_epoch99.onnx`. Runtime assets are intentionally excluded from Git. Use `scripts/export_zero_dce_onnx.py` with the official checkpoint to reproduce the model.

## 14. Remaining implementation

- Evaluate an identity-preserving face-restoration model for severe blur.
- Add advanced retry and user-requested cancellation.
- Add face-aware local exposure correction after multi-face detection exists.
- Evaluate colour preservation across diverse, consented test images.
- Connect the frontend to the real APIs and job states.

## 15. Main implementation files

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
- `config/settings.py`
- `config/urls.py`
