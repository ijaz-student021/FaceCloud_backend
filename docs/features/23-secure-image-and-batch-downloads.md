# Feature 23: Secure Image and Batch Downloads

## Status

**Complete backend.** Frontend integration remains.

Feature 23 provides authenticated individual downloads, asynchronous batch ZIP
generation, expiring generated packages, and an owner-only download history.

## User capabilities

- Download an owned original image.
- Download its standardized processing copy when available.
- Download a completed enhanced result.
- Download a generated JSON, CSV, or PDF processing report.
- Request a ZIP containing only accepted enhanced images.
- Request a complete batch ZIP containing every available original,
  standardized, and enhanced image.
- Select JSON, CSV, or PDF as the report embedded in a ZIP.
- View package generation status and download history.
- Regenerate a failed or expired package.
- Delete a generated package and its physical ZIP file.

## Package structure

An accepted-results package contains:

```text
enhanced/
reports/batch-report.csv
manifest.json
```

A complete-batch package can contain:

```text
original/
standardized/
enhanced/
reports/batch-report.json
manifest.json
```

The report extension reflects the requested report format. Missing processing
artifacts are listed safely in `manifest.json`; their server storage paths are
never exposed.

## API contract

All endpoints require a valid JWT access token.

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/downloads/batches/{batch_id}/packages/` | Queue or reuse a batch package |
| `GET` | `/api/downloads/packages/` | List the user's generated packages |
| `GET` | `/api/downloads/packages/{id}/` | Read package state and metadata |
| `DELETE` | `/api/downloads/packages/{id}/` | Delete metadata and the generated ZIP |
| `GET` | `/api/downloads/packages/{id}/download/` | Download a ready, unexpired ZIP |
| `POST` | `/api/downloads/packages/{id}/regenerate/` | Replace a failed or expired package |
| `GET` | `/api/downloads/` | List the user's download history |

Package creation request:

```json
{
  "package_type": "accepted_results",
  "report_format": "csv"
}
```

Supported package types are `accepted_results` and `complete_batch`. Supported
report formats are `json`, `csv`, and `pdf`.

The package list supports `status`, `package_type`, `report_format`, `batch`,
`page`, and `page_size` query parameters. Download history supports
`resource_type`, `batch`, `page`, and `page_size`.

## Background generation workflow

1. The API verifies that the batch belongs to the authenticated user.
2. The batch must be in a terminal processing state.
3. An accepted-results request must have at least one accepted image with a
   completed enhancement.
4. An idempotency constraint reuses an equivalent queued or generating package.
5. Celery runs the `downloads.generate_package` task.
6. Source files are streamed through Django storage into a temporary ZIP.
7. The completed ZIP is stored under the owner's generated-package directory.
8. File size, SHA-256 checksum, manifest, generation time, and expiry are saved.
9. The temporary file is removed even when generation fails.

## Security and privacy controls

- Every list, detail, action, and file query is filtered by the authenticated
  owner.
- Another user's resource returns `404` rather than disclosing its existence.
- Archive names are sanitized, length-bounded, and collision-safe.
- ZIP members cannot use absolute paths or path traversal.
- Package file count and uncompressed size are bounded before generation.
- ZIP64 and streamed file copying avoid loading an entire batch into memory.
- Stored ZIP objects use UUID-based internal names.
- Queue and generation failures return generic messages without infrastructure
  details.
- Deleting a package or its owner removes the physical generated file.
- Download events contain safe snapshot metadata, not private storage paths.

## Expiry and cleanup

Generated packages expire after 24 hours by default. An expired request returns
HTTP `410 Gone`, removes the physical ZIP, and keeps safe history metadata.

Manual cleanup command:

```powershell
python manage.py cleanup_expired_downloads
```

Celery also exposes `downloads.cleanup_expired` for scheduled cleanup. Production
deployment should invoke it periodically with Celery Beat or an equivalent
scheduler.

## Configuration

```dotenv
DOWNLOAD_PACKAGE_TTL_HOURS=24
DOWNLOAD_PACKAGE_MAX_FILES=500
DOWNLOAD_PACKAGE_MAX_UNCOMPRESSED_SIZE=16106127360
DOWNLOAD_PACKAGE_COMPRESSION_LEVEL=6
```

Package creation is additionally protected by the `download_package` API
throttle, configured as 30 requests per hour by default.

## Download history

Successful downloads from the original-image, standardized-image,
enhanced-image, processing-report, and generated-package endpoints create a
`DownloadEvent`. Events retain the resource type, safe filename, byte size,
content type, optional batch/image identifiers, and timestamp.

## Verification

- Migration `downloads.0001_initial` is applied to PostgreSQL.
- 13 focused tests cover ZIP contents, duplicate filenames, owner isolation,
  expiry, regeneration, cleanup, history, size limits, queue failures, and file
  deletion.
- The complete backend suite passes **131 tests** against PostgreSQL.
- `python manage.py check` reports no issues.
- `python manage.py makemigrations --check --dry-run` reports no drift.

## Frontend integration remaining

- Add accepted-results and complete-batch actions to the Downloads page.
- Let the user choose JSON, CSV, or PDF for the embedded report.
- Poll the returned package detail while status is `queued` or `generating`.
- Enable download only when status is `ready` and `download_url` is present.
- Show expiry, failure, regenerate, and delete actions.
- Replace demonstration download history with `GET /api/downloads/`.
