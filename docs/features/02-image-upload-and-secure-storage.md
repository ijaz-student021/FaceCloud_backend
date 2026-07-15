# Feature 02: Image Upload and Secure Storage

## 1. Overview

This feature accepts facial-image inputs before enhancement. It supports a single image, multiple images, and ZIP archives while validating actual file content, recording metadata, enforcing ownership, and storing files under secure generated names.

The feature deliberately handles ingestion and storage only. Resizing, enhancement, quality scoring, and face detection are implemented as later processing stages so failures remain isolated and testable.

## 2. Implemented requirements

- Single-image upload
- Multiple-image upload
- ZIP archive upload
- JPG, JPEG, and PNG support
- Maximum 400 MB per image
- Maximum 2 GB uploaded ZIP
- Maximum 5 GB uncompressed ZIP content
- Maximum 150 files per batch
- Content-based image validation with Pillow
- Secure UUID storage names
- Original filename preservation in metadata
- Width, height, MIME type, size, and SHA-256 checksum
- Per-file acceptance or rejection
- Partial success for multiple-image and ZIP batches
- Owner-only listing, details, downloads, and deletion
- Physical file cleanup after image, batch, or account deletion

## 3. Architecture and workflow

```text
Authenticated multipart request
  -> request and batch limit checks
  -> file or ZIP security validation
  -> actual image-content verification
  -> metadata and checksum calculation
  -> secure UUID storage path
  -> ImageUpload record
  -> batch counters and status
  -> structured API response
```

For mixed batches, a bad file creates a rejected metadata record with a reason. Other valid files continue processing.

## 4. Database design

### UploadBatch

Represents one upload operation:

- `owner`
- `upload_type`: single, multiple, or ZIP
- `original_archive_name`
- `status`: uploaded, partial, rejected, or failed
- `total_files`
- `accepted_files`
- `rejected_files`
- `total_size`
- Creation and update timestamps

### ImageUpload

Represents one accepted or rejected input:

- `owner`
- Related upload batch
- Stored original file, blank for rejected inputs
- Original filename
- File extension and MIME type
- File size
- Width and height
- SHA-256 checksum
- Upload status
- Rejection reason
- Creation and update timestamps

The schema is introduced by `uploads.0001_initial`.

## 5. API endpoints

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| POST | `/api/images/upload/single/` | Authenticated owner | Upload one image using `file` or `image` |
| POST | `/api/images/upload/multiple/` | Authenticated owner | Upload repeated `files` fields |
| POST | `/api/images/upload/zip/` | Authenticated owner | Upload `file` or `archive` ZIP |
| GET | `/api/images/` | Authenticated owner | List the user's images |
| GET | `/api/images/{id}/` | Authenticated owner | View image metadata |
| GET | `/api/images/{id}/download/` | Authenticated owner | Download the stored original |
| DELETE | `/api/images/{id}/` | Authenticated owner | Delete metadata and physical file |
| GET | `/api/images/batches/` | Authenticated owner | List upload batches |
| GET | `/api/images/batches/{id}/` | Authenticated owner | View batch and per-file results |
| DELETE | `/api/images/batches/{id}/` | Authenticated owner | Delete batch and all stored files |

## 6. Example upload response

```json
{
  "id": 1,
  "upload_type": "single",
  "status": "uploaded",
  "total_files": 1,
  "accepted_files": 1,
  "rejected_files": 0,
  "total_size": 351758,
  "images": [
    {
      "id": 1,
      "original_name": "image1.png",
      "file_extension": ".png",
      "mime_type": "image/png",
      "width": 1200,
      "height": 1200,
      "status": "uploaded",
      "rejection_reason": "",
      "download_url": "http://127.0.0.1:8000/api/images/1/download/"
    }
  ]
}
```

## 7. Main implementation files

- `uploads/models.py`: batches, images, secure upload paths, and deletion cleanup
- `uploads/validators.py`: image-content and ZIP security validation
- `uploads/services.py`: per-file processing, batch finalization, ZIP streaming, and deletion
- `uploads/serializers.py`: metadata and nested batch responses
- `uploads/views.py`: upload, listing, download, and deletion APIs
- `uploads/urls.py`: `/api/images/` routes
- `uploads/admin.py`: batch and image inspection
- `uploads/tests.py`: validation, ownership, ZIP, and cleanup tests
- `uploads/README.md`: concise endpoint reference

## 8. Image validation

- Allows only `.jpg`, `.jpeg`, and `.png` names.
- Rejects empty files.
- Enforces configured byte limits.
- Uses Pillow to verify actual image content.
- Rejects mismatches between extension and image format.
- Rejects unreadable or corrupted images.
- Enforces maximum pixel count.
- Calculates dimensions and a SHA-256 checksum.
- Resets file streams after validation so storage receives complete content.

## 9. ZIP security

- Valid ZIP signature required
- Empty archive rejection
- Maximum archive byte size
- Maximum file count
- Maximum individual member size
- Maximum total uncompressed size
- Maximum compression ratio
- Path-traversal rejection
- Symbolic-link rejection
- Password-protected member rejection
- Nested-ZIP rejection
- Corrupt archive rejection
- Streamed member processing through spooled temporary files

ZIP entries are never extracted using their supplied filesystem paths.

## 10. Ownership and storage security

- Every batch and image is tied to the authenticated user.
- Querysets filter by the requesting owner.
- Other users receive `404` rather than ownership details.
- Files use UUID names under `users/{user_id}/originals/`.
- The public API exposes an authenticated download endpoint instead of an unrestricted direct file URL.
- Deletion signals remove physical files during image, batch, admin, cascade, or account deletion.
- Django's storage abstraction allows later replacement with private AWS S3 storage.

## 11. Configuration

| Setting | Default | Meaning |
|---|---:|---|
| `MAX_IMAGE_UPLOAD_SIZE` | 419430400 | 400 MB per image |
| `MAX_ZIP_UPLOAD_SIZE` | 2147483648 | 2 GB uploaded ZIP |
| `MAX_ZIP_UNCOMPRESSED_SIZE` | 5368709120 | 5 GB expanded ZIP content |
| `MAX_UPLOAD_BATCH_FILES` | 150 | Maximum files per batch |
| `MAX_IMAGE_PIXELS` | 100000000 | Maximum decoded pixels |
| `MAX_ZIP_COMPRESSION_RATIO` | 100 | ZIP-bomb threshold |
| `FILE_UPLOAD_MAX_MEMORY_SIZE` | 10485760 | Files above 10 MB spool to disk |

Hosting platforms, reverse proxies, and load balancers must also allow the configured request sizes.

## 12. Error and partial-success behavior

- Missing or structurally invalid requests return HTTP 400.
- Valid batch requests return HTTP 201.
- An invalid image inside a batch is recorded as rejected.
- A batch containing valid and invalid images receives `partial` status.
- A completely rejected batch receives `rejected` status.
- File-save exceptions remove partially stored content where possible.
- Physical storage is cleaned when database records are deleted.

## 13. Automated tests

The upload suite contains 15 test methods covering valid images, invalid content, byte and pixel limits, mixed multiple uploads, file-count limits, ZIP handling, path traversal, nested and invalid ZIP files, authentication, ownership isolation, secure filenames, and physical cleanup.

```text
Command: python manage.py test uploads --verbosity 2
Latest result: Passed
Combined authentication/upload suite: 29 tests passed
Verification date: 2026-07-14
```

## 14. Manual verification result

A real PNG was uploaded through the running development API:

```text
Original name: image1.png
Batch ID: 1
Image ID: 1
Status: uploaded
Accepted: 1
Rejected: 0
Dimensions: 1200 x 1200
Size: 351758 bytes
Checksum: generated
Protected download URL: generated
```

This confirmed multipart upload, database persistence, secure storage, metadata extraction, and response serialization outside the automated test environment.

## 15. Frontend integration

The frontend must provide:

- Single, multiple, and ZIP file selectors
- Client-side format and size guidance
- Upload progress for large requests
- Per-file accepted and rejected results
- Batch summary display
- Authenticated image listing and downloads
- Delete confirmation
- Clear display of backend rejection reasons

Client-side validation is for usability; backend validation remains authoritative.

## 16. Limitations and next stages

- Storage currently defaults to the local filesystem; private S3 storage is a later deployment feature.
- A 2 GB upload also requires compatible proxy and hosting limits.
- Upload completion does not mean enhancement completion.
- Resizing, standardization, Zero-DCE, quality assessment, face detection, and identity grouping belong to subsequent processing features.
- Background processing and live progress will be added before large batches are enhanced.

## 17. Completion checklist

- [x] Single upload complete
- [x] Multiple upload complete
- [x] ZIP upload complete
- [x] Migration applied
- [x] Ownership verified
- [x] ZIP security verified
- [x] Physical cleanup verified
- [x] Automated tests passed
- [x] Django system check passed
- [x] API documented
- [x] Real manual upload verified
