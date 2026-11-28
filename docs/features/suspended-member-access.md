# Suspended and removed members: what they can still reach

A member whose membership of a **shared** organization is not `active` (suspended, removed or never accepted) can no longer reach anything that was filed under
that organization. This is enforced by the backend on every request; a token issued before the suspension changes nothing because the membership is read live.

| | Suspended / removed member | Organization's moderators, owner, administrator |
|---|---|---|
| Content filed under that organization (photos, batches, results, edits, person folders, exports, reports, download requests and grants, share links, download history, activity timeline, data export) | **Unreachable** through every owner-scoped endpoint (404/403/410, or absent from lists and counts). Public share links of that content answer 410 while the membership is not active | Unchanged: they keep seeing the organization's records |
| Personal workspace, albums, person folders and photos not filed under the organization | Unchanged | n/a |
| Other organizations they belong to | Unchanged | n/a |
| Account, login, profile | Unchanged | n/a |
| Uploading into that organization | Refused (needs an active membership) | n/a |

Nothing is deleted, moved or re-owned. Reactivating the membership (`update_member`, which applies the plan's seat limits) restores access, including dormant
share links.

## How it works

`organizations/access.py` keeps, per request, the set of shared organizations in which the requesting person has a non-active membership (one lazy query, only when
filed content is touched). Every model that holds organization-filed content has a default manager that excludes rows filed under those organizations
(`locked_manager(...)`; `ImageUpload.objects`/`all_objects`, `UploadBatch`, results, person folders, reports, requests, grants, share links, albums' items,
thumbnails, embeddings, ... and, through ids, `DownloadEvent` and `ActivityEvent`). With an empty set nothing is added, so moderators, Celery tasks, management
commands and everybody who is not locked out of something are unaffected. The public share view checks the link owner explicitly.

Tests: `organizations/test_suspended_member_lock.py`.

## Known limits

* Notifications that were already delivered keep their text (a title may name a photo); they carry no content.
* A locked member cannot reach organization-filed biometric data through the privacy endpoints either; the organization's administrators decide about it.
* Files already downloaded cannot be recalled.
