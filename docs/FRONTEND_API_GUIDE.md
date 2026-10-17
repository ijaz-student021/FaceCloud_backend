# Frontend API guide

For the developer building the FaceCloud frontend. This is the practical
"how do I use the API" companion to two things that stay authoritative:

- **The live schema** at `/api/docs/` (Swagger), `/api/redoc/` and
  `/api/schema/` (raw OpenAPI). Every endpoint, field and enum is there and is
  checked in CI, so if this guide and the schema ever disagree, the schema wins.
- [`API_ERRORS.md`](API_ERRORS.md) for every error shape and status code.

This guide covers what the schema cannot say well: the order to call things in,
what to poll, and which rules the UI must respect.

## 1. Basics

**Base URL:** the backend origin, e.g. `http://127.0.0.1:8000`. All paths below
start with `/api/`.

**Format:** send and receive JSON (`Content-Type: application/json`). File
uploads use `multipart/form-data`.

**Authentication:** send `Authorization: Bearer <access token>` on every request
except the public endpoints (registration, email verification, login, token
refresh, password reset, public share links).

**CORS:** open only while the backend runs with `DJANGO_DEBUG=True`. In
production the backend allows only the origins in `CORS_ALLOWED_ORIGINS`, so
your frontend's origin must be listed there.
Browser scripts can read the `Content-Disposition` (download filename) and
`Retry-After` (429 wait time) response headers; the backend exposes both.

**Dates** are ISO 8601 strings in UTC.

**Lists:** some list endpoints are paginated (`{count, next, previous, results}`,
e.g. `GET /api/images/my/`, default 24 per page, `?page=` and `?page_size=` up
to 100) and some return a plain array. Check the endpoint in Swagger, and write
one helper that accepts both shapes.

**Ownership:** you only ever see your own data. Someone else's record returns
**404, not 403**, so do not build UI that expects a 403 for "not yours".

## 2. Sign up, log in, stay logged in

Registration is a two-step flow. No account exists until the link in the
verification email is opened.

```text
POST /api/auth/register/            {full_name, email, password, password_confirm?, username?}
   -> 202 {registration_id, ...}    (an email with a link is sent)
        link: {FRONTEND_URL}/verify-email?registration_id=<uuid>&token=<token>
POST /api/auth/verify-email/        {registration_id, token}      (both come from the link)
   -> 201                           (account now exists; the link works once)
POST /api/auth/verify-email/resend/ {registration_id}     (new link, the old one stops working; has a cooldown)
GET  /api/auth/registrations/{registration_id}/status/    (masked status, for a "check your email" screen)
```

**Login**

```text
POST /api/auth/login/  {identifier, password}     (identifier = username or email)
   -> 200 {access, refresh, role, dashboard, user}
   -> 200 {two_factor_required: true, login_token}     (this account has 2FA on; see below)
```

- `role` is `user`, `moderator` or `admin`. `dashboard` is **always the same
  value** (`"user"`, `"moderator"`, `"admin"`), meant for choosing which
  dashboard to open after login. A person who is an *active moderator of an
  active organization* gets `moderator` even though their stored account role is
  `user` (`user.role` is the stored value; organization owners stay `user`).
  This chooses the portal only: what each endpoint allows is unchanged.
- Every user object (`user` here, `GET/PATCH /api/user/profile/`, the admin user
  list) carries `full_name` (the username when no name is set). `PATCH
  /api/user/profile/ {full_name}` changes it.
- Wrong credentials, an unverified account and a suspended account all return
  **400** with the same kind of message (not 401), so accounts cannot be
  enumerated. Show the message as is.
- After repeated wrong passwords the account is refused with **429** and a
  `Retry-After` header. Show the message and offer "Forgot password".

**Two-factor authentication (optional per account)**

```text
POST /api/auth/2fa/verify/ {login_token, code}     -> 200 {access, refresh, role, dashboard, user}
```

- When `POST /api/auth/login/` returns `two_factor_required`, show a "enter your
  6-digit code" screen (accept a recovery code too, in the same field) and POST
  it with the `login_token` to `/api/auth/2fa/verify/`. That response is the same
  shape a normal login returns. `login_token` expires in a few minutes; if it
  does, send the user back to the password screen.
- **Managing 2FA** (needs a normal authenticated session, not `login_token`):
  `POST /api/auth/2fa/setup/` returns `{secret, otpauth_uri}` — render `otpauth_uri`
  as a QR code for an authenticator app (Google Authenticator, Authy, 1Password)
  to scan, and show `secret` for manual entry as a fallback. `POST
  /api/auth/2fa/confirm/ {code}` (one real code from the app) turns it on and
  returns `{recovery_codes: [...]}` — **show these once and tell the user to save
  them**; they are never retrievable again. `GET /api/auth/2fa/status/` returns
  `{enabled}`. `POST /api/auth/2fa/disable/ {code}` and `POST
  /api/auth/2fa/recovery-codes/ {code}` (regenerate) both need a real device code
  or an unused recovery code — being logged in is not enough on its own.

**Tokens**

- The access token lasts **15 minutes**. The refresh token lasts 7 days and
  **rotates**: each `POST /api/auth/refresh/ {refresh}` returns a new refresh
  token that replaces the old one. Always store the newest.
- On a 401 with `code: "token_not_valid"`, call refresh once and retry the
  original request. If refresh fails, send the user to login.
- A 401 saying "This token has been revoked" means the password, email, role or
  suspension changed, or the user logged out everywhere. Refreshing will not
  help; go to login.
- `POST /api/user/logout/ {refresh}` logs out this device.
  `POST /api/user/logout-all/` revokes every session.

**Forgot password**

```text
POST /api/auth/password-reset/          {email}
   -> 200 always the same message ("If an eligible account exists...")
```

The email contains a link to **your frontend**:
`{FRONTEND_URL}/reset-password/{uid}/{token}`. Build a page at that route that
reads `uid` and `token` from the URL, asks for a new password, then:

```text
POST /api/auth/password-reset/confirm/  {uidb64: uid, token, new_password}
   -> 200 "Password reset successfully. Please log in."
   -> 400 {"detail": ["Invalid or expired reset link."]}   or password-rule errors
```

Every session is revoked after a reset, so send the user to login. Because the
first endpoint always answers the same way, do not tell the user whether the
email exists; say "If an account exists, we sent an email."

**Profile and account:** `GET/PUT/PATCH /api/user/profile/`,
`POST /api/user/change-password/`, `DELETE /api/user/delete-account/`,
`GET /api/user/dashboard/`, `GET /api/user/activity/` (the activity timeline).

## 3. Upload and processing

```text
POST /api/images/upload/single/    multipart, field: file
POST /api/images/upload/multiple/  multipart, field: files (repeat the field)
POST /api/images/upload/zip/       multipart, field: file
   -> 201 a batch: {id, status, total_files, accepted_files, rejected_files,
                    processing_status, images: [...], ...}
```

Every upload creates a **batch** (a single image is a batch of one). Files that
fail validation are rejected individually; check `rejected_files` and each
image's status rather than assuming all succeeded. Accepted formats are JPG/JPEG,
PNG, WebP, BMP, TIFF and static GIF (an animated GIF is rejected). The file's
extension must match its real content, and the size and pixel limits in
`.env.example` apply.

**Following progress.** Processing runs in the background. Poll:

```text
GET /api/images/batches/{batch_id}/progress/
   -> {batch_id, status, stage, progress_percentage, counts,
       estimated_completion_at, cancellation_requested, retry_count,
       images: [{image_id, filename, stage, status, progress_percentage,
                 failure_stage, failure_reason, ...}]}
```

- Poll every 2 to 3 seconds while the batch is active and stop when
  `status` is terminal: `completed`, `partial`, `failed` or `cancelled`.
- `stage` moves through: `uploaded, queued, preprocessing, face_validation,
  quality_analysis, identity_grouping, enhancement, finalizing, completed`.
- A batch can be `partial`: some images succeeded and some did not. Show both.
- `POST /api/images/batches/{id}/cancel/` stops a batch;
  `POST /api/images/batches/{id}/retry/` requeues its failed images (there is a
  retry cap, and a 400 explains when it is reached or nothing failed).
- The notifications endpoints (section 8) can replace polling for "it's done"
  toasts, but progress bars still need the progress endpoint.

**Listing.** `GET /api/images/my/` (paginated) is the gallery view,
`GET /api/images/batches/` lists batches, `GET /api/images/{id}/` and
`GET /api/images/{id}/download/` fetch one image. The single-image response is
the **same shape** as a row in `my/` — including `display_status`, the quality
decision and its reasons — so one screen's type works for both. Downloads are protected
endpoints, so fetch them with the Authorization header (not a bare `<img src>`;
use fetch with a blob, or a short-lived approach agreed with the backend).

**Thumbnails.** Never draw a gallery from `download_url`: originals can be
hundreds of megabytes. Each image in the list carries `thumbnail_url`, which
serves a small JPEG (`GET /api/images/{id}/thumbnail/`, optional `?size=256`
default or `?size=1024`; any other size is a 400). The 256 size is normally
already made in the background right after upload; if not, the first request
generates it (about 0.1 to 0.8 seconds for a large photo) and every later one
is served from the cache. The 1024 size is made the first time it is asked for.
It is a normal protected endpoint: fetch it with the Authorization header and
use a blob URL, exactly like a download. Use 256 for tiles and 1024
for a lightbox, then `download_url` only for the real file. The response carries
`Cache-Control: private, max-age=3600`. Rejected images have no thumbnail
(`thumbnail_url` is `null`).

**Quality, faces, duplicates.** Each image has read endpoints:
`/api/quality/images/{image_id}/`, `/api/validation/images/{image_id}/`,
`/api/duplicates/images/{image_id}/`. A decision is `accepted`, `rejected` or
`needs_review`, with human-readable reasons to show the user.

## 4. Enhancement

```text
POST /api/enhancements/images/{image_id}/   {level, style}     -> 202
POST /api/enhancements/batches/{batch_id}/  {level, style}     -> 201
```

- `level`: `auto` (default), `low`, `medium`, `high`.
- `style`: `natural` (default), `dynamic`, `ai_max`.
- `applied_parameters.no_correction_needed` is `true` when the photo had nothing to
  correct and was left as it is; say that rather than showing an identical
  before/after as an enhancement.
- **`ai_max` and identity.** `natural` and `dynamic` restore faces only when the
  result can be checked against the original. `ai_max` skips that requirement.
  Say so where the user picks it, and on every result read these three fields:
  - `identity_status`: `verified` (faces were compared with the original and
    matched), `inconclusive`, `unavailable` (no check ran) or `failed`. Only
    `verified` means a check passed; never present the others as "verified".
  - `identity_check_bypassed`: `true` when faces were generatively restored
    without a verified check. Show a visible notice on that result.
  - `identity_notice`: the sentence to show; empty when there is nothing to say.
  The same three fields are on every entry of a model comparison's `outputs`
  (`GET /api/enhancements/comparisons/{id}/`) and at the end of its CSV report,
  with the same meaning, so a profile reads there as it would on the result it
  produces. They are null / false / empty for the untouched `original` entry.
- The job is asynchronous. Poll `GET /api/enhancements/results/{id}/` until
  `status` is `completed` (or `failed`, with `failure_reason`). Other statuses:
  `pending, queued, processing, partial, cancelled`.
- A result carries `download_url`, `original_download_url`,
  `standardized_download_url` and an `editor_url`. `applied_level` may differ
  from the requested level (the backend adapts to the image), so show what was
  actually applied.
- **429 on start** means the user already has the maximum number of active
  enhancement jobs. The message states the limit; retry after some finish.
- Compare before/after: `GET /api/enhancements/results/{id}/comparison/`.
- **Model comparison** (one image through several profiles side by side):
  `POST /api/enhancements/images/{image_id}/compare/`, then
  `GET /api/enhancements/comparisons/{id}/`, previews at
  `.../outputs/{profile}/preview/`, CSV at `.../report/`.

**Editor (sliders).** Get the slider ranges from
`GET /api/enhancements/editor/configuration/`. Create an edit with
`POST /api/enhancements/results/{result_id}/edits/`, then poll
`GET /api/enhancements/edits/{id}/`. Edits are immutable versions:
`POST .../edits/{id}/activate/` picks the current one and
`GET .../edits/{id}/download/` fetches it. The original is never overwritten.
Show client-side previews while the user drags sliders and submit the final
values for the full-resolution server render.

## 5. People (identity groups)

Faces are grouped into "person folders". A photo with no detectable face is
analysed as `completed` with `face_count` 0 (not `failed`), so do not show an
error for it.

- **Consent first.** Face grouping only runs for users who granted biometric
  consent. `GET /api/privacy/consents/` returns the current state and policy
  version; `POST /api/privacy/consents/ {granted, policy_version}` grants or
  withdraws. Withdrawing with `erase_facial_data: true` also deletes the data.
  Show a consent screen before the first upload and disable the People page
  until consent exists.
- `GET /api/identities/groups/` and `/{id}/` list and show folders
  (`display_name`, `member_count`, `preview_image`). **Embeddings and centroids
  are never returned**; do not expect them. Each folder also has
  `cover_images` (up to four distinct photos, `[{id, thumbnail_url}]`, empty for
  an empty folder) and `matching_confidence` (the average of its members'
  similarity scores **as a percentage, 0-100 with one decimal**, so `98.4` can be
  shown as `98.4%`; `null` when no member has a score, never a made-up number).
  The detail response also keeps `memberships` (raw 0-1 `similarity_score` per
  face).
- **Photos in a folder:** `GET /api/identities/groups/{id}/images/` (paginated:
  `{count, next, previous, results}`, `?page=`, `?page_size=` up to 100, newest
  first). Each row: `id`, `original_name`, `thumbnail_url`, `file_size`,
  `width`, `height`, `display_status` (the same value `GET /api/images/my/`
  reports), `matching_confidence` (this photo's best score in the folder, same
  percentage scale; `null` when unscored). A photo with two faces of the same
  person appears once. Another user's folder is a 404.
- Manage folders: `PATCH .../groups/{id}/rename/ {display_name}`,
  `POST .../groups/{id}/merge/ {source_group_id, reason}` (merges the source
  into this folder), `PATCH .../groups/{id}/profile/`,
  `DELETE .../groups/{id}/delete/` (only when empty), and per face
  `PATCH /api/identities/memberships/{id}/move/` and `/remove/`.
- **Review queue.** `GET /api/identities/review/` lists uncertain assignments
  (`state: needs_review`). Resolve one with
  `PATCH /api/identities/assignments/{id}/review/ {action, group_id?}` where
  `action` is `confirm`, `select_group` (needs `group_id`), `new_group` or
  `reject`.
- Change history (renames, merges, moves, reviews): `GET /api/identities/history/`.
  There is no one-click undo endpoint; to reverse a change, use the opposite
  operation (for example move a face back).
- **Search by photo.** `POST /api/identities/search/` (multipart, field
  `query_image`) finds the caller's own photos that contain the face in the
  uploaded photo. Needs biometric consent, same as the rest of this section.
  Response: `{faces_detected, matches: [{group_id, display_name, similarity,
  photo_count, photos: [{id, original_name, thumbnail_url, download_url}]}]}`.
  `faces_detected: false` means no face was found in the uploaded photo at all
  (show "no face found"); `faces_detected: true` with an empty `matches` list
  means a face was found but it doesn't match anyone in the account yet (show
  "no matches"). The query photo and its embedding are never stored anywhere;
  do not expect a way to look up a past search.
- Per-folder ZIP export is async: `POST .../groups/{group_id}/exports/`, poll
  `GET /api/identities/exports/{id}/`, then `.../download/`. The request body
  accepts an optional `image_ids: [int, ...]` to export only those photos;
  leave it out to export the whole folder. Every id must be a photo currently
  in that folder, otherwise the answer is **400** `{"image_ids": ["These photos
  are not in this folder: 12, 40."]}` and nothing is queued. An empty list is a
  400 too (omit the field, or send `null`, for "everything"). Two requests for
  the same set of photos share one queued job; a different set is its own job.
  The export object reports the `image_ids` it was made for (`[]` = all) and
  regenerating keeps them.
- **Share a finished folder ZIP:** `POST /api/identities/exports/{id}/share/
  {ttl_hours?, label?}` -> **201** with the same body as an image share link
  (`id`, `state`, `expires_at`, `token`, `public_url`, `info_url`, ...,
  `identity_export: <export id>`, `result: null`). The `token` appears **only in
  this response**; copy `public_url` straight away. Anyone with `public_url`
  (no login) gets the ZIP as a download. The link never outlives the ZIP (its
  `expires_at` is capped at the export's), is revoked with
  `POST /api/shares/{link id}/revoke/`, and answers **410** once revoked or
  expired. Only a `ready`, unexpired export can be shared: a queued/generating
  one is **409**, an expired one **410** (regenerate it first). Share links
  count toward the same per-user limit as image links.

## 5a. Duplicates

`GET /api/duplicates/results/` and `/images/{image_id}/matches/` list suspected
duplicates. Confirm or dismiss with
`PATCH /api/duplicates/matches/{id}/review/`. An exact checksum match cannot be
dismissed (the API returns a 400).

## 5b. Events (moments timeline)

Photos are grouped into events by upload time. No face data is used.

- `GET /api/events/` lists events, newest first (paginated). Each has `id`,
  `start`, `end`, `photo_count`, `cover_image_id`, `cover_download_url` and
  `cover_thumbnail_url`.
- `GET /api/events/{id}/` shows one event; `GET /api/events/{id}/photos/` lists
  its photos (paginated, oldest first) with `thumbnail_url` and `download_url` for each.
- The optional `gap_hours` query parameter (0.25-168) sets how long a silence
  starts a new event. Send the same value on every call, because event ids
  depend on it. Omit it to use the server default (6 hours).
- Use `cover_thumbnail_url` for the tile; `cover_download_url` is the full original.
- Event ids are not permanent: a photo that is not the first of its event is
  not a valid id. Always take ids from the list response.

## 5c. Collages

A collage is a generated JPEG made from an event's photos or one person's photos.

- `POST /api/collages/` with `{"source_type": "event", "event_id": 12, "gap_hours": 6}`
  or `{"source_type": "person", "group_id": 3}` (optional `title`). Returns `202`
  with the collage in `queued` state, or `200` with the already-running one:
  each user can have only one collage in progress.
- Poll `GET /api/collages/{id}/` until `status` is `ready` (or `failed`, with
  `failure_reason`). `download_url` then serves the JPEG. Use the same
  `gap_hours` you used for the events list.
- `GET /api/collages/` lists them (paginated). `DELETE /api/collages/{id}/` removes one.
- Needs at least 3 processed photos (a clear `400` otherwise); at most 6 are
  used, spread across the source.
- A person collage needs biometric consent. Without it the request is a `400`;
  the same check runs again when the job starts.
- A ready collage expires after about 72 hours (`410` on download); create a new one.
- `manifest.photos[].faces_in_frame` is `false` when faces could not all fit the tile.

## 6. Reports, packages, sharing

**Reports** (`json`, `csv`, `pdf`) and **download packages** (ZIP) are
asynchronous jobs with the same lifecycle:

```text
POST /api/reports/batches/{batch_id}/            {format}
POST /api/reports/images/{image_id}/             {format}
POST /api/downloads/batches/{batch_id}/packages/ {package_type, report_format}
   -> 200 (an identical job already exists) or 202 (queued)
GET  /api/reports/{id}/  |  /api/downloads/packages/{id}/    poll status
GET  .../{id}/download/                                       once status is "ready"
POST .../{id}/regenerate/                                     after expiry or failure
```

Statuses: `queued, generating, ready, failed` (packages also `expired`).
Downloading before `ready` returns **409**; after expiry **410**. Packages
(`package_type`: `accepted_results` or `complete_batch`) expire after a
configurable time, so show an expiry and offer "regenerate".

**Share links.** `POST /api/shares/ {result, ttl_hours?, label?, watermark?,
watermark_position?, watermark_opacity?, watermark_text?}` returns the link
**including the raw `token` exactly once**, plus `public_url` and `info_url`.
The token is never shown again, so display and copy it immediately.
`watermark`: `none, small, facecloud, username, custom`; `watermark_position`:
`bottom_right, bottom_left, top_right, top_left, center, tile`. Revoke with
`POST /api/shares/{id}/revoke/`. Preview the watermark before sharing with
`GET /api/enhancements/results/{id}/watermark-preview/`.

**Share link shape.** `GET /api/shares/` and `/{id}/` never return a token or a
public address. A link made from a person-folder ZIP has `identity_export` set
and `result` null; an image link has `result` set and `identity_export` null.

**Downloads screen.** `GET /api/downloads/` (paginated history, newest first)
keeps its original fields and adds, per row:

| Field | Meaning |
|---|---|
| `kind` | `single` (one photo: original, standardized, enhanced, edited), `batch` (a ZIP: batch package or person-folder ZIP) or `report` |
| `expires_at` | When the file ends (batch package / folder ZIP expiry); for an enhanced or edited photo with a live share link, when that link ends; otherwise `null` |
| `image_count` | Photos in the ZIP or batch report; `null` for a single photo and for an image report |
| `has_active_link` | `true` when a live (not revoked, not expired) share link exists for it. The link itself is never listed |

To give the user a **fresh secure link** from a row, use the existing endpoints
(`resource_type` and `resource_id` say what the row is):

| `resource_type` | Mint a link with |
|---|---|
| `enhanced_image` (`resource_id` = result id) | `POST /api/shares/ {result: resource_id}` |
| `edited_image` (`resource_id` = edit id) | `POST /api/shares/ {result: <its result>, edit: resource_id}` |
| `identity_group_package` (`resource_id` = export id) | `POST /api/identities/exports/{resource_id}/share/`; if expired, `POST .../exports/{id}/regenerate/` first |
| `batch_package` (`resource_id` = package id) | no public link; while `ready` use the authenticated `/api/downloads/packages/{id}/download/`, and `POST .../packages/{id}/regenerate/` after expiry |
| `original_image`, `standardized_image`, `processing_report` | no share link; use the authenticated download endpoints |

**Public share page** (no login, no Authorization header):
`GET /api/shares/public/{token}/info/` for metadata (size, expiry, whether it is
watermarked) and `GET /api/shares/public/{token}/` for the image. 404 means the
token is unknown, **410** means expired, revoked or the owner is inactive. For a
person-folder link `info` reports `content_type: "application/zip"` and the
public address answers with the ZIP as an attachment (`Content-Disposition:
attachment`), never watermarked.

## 7. Organizations and the moderator area

Every user has a **personal organization** created at signup. Users can also
belong to shared organizations (owner, moderator or member roles) by accepting
an invitation.

**Every organization endpoint has the organization id in the URL**
(`/api/organizations/{organization_id}/...`). The backend re-checks membership
on every request. An organization you do not belong to is **404**; an
organization you belong to but lack the role for is **403**. When the user
belongs to several organizations, keep the selected organization id in your app
state and put it in the URL; changing it client-side grants nothing.

- `GET /api/organizations/` lists the caller's organizations (use it for the
  organization switcher). `GET /api/organizations/{id}/` shows one.
- Members and invitations (owner or moderator only):
  `GET .../members/`, `PATCH/DELETE .../members/{membership_id}/`,
  `GET/POST .../invitations/ {email, role}` (role `moderator` or `member`;
  only an owner can invite a moderator), `POST .../invitations/{id}/resend/`,
  `DELETE .../invitations/{id}/`. "Remove" only removes organization access, not
  the person's account.
- **Accepting an invitation:** the emailed link is
  `{FRONTEND_URL}/invitations/{token}`. Build that route; the logged-in user
  (who must be signed in with the invited email) calls
  `POST /api/organizations/invitations/accept/ {token}`.
- Audit trail: `GET .../audit/`. Organization-wide data export (async):
  `POST .../export/`, poll `GET .../export/{id}/`, download
  `.../export/{id}/download/`.

**Moderator review queue** (owner or moderator):

```text
GET  .../moderator/reviews/?status=&source=&overdue=&assignment=&ordering=
GET  .../moderator/reviews/{id}/
POST .../moderator/reviews/{id}/decision/   {action, reason?, notes?, group_id?}
POST .../moderator/reviews/{id}/assign/     {moderator_id}
POST .../moderator/reviews/{id}/escalate/   {reason?}
GET  .../moderator/reviews/{id}/image/?size=  the photo, as a thumbnail
POST .../moderator/reviews/{id}/claim/      take the review (soft lock)
POST .../moderator/reviews/{id}/release/    give it back to the queue
POST .../moderator/reviews/bulk-decision/   {review_ids[], action, reason?, notes?}
POST .../moderator/reviews/{id}/merge-identity-groups/  {source_group_id?, target_group_id?, reason?}
GET  .../moderator/dashboard/               counts for the overview cards
GET  .../moderator/metrics/?days=30         per-moderator performance
```

Queue filters: `source` is `quality | duplicate | identity`, `overdue=true`
narrows to reviews past their deadline, `assignment` is `mine` or `unassigned`,
and `ordering=due_at` works to the deadline first (reviews without one last).

- `ai_confidence` is a **percentage, 0 to 100**, whatever raised the review.
  The three sources do not agree among themselves underneath — a quality score
  and a duplicate similarity are already 0-100, an identity similarity is 0-1 —
  so the backend normalises on the way in. Render it directly; do not multiply.
- `action` is `approve`, `reject`, `correct` or `keep_in_review`. For identity
  reviews `correct` with a `group_id` reassigns to that folder, without one it
  starts a new folder. A decided review returns **409** on a second decision.
- Merging groups is only for duplicate reviews. If either side maps to several
  groups the API returns 400 listing `candidates`; let the moderator pick and
  resend with explicit group ids.

**Deadlines.** Every review carries `due_at` and a derived `is_overdue`. The
dashboard adds `overdue`, `due_soon`, `unassigned`, `claimed_now`,
`oldest_open_age_hours` and the configured `sla_hours`, so the overview cards
can show the service level without a second request. A review that passes its
deadline while still undecided produces one `moderator_review_overdue`
notification -- once, not on every check -- to its assigned moderator, or to the
owners and moderators when nobody has picked it up.

**Seeing the photo.** `image/` serves the photo behind a review as a JPEG
thumbnail (`size` 256 by default, 1024 also configured; a photo is never
enlarged). It is keyed by **review**, not by image, so a moderator can only see
photos they have actually been asked to judge, and it serves thumbnails only --
there is no way to pull the original through a moderation role. The same
organization scope and owner/moderator role as the review apply, so losing the
role closes it at the same moment. It needs the `Authorization` header like any
other call, so load it with `fetch` and `URL.createObjectURL` rather than
putting the path straight in `<img src>`; remember to revoke the object URL.
Each review also carries `image_name`, the photo's own filename, so a queue row
is recognisable before its thumbnail arrives.

**Claiming.** `claim/` holds a review for the moderator so two people do not
work the same photo: the response carries `claimed_by`, `is_claimed` and
`claim_expires_at`. While someone holds a review, another moderator's
`decision/` or `escalate/` answers **409** with code `review_claimed`. The hold
expires on its own, so nothing is ever stranded; claiming your own review again
simply extends it. `release/` returns it to the queue -- the holder can always
release, and an organization owner can release somebody else's hold.

**Bulk decisions.** `bulk-decision/` takes up to 50 `review_ids` with `action`
`approve`, `reject` or `escalate` (`correct` is not available in bulk because it
needs a per-photo choice of person folder). The response is **200** with
`succeeded`, `succeeded_count`, `failed` (`[{review_id, detail}]`) and
`failed_count`: rows that were already decided, held by another moderator, or
not in this organization are reported individually and do **not** fail the rest,
so show per-row outcomes rather than one success or error.

**Performance.** `metrics/?days=30` returns `moderators[]` with
`decided_count`, `approved`, `rejected`, `corrected`, `on_time`, `late`,
`on_time_rate`, `average_decision_hours` and `slowest_decision_hours`, plus
`totals` including `open_now` and `overdue_now`. Decision time is measured from
when the review arrived, not from when it was assigned. A review with no
deadline counts as neither on time nor late, so `on_time_rate` is `null` when
nothing measurable was decided.
- Batch actions: `GET .../moderator/batches/?processing_status=`,
  `POST .../moderator/batches/{id}/retry/` and `/cancel/`.
- Reports: `POST .../moderator/reports/ {report_type, format}` with
  `report_type` one of `review_summary`, `moderator_activity`, `rejection_reasons`,
  `quality_scores`, `batches` or `duplicate_decisions` (and `format` `json`, `csv` or
  `pdf`); then the same poll-and-download lifecycle as section 6. Every report has a
  `summary` (totals and breakdowns, good for a chart) and up to 200 `rows`. None of
  them contains facial data.

## 8. Notifications

`GET /api/notifications/` (list), `GET /api/notifications/unread-count/` (badge),
`POST /api/notifications/{id}/read/`, `POST /api/notifications/mark-all-read/`,
`DELETE /api/notifications/{id}`, and `GET/PATCH /api/notifications/preferences/`
for the settings screen. Each notification has a `title`, `message`, `severity`,
`resource_type`/`resource_id` and an `action_path` you can route to. Poll the
unread count (every 30 to 60 seconds is plenty); there is no websocket.

**Severity -> toast/colour.** `severity` is already one of `info`, `success`,
`warning`, `error`, so use it directly as the notification type (there is no
separate `type` field to map). Typical sources: `success` (batch finished, report
or package ready), `warning` (a photo needs review, or a job finished only partly), `error` (a job
failed or a photo was rejected), `info` (everything else). Note the notification
`event_type` is the specific event name, not the colour.

Moderators and owners of an organization also receive `event_type`
`moderator_batch_failed` when one of the organization's batches fails entirely
(`resource_type` `batch`, `metadata.batch_id` and `metadata.organization_id`,
`action_path` `/moderator/batches`). It is in-app only, follows the
`moderator_review_updates` preference, and never goes to the person who uploaded
the batch (they get their own `batch_completed`).

## 8a. Activity (history button and Activity page)

`GET /api/user/activity/?category=&page=&page_size=` (default 20 per page, max
100) answers `{page, page_size, has_next, results: [...]}`. Each entry has the
display fields

| Field | Values |
|---|---|
| `id` | stable string, `activity-<n>` or `auth-<n>` |
| `type` | `upload`, `enhance`, `validate`, `download`, `live`, `group`, `reject`, `login`, `email`, `admin` (`live` is reserved: nothing emits it yet) |
| `message` | a ready-to-show sentence |
| `timestamp` | ISO 8601, same instant as `created_at` |
| `status` | `success`, `warning`, `error`, `info` |

and the original record fields (`source`, `category`, `event`, `object_type`,
`object_id`, `identifier`, `ip_address`, `details`, `created_at`), unchanged.
`category` filters to one of `upload, processing, moderation, download, group,
share, admin, privacy, auth`.

How stored events map to `type` / `status`:

| Stored event | type | status |
|---|---|---|
| `upload_received` | upload | info |
| `processing_started` | enhance | info |
| `batch_completed` / `batch_partial` / `batch_failed` / `batch_cancelled` | enhance | success / warning / error / warning |
| `enhancement_completed` / `enhancement_failed` / `enhancement_cancelled` | enhance | success / error / warning |
| `quality_accepted` / `quality_failed` | validate | success / error |
| `quality_rejected` / `quality_needs_review` | reject | warning |
| `package_*`, `identity_export_*`, `report_*` (`ready` / `failed` / `expired`) | download | success / error / warning |
| `share_link_created` / `share_link_revoked` | download | success / info |
| `group_renamed`, `group_merged`, `group_moved`, `group_removed`, `group_profile_set`, `group_deleted`, `group_reviewed` | group | info |
| `consent_granted` / `consent_withdrawn` | validate | success / warning |
| `facial_data_erased` / `data_exported` | group / download | success |
| `registration_pending`, `verification_code_sent` | email | info |
| `registered`, `email_verified`, `password_reset` | email | success |
| `login` / `login_failed` / `login_locked` | login | success / error / warning |
| `logout`, `logout_all`, `session_revoked` | login | info |
| `password_changed`, `profile_updated` | login | success |
| `role_changed`, `suspended`, `reactivated`, `account_deleted`, `admin_created_account` | admin | info / warning / success / info / info |
| anything else | by category; `*failed` -> error, `*rejected`/`*cancelled`/`*expired` -> warning, `*completed`/`*ready` -> success, otherwise info | |

## 8b. Top-bar search

`GET /api/search/?q=<text>&limit=<n>` (signed in). `q` must be at least 2
characters after trimming, otherwise **400** `{"q": ["Type at least 2 characters
to search."]}`. `limit` is per kind: default 5, maximum 10 (larger values are
capped; `0`, negatives or non-numbers are a 400). Matching is case-insensitive
"contains"; `%` and `_` in `q` are ordinary characters. Only the caller's own
records are searched.

```json
{
  "query": "beach",
  "images": [{"id": 12, "name": "beach-01.jpg", "thumbnail_url": "http://.../api/images/12/thumbnail/",
              "status": "ready", "created_at": "2026-10-16T09:12:00Z"}],
  "groups": [{"id": 3, "display_name": "Beach friends", "member_count": 4}],
  "jobs":   [{"id": 7, "display_name": "Beach weekend", "status": "uploaded", "total_files": 3,
              "created_at": "2026-10-16T09:10:00Z"}]
}
```

`images` match the original file name (`status` is the same `display_status` as
`/api/images/my/`; `thumbnail_url` is `null` for a rejected image), `groups` match
the folder name (active folders only) and `jobs` are upload batches, matched on
the name the person gave the batch **or** the ZIP's file name (`wedding-photos.zip`
is found by "wedding"). A job's `display_name` is always the label "Your uploads"
shows: the chosen name, else the ZIP's file name, else `Upload <id>`, so a row is
never blank. Single and multiple uploads that were never renamed have no name to
search; they appear as `Upload <id>` wherever they are listed but are not found
by that label. Rate limit: 600 requests per hour per user.

## 9. Privacy screens

- Consent: section 5.
- `GET /api/privacy/facial-data/` shows counts of stored biometric data;
  `DELETE /api/privacy/facial-data/ {"confirm": true}` erases it and verifies
  the erasure.
- `GET /api/privacy/export/` downloads the user's own data as JSON (biometric
  templates are never included).
- **"Consent required" is machine-readable.** When something that needs
  face-recognition consent is refused because the person has not agreed (starting
  identity analysis `POST /api/identities/images/{id}/`, face search
  `POST /api/identities/search/`, and a person collage `POST /api/collages/`
  with `source_type: "person"`), the status stays **400** and the sentence is
  unchanged, and the body now carries `"code": "consent_required"`:
  `{"detail": ["Biometric consent is required before face search."], "code":
  "consent_required"}` (the collage form has `detail` as a plain string). Match on
  `code`, then send the person to the consent screen. Before this change the two
  identity endpoints answered with a bare list `["..."]`.

## 10. Admin pages (role `admin` only)

`/api/system/health/`, `/queue/`, `/analytics/`, `/audit/`, `/moderation/`,
`/batches/failed/`, `POST /api/system/batches/{id}/retry/`, `/jobs/failed/`,
`GET /api/user/admin/users/` (a plain array), `POST /api/user/admin/users/` (create an account) and
`PATCH /api/user/admin/users/{id}/`,
`GET /api/privacy/audit/`. A non-admin gets 403; use the `role` from login to
hide these routes, but never rely on that for security.

**Creating an account (Add User / Create moderator forms)**

```text
POST /api/user/admin/users/  {username, email, password, role: "user" | "moderator", organization_id?, full_name?}
   -> 201 {id, username, email, full_name, role, is_email_verified, is_suspended, suspension_reason, is_active, date_joined}
   -> 400 {"email": [...]} / {"username": [...]} / {"password": [...]} / {"role": [...]}      (field errors)
   -> 400 {"detail": "Create an organization first, then try again."}                         (moderator, no organization)
   -> 400 {"detail": "There is more than one organization: send organization_id to choose one."}
   -> 403 for anyone who is not an administrator
```

The account is created already verified; no email is sent and the password is never returned, so show the sign-in details to the
administrator once and hand them over. `confirm password` is checked in the browser only. `role: "admin"` is refused. A moderator is
added to an active non-personal organization, so the moderator panel (which takes the first non-personal organization of
`GET /api/organizations/`) finds it; they log in with `role` and `dashboard` = `moderator`.

## 11. Things the UI must handle

| Situation | What to do |
|---|---|
| 401 `token_not_valid` | Refresh once, retry, else login |
| 401 "revoked" | Go to login (refresh will fail too) |
| 400 `{"detail": ["..."]}` | `detail` is a **list** here; show the first message |
| 400 `{"field": ["..."]}` | Show each message next to its field |
| 404 | "Not found" (also means "not yours") |
| 409 | Not ready or already decided; refresh the resource |
| 410 | Expired; offer regenerate (or explain a dead share link) |
| 429 | Show the message; honor `Retry-After` when present |
| `status: "failed"` on a job | Show `failure_reason`; offer retry where the API allows |

## 12. Not built yet

- There is no websocket/server-sent events channel; use polling.
- Live camera features are not implemented.

When in doubt about a field name or enum, open `/api/docs/`. The schema is
generated from the code and validated with zero warnings in CI.
