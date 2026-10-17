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
| 10 | Person Folders (list) | `GET /api/identities/groups/` | `id`, `display_name`, `member_count`, **cover images**, **matching confidence** | matches: `cover_images` (up to 4, with `thumbnail_url`) and `matching_confidence` (percentage, null when unscored) added to list and detail |
| 11 | Person Folder detail | `GET /api/identities/groups/{id}/`, **images list**, export, share | members with name, thumbnail, size, dimensions, quality, confidence; export subset; share link | member photos: matches (`GET /api/identities/groups/{id}/images/`, paginated); export subset: matches (`image_ids` on the export request, ids outside the folder are a 400); folder share link: matches (`POST /api/identities/exports/{id}/share/`, revoke via `/api/shares/{id}/revoke/`) |
| 12 | Downloads | `GET /api/downloads/` | `kind`, `expires_at`, `image_count`, `has_active_link`; a way to mint a fresh link | matches: `kind`, `expires_at`, `image_count`, `has_active_link` added to `GET /api/downloads/`; fresh links: images `POST /api/shares/`, folder ZIPs `POST /api/identities/exports/{id}/share/`, batch ZIPs `POST /api/downloads/packages/{id}/regenerate/` |
| 13 | Notifications and bell | `GET /api/notifications/`, `unread-count/`, `mark-all-read/`, `{id}/read/` | `severity` (info/success/warning/error), `title`, `message`, `is_read`, `created_at` | matches; severity values already read as success/warning/error/info (item 5: documented, no alias) |
| 14 | Activity and history button | `GET /api/user/activity/` | `id`, `type`, `message`, `timestamp`, `status` | matches: entries gain `id`, `type`, `message`, `timestamp`, `status` (old keys kept) and the response has a schema |
| 15 | Settings: Profile | `GET/PATCH /api/user/profile/` | Full Name, Organization, Job Title, Phone, Country, avatar | waiting for #68 |
| 16 | Settings: Security (password, sign out everywhere) | `POST /api/user/change-password/`, `POST /api/user/logout-all/` | status only | matches |
| 17 | Settings: Active Sessions | session list and revoke | device name, ip, `last_seen_at`, `current`, revoke | waiting for #68 |
| 18 | Settings: Notifications | `GET/PATCH /api/notifications/preferences/` | per-event toggles | matches |
| 19 | Settings: Camera & Privacy | `GET/POST /api/privacy/consents/`, `GET /api/privacy/export/`, `GET/DELETE /api/privacy/facial-data/` | consent state, export, erase | matches (consent errors gain a stable `code`, item 8) |
| 20 | Top-bar search | **`GET /api/search/?q=&limit=`** | images, groups, jobs | matches: `GET /api/search/` added (ownership-scoped, min 2 chars, limit 5/max 10, throttle scope `search`); jobs match the batch name or the ZIP's file name and are labelled like "Your uploads" (`display_name`, else `original_archive_name`, else `Upload <id>`) |

## Notes on the brief

* `GET /api/identities/groups/{id}/` already returns `memberships` (id, image id, `similarity_score`); the new images list adds names, thumbnails, sizes, quality and pagination.
* Notification `severity` already uses `info|success|warning|error`; nothing to alias.
* The frontend `ActivityEvent.type` also lists `admin`; the backend does not emit it for the user timeline.
* `ProcessingReport` has no `expires_at`; the Downloads `expires_at` is `null` for reports.

## Closing the gaps

| Item | Rows | What was added |
|---|---|---|
| 1 | 20 | New `search` package: `GET /api/search/?q=&limit=` (throttle scope `search`, 600/hour) |
| 2a | 10, 11 | `cover_images` and `matching_confidence` on `GET /api/identities/groups/` and `/{id}/` |
| 2b | 11 | `GET /api/identities/groups/{id}/images/` (paginated) |
| 2c | 11 | `image_ids` on `POST /api/identities/groups/{id}/exports/`; reported on the export; identities migration 0007 |
| 2d | 11 | `POST /api/identities/exports/{id}/share/`; `ShareLink.identity_export`, sharing migration 0002; public view serves the ZIP |
| 3 | 12 | `kind`, `expires_at`, `image_count`, `has_active_link` on `GET /api/downloads/` |
| 4 | 14 | `id`, `type`, `message`, `timestamp`, `status` on `GET /api/user/activity/` entries, plus a response schema |
| 5 | 13 | Documentation only: severity already is success/warning/error/info |
| 7 | 4, 5 | Verified, no code change; the response is pinned by `uploads/test_batch_results_contract.py` |
| 8 | 19 | `code: "consent_required"` on the three consent refusals (identity analysis, face search, person collage) |

Rows 15 (Profile) and 17 (Active Sessions) stay **waiting for #68**; see the pull request for what the follow-up should check.
