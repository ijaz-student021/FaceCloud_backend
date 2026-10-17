# Feature 33: Administrative management

## Status

Complete for the backend pieces listed below (`operations` app). User listing,
roles, suspension and reactivation were already complete before this feature
(`accounts.AdminUserListView`/`AdminUserUpdateView`); this feature adds the
operational side: monitoring failed work and acting on it, and a platform-wide
audit trail.

## Creating accounts (added 2026-10-16)

`POST /api/user/admin/users/` (administrators only, `accounts.AdminUserListView`) creates an already-verified account: `{username, email,
password, role: "user" | "moderator", organization_id?, full_name?}` -> `201` with the same representation the list returns. The signup
password rules and duplicate checks apply (field errors under `email`, `username`, `password`, `role`); a blank username is derived from the
email; `role: "admin"` is refused. For a moderator, the account and an active moderator `OrganizationMembership` are created in one
transaction, in the organization given by `organization_id` or else the only active non-personal one (none or several -> 400 with a
plain `detail`; a suspended, missing or personal organization is refused). One `admin_created_account` `AuthEvent` names the administrator
(`details.administrator_id`) and the new account; the password is never logged, returned or emailed. Admin-created accounts get **no personal
workspace** (signup creates one when the email is verified). Verified by tests: such a user can sign in and upload (their batches belong to no
organization), and a moderator's own uploads are tagged to the organization they moderate, because that is their only membership.
`createsuperuser` now produces a verified `admin` account (`accounts.models.FaceCloudUserManager`).

## What it adds

| Endpoint | Purpose |
|---|---|
| `GET /api/system/batches/failed/` | Paginated, platform-wide list of failed/stuck upload batches, with owner. |
| `POST /api/system/batches/{id}/retry/` | Retry any user's failed batch. |
| `GET /api/system/jobs/failed/` | The most recent failures of every background-job kind, across all owners. |
| `GET /api/system/audit/` | A combined, paginated, filterable feed of `AuthEvent` and `ActivityEvent`. |
| `GET /api/system/moderation/` | Platform-wide moderation totals, organizations needing attention, and moderator workload. |

All five are administrator-only (`IsAdminRole`), alongside the existing
`GET /api/system/health/` and `GET /api/system/queue/`.

### Failed batches and admin retry

`FailedBatchListView` lists `UploadBatch` rows with `processing_status="failed"`,
newest first, paginated (25 per page, up to 100), including the owner's id and
username.

`AdminBatchRetryView` retries **any** user's batch, not just the caller's own.
It is not a second implementation: the existing owner-facing retry logic
(`UploadBatchRetryView`) was extracted into `uploads.services.retry_batch()`, and
both the owner endpoint and this admin endpoint call that one function. The
per-batch retry cap (`BATCH_PROGRESS_MAX_RETRIES`) still applies to admin
retries, so a batch that is genuinely broken cannot be retried forever by
either path.

### Failed jobs across all kinds

`FailedJobListView` reuses the `JOB_KINDS` registry already built for the
stuck-job reaper (`operations/jobs.py`, Feature 20) to list the most recent
failed row of every job kind (enhancement results, batch jobs, model
comparisons, gallery edits, identity analyses, preprocessing, upload batches,
download packages, reports, identity-group exports), each with its owner and
failure reason. `limit_per_kind` bounds how many rows come back per kind
(default 20, max 100). This is read-only visibility; the reaper (Feature 20)
handles bulk recovery of *stuck* jobs automatically, and the admin batch retry
above handles the one job kind with an existing safe single-item requeue path.

### Platform-wide audit feed

`AdminAuditView` merges `AuthEvent` (registration, login, password, role and
suspension changes) and `ActivityEvent` (uploads, processing, downloads,
groups, shares, privacy) into one chronological list, filterable by `user`,
`category` (`auth`, or an `ActivityEvent.Category` value), `source` (`auth` or
`activity` only) and paginated (`page`, `page_size`, default 50, max 200).

The two tables are queried separately and merge-sorted in Python rather than
unioned in SQL, because they have different columns and vocabularies. Each
request reads at most `page_size` rows from each table per page fetched, which
is appropriate for "what happened recently / around this user", not for
scanning a full history.

### Platform-wide moderator monitoring

`AdminModerationOverviewView` aggregates `moderation.services.dashboard_summary`'s
same per-status counts platform-wide, the same way `operations/analytics.py`
aggregates enhancement data platform-wide — nothing new is measured or
recorded, only an existing org-scoped view generalized across every
organization. It adds three things an org's own dashboard can't show an
administrator:

- **`stale_open`**, a `stale_hours`-configurable (default 48, 1-2160) count of
  still-open reviews (`pending`/`in_review`/`escalated`) older than the
  cutoff — the single most actionable number here, since a review nobody has
  looked at in days is what an administrator actually wants surfaced.
- **`organizations_needing_attention`**, the top 20 organizations with any
  open review, ordered by escalated, then stale, then pending counts, each
  with its oldest still-open review's timestamp.
- **`moderator_workload`**, the top 20 moderators by open assignments, each
  with their assigned-open and total-decided counts, computed as two separate
  grouped queries merged in Python rather than one queryset annotated with
  both counts — `decided_by` has no reverse accessor (`related_name="+"`),
  and annotating two independent `Count()`s over different reverse-FK paths
  on the same queryset risks a join fan-out that silently multiplies one
  count by the other's row count.

## Verification

`operations/test_admin_management.py` (18 tests),
`operations/test_audit.py` (13 tests) and
`operations/test_moderation_overview.py` (14 tests): failed-job listing filters (kind, owner,
limit, unknown kind), only-failed-rows, every registered kind present; failed
batch listing and pagination; admin retry succeeds across owners, honours the
retry cap, 404s on an unknown batch, 400s on nothing-to-retry, requires the
admin role; audit merging, ordering, user/category/source filtering
(including a real bug the tests caught: `category=auth` was incorrectly
validated against `ActivityEvent.Category` instead of being treated as "auth
events only", now fixed), pagination and `has_next`, admin-only permission;
moderation overview totals, stale-cutoff filtering, per-organization ordering
and scoping, per-moderator workload correctness including a regression guard
for the join-fan-out trap (mutation-tested: reintroducing the bug makes that
test fail, confirming it is not vacuous), `stale_hours` validation, and
agreement with `dashboard_summary` for a single-organization scenario.

## Deliberately not built

- **Storage and threshold configuration.** Every threshold in this project
  (quality scoring, enhancement guards, retention, etc.) is an environment
  variable read once at process start. Making them live-editable from an admin
  UI would need a settings-override store with cache invalidation across every
  worker and web process — a real architectural addition, not a small one, and
  out of scope for this pass. Thresholds are documented in `.env.example` and
  changed by redeploying with new values today.
- **Per-job-kind admin retry.** Only upload batches have an existing,
  already-tested single-item requeue path reused here. Building bespoke
  requeue logic for the other nine job kinds (each with different re-enqueue
  mechanics) is real new pipeline behaviour, not administrative plumbing, and
  was not invented for this feature. `GET /api/system/jobs/failed/` still gives
  full visibility into what failed and why.
