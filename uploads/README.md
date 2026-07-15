# FaceCloud image upload API

All endpoints require a verified, active, non-suspended user JWT.

## Limits

- Image: 400 MB by default (`MAX_IMAGE_UPLOAD_SIZE`)
- ZIP: 2 GB by default (`MAX_ZIP_UPLOAD_SIZE`)
- Total uncompressed ZIP content: 5 GB by default (`MAX_ZIP_UNCOMPRESSED_SIZE`)
- Batch: 150 files by default (`MAX_UPLOAD_BATCH_FILES`)
- Formats: JPG/JPEG, PNG, WebP, BMP, TIFF, and static GIF

The limits are environment-configurable. Reverse proxies and hosting platforms must be configured to allow the same request sizes.

## Endpoints

- `POST /api/images/upload/single/` - exactly one image
- `POST /api/images/upload/multiple/` - at least two images using repeated `files` fields
- `POST /api/images/upload/zip/` - exactly one ZIP archive
- `GET /api/images/batches/`
- `GET /api/images/batches/{id}/`
- `DELETE /api/images/batches/{id}/`
- `GET /api/images/`
- `GET /api/images/{id}/`
- `DELETE /api/images/{id}/`
- `GET /api/images/{id}/download/`
- `POST /api/images/{id}/preprocess/` - create or reuse a PNG/JPEG processing copy
- `GET /api/images/{id}/preprocessing/` - preprocessing status and metadata
- `GET /api/images/{id}/preprocessing/download/` - download the processing copy
- `PATCH /api/images/batches/{id}/` - update the user-visible batch name

Every list, detail, download, and deletion query is restricted to the authenticated owner. Invalid files in a multiple-image or ZIP batch are recorded with a rejection reason while valid files continue processing.

Accepted ZIP images are queued for preprocessing. Existing PNG/JPEG formats are
preserved; other transparent images produce PNG and other images produce JPEG.

Single images preprocess immediately. Multiple images use the Celery queue. The
preprocessing endpoint remains available for explicit PNG/JPEG output or forced
reprocessing. Batch responses expose processing state, counters, task ID,
timestamps, and a safe error message.
