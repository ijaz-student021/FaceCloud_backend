# Features 20–21: Background Processing and Live Batch Progress

## Status

Backend implementation complete. The frontend should poll the progress endpoint every 2–3 seconds while a batch is active.

## Purpose

Multiple-image and ZIP batches run through Celery without keeping the upload HTTP request open. The progress system stores the actual state of every image and aggregates those records into a batch response.

## Processing lifecycle

`uploaded → queued → preprocessing → face_validation → quality_analysis → enhancement → completed`

An image can finish as `accepted`, `rejected`, `needs_review`, `completed`, `failed`, or `cancelled`. Enhancement is included only after the user requests an enhancement level; the tracker does not choose one automatically.

## Progress calculation

- Upload accepted: 10%
- Preprocessing: up to 30%
- Face validation: up to 45%
- Quality analysis: up to 65%
- Enhancement, when requested: up to 95%
- Finalization: 100%

The batch percentage is the average of its persistent per-image records. Counts cover every terminal and active state. ETA is returned when completed-image duration evidence is available; otherwise it is `null`.

## API endpoints

All endpoints require a valid JWT and restrict access to the batch owner.

- `GET /api/images/batches/{batch_id}/progress/` returns the aggregate and per-image state.
- `POST /api/images/batches/{batch_id}/retry/` retries only failed images from their recorded failure stage. The default limit is three retries.
- `POST /api/images/batches/{batch_id}/cancel/` requests cooperative cancellation.

Workers inspect the cancellation flag before expensive work and between batch items. A running task is not killed while it may be writing a file.

## Storage

- `UploadBatch` stores current stage, cancellation flag, retry count, and the last progress update.
- `BatchImageProgress` stores stage, status, percentage, failure information, retry count, and timestamps for each image.
- Image files remain in the existing owner-scoped original, preprocessed, and enhanced media folders.

## Frontend integration

1. Upload multiple images or a ZIP archive and retain the returned batch ID.
2. Poll the progress endpoint every 2–3 seconds.
3. Stop on `completed`, `partial`, `failed`, or `cancelled`.
4. Show per-image failure reasons and Retry only when `counts.failed` is greater than zero.
5. Ask for confirmation before sending Cancel.

## Verification

Automated tests cover persistence, owner isolation, cooperative cancellation, and failed-image retry.
