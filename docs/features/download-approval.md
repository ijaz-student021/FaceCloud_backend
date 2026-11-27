# Download approval

An organization can require that its members download an image only after a moderator has approved it. It is **off for every organization** until the FaceCloud
administrator turns it on, and it never applies to a personal workspace. Where it is off, every download behaves exactly as before.

Status: backend implemented and tested on SQLite and PostgreSQL; no frontend yet; not deployed. Code: `downloads/approval.py` (rules), `downloads/previews.py` (reduced previews),
`downloads/approval_views.py` and `downloads/approval_serializers.py` (API), `downloads/models.py` (three models). Tests: `downloads/test_download_approval.py` (86) and
`downloads/test_download_approval_concurrency.py` (6, PostgreSQL only: separate connections and threads).

## The setting

| Field on `Organization` | Meaning |
|---|---|
| `download_approval_required` | `false` by default. Only the platform administrator changes it (`PATCH /api/organizations/admin/{id}/`); an owner gets 403 if they try. A personal workspace cannot turn it on. **Turning it on needs at least one active moderator** in the organization (not suspended or deactivated); otherwise 400, because nobody approves their own request and the owner's own requests would have no reviewer. |
| `download_grant_hours` | How long an approval lasts for this organization. `null` = the platform default `DOWNLOAD_GRANT_DEFAULT_HOURS` (24). |

Both are read-only in the organization endpoints members use, so a screen can decide whether to show "Request download access".
Changing them is written to the organization's audit trail (`organization_updated`).

## The flow

1. A member lists their images and (optionally) asks `POST /api/downloads/requests/check/` which can be requested and why not.
2. The member submits **one** request for several images: `POST /api/downloads/requests/`. The list is saved exactly and cannot be edited afterwards.
3. The organization's owner and the moderators who may see that member are told (in-app and e-mail). If nobody in the organization can decide, the platform administrators are told.
4. A moderator opens the request, previews each image, and decides: approve all, approve selected, deny selected or deny all, with a reason for every denial.
5. Each approved image gets a grant for that one file, valid for the organization's number of hours.
6. When every image is decided, the member gets one summary notification. They download the approved images one by one or as a ZIP.

### Request status

| Status | When |
|---|---|
| `pending` | no image decided yet |
| `in_review` | some images decided, some not |
| `approved` | every image approved |
| `partially_approved` | every image decided, some approved and some denied |
| `denied` | every image denied |
| `cancelled` | the requester withdrew it before any decision (`POST .../cancel/`) |

The status is always computed from the items' decisions, so it cannot disagree with them. 12 requested, 10 approved, 2 denied is `partially_approved`.

### Who can decide

The organization's owner and its moderators, but **never the person who made the request** (no self-approval for owners or moderators either), and a moderator only for the members they may see
(`moderator_data_scope`). The platform administrator has no access to decide: they are not a member, so the decision endpoints answer 404. When nobody can decide a request, submitting it
answers `409 no_eligible_reviewer` ("ask your organization owner or the FaceCloud administrator to add a moderator"), and `POST requests/check/` returns `reviewer_available: false` so a screen can
say so before the member selects anything. This happens for example to an owner whose organization has no moderator any more.

### What can be requested

An image is eligible when it belongs to the member, is filed under that organization, is not in the trash, has a **completed quality verdict of accepted** (a moderator's
approval counts; "needs review" and "rejected" do not), has the requested file, and is not already in an undecided request or covered by an active grant. The backend decides;
every refusal comes back with a code and a sentence. If any selected image is refused, nothing is saved (`400 items_not_eligible` lists each).

### What a grant covers

`artifact_type` is `enhanced` (default: the image's finished enhanced result), `original` (the uploaded file; the standardized copy needs this one too) or `edited`
(one saved edit, with its `artifact_id`). A grant for one never opens another, and an `enhanced` grant is pinned to the result that existed when it was requested.

### What makes a grant stop working (checked on every download)

Expiry, revocation (`POST /api/organizations/{id}/download-grants/{grant}/revoke/`), the use limit (`DOWNLOAD_GRANT_MAX_USES`, 0 = none), the image no longer accepted or in
the trash, the member no longer an active member, or the organization suspended. Nothing is cached, so these take effect at once. **A file already downloaded cannot be recalled.**

## Routes protected when approval is on

| Route | Without a grant |
|---|---|
| `GET /api/images/{id}/download/` (original) | 403 `download_approval_required`, `reason: no_grant` (or `expired`, `revoked`, `used_up`, `not_approved`, `not_a_member`, `organization_suspended`) |
| `GET /api/images/{id}/preprocessing/download/` (standardized copy) | same; needs the `original` grant |
| `GET /api/enhancements/results/{id}/download/` (enhanced, also watermarked) | same; needs the `enhanced` grant |
| `GET /api/enhancements/edits/{id}/download/` (saved edit) | same; needs the `edited` grant |
| batch ZIP packages (create, download, regenerate) | 403 `reason: bulk_not_allowed` |
| person-folder ZIPs (create, download, share, regenerate) when any photo in the folder is in such an organization | 403 `bulk_not_allowed` |
| public share links: creating one for such an image | 403 `bulk_not_allowed`; links made earlier answer 410 while approval is on |

Every refusal is written to the organization's audit trail (`download_blocked`). **Not** gated, on purpose: the other members' images in the moderators' review previews, and the JSON organization export and CSV/PDF
moderator reports (they contain no image bytes; the moderator PDF has no pictures at all).

## Previews while no approval is held

A preview on the screen is a copy too. For an image in an organization that requires approval, its owner (a member, or a moderator looking at their own image) who holds **no active approval** for it
gets only a reduced, watermarked preview: **at most 800 px** (`DOWNLOAD_APPROVAL_PREVIEW_MAX_EDGE`, never above 800), with a tiled FaceCloud watermark, rendered on the fly (the clean cached thumbnail is never
used), `Cache-Control: private, no-store`, header `X-Preview-Restricted: approval-required`. It applies to every route that shows the owner the image:

| Route | Restricted |
|---|---|
| `GET /api/images/{id}/thumbnail/` (and the trash thumbnail), both versions, sizes 256 and 1024 | yes: at most the requested size, never above 800 |
| `GET /api/enhancements/results/{id}/watermark-preview/` | yes: 800 px, tiled watermark whatever was asked |
| `GET /api/enhancements/comparisons/{id}/outputs/{profile}/preview/` | yes |
| `GET /api/enhancements/previews/{id}/image/` (live-edit preview) | yes |
| `GET /api/organizations/{id}/moderator/images/{image}/preview/`, `.../reviews/{review}/image/` | yes, **only** when the moderator looks at their own image; the images of other members keep the normal review preview |
| `GET /api/identities/groups/{id}/avatar/` (person-folder avatar, 96/192/384 px) | yes, when the face it is cut from is in such an image: the size asked for (never above 384), tiled watermark, `no-store`. The route is owner-only; there is no moderator avatar route |
| the pictures inside a **processing report PDF** (160 px) | yes: when the report is generated, and again when a stored one is downloaded (see *Stored report PDFs*). JSON and CSV reports have no pictures |

With an active approval, with approval off, and in a personal workspace nothing changes. Once an approval expires or is revoked the preview is restricted again at once.

## API

Member (`/api/downloads/`):

| Call | Purpose |
|---|---|
| `POST requests/check/` | eligibility and access state (`not_requested`, `pending`, `granted`, `denied`, `expired`, `revoked`) per image, and `reviewer_available` |
| `POST requests/` | submit one request; send `client_token` so a retry returns the same request (200 instead of 201) |
| `GET requests/`, `GET requests/{id}/` | my requests (filters `status`, `organization`) and one request with its items |
| `POST requests/{id}/cancel/` | withdraw a request nobody started to decide |
| `GET requests/{id}/items/{item}/file/` | download one approved image |
| `GET requests/{id}/zip/` | ZIP of the approved images that are still authorized now (denied, expired and revoked ones are left out) |

Owner and moderators (`/api/organizations/{id}/`):

| Call | Purpose |
|---|---|
| `GET download-requests/` | the queue (`status` may list several, e.g. `pending,in_review`; `requester`) |
| `GET download-requests/{id}/` | the request with all its items (preview an image with the existing `moderator/images/{image_id}/preview/` endpoint) |
| `POST download-requests/{id}/decision/` | `{"action": "approve_all" \| "approve_selected" \| "deny_selected" \| "deny_all", "item_ids": [...], "reason": {"code", "text"}, "item_reasons": {"<item id>": {...}}, "expected_version": n}` |
| `POST download-grants/{id}/revoke/` | withdraw an approval |

Denial reason codes: `privacy_or_consent`, `client_restriction`, `distribution_rights`, `incorrect_selection`, `policy_restriction`, `other` (needs text).
`approve_selected` denies the other undecided images with `reason` (finalizing the request); `deny_selected` leaves the others undecided. An image that stopped being eligible
since the request is denied as `no_longer_eligible` instead of approved. Errors: `409 request_closed`, `409 item_already_decided`, `409 request_changed` (stale `expected_version`),
`403 cannot_decide_own_request`. A moderator sees only the requests of the members they may see (`moderator_data_scope`); the owner sees all; another organization's id is 404.

## Stored report PDFs

A report file is stored, and its pictures were decided when it was made. `GET /api/reports/{id}/download/` therefore checks the **current** approval every time. For a PDF whose pictures (the first 40 images of its stored payload)
include an image of an organization that requires download approval, which the owner holds no active approval for at that moment, the answer is a **watermarked copy rendered from the stored payload**, with header
`X-Report-Restricted: approval-required`; the stored file itself is never modified or deleted. This covers a PDF made before approval was switched on and one made while an approval was active, once that approval expired or was revoked.
In every other case the stored file is served exactly as before: approval off, personal workspace, an active approval, a PDF with no pictures, JSON and CSV. A PDF made while the picture was already restricted is served as stored (it is watermarked).
`POST /api/reports/{id}/regenerate/` makes a new file under the same rule. No frontend work is needed beyond saving the response as a file.

## Which workspace a photograph is in

The image and batch APIs say it, taken from the photograph's **batch** (`UploadBatch.organization`, which always existed), never from the owner's memberships: a member of organization A who uploaded into their personal workspace has `organization_id` = their
personal workspace, not A. No column and no migration were needed.

| Where | Fields |
|---|---|
| every image object: `GET /api/images/`, `/api/images/my/`, `/api/images/{id}/`, the images of a batch, albums, memories, search results, person-folder images | `organization_id` (integer or `null`), `download_approval_required` (boolean) |
| every batch object: `GET /api/images/batches/`, `/api/images/batches/{id}/`, the upload answers | `organization_id` (integer or `null`) |

`download_approval_required` is true only for a photograph in a **non-personal** organization whose setting is on. `organization_id: null` (and `download_approval_required: false`) appears only for a batch from before workspaces existed.
An organization's own settings are read from `GET /api/organizations/` and `/api/organizations/{id}/` (`download_approval_required`, `download_grant_hours`, `is_personal`).

## Contract for the frontend (real responses)

Errors have `detail` and `code` (and `reason` / `items` where noted). The responses below are from the running API (ids and times are examples).

**Check** `POST /api/downloads/requests/check/` body `{"organization_id": 1, "items": [{"image_id": 1}]}` ->
`{"approval_required": true, "reviewer_available": true, "items": [{"image_id": 1, "artifact_type": "enhanced", "eligible": true, "reason": "", "detail": "", "access_state": "not_requested"}]}`.
Reasons: `not_found`, `not_in_organization`, `not_yours`, `not_available`, `not_approved`, `not_processed`, `artifact_unavailable`, `already_requested`, `already_granted` (each has a sentence in `detail`).

**Create** `POST /api/downloads/requests/` body `{"organization_id": 1, "items": [{"image_id": 1}, ...], "message": "For the album", "client_token": "c1"}` -> **201** (**200** with `"created": false` on a replay):
```
{"created": true, "request": {"id": 1, "organization": 1, "organization_name": "Event Planners",
  "requester": {"id": 3, "username": "ap-member", "email": "ap-member@example.com"},
  "status": "pending", "requester_message": "For the album", "version": 0,
  "counts": {"total": 3, "pending": 3, "approved": 0, "denied": 0},
  "created_at": "...", "updated_at": "...", "decided_at": null,
  "items": [{"id": 1, "image_id": 1, "image_name": "p0.png", "artifact_type": "enhanced", "artifact_id": 1, "status": "pending",
             "denial_reason_code": "", "denial_reason_text": "", "reviewed_at": null, "grant": null, "file_url": null}]}}
```
**400** `{"detail": "Some of the selected images cannot be requested.", "code": "items_not_eligible", "items": [{"image_id": 4, "artifact_type": "enhanced", "reason": "not_approved", "detail": "..."}]}` (nothing is saved).
**409** codes: `approval_not_required`, `no_eligible_reviewer`, `client_token_reused`. **403** `organization_suspended`.

**List** `GET /api/downloads/requests/` (member) and `GET /api/organizations/{org}/download-requests/` (moderator): an array of the request object above **without** `items` (a page object only when `?page=` is sent). **Detail** (`GET .../{id}/`) adds `items`.
For an approved item `grant` is `{"id", "artifact_type", "artifact_id", "expires_at", "revoked_at", "revoke_reason", "max_uses", "use_count", "active", "expired", "created_at"}`; a denied item has `grant: null` and `denial_reason_code` (+ `denial_reason_text`).
`file_url` (`/api/downloads/requests/{id}/items/{item}/file/`) is set only for the requester and only for an approved item; moderators always see `null`.

**Decision** `POST /api/organizations/{org}/download-requests/{id}/decision/` body `{"action": "approve_selected", "item_ids": [1, 2], "reason": {"code": "client_restriction", "text": ""}, "item_reasons": {"3": {"code": "other", "text": "..."}}, "expected_version": 0}` ->
**200** with the detail object (here `status: "partially_approved"`, `version: 1`, `counts: {"total": 3, "pending": 0, "approved": 2, "denied": 1}`, `decided_at` set).
Errors: **409** `request_closed` (`"This request is already decided or was cancelled."`), `item_already_decided`, `request_changed`; **403** `cannot_decide_own_request` (a requester who is a moderator or owner; a plain member gets the role error `"Organization owner or moderator access is required."`); **400** a missing denial reason, or `other` without text; **404** a request the moderator may not see; **429** the review limit.

**Download refusal** (any of the file routes, with approval on and no valid approval): **403** `{"detail": "This image has not been approved for download. Request approval first.", "code": "download_approval_required", "reason": "no_grant"}`;
`reason` is one of `no_grant`, `expired`, `revoked`, `used_up`, `not_approved`, `not_a_member`, `organization_suspended`, or `bulk_not_allowed` (ZIP packages, person-folder ZIPs, share links).

**Notifications** (`GET /api/notifications/`): `event_type` `download_request_submitted`, `download_request_received`, `download_request_decided`; `action_path` `/dashboard/download-requests/{id}` (requester) or `/moderator/download-requests/{id}?organization={org}` (reviewers);
`metadata` `{"request_id", "organization_id", "total"}`, and on a decision `{"request_id", "status", "total", "approved", "denied"}`. Titles: "Download Request Submitted", "Download request to review", "Download Request Approved" / "Partially Approved" / "Denied".

**Headers** the frontend may read: `X-Preview-Restricted: approval-required` (a reduced, watermarked preview), `X-Report-Restricted: approval-required` (a watermarked report copy).

## Concurrency and repeats

A decision takes a lock on the request and its item rows (`select_for_update(of=("self",))`: PostgreSQL refuses `FOR UPDATE` on the nullable side of an outer join, so only the model's own rows are locked), so
two moderators cannot decide the same image twice: the later one gets 409. A submission takes a lock on the requester, so the "already requested" check cannot race. `client_token` (unique per requester)
makes a retry return the existing request, also when two identical submissions arrive at the same instant; reusing a token for a **different organization, selection or file scope is `409 client_token_reused`**.
The PostgreSQL concurrency tests prove this with separate connections (and were checked to fail when the lock is removed).

## Notifications and e-mail

New event types `download_request_submitted` (requester, in-app), `download_request_received` (owner and moderators who may see the requester; always an in-app task, even if their e-mail or review-update preferences are off; the e-mail follows the preferences) and `download_request_decided` (requester; in-app and e-mail; one per finalized request, titles "Download Request Approved", "...Partially
Approved", "...Denied"). They are created after the decision is committed and idempotent per request; each person is handled on their own, so a failure for one moderator never stops the others, and a failure to create one or to queue
its e-mail is logged and recorded on the notification (`email_status`) and never undoes the decision or removes the request from the moderator queue (the queue is read from the database, not from notifications). E-mails carry a link to the authenticated page and never a photograph. Frontend routes used in links (to be built or
adjusted): `/dashboard/download-requests/{id}` and `/moderator/download-requests/{id}?organization={id}`.

## Audit trail

`download_requested`, `download_decided` (approved items, denied items with reason codes, expiry), `download_request_cancelled`, `download_grant_revoked`, `download_completed`
(single file or ZIP), `download_blocked`. Each has actor, organization, target, time and IP. No image bytes, tokens or passwords are logged.

## Settings (environment variables)

`DOWNLOAD_GRANT_DEFAULT_HOURS` (24), `DOWNLOAD_REQUEST_MAX_ITEMS` (100), `DOWNLOAD_GRANT_MAX_USES` (0 = unlimited), `DOWNLOAD_APPROVAL_PREVIEW_MAX_EDGE` (800, at most 800),
`THROTTLE_RATE_DOWNLOAD_REVIEW` (120/hour per moderator for decisions and revocations; its own limit, separate from the invitation limit). Existing `NOTIFICATION_EMAILS_ENABLED` and the e-mail
settings apply to the e-mails. Nothing is read from the storage layout: files stay under `users/{owner_id}/...` and none is moved.

## Migrations (additive only)

`organizations/0014` (two fields on `Organization`, new audit actions), `downloads/0004` (`DownloadRequest`, `DownloadRequestItem`, `DownloadGrant`, a ZIP download type),
`notifications/0011` (three event types). All three are new columns/tables or wider choice lists; nothing existing is changed or dropped. Rolling back = migrate the three apps back
(the new tables and columns are dropped; no existing data is touched). Not applied to any live database.

## Known limits

* A ZIP is built when it is asked for, in the request, for at most `DOWNLOAD_REQUEST_MAX_ITEMS` images; a very large request would be better as a background job.
* Concurrency is tested on PostgreSQL with threads and separate connections (skipped on SQLite, which cannot lock rows); it was not load-tested.
* While approval is on, a member sees the image at most 800 px and watermarked until approved; they can still look at it. A determined person can photograph a screen: approval controls files, not eyes.
* A report PDF pictured an image clean while an approval was active; once it expires, a download answers with a watermarked copy (see *Stored report PDFs*). A PDF already saved on the person's own device cannot be recalled, like any downloaded file.
* Statistics for the administrator and the owner dashboard (counts of requests by status) are not part of this change.
* Before turning the setting on for any organization, the frontend must handle the 403 `download_approval_required` answer of the download routes and the new request screens, or members see failing download buttons.
* The frontend screens (request button and selection, "My requests", the moderator queue and preview) are not built yet.
