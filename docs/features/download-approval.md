# Download approval

An organization can require that its members download an image only after a moderator has approved it. It is **off for every organization** until the FaceCloud
administrator turns it on, and it never applies to a personal workspace. Where it is off, every download behaves exactly as before.

Status: backend implemented and tested; no frontend yet; not deployed. Code: `downloads/approval.py` (rules), `downloads/approval_views.py` and
`downloads/approval_serializers.py` (API), `downloads/models.py` (three models), `downloads/test_download_approval.py` (57 tests).

## The setting

| Field on `Organization` | Meaning |
|---|---|
| `download_approval_required` | `false` by default. Only the platform administrator changes it (`PATCH /api/organizations/admin/{id}/`); an owner gets 403 if they try. A personal workspace cannot turn it on. |
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

Every refusal is written to the organization's audit trail (`download_blocked`). **Not** gated, on purpose: thumbnails and inline previews (the gallery), the 160-pixel
previews inside a processing report PDF, the moderators' own previews, and the JSON organization export and CSV/PDF moderator reports (they contain no image bytes).

## API

Member (`/api/downloads/`):

| Call | Purpose |
|---|---|
| `POST requests/check/` | eligibility and access state (`not_requested`, `pending`, `granted`, `denied`, `expired`, `revoked`) per image |
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

## Concurrency and repeats

A decision takes a lock on the request row, so two moderators cannot decide the same image twice: the later one gets 409. A submission takes a lock on the requester, so the
"already requested" check cannot race, and `client_token` (unique per requester) makes a retry return the existing request.

## Notifications and e-mail

New event types `download_request_submitted` (requester, in-app), `download_request_received` (owner and moderators who may see the requester; in-app and e-mail, controlled by
the moderator-updates preference) and `download_request_decided` (requester; in-app and e-mail; one per finalized request, titles "Download Request Approved", "...Partially
Approved", "...Denied"). They are created after the decision is committed and idempotent per request; a failure to create one or to queue its e-mail is logged and recorded on the
notification (`email_status`), and never undoes the decision. E-mails carry a link to the authenticated page and never a photograph. Frontend routes used in links (to be built or
adjusted): `/dashboard/download-requests/{id}` and `/moderator/download-requests/{id}?organization={id}`.

## Audit trail

`download_requested`, `download_decided` (approved items, denied items with reason codes, expiry), `download_request_cancelled`, `download_grant_revoked`, `download_completed`
(single file or ZIP), `download_blocked`. Each has actor, organization, target, time and IP. No image bytes, tokens or passwords are logged.

## Settings (environment variables)

`DOWNLOAD_GRANT_DEFAULT_HOURS` (24), `DOWNLOAD_REQUEST_MAX_ITEMS` (100), `DOWNLOAD_GRANT_MAX_USES` (0 = unlimited). Existing `NOTIFICATION_EMAILS_ENABLED` and the e-mail
settings apply to the e-mails. Nothing is read from the storage layout: files stay under `users/{owner_id}/...` and none is moved.

## Migrations (additive only)

`organizations/0014` (two fields on `Organization`, new audit actions), `downloads/0004` (`DownloadRequest`, `DownloadRequestItem`, `DownloadGrant`, a ZIP download type),
`notifications/0011` (three event types). All three are new columns/tables or wider choice lists; nothing existing is changed or dropped. Rolling back = migrate the three apps back
(the new tables and columns are dropped; no existing data is touched). Not applied to any live database.

## Known limits

* A ZIP is built when it is asked for, in the request, for at most `DOWNLOAD_REQUEST_MAX_ITEMS` images; a very large request would be better as a background job.
* Locking is tested sequentially here; the database locks (`select_for_update`) take effect on PostgreSQL, which is what CI and production use.
* Statistics for the administrator and the owner dashboard (counts of requests by status) are not part of this change.
* The frontend screens (request button and selection, "My requests", the moderator queue and preview) are not built yet.
