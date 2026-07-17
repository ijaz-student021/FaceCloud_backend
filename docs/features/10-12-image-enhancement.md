# Features 10–12: Image Enhancement

## 1. Overview

This feature enhances standardized images while preserving the uploaded original.
It provides Low, Medium, High, and Auto enhancement levels and records every
single-image or batch operation so progress and failures can be reported safely.

Current status: **Partial overall**. Features 10 and 11 are complete at the backend
baseline: enhancement levels, OpenCV processing, Celery batch execution, progress,
owner-only APIs, comparison URLs, and secure downloads are implemented. Feature 12,
the Zero-DCE model, remains planned.

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
- Owner-only result, job, comparison-link, and download APIs
- Automated processing, batch-progress, auto-level, and cross-user privacy tests
- Image-quality analysis for exposure, contrast, blur, noise, clipping, and resolution
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
and processing duration.

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
| POST | `/api/enhancements/images/{image_id}/` | Enhance one preprocessed image |
| POST | `/api/enhancements/batches/{batch_id}/` | Queue a complete batch |
| GET | `/api/enhancements/jobs/` | List the user's batch jobs |
| GET | `/api/enhancements/jobs/{id}/` | Read progress and results |
| GET | `/api/enhancements/results/` | List the user's results |
| GET | `/api/enhancements/results/{id}/` | Read one result and comparison URLs |
| GET | `/api/enhancements/results/{id}/download/` | Download an enhanced image securely |

Request body:

```json
{"level": "auto"}
```

Allowed levels are `low`, `medium`, `high`, and `auto`.

## 7. Fairness and skin-tone preservation

The OpenCV baseline does not classify skin colour. It measures overall luminance
and changes only the LAB lightness channel while preserving the chroma channels.
This reduces unintended colour shifts between people with different skin tones.
Face-aware local exposure correction and diverse evaluation images remain required
before claiming demographic fairness in production.

The EDSR stage improves spatial resolution for eligible small images but cannot
guarantee recovery of facial detail absent from the source. Severe blur remains
flagged for a future face-restoration model and quality review.

## 8. Remaining implementation

- Integrate and evaluate pretrained Zero-DCE weights.
- Evaluate an identity-preserving face-restoration model for severe blur.
- Add advanced retry and user-requested cancellation.
- Add face-aware local exposure correction after multi-face detection exists.
- Evaluate colour preservation across diverse, consented test images.
- Connect the frontend to the real APIs and job states.

## 9. Main implementation files

- `enhancement/models.py`
- `enhancement/admin.py`
- `enhancement/apps.py`
- `enhancement/services.py`
- `enhancement/tasks.py`
- `enhancement/serializers.py`
- `enhancement/views.py`
- `enhancement/urls.py`
- `enhancement/migrations/0001_initial.py`
- `enhancement/tests.py`
- `config/settings.py`
- `config/urls.py`
