# FaceCloud image upload API

All endpoints require a verified, active, non-suspended user JWT.

## Limits

- Image: 400 MB by default (`MAX_IMAGE_UPLOAD_SIZE`)
- ZIP: 2 GB by default (`MAX_ZIP_UPLOAD_SIZE`)
- Total uncompressed ZIP content: 5 GB by default (`MAX_ZIP_UNCOMPRESSED_SIZE`)
- Batch: 150 files by default (`MAX_UPLOAD_BATCH_FILES`)
- Formats: JPG, JPEG, PNG

The limits are environment-configurable. Reverse proxies and hosting platforms must be configured to allow the same request sizes.

## Endpoints

- `POST /api/images/upload/single/` - multipart field `file` or `image`
- `POST /api/images/upload/multiple/` - repeated multipart field `files`
- `POST /api/images/upload/zip/` - multipart field `file` or `archive`
- `GET /api/images/batches/`
- `GET /api/images/batches/{id}/`
- `DELETE /api/images/batches/{id}/`
- `GET /api/images/`
- `GET /api/images/{id}/`
- `DELETE /api/images/{id}/`
- `GET /api/images/{id}/download/`

Every list, detail, download, and deletion query is restricted to the authenticated owner. Invalid files in a multiple-image or ZIP batch are recorded with a rejection reason while valid files continue processing.
