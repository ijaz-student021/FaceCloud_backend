# Frontend handoff: backend contracts ready for integration

For the frontend repository (`FYP`, reviewed at `ee7d126`). Everything here is
implemented and tested on the backend branch `feat/backend-frontend-alignment`.
**No frontend code has been changed.** The live contract is the OpenAPI schema:
`/api/schema/` (also Swagger at `/api/docs/`, ReDoc at `/api/redoc/`). It
validates with `spectacular --validate --fail-on-warn`.

Conventions:
* All paths are under `/api/`. The frontend's `VITE_API_URL` is the backend origin
  with no `/api` suffix.
* Authentication is `Authorization: Bearer <access>` unless marked public.
* Errors are JSON: `{"detail": "..."}` or `{"field": ["..."]}`. `429` carries
  `Retry-After`.

## 1. Cross-cutting

### Pagination (opt-in) — affects every list
* Add `?page=1` (and optionally `page_size`, max 100) to get
  `{count, next, previous, results}`.
* Without `page` or `page_size` a list returns the old plain array. That form
  is unbounded and kept only so the current screens keep working.
* Notifications, my images, collages, failed batches and event photos were
  already paginated and are unchanged.
* `/api/system/images/` is always paginated.
* **Action:** move each list consumer to `page`, then treat the plain-array
  form as deprecated.

### Single upload: a rejected file is a 201
`POST /api/images/upload/single/` answers **201** even when the file is
rejected. Check `status === "rejected"` and `images[0].rejection_reason`; do not
treat 201 as "uploaded".

## 2. New and extended endpoints

### Organization membership and capabilities (B01)
* `GET /api/organizations/`
* `GET /api/organizations/{id}/`

Each organization now includes:
```json
"membership": {"id": 7, "role": "moderator", "status": "active"},
"capabilities": {"view_members": true, "invite_members": true, "invite_moderators": false,
  "manage_members": true, "manage_moderators": false, "grant_owner": false, "view_audit": true,
  "moderate_reviews": true, "manage_batches": true, "create_reports": true, "export_organization": true}
```
Use these to show or hide actions in the moderator portal. They are advisory:
every action is still enforced, so keep handling `403` (staff action by a
member) and `404` (no access to the organization). The global user `role` is
not the organization role.

### Upload limits (B01)
`GET /api/images/upload/limits/` returns:
* `accepted_extensions` and `accepted_mime_types`
* `max_image_bytes` and `max_image_pixels`
* `max_files_per_batch`
* `max_zip_bytes`, `max_zip_uncompressed_bytes` and `max_zip_compression_ratio`
* `enhancement_max_input_pixels`
* `rate_limits` (`image_upload`, `zip_upload`)

These are the values the validators enforce. Use them on UploadCenter and as
client pre-checks.

### Profile and avatar (B02, Settings → Profile)
* `GET` / `PATCH /api/user/profile/` now accept and return `display_name`,
  `organization_name`, `job_title`, `phone` and `country` (all optional) and a
  read-only `avatar_url`. Validation errors come back as `400` per field
  (phone pattern, lengths, control characters).
* `organization_name` is descriptive text only and never grants organization
  access.
* Login responses (`user`) carry the same fields.
* Map the form's "Full Name" to `display_name`, "Organization" to
  `organization_name`, and "Job Title" to `job_title`.
* `PUT /api/user/avatar/` takes multipart field `avatar` (JPEG, PNG or WebP,
  ≤ 5 MB). It returns the profile, or `400`.
* `GET /api/user/avatar/` returns the owner's PNG (≤ 512 px), or `404` when
  none is set. It is authenticated, so fetch it as a blob like other protected
  images; an `<img src>` cannot send the bearer header.
* `DELETE /api/user/avatar/` returns `204`.
* `avatar_url` changes on every replacement; use it as the cache key.

### Signed-in sessions (B02, Settings → Security)
* `GET /api/user/sessions/` returns
  `[{id, device, user_agent, ip_address, created_at, last_seen_at, expires_at, is_current}]`.
  `device` is a label like "Chrome on macOS".
* `POST /api/user/sessions/{id}/revoke/` returns `204`, or `404` when the
  session is not the caller's or has already ended. The revoked session's
  access tokens fail at once with `401` and code `session_revoked`.
* Revoking `is_current` signs this client out: clear local state as on logout.
* Logout, logout-all and password change end sessions.
* **Action:** on any `401` with code `session_revoked` or `token_revoked`, drop
  the tokens and go to login; do not retry the refresh.
* Password change itself is the existing `POST /api/user/change-password/`
  with `{current_password, new_password}`.

### Moderator reports with a date window (B05, ReportsExports)
`POST /api/organizations/{org}/moderator/reports/` takes
`{report_type, format, date_from?, date_to?}`.
* Dates are ISO and UTC. A plain date for `date_to` includes that whole day.
  At most 366 days.
* `201` means created. `200` means an identical report (same type, format and
  window) is already in flight, and it is returned.
* Responses include `date_from` and `date_to`.
* Poll the report until `status` is `ready`, `failed` or `expired`, then
  download.
* The JSON payload has a `period` block; PDF prints it.

UI-to-backend type mapping (a content choice, not a rename):

| UI option | `report_type` |
|---|---|
| review activity | `review_summary`, or `moderator_activity` for per-moderator totals |
| batch activity | `batches` |
| quality stats | `quality_scores` (scores) or `rejection_reasons` (why) |
| duplicate stats | `duplicate_decisions` |
| general activity | not available; needs a backend report type, or remove the option |

`format` is one of `json`, `csv` or `pdf`.

### Organization batch detail (B04, BatchManagement)
`GET /api/organizations/{org}/moderator/batches/{batch_id}/` returns
`{batch: {...summary, rejected_files}, images: [...], counts_from_images: {...}}`.

Each entry in `images` has:
* `id`, `original_name`
* `upload_status`, `rejection_reason`
* `stage`, `progress_status`, `progress_percentage`
* `failure_stage`, `failure_reason`, `retry_count`
* `quality_decision`, `enhancement_status`

Access: owner or moderator; `403` for a member, `404` for another
organization. Metadata only, with no image previews (see open decision D2).
Retry and cancel are the existing `.../retry/` and `.../cancel/`.

### Admin image monitoring (B06, ImageMonitoring)
* `GET /api/system/images/` (administrators only) is always paginated:
  `page`, `page_size` ≤ 200. Filters: `status`, `owner` (id), `organization`,
  `quality_decision`, `q` (filename or owner email), `created_after`,
  `created_before` (ISO dates).
* Each row: `id`, `original_name`, `owner{id,username,email}`,
  `organization_id`, `batch`, `status`, `rejection_reason`, `file_extension`,
  `mime_type`, `file_size`, `width`, `height`, `created_at`,
  `quality{status,decision,overall_score,sharpness_score,brightness_score}|null`,
  `latest_enhancement|null`.
* `GET /api/system/images/{id}/` adds `preprocessing`, `face_validation`,
  `duplicate_matches` and `enhancements[]`.
* `GET /api/system/images/{id}/preview/` returns a JPEG thumbnail, or `404` for
  rejected uploads.
* Detail and preview are recorded in the audit feed.
* **Action:** the page's "Audit Details" link points to `/dashboard/images/:id`,
  which is the owner's page and returns `404` for an admin. Add an admin detail
  route using the endpoints above.
* "Blur score" should show `quality.sharpness_score` with its scale; it is not
  a percentage of accuracy.

### Admin user detail (B06, Users → View)
* `GET /api/user/admin/users/{id}/` returns the account state, profile,
  `last_login`, `two_factor_enabled`, `active_sessions`, `organizations[]`
  and `usage{upload_batches, images, enhancements}`.
* `PATCH` is unchanged (role, suspension, `is_active`).

### Typed dashboard and analytics (no shape change)
`GET /api/user/dashboard/` and `GET /api/system/analytics/` are now documented
field by field in Swagger, with units and what each number counts. The JSON is
unchanged; generate types from the schema instead of hand-writing them.

### Batch start errors (`POST /api/enhancements/batches/{id}/`)
* `409 {detail, image_ids: [int]}`: some images already have an enhancement
  queued or running; nothing was created. Show which images and retry later.
* `500 {detail, error_reference}`: unexpected fault. Show the reference; the
  cause is only in the server log.
* The old catch-all `400` message no longer appears.

### Moderator reports: `general_activity`
`report_type: "general_activity"` is now valid (audit trail of the window
summarised by action and actor, plus batches, images and reviews created).
It takes the same `date_from`/`date_to`, formats and polling as the others.

### Security alerts (admin, Security page)
* `GET /api/system/security/alerts/` (paginated; filters `status`, `rule`,
  `severity`): `{id, rule, subject, severity, status, event_count, threshold,
  window_minutes, first_seen_at, last_seen_at, acknowledged_by,
  acknowledged_at, acknowledgement_note}`. An empty list means no rule
  crossed its threshold, **not** that the system is secure; do not show a
  "secure" badge.
* `GET .../alerts/{id}/` adds `evidence` (up to 50 triggering events).
* `POST .../alerts/{id}/acknowledge/ {note}`: note required; `409` if already
  acknowledged. Nothing is deleted; the evidence stays.
* `GET /api/system/security/rules/` returns the thresholds in force; show
  them on the page instead of hard-coding.
* Rules: repeated failed logins (per account, per address), repeated 403s,
  repeated 429s. Subjects look like `account:<email>`, `ip:<address>`,
  `user:<id>`.

### Moderator image previews (ModeratorImageReview, BatchManagement)
`GET /api/organizations/{org}/moderator/images/{image_id}/preview/
?variant=original|enhanced&size=256|1024` returns a JPEG thumbnail. Owners
and moderators only; images in a personal space, rejected uploads and other
organizations are `404`; every request is audited. Fetch as a blob (needs the
bearer header). The response is `no-store`.

### Moderator reprocess (BatchManagement)
`POST /api/organizations/{org}/moderator/batches/{batch_id}/reprocess/
{level?, style?, reason?}` starts enhancement again from the preserved
originals as a **new version**. Earlier outputs are untouched.
`201 {job_id, status, total_images}`; `409 {image_ids}` while any image has
work in flight; `429` when the batch owner is at their fair-use limit.
Progress and results are the batch owner's existing job/result endpoints.

### Admin "Add user" by invitation
* `POST /api/user/admin/invitations/ {email, role}` (roles `user`,
  `moderator`; `admin` only for a superuser, otherwise `403`); `GET` lists
  (`?status=`); `POST .../{id}/resend/`; `DELETE .../{id}/` cancels.
  `409` if the email has an account or a pending invitation.
* The invitee gets an email linking to
  `{FRONTEND_URL}/accept-invitation?token=...`. **The frontend needs that
  route.** It calls the public `POST /api/auth/invitations/inspect/ {token}`
  (shows email and role; `404` if invalid, used or expired) and then
  `POST /api/auth/invitations/accept/ {token, username, password}`, which
  creates the verified account and answers like login (`201` with tokens).
* `PATCH /api/user/admin/users/{id}/` now also returns `403` when an
  administrator tries to grant `admin`.

### Camera sessions (LiveEnhancement)
* `POST /api/camera/sessions/ {device_label?, settings?}` starts a session;
  `GET` lists the last 30 days; `GET .../{id}/` includes snapshots;
  `POST .../{id}/end/`.
* `POST .../{id}/snapshots/` (multipart `file`) saves **one** snapshot, only
  when the user presses a save action. It is an ordinary single upload (same
  response fields; check `image_status` for rejection) linked to the session;
  `409` once the session ended.
* There is no video endpoint. Local canvas filters are previews only; the
  server never receives frames unless the user saves a snapshot. Session
  metadata is deleted 30 days after it started; saved images stay.

### Contact form (B07, public)
`POST /api/contact/` (no authentication) takes
`{name, email, company?, reason, message}`.
* `reason` is one of the form's five option strings exactly.
* `message` must be 10–5000 characters.

Responses:
* `202 {id, status:"queued", detail}`: accepted for delivery. Show "sent to the
  team", not "delivered".
* `400`: per-field errors.
* `429`: 5 per hour per address; show `Retry-After`.
* `503`: the form is not configured; show the message, never a fake success.

Leave the hidden `website` field out (or empty); it is a spam trap.

## 3. Existing contracts the screens should switch to (no backend change)

| Screen | Endpoint |
|---|---|
| ApiDocs | Link `/api/docs/` and `/api/redoc/`; `/api/v1` does not exist |
| ApiTest | Use Swagger; the mock paths `/api/admin/users`, `/api/admin/audit-logs`, `/api/images/list` and `/api/images/upload` do not exist (real: `/api/user/admin/users/`, `/api/system/audit/`, `/api/images/my/`, `/api/images/upload/{single,multiple,zip}/`) |
| Reset password, 2FA | `/api/auth/password-reset/`, `/password-reset/confirm/`, `/api/auth/2fa/{setup,confirm,status,disable,recovery-codes,verify}/`; frontend routes are missing |
| Settings → Notifications | `GET`/`PATCH /api/notifications/preferences/` |
| Settings → Privacy | `/api/privacy/consents/`, `/export/`, `/facial-data/` |
| Moderator notification centre | `/api/notifications/` (assignment, escalation, overdue and batch-failed events are produced server-side) |

## 4. Product decisions

Resolved and implemented: security alert rules (D1), moderator previews (D2),
reprocess semantics (D3), admin user invitations (D4), camera session
retention (D5; 30 days, snapshots only on explicit action).

Still open:
* **D6 System telemetry:** host CPU, GPU and uptime history belong to an
  operational monitor, not Django; the page should show only
  `/api/system/health/` and `/queue/`.
* Organization download packages for staff (owner-only today).
