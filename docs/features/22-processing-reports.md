# Feature 22: Processing Reports

## Status

**Complete backend**

FaceCloud generates durable, owner-scoped reports for one processed image or a
completed batch. JSON, CSV, and PDF outputs are generated asynchronously from the
same immutable payload snapshot, so every format describes the same result.

Frontend integration for the Downloads page remains a separate task.

## Purpose

Reports show what FaceCloud received, which stages ran, why an image was accepted
or rejected, and where its authenticated owner can retrieve available results.
They support user downloads, demonstrations, batch review, and future audits.

## Supported reports

| Report type | Source | Formats |
|---|---|---|
| Image | One owner-scoped image | JSON, CSV, PDF |
| Batch | One owner-scoped completed batch | JSON, CSV, PDF |

## Contents

Each image record can contain:

- Image and batch identifiers, filename, status, and timestamps
- Safe rejection and failure reasons
- Original format, dimensions, MIME type, and size
- Preprocessing state and standardized-output metadata
- Requested/applied enhancement level, public algorithm identifier, and output metadata
- Overall, brightness, sharpness, contrast, noise, exposure, and resolution scores
- Derived blur score
- Face decision, face count, detector, and normalized confidence
- Duplicate decision, match count, and policy version
- Total recorded processing duration
- Protected original, standardized, and enhanced API links
- An explicit identity placeholder until Feature 18 is implemented

Batch reports also contain upload counters, processing state, status totals, total
input size, and batch metadata.

## Score semantics

Quality values use a normalized 0–100 scale. Higher sharpness means a clearer
image. The report derives blur as:

```text
blur score = 100 - sharpness score
```

A higher blur score therefore indicates more blur. It is not the raw Laplacian
measurement used internally by the quality analyzer.

## Identity behavior

Feature 18 is not implemented, so reports return an explicit placeholder instead
of guessing an identity:

```json
{
  "available": false,
  "group_id": null,
  "confidence": null
}
```

No face embeddings or private biometric fingerprints are exposed.

## Workflow

```text
Authenticated request
        |
Verify owner and terminal processing state
        |
Create queued ProcessingReport
        |
Dispatch reports.generate Celery task
        |
Build one normalized immutable snapshot
        |
Render JSON, CSV, or PDF
        |
Store protected file and mark report ready
```

## API endpoints

Every endpoint requires a valid JWT access token.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/reports/images/{image_id}/` | Queue an image report |
| `POST` | `/api/reports/batches/{batch_id}/` | Queue a batch report |
| `GET` | `/api/reports/` | List the current user's reports |
| `GET` | `/api/reports/{report_id}/` | Read state and snapshot |
| `GET` | `/api/reports/{report_id}/download/` | Download a ready report |
| `POST` | `/api/reports/{report_id}/regenerate/` | Generate another report |

Request body:

```json
{
  "format": "pdf"
}
```

`format` accepts `json`, `csv`, or `pdf` and defaults to `json`.

A new job returns HTTP `202 Accepted`. If the same source and format already has
a queued or generating job, the API reuses it and returns HTTP `200 OK` with
`created: false`. The list endpoint supports `status`, `format`, and `report_type`
filters plus bounded pagination.

## Status and readiness rules

- `queued`: waiting for Celery
- `generating`: snapshot/file generation is active
- `ready`: payload and file are available
- `failed`: generation failed safely

The source must reach a terminal processing state. Premature generation and
download requests return HTTP `409 Conflict` with code `report_not_ready`.

## Security and privacy

- Every query and action is owner-scoped.
- Cross-user sources and reports return a non-disclosing HTTP `404`.
- Files use UUID names under `users/{owner_id}/reports/`.
- Downloads are authenticated and never expose storage paths.
- Original checksums, perceptual hashes, face fingerprints, task identifiers,
  private model parameters, and filesystem paths are excluded.
- CSV formula-control prefixes are escaped to prevent spreadsheet injection.
- User-controlled PDF text is escaped before rendering.
- Generation has a dedicated API throttle.
- Source, batch, account, and report deletion clean up stored report files.

## Reliability and concurrency

- Celery keeps generation outside the HTTP request.
- Database constraints allow one active report per owner, source, and format.
- Transaction locking plus constraint recovery makes concurrent duplicate requests
  idempotent.
- Task IDs stop obsolete workers from replacing newer work.
- Queue and rendering failures use generic messages without leaking internals.
- Successful files store their byte size and SHA-256 integrity checksum.

## Database model

`ProcessingReport` stores its owner, type, format, state, exactly one source,
schema version, immutable JSON payload, protected file metadata, integrity
checksum, task identifier, safe failure reason, and lifecycle timestamps.

A database check constraint enforces a matching source/type. Conditional unique
constraints prevent duplicate active generation jobs.

## Verification

Automated tests cover normalized values, private-data exclusion, JSON/CSV/PDF
validity, CSV injection defense, immutable snapshots, checksums, batch summaries,
queueing, idempotency, readiness, owner isolation, downloads, safe queue failure,
filters, regeneration, and physical file cleanup.

Run the focused suite:

```powershell
python manage.py test reports --keepdb
```

## Frontend integration

The Downloads page should request a report, poll its detail while queued or
generating, enable download only when ready, and show regenerate when failed. It
must use the authenticated `download_url`, not a media path. Identity information
must be shown only when `identity_group.available` is true.

## Related remaining work

- Feature 18: real identity-group values
- Feature 23: accepted-image and full-batch ZIP packages
- Feature 28: report/download activity events
- Feature 29: report-ready notifications
- Feature 43: private object storage for production
