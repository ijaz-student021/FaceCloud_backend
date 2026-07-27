# Feature 30: User Dashboard Statistics and My Images API

## Status

Backend complete. The frontend can now replace demonstration values with authenticated API data.

## User dashboard

`GET /api/user/dashboard/` returns only the authenticated user's totals, storage usage, seven-day upload/accepted/rejected chart, five recent uploads, and ten recent upload or enhancement activities.

`accepted_images` currently means images with a successfully completed enhancement. It is not final face-quality acceptance until face validation is implemented. Identity grouping is also not implemented, so the API explicitly returns `identity_groups: 0` and `identity_groups_available: false` instead of invented data.

## My Images

`GET /api/images/my/` is owner-only and paginated. It accepts `page`, `page_size`, `search`, `status`, `batch`, `upload_type`, and `ordering`. Page size defaults to 24 and is limited to 100. Ordering supports `newest`, `oldest`, `name`, `-name`, `size`, and `-size`.

Status filters support `uploaded`, `preprocessing`, `ready`, `pending`, `queued`, `processing`, `completed`, `partial`, `failed`, `cancelled`, and `rejected`. Each item includes original metadata, preprocessing, batch information, download URL, normalized display status, and the latest enhancement result. Historical enhancement attempts cannot override the latest state.

The old `GET /api/images/` endpoint remains unchanged for compatibility.

## Privacy and frontend integration

- JWT authentication is required and every query is scoped to `request.user`.
- Another user's images, storage, activity, and results are excluded.
- Invalid filters return HTTP 400 with a useful explanation.
- Overview should load `/api/user/dashboard/`.
- My Images should load `/api/images/my/` and send UI filters as query parameters.
- The frontend must remove hard-coded sample totals and show an empty state when `count` is zero.

Automated tests cover calculations, seven-day output, pagination, filtering, latest-result selection, and cross-user isolation.
