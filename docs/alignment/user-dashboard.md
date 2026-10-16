# User dashboard alignment audit

The frontend (`ali4347/facecloud-ai-platform`, read-only) is the reference; the backend adapts. This table has one row per
user-portal screen: the endpoint(s) it calls, the fields it reads, and whether the backend already provides them.
Status is `matches`, `gap` (closed later in this branch; the row is updated when it is), `waiting for #68`, or an explicit exception.

Audited against `main` at `945ff58`. Everything added by this branch is additive: no field or endpoint is renamed or removed,
and consent behaviour is unchanged.

| # | Screen | Endpoint(s) | Fields the screen reads | Status |
|---|---|---|---|---|
| 1 | Overview | `GET /api/user/dashboard/`, `GET /api/images/my/` | counts, recent uploads, status | matches |
| 2 | Upload Center | `POST /api/images/upload/{single,multiple,zip}/` | `UploadBatch` id, counts | matches |
| 3 | Single upload | `POST /api/images/upload/single/` | batch + `images[]` | matches |
| 4 | Multiple upload (Summary / upload results, Processing Complete) | `POST /api/images/upload/multiple/`, `GET /api/images/batches/{id}/progress/` | `accepted_files`, `rejected_files`, `images[].status`, `images[].rejection_reason` | matches (verified, item 7) |
| 5 | ZIP upload | `POST /api/images/upload/zip/` | same as above | matches (verified, item 7) |
| 6 | Processing | `GET /api/images/batches/{id}/progress/`, `POST .../retry/`, `.../cancel/` | progress, per-file state | matches |
| 7 | Your uploads | `GET /api/images/batches/` | `display_name`, `total_files`, `status`, `created_at` | matches |
| 8 | My Images | `GET /api/images/my/` | name, thumbnail, `display_status`, quality | matches |
| 9 | Image detail | `GET /api/images/{id}/`, `/thumbnail/`, `/download/`, enhancement endpoints | image, latest enhancement, groups | matches |
| 10 | Person Folders (list) | `GET /api/identities/groups/` | `id`, `display_name`, `member_count`, **cover images**, **matching confidence** | gap: `cover_images`, `matching_confidence` missing (item 2a) |
| 11 | Person Folder detail | `GET /api/identities/groups/{id}/`, **images list**, export, share | members with name, thumbnail, size, dimensions, quality, confidence; export subset; share link | gap: no paginated member-image list (2b), export has no `image_ids` (2c), no folder share link (2d) |
| 12 | Downloads | `GET /api/downloads/` | `kind`, `expires_at`, `image_count`, `has_active_link`; a way to mint a fresh link | gap (item 3) |
| 13 | Notifications and bell | `GET /api/notifications/`, `unread-count/`, `mark-all-read/`, `{id}/read/` | `severity` (info/success/warning/error), `title`, `message`, `is_read`, `created_at` | matches; severity values already read as success/warning/error/info (item 5: documented, no alias) |
| 14 | Activity and history button | `GET /api/user/activity/` | `id`, `type`, `message`, `timestamp`, `status` | gap: entries have no `id`, `type`, `message`, `timestamp` or `status`, and no response schema (item 4) |
| 15 | Settings: Profile | `GET/PATCH /api/user/profile/` | Full Name, Organization, Job Title, Phone, Country, avatar | waiting for #68 |
| 16 | Settings: Security (password, sign out everywhere) | `POST /api/auth/password-change/`, `POST /api/user/logout-all/` | status only | matches |
| 17 | Settings: Active Sessions | session list and revoke | device name, ip, `last_seen_at`, `current`, revoke | waiting for #68 |
| 18 | Settings: Notifications | `GET/PATCH /api/notifications/preferences/` | per-event toggles | matches |
| 19 | Settings: Camera & Privacy | `GET/POST /api/privacy/consents/`, `GET /api/privacy/export/`, `GET/DELETE /api/privacy/facial-data/` | consent state, export, erase | matches (consent errors gain a stable `code`, item 8) |
| 20 | Top-bar search | **`GET /api/search/?q=&limit=`** | images, groups, jobs | gap: endpoint does not exist (item 1) |

## Notes on the brief

* `GET /api/identities/groups/{id}/` already returns `memberships` (id, image id, `similarity_score`); the new images list adds names, thumbnails, sizes, quality and pagination.
* Notification `severity` already uses `info|success|warning|error`; nothing to alias.
* The frontend `ActivityEvent.type` also lists `admin`; the backend does not emit it for the user timeline.
* `ProcessingReport` has no `expires_at`; the Downloads `expires_at` is `null` for reports.

## Closing the gaps

Updated as each commit lands; see the pull request for before/after bodies.
