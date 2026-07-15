# Feature 03: Image Preprocessing and Standardization

## Status

Implemented and covered by automated tests.

## Purpose and pipeline

This feature creates a safe PNG or JPEG processing copy before enhancement. The
original upload is never modified.

```text
Upload -> content validation -> format gateway -> EXIF orientation correction
       -> color normalization -> bounded resize -> metadata-free PNG/JPEG copy
       -> enhancement-ready result
```

## Supported inputs

- JPG/JPEG, PNG, WebP, BMP, TIFF (`.tif`/`.tiff`), and static GIF
- Animated GIF files are rejected.
- HEIC/HEIF, AVIF, RAW camera files, and other formats remain unsupported until
  secure decoders and dedicated tests are added.

## Conversion and standardization

- `auto`: PNG when real transparency exists; JPEG otherwise.
- `png`: lossless PNG output with transparency when present.
- `jpeg`: optimized RGB JPEG using the configured quality; transparency is
  composited over the configured background.
- EXIF orientation is applied before output.
- Aspect ratio is preserved and smaller images are not enlarged.
- Output is bounded by the configured maximum width and height.
- RGB, RGBA, grayscale, palette, and CMYK inputs are normalized safely.
- The generated copy does not retain source EXIF or GPS metadata.

## Storage and naming

- Originals remain under `users/{user_id}/originals/`.
- Processing copies use UUID paths under `users/{user_id}/preprocessed/`.
- User-selected download names are sanitized; user input never controls a
  physical storage path.
- The batch `display_name` can be edited without renaming its secure storage
  directory.
- Deleting an image, batch, or account also deletes its processing copy.

## Database model

`ImagePreprocessingResult` has a one-to-one relationship with `ImageUpload`. It
records the owner, status, requested options, source/output formats, dimensions,
sizes, transparency, duration, failure reason, file, and timestamps.

Statuses are `pending`, `processing`, `completed`, and `failed`.

## API

All endpoints require JWT authentication and restrict records to their owner.

ZIP uploads automatically preprocess every accepted extracted image using
`output_format=auto`. Transparent inputs become PNG; other images become JPEG.
Invalid archive entries remain rejected with their individual reason. A failed
conversion does not delete the accepted original and can be retried later.

### Start preprocessing

```http
POST /api/images/{image_id}/preprocess/
Content-Type: application/json

{
  "output_format": "jpeg",
  "output_name": "Ali Profile",
  "force": false
}
```

`output_format` accepts `auto`, `png`, or `jpeg`. An identical completed request
returns HTTP 200; a new or forced operation returns HTTP 201.

### Read result and download

```http
GET /api/images/{image_id}/preprocessing/
GET /api/images/{image_id}/preprocessing/download/
```

### Rename the batch display name

```http
PATCH /api/images/batches/{batch_id}/
Content-Type: application/json

{"display_name": "Ali Wedding Photos"}
```

## Configuration

```env
PREPROCESS_MAX_WIDTH=1920
PREPROCESS_MAX_HEIGHT=1920
PREPROCESS_JPEG_QUALITY=92
PREPROCESS_TRANSPARENT_BACKGROUND="#FFFFFF"
```

## Security and reliability

- File content is verified instead of trusting its extension.
- Extensions must match detected content.
- File-size, pixel-count, ZIP, and decompression protections remain enforced.
- Unsupported, animated, and unsafe files are rejected with a reason.
- Cross-user preprocessing, status, and download requests return 404.
- Identical operations reuse the completed result; `force` creates a replacement.
- The previous successful file is removed only after replacement succeeds.

## Tests

```powershell
.\venv\Scripts\python.exe manage.py test uploads --keepdb -v 1
.\venv\Scripts\python.exe manage.py test --keepdb -v 1
```

Coverage includes formats, conversion, transparency, EXIF rotation, resizing, no
upscaling, safe naming, idempotency, forced replacement, ownership, downloads,
batch display names, original preservation, and file cleanup.

## Current limitation

ZIP preprocessing currently runs synchronously after extraction. Very large ZIP
batches will move to the planned Celery and progress-tracking feature so the HTTP
request does not remain open for the complete processing duration.
