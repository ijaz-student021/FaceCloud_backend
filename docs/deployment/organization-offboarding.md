# Organization offboarding: export, suspension, retention, purge (design for approval)

**Status: design only. Nothing in this document is implemented and no code or migration accompanies it.** It is written for review: it ends with the decisions that need an answer
before anything is built. It is based on the code at the tip of the multi-tenancy chain (PRs #95 to #102: moderator organization, data scope, member notification, administrator
management and suspension, owner console, plan limits, registration from an invitation, branding). Every statement about the data is taken from the models as they are; the places
where I could not be sure are marked **Not verified**.

---

## 1. What "an organization's data" is in this system (findings that shape the design)

These facts decide how offboarding has to work. Several are surprising and are the reason a naive "delete the organization" would be wrong.

1. **The organization does not own the photographs; the members do.** `UploadBatch.owner` and `ImageUpload.owner` point at the member who uploaded. The organization is only a **tag**
   (`UploadBatch.organization`) saying "this upload was filed under that organization". A member's personal photographs and the ones filed under a team are rows of the same tables, with the same
   owner, in the same folder on disk.
2. **Deleting the `Organization` row would delete almost nothing.** The foreign keys that matter are `on_delete=SET_NULL`: `UploadBatch.organization`, `IdentityGroup.organization`,
   `AccountInvitation.organization`. Dropping the row would leave every photograph, thumbnail, embedding and file in place, merely un-tagged. Only the organization's own rows
   (memberships, invitations, audit events, moderator reviews and reports, exports) are `CASCADE`. **A purge therefore has to be an explicit, ordered job; it cannot be a delete.**
3. **Files are not stored per organization.** Every file lives under `users/<owner id>/<kind>/<uuid>.<ext>` (the layout the orphan scanner in `privacy/cleanup.py` relies on). An
   organization's photographs are scattered across its members' folders, next to their personal ones. Files can only be found through the database rows that name them, and deleting a row does
   **not** delete its file (Django does not do that); the purge must delete files itself.
4. **Person folders (`IdentityGroup`) belong to a member, not to an organization.** They are created with `organization=get_default_organization(owner)` (`identities/services.py`), which prefers the
   member's **personal** workspace. A folder can hold faces from photographs of several organizations and the personal workspace. The moderator console finds "the organization's folders" by looking at
   which photographs are in them (`moderation/identity_groups.py`), not by the folder's `organization` field. Two consequences: (a) removing an organization's photographs leaves **folders whose
   centroid (a biometric template) was computed partly from deleted faces** and must be recomputed or the folder deleted, exactly as `privacy.services.erase_facial_data(older_than=...)` already does;
   (b) the existing organization export lists identity groups with `filter(organization=organization)`, which for a shared organization is, by (4), normally **empty**. That is a gap in today's export,
   found by reading the code; **Not verified** by a test.
5. **Encryption keys are instance-wide.** Images are AES-256-GCM encrypted at rest (`config/image_crypto.py`) and embeddings too (`identities/storage.py`), but with keys derived from one configured
   secret (key *ids* allow rotation, not per-organization keys). **Crypto-shredding an organization is therefore not possible**; ciphertext and rows must actually be deleted.
6. **The existing organization export is metadata only and capped.** `build_organization_export_payload` writes JSON: members, invitations, batches, images (names and status, not bytes), quality decisions, duplicate
   matches, identity groups and memberships (no embeddings), moderator reviews, audit events. Each list is capped at `ORGANIZATION_EXPORT_MAX_ROWS` (2000) and carries a `truncated` flag; the file expires after
   `ORGANIZATION_EXPORT_TTL_HOURS` (24 h). It **contains no photographs**. A customer leaving would reasonably expect their photographs back.
7. **Soft references exist that no foreign key follows.** `ActivityEvent` (`object_type`/`object_id`/`details`), `UserNotification` (`resource_type`/`resource_id`/`metadata`), `DownloadEvent`
   (`batch_id`/`image_id`/`filename`), `Collage.image_ids` and `IdentityGroupExport.image_ids` (JSON lists of image ids, with ZIP/PNG files that contain the photographs themselves) refer to org-filed
   photographs by number or inside JSON. They are **not removed by a cascade** and the files behind two of them contain the photographs.
8. **Backups outlive deletion.** `docs/deployment/VPS_RUNBOOK.md` section 10 keeps database dumps 14 days and media archives 7 days. A purge removes data from the live system immediately and from backups only as they
   rotate. This must be stated to the customer; it is not something code can fix.
9. **Members and moderators are people with their own accounts.** Offboarding an organization must not delete accounts. A member keeps their account and personal workspace. A moderator account created for
   this organization is left with no organization, and (rule from PR #95) a moderator without one cannot sign in.

## 2. Lifecycle

```
 active ──(owner asks, or the administrator decides)──► offboarding requested
    ▲                                                        │ administrator confirms
    │ reactivate (any time before purge)                     ▼
    └────────────────────────────── suspended + final export + notices  ◄── "retention window" (default 30 days)
                                                             │ window ends, no hold, no objection
                                                             ▼
                                                        purge job (ordered, verified)
                                                             │
                                                             ▼
                                                  purged (tombstone row kept)
```

| Step | What happens | Who may trigger it | Reversible? |
|---|---|---|---|
| **0. Request** | The owner asks to leave, or the administrator starts it (non-payment, contract end, abuse). Creates an offboarding record with a reason. | Owner (request only) or administrator | Yes, cancel |
| **1. Export** | A complete archive is produced for the owner (section 4). | Owner may start it at any time (today's `POST export/` already exists); the administrator starts the final one | n/a |
| **2. Suspend** | Existing suspension (PR #98) plus the extras in section 5. Starts the retention clock. | **Administrator only** | **Yes**: reactivate restores everything |
| **3. Retention window** | Nothing is deleted. Reminders go out. The administrator can extend it or place a legal hold. | Administrator | Yes |
| **4. Purge** | The ordered, verified deletion in section 7, run by a background job when the window ends. | **Administrator only**, and only after the window; confirmed by typing the organization's slug | **No** (except from backups, section 1.8) |

Why this split of responsibility: an owner can *ask* (they are the customer) but cannot make irreversible decisions about data that belongs partly to their members; the platform administrator is the
only role outside every organization (PR #95) and is the one who can be held accountable. A purge needs the administrator **and** a prior request (owner's or a recorded administrator reason), so a single
click by one person cannot destroy a customer's data. The default retention window is **30 days**, configurable.

## 3. Proposed settings (all with environment overrides, none implemented)

| Setting | Default | Meaning |
|---|---|---|
| `ORG_OFFBOARDING_RETENTION_DAYS` | 30 | Days between suspension and the earliest purge |
| `ORG_OFFBOARDING_REMINDER_DAYS` | `14,3,1` | Days before the purge when owner and members are reminded |
| `ORG_AUDIT_RETENTION_DAYS` | 365 | How long the organization's audit trail and memberships are kept after the purge (section 8) |
| `ORG_PURGE_BATCH_SIZE` | 500 | Images deleted per transaction |
| `ORG_OFFBOARDING_ARCHIVE_TTL_DAYS` | = retention days | How long the final archive stays downloadable (the normal export expires after 24 h and would be gone before the purge) |

## 4. Step 1: how the data is exported

**What exists:** the metadata export of section 1.6, owner or moderator (owner only when moderators are limited to their own members, PR #96). It is useful as a record but is not enough for offboarding.

**Gaps to close before offboarding (proposed work, needs approval):**

1. **Uncapped and complete.** The final export must not truncate at 2000 rows per list: it is produced in parts (JSON Lines, one file per table) inside the archive.
2. **Photographs.** The archive includes, per member, the **originals and the enhanced results** of the batches filed under the organization, in folders by member and batch, plus a manifest with checksums.
   Each member can already download their own batches with the existing per-batch download packages; the organization-level archive is new.
3. **Correct person folders.** Replace `IdentityGroup.filter(organization=...)` by "folders that contain at least one organization photograph" (the definition the moderator console uses).
   Biometric templates are never exported (unchanged rule).
4. **Longer life.** The archive file lives until the end of the retention window (setting above), not 24 hours.
5. **Reviews and notes.** `ModeratorReview.decision_reason` and `decision_notes` are included (they are the organization's records), `message_to_member` too.

The owner downloads it through the existing authenticated download pattern. Members are told, in their own notification, that organization-filed photographs will be deleted on a stated date and how to
download their own (existing flow).

## 5. Step 2: what suspension does and what it should additionally do

**Already true (PR #98):** members, moderators and the owner get 403 on every organization endpoint; uploads to it are refused; invitations can be neither previewed nor accepted; personal workspaces
are unaffected; nothing is deleted.

**To add for offboarding (design):**

* **Revoke every share link** that points at an organization photograph (`sharing_sharelink.revoked_at`), because share links are public by token and must not keep serving a departed customer's
  images during the retention window. Reactivating does *not* silently re-enable them; the owner re-creates them.
* **Cancel pending invitations** (`organizations_organizationinvitation`, `accounts_accountinvitation` naming the organization).
* **Stop work in flight:** cancel queued or running batches of the organization (existing cancel logic), stop generating moderator reports and exports, release moderators' claimed reviews.
* **Notify** the owner and every member (in-app, plus email by preference) with the dates.
* Record who suspended it, when, and why (the offboarding record).

## 6. Step 3: the retention window

* Nothing is deleted. The administrator can **reactivate** (everything is as it was, except share links) or **extend** the window or set a **legal hold** (a flag that blocks the purge job until cleared).
* Reminders at 14, 3 and 1 day(s) before the purge to the owner and to every member who has organization-filed photographs.
* A member may choose **"keep my photos"** for their own organization-filed batches (decision 1 below): those batches are re-tagged to the member's personal workspace instead of being purged.

## 7. Step 4: the purge

A Celery task, one per organization, idempotent and resumable (it can be run again after a crash and finishes the job), writing progress and counts (never content) to the offboarding record.

**Order** (children before parents; files after rows commit; each stage in batches of `ORG_PURGE_BATCH_SIZE`):

1. **Freeze.** Confirm status is suspended, no legal hold, window elapsed, the slug confirmation matches. Mark the offboarding record `purging`.
2. **Collect the file names** of everything about to be deleted (all `FileField` values of the rows in the inventory, section 9), into a work list stored on the offboarding record. Files cannot be found again once the rows are gone.
3. **Soft references first** (they cannot be reached after the images are gone): delete `Collage` and `IdentityGroupExport` rows whose `image_ids` intersect the organization's images (and queue their files); delete
   `ActivityEvent`, `DownloadEvent` and `UserNotification` rows that refer to the organization's batches, images, reviews or `metadata.organization_id`.
4. **Identity data.** Delete `IdentityChange` rows of the memberships about to go; then for each affected person folder: if it still holds faces from other workspaces, **recompute** it (`identities.services._refresh_group`, as
   `erase_facial_data` does: member count and the encrypted centroid); if it holds nothing, delete it with its history. Biometric rows (`IdentityFace.protected_embedding`, `IdentityGroup.protected_centroid`, `DuplicateAnalysis.face_fingerprints`) go with
   their images.
5. **The photographs.** Delete the organization's `ImageUpload` rows batch by batch. The cascade removes preprocessing results, validation results, quality decisions, duplicate analyses and matches (both sides),
   thumbnails, enhancement results, edit versions, previews, comparisons, advisor recommendations, share links and their access logs, reports, download packages, camera snapshots, identity analyses, faces and memberships. Then delete the empty `UploadBatch` rows.
6. **The organization's own records:** moderator reviews, moderator reports, exports (the final archive is deleted last, in step 8), invitations.
7. **Files.** Delete every file on the work list from storage. Missing files are not errors (idempotent).
8. **The archive** and the organization logo are deleted.
9. **Tombstone.** The `Organization` row is kept (section 8): `status = purged`, brand name, accent and logo cleared, `purged_at` set. Memberships and audit events are kept until `ORG_AUDIT_RETENTION_DAYS`, then removed by a second, small scheduled job.
10. **Verify.** Re-count every table of the inventory by the same selectors (all must be zero), check that none of the work-list files still exists, and re-check the person folders. The result (`verified: true/false`, counts) is stored on the offboarding record and in the
    organization's audit trail. If verification fails the record stays `failed`, an alert is raised, and the job can be run again. The same pattern as `erase_facial_data` (counts, delete, count again, record).

**Members and moderators after a purge:** accounts, sessions, avatars, consents, notification preferences and personal workspaces are untouched. A moderator whose only organization was this one is
listed in a pre-purge checklist for the administrator (the existing `report_moderators_without_organization` command already finds them afterwards): the administrator chooses to deactivate the account or
change its role to `user`. The system does neither automatically.

**What the customer is told:** deleted from the live system when the job reports `verified`; remains in backups for up to 14 days (database) and 7 days (media) and is not restored except for disaster recovery, after which the purge is re-run.

## 8. What is kept, and why

I am not a lawyer; the retention periods below are a **proposal** that whoever advises FaceCloud on data protection should confirm before it is built. All are settings.

| Kept | For how long | Why | Personal data in it |
|---|---|---|---|
| `Organization` tombstone: id, name, slug, plan, created / suspended / purged dates, counts at purge | indefinitely (a few hundred bytes) | The audit trail and platform logs refer to it by id; proof that the customer existed and was offboarded | the organization's name (a business) |
| `OrganizationAuditEvent` | `ORG_AUDIT_RETENTION_DAYS` (365) after the purge, then deleted | Accountability for who decided what about members' photographs; resolves disputes after exit | actor ids; **`ip_address` is nulled at purge** |
| `OrganizationMembership` (user, role, status, dates) | same as the audit trail | Shows who was owner / moderator when an event happened | user ids only |
| The offboarding record (reason, who, dates, counts, verification result) | same | Evidence that the deletion happened | none |
| `AuthEvent`, `SecurityEvent`, `SecurityAlert`, `PrivacyRequest` | their existing retention (unchanged) | Platform security and privacy records, not the organization's data | IPs, paths (these include `/api/organizations/<id>/...`) |
| Backups | 14 days (database), 7 days (media) | Disaster recovery | everything, until rotated |

**Not kept:** photographs (originals, preprocessed, enhanced, edited), thumbnails, previews, embeddings, centroids, face fingerprints, person-folder membership of those photographs, quality, validation and duplicate results, moderator reviews with their reasons and notes,
moderator reports, exports, shared links, invitations (invitee email addresses), the logo and branding.

## 9. Exact data inventory per table

Notation: **ORG** = the organization; **B** = `uploads_uploadbatch` rows with `organization_id = ORG`; **I** = `uploads_imageupload` rows with `batch_id` in B; **R** = `moderation_moderatorreview` rows of ORG.
"Cascade" means "removed by the database when its parent row is deleted" (verified from each model's `on_delete`). Table names are the real `db_table` names.

### 9.1 The organization's own rows (foreign key to ORG)

| Table | Holds | Selected by | At purge | Files |
|---|---|---|---|---|
| `organizations_organization` | the tenant | `id = ORG` | **keep as tombstone**; clear `brand_name`, `brand_accent`, `logo`, `logo_updated_at`, `logo_uploaded_by` | `logo` (`users/<uploader>/organization-logos/`): **delete** |
| `organizations_organizationmembership` | who was in it and in which role | `organization_id` | keep for `ORG_AUDIT_RETENTION_DAYS`, then delete | none |
| `organizations_organizationinvitation` | invitee email, role, token hash | `organization_id` | **delete** (pending ones cancelled at suspension) | none |
| `organizations_organizationauditevent` | the audit trail (`previous_value`, `new_value`, `reason`, `ip_address`) | `organization_id` | keep, null `ip_address`; delete after the retention period | none |
| `moderation_moderatorreview` | review of a photo: AI recommendation, `decision_reason`, `decision_notes`, `message_to_member`, assignments | `organization_id` (also cascade from I) | **delete** | none |
| `moderation_moderatorreport` | generated reports; `payload` JSON has per-member counts | `organization_id` | **delete** | `report_file` (`users/<requester>/moderator-reports/`): **delete** |
| `privacy_organizationexport` | export jobs; `row_counts` | `organization_id` | **delete** (the final archive last) | `export_file` (`.../organization-exports/`): **delete** |
| `accounts_accountinvitation` | administrator's account invitations naming ORG (invitee email) | `organization_id` (SET_NULL) | cancel at suspension, **delete** the rows at purge | none |

### 9.2 Photographs filed under ORG and everything derived from them (owned by the members)

| Table | Holds | Selected by | At purge | Files |
|---|---|---|---|---|
| `uploads_uploadbatch` | one upload (single, multiple, zip) | **B** (`organization_id`, SET_NULL) | **delete** after its images | none |
| `uploads_imageupload` | one photograph: name, size, checksum, status | **I** (`batch_id`) | **delete** (the cascade below) | `original_file` (`users/<owner>/originals/`): **delete** |
| `uploads_batchimageprogress` | per-image pipeline progress | cascade from B / I | cascade | none |
| `uploads_imagepreprocessingresult` | resized / converted copy | cascade from I | cascade | `processed_file` (`.../preprocessed/`): **delete** |
| `validation_facevalidationresult` | detected faces, boxes | cascade from I | cascade | none |
| `quality_control_imagequalitydecision` | scores, defects, rejection reasons | cascade from I | cascade | none |
| `duplicates_duplicateanalysis` | perceptual hash, **`face_fingerprints`** (biometric-derived) | cascade from I | cascade | none |
| `duplicates_duplicatematch` | a pair of images (either side in I) | cascade from I (both FKs) | cascade (the surviving photo's `match_count` may be stale; harmless) | none |
| `thumbnails_imagethumbnail` | thumbnails | cascade from I | cascade | `file` (`.../thumbnails/`): **delete** |
| `enhancement_enhancementbatchjob` | batch enhancement job | cascade from B | cascade | none |
| `enhancement_imageenhancementresult` | enhanced image | cascade from I | cascade | `enhanced_file` (`.../enhanced/`): **delete** |
| `enhancement_imageeditversion` | manual edits | cascade from the result | cascade | `edited_file` (`.../edited/`): **delete** |
| `enhancement_editpreview`, `enhancement_editpreviewsource` | cached edit previews | cascade from the result | cascade | `file`, `baseline_file` (`.../previews/`): **delete** |
| `enhancement_modelcomparison`, `enhancement_modelcomparisonoutput` | model comparison | cascade from I | cascade | `preview_file` (`.../comparisons/`): **delete** |
| `enhancement_advisorrecommendation` | AI advisor suggestion and rationale text | cascade from the result | cascade | none |
| `enhancement_advisorrun` | grouping row of recommendations (owner-level) | **not** cascaded (parent of the recommendation) | delete when it has no recommendation left | none |
| `enhancement_advisorapicall` | token and latency accounting | `recommendation` is SET_NULL | keep (no content, no photo reference) | none |
| `reports_processingreport` | per-image / per-batch report with `payload` | cascade from I / B | cascade | `report_file` (`.../reports/`): **delete** |
| `downloads_downloadpackage` | prepared ZIP of a batch | cascade from B | cascade | `package_file` (`.../download-packages/`): **delete** |
| `sharing_sharelink`, `sharing_sharelinkaccess` | public share links and their access log | cascade from the enhancement result / edit version / identity export | cascade (links revoked earlier, at suspension) | none |
| `camera_camerasnapshot` | snapshot taken from a camera session | cascade from I | cascade (the session row belongs to the member and stays) | none |
| `identities_identityanalysis` | face-analysis run | cascade from I | cascade | none |
| `identities_identityface` | one face: box, landmarks, **`protected_embedding`** (biometric template) | cascade from I | cascade | none |
| `identities_identitymembership` | face belongs to a folder | cascade from the face | cascade | none |
| `identities_identitygroup` | a person folder (member-owned), **`protected_centroid`** | folders holding at least one face of I | **recompute** if it still holds other faces, **delete** if it ends up empty | none |
| `identities_identitychange` | history of folder edits (`before` / `after` JSON) | `membership` / `group` are SET_NULL: select by the membership and group ids collected in step 4 | **delete** those rows first | none |
| `identities_identitygroupexport` | ZIP of a folder's photos; `image_ids` JSON | `image_ids` intersects I, **or** its group is deleted | **delete** | `export_file` (`.../identity-exports/`): **delete** |
| `collages_collage` | collage of photos; `image_ids` JSON | `image_ids` intersects I | **delete** | `collage_file` (`.../collages/`): **delete** |

### 9.3 Member-level logs that mention ORG's objects without a foreign key

| Table | Holds | Selected by | At purge |
|---|---|---|---|
| `activity_activityevent` | member's activity history (`object_type` / `object_id` / `details`, may contain file names) | `object_type` in (`UploadBatch`, `ImageUpload`, `ImageEnhancementResult`, `ProcessingReport`, `DownloadPackage`, `IdentityGroupExport`, `ShareLink`) with `object_id` in the matching sets, or `details.organization_id = ORG` | **delete** those rows (**Not verified**: the full list of `object_type` values emitted was taken from a search of the code and should be re-checked when building) |
| `downloads_downloadevent` | log of what the member downloaded (`batch_id`, `image_id`, `filename`) | `batch_id` in B or `image_id` in I | **delete** |
| `notifications_usernotification` | in-app notices (`resource_type` `batch` / `image` / `moderator_review` / `report`..., `metadata.organization_id`) | `metadata.organization_id = ORG`, or `resource_type` / `resource_id` in B, I, R | **delete** |

### 9.4 Left alone on purpose

`accounts_user` (including `avatar`), `accounts_usersession`, `accounts_loginattempt`, `accounts_pendingregistration`, `accounts_authevent` (kept: security record; `details.organization_id` is an id), `privacy_biometricconsent` and `privacy_privacyrequest`
(per person, not per organization), `notifications_notificationpreference`, `enhancement_advisorconsent`, `camera_camerasession`, `security_securityevent` and `security_securityalert` (platform security; their `path` / `evidence` may
contain `/api/organizations/<id>/`), `contact_contactmessage`, and every row of the members' **personal workspaces** (their batches have `organization` = the personal workspace, which is never the organization being offboarded).

## 10. What to test if this is built

* A fixture with an organization of three members, two moderators, photographs (with every derived row and every file), person folders that mix org and personal photographs, a collage and a folder export that include org photographs, share links, notifications and activity rows.
* After the purge: every selector in section 9 returns zero; none of the collected files exists; the personal photographs, their folders (recomputed centroid, correct `member_count`) and files of the same members are **byte-for-byte untouched**; accounts exist; a second organization's data is untouched; the tombstone and the audit trail remain with `ip_address` null.
* Preconditions: refused while active, without the window elapsed, with a legal hold, with a wrong slug confirmation, by a non-administrator, by the owner alone.
* Idempotency: killing the job halfway and running it again finishes without error and with the same final counts; running it twice is a no-op.
* Verification failure path (a file that cannot be deleted): the record is `failed`, an alert is raised, nothing is reported as done.
* Reactivation inside the window restores access and does not resurrect revoked share links.
* The export: more than 2000 rows are not truncated; photographs are in the archive with correct checksums; person folders are non-empty for a shared organization.

## 11. Decisions I need before any code

1. **Whose photographs are they?** Photographs filed under an organization are owned by the members who uploaded them. My recommendation, and what this document assumes: **purge them**, after notice, with the "keep my photos" option (re-tag to the member's personal workspace) for members who ask in time. The alternative, never purging member-owned photographs and only un-tagging them, is simpler and safer for members but leaves the customer's data on the platform. Which one?
2. **Retention window:** 30 days default. Shorter, longer, or per contract?
3. **Audit retention after the purge:** 365 days proposed. Is there a contractual or legal period I should use instead?
4. **Who may start it:** owner requests, administrator confirms (proposed). Should a second administrator also be required for the purge itself ("four eyes")?
5. **Moderator accounts left without an organization:** leave them listed for the administrator to decide (proposed), or deactivate automatically?
6. **The archive:** is a photographs-included archive (new work, section 4) required, or is the existing metadata export plus each member's own downloads acceptable for now? This is the largest part of the implementation.
7. **Backups:** acceptable to tell customers the 14 / 7 day rotation, or is a shorter rotation or a targeted backup purge required?
8. **Scope of the first version:** I suggest building only suspension extras + the offboarding record + the purge (without the photographs archive) first, behind a command-line entry point for the administrator, and the API / screens afterwards.

## 12. Rough implementation shape (for estimating, not a commitment)

* Models: `OrganizationOffboarding` (organization, requested_by, reason, status, dates, legal_hold, work list, counts, verification result); `Organization.status` gains `purged`. One migration.
* Services: `request_offboarding`, `start_suspension_extras`, `purge_organization` (the stages of section 7), `verify_purge`, `expire_retained_audit`.
* Celery: the purge task, a daily "purge due" job, reminders, the audit-retention job (added to the beat schedule, like `expire-organization-exports`).
* API (administrator): create / read / cancel an offboarding, set a legal hold, confirm the purge with the slug; (owner) request one and read its status. Management command for the first version.
* Tests as in section 10. No frontend work is implied until the API is approved.
