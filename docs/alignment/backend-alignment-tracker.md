# Backend alignment tracker

The one canonical tracker for aligning this backend with the frontend
(`/Users/Ali/Downloads/FYP`, commit `ee7d126`, read-only reference). Source of
scope: `facecloud-detailed-feature-alignment-plan.md` and
`facecloud-page-alignment-inventory.csv` (review deliverables, 2026-10-14).

Backend baseline: branch `feat/backend-frontend-alignment`, based on
`fix/pipeline-review-followups` at `51d746f` (pushed). The current route list
(156 API routes) was generated from Django's URL resolver on 2026-10-14.

Frontend contracts for everything added: `frontend-handoff.md`.

**Backend status values:** `exists` (reuse; verified against routes and
serializers), `extended` / `added` (with commit), `planned`, `frontend-only`
(no backend work), `deferred` (with reason). Frontend integration is tracked
separately in `frontend-handoff.md`; nothing here claims a frontend change.

## Findings that apply to every page

* The frontend calls about 35 real API paths. `/api/admin/users`,
  `/api/admin/audit-logs`, `/api/images/list` and `/api/images/upload` occur only
  in `src/data/mock.ts` and the ApiTest placeholder: they are mock data, not a
  contract. The real equivalents are `/api/user/admin/users/`,
  `/api/system/audit/`, `/api/images/my/` and `/api/images/upload/{single,multiple,zip}/`.
* Swagger `/api/docs/`, schema `/api/schema/` and ReDoc `/api/redoc/` exist
  (drf-spectacular). The schema validates with `--fail-on-warn`.
* Organization staff checks: `OrganizationScopedMixin` resolves membership per
  request (404 for non-members, 403 for members without a staff role).

## Public and authentication

| ID | Page | Backend | Status | Gap / package |
|---|---|---|---|---|
| PAGE-003 | Landing | none | frontend-only | Copy must match capabilities |
| PAGE-001 | About | none | frontend-only | as above |
| PAGE-002 | Contact | `POST /api/contact/` | added `3836620` | — |
| PAGE-006 | Register | `POST /api/auth/register/` | exists | Display name: B02 profile field |
| PAGE-007 | Signup (unmounted) | as Register | frontend-only | `/signup` redirects to Register |
| PAGE-009 | VerifyEmail | `verify-email/`, `verify-email/resend/`, `registrations/{id}/status/` | exists | — |
| PAGE-004/010/031 | Login (all roles) | `auth/login/`, `auth/refresh/`, `auth/2fa/verify/` | exists | Session claim: B02 |
| — | Reset / 2FA screens | `password-reset/`, `password-reset/confirm/`, `2fa/*` | exists | Frontend routes missing (handoff) |
| PAGE-005/008 | NotFound, Unauthorized | none | frontend-only | — |

## User portal

| ID | Page | Existing backend | Status | Gap / package |
|---|---|---|---|---|
| PAGE-038 | Dashboard | `GET /api/user/dashboard/` | typed `ce9c212` | — |
| PAGE-049 | UploadCenter | `GET /api/images/upload/limits/` | added `698f68e` | — |
| PAGE-051 | UploadSingle | `upload/single/`, batch progress, `enhancements/images/{id}/` | exists | — |
| PAGE-050 | UploadMultiple | `upload/multiple/` | exists | — |
| PAGE-052 | UploadZip | `upload/zip/` | exists | — |
| PAGE-047 | ProcessingPage | batch detail/progress, enhancement result | exists | ID kind is batch id (handoff) |
| PAGE-046 | ProcessingJobs | `enhancements/jobs/`, `images/batches/` | exists; pagination added `f8f9f4b` | — |
| PAGE-044 | MyImages | `GET /api/images/my/` (paginated) | exists | Filters already in view; verify |
| PAGE-042 | ImageDetail | image, preprocessing, quality, validation, duplicates, enhancement, edits, shares | exists | — |
| PAGE-041 | IdentityGroups | `identities/groups/`, `search/`, `review/`, privacy consents | exists; pagination added `f8f9f4b` | — |
| PAGE-040 | GroupDetail | rename/merge/profile/delete/memberships/exports | exists | — |
| PAGE-039 | Downloads | `downloads/`, `downloads/packages/`, reports, identity exports | exists | Mixed-selection package: deferred until the UI defines scope |
| PAGE-045 | Notifications | list/read/all-read/unread-count/preferences | exists (already paginated) | — |
| PAGE-037 | ActivityLogs | `GET /api/user/activity/` | exists | — |
| PAGE-048 | Settings: profile | `user/profile/`, `user/avatar/` | extended `3c62344` | — |
| PAGE-048 | Settings: security | `user/sessions/`, `sessions/{id}/revoke/`, change-password, logout-all, 2FA | added `1a2c1b8` | — |
| PAGE-048 | Settings: notifications | `notifications/preferences/` | exists | — |
| PAGE-048 | Settings: privacy | `privacy/consents/`, `export/`, `facial-data/` | exists | Camera permission stays in the browser |
| PAGE-043 | LiveEnhancement | `camera/sessions/`, snapshots, upload + enhancement | added `4586aa5` | 30-day metadata retention; no video endpoint exists |

## Moderator portal (organization-scoped)

| ID | Page | Existing backend | Status | Gap / package |
|---|---|---|---|---|
| PAGE-031 | ModeratorLogin / access gate | `organizations/` with membership + capabilities | extended `689f90d` | — |
| PAGE-029 | ModeratorDashboard | `moderator/dashboard/`, `moderator/metrics/` | exists | — |
| PAGE-026 | MemberManagement | members, invitations, resend, cancel, update, remove, audit | exists; capabilities `689f90d` | Copy "only invited by you" differs from policy (handoff) |
| PAGE-036 | ReviewQueue | reviews list/claim/release/assign/bulk-decision | exists | — |
| PAGE-030 | ModeratorImageReview | review detail/image/decision/escalate/merge, `moderator/images/{id}/preview/` | extended `18a3f86` | Audited previews (owner/moderator, not personal spaces) |
| PAGE-024 | BatchManagement | `moderator/batches/{id}/`, retry, cancel, `reprocess/` | added `0765596`, `d2beaff` | Organization packages: still deferred |
| PAGE-033 | NotificationCenter | user notifications; assignment/escalation/overdue/batch-failed producers verified | exists | — |
| PAGE-035 | ReportsExports | `moderator/reports/` with date window, `general_activity` | extended `f30722d`, `3809836` | — |
| PAGE-028 | ModeratorAudit | `{org}/audit/` | exists | — |
| PAGE-032 | ModeratorSettings | profile, notification preferences | exists | — |
| PAGE-025/027/034 | IdentityReview, ModerationLogs, RejectedImages (unmounted) | moderation, audit | frontend-only | Consolidate or route (frontend decision) |

## Administrator portal

| ID | Page | Existing backend | Status | Gap / package |
|---|---|---|---|---|
| PAGE-011 | AdminOverview | `system/analytics/`, `system/health/`, `system/queue/`, `system/moderation/` | exists | — |
| PAGE-023 | Users | `user/admin/users/` list, GET detail, PATCH; `user/admin/invitations/` | extended `616b628`, `80ac70f` | Only a superuser may grant admin |
| PAGE-014 | AuditLogs | `system/audit/` | exists | — |
| PAGE-018 | Security | `system/security/alerts/`, acknowledge, rules | added `a747ff3` | Alert rules per product decision; thresholds configurable |
| PAGE-012 | ApiDocs | `/api/docs/`, `/api/schema/`, `/api/redoc/` | exists | Frontend should link them (handoff) |
| PAGE-013 | ApiTest | existing API | frontend-only | Use Swagger |
| PAGE-021 | SystemMonitoring | health, queue, failed jobs | exists | Host telemetry: deferred (needs an operational monitor, not Django) |
| PAGE-022 | UploadStatistics | `system/analytics/` | exists | Check buckets in B06 |
| PAGE-016 | RejectionSummary | analytics, moderation overview | exists | — |
| PAGE-015 | ImageMonitoring | `system/images/`, `{id}/`, `{id}/preview/` | added `0215bc8` | Frontend needs an admin detail route |
| PAGE-017/019/020 | Rejections, Statistics, SystemHealth (unmounted) | as routed equivalents | frontend-only | — |

## Cross-cutting backend items

| Item | Status | Package |
|---|---|---|
| Pagination on 19 list endpoints (audit P2-14) | added `f8f9f4b` | B01, opt-in |
| Per-process rate limits (audit P2-15) | added `33008a3` | B01, `CACHE_URL`, required in production |
| Typed response schemas for aggregates | done `ce9c212` | Dashboard and analytics render through documented serializers, drift-tested |
| EDSR runtime checksum (audit P2-16) | fixed `02ead0c` | B08 |
| Deployment artefacts | prepared, not run | B09: local staging (PostgreSQL, Redis, Django, Celery, Beat); see `docs/deployment/` |

## Lightweight enhancement (branch `feat/lightweight-enhancement`)

| Phase | Scope | Status |
|---|---|---|
| A | `lightweight_v1` policy; no automatic lift of dark photos; control contract (sharpness added), float-LAB renderer, local recipe, configuration endpoint | implemented `053e8b0`, `4eac545`, `5c4ff92`; tests and schema pass |
| B | Bounded authenticated previews, revision-specific atomic outputs, full-resolution save from a stable source, preview cleanup, edit-task hardening (alpha, deletion, duplicate delivery) | implemented; migration `0009` (disposable databases only); see `docs/enhancement/lightweight-pipeline.md` section 5 |
| C | Optional OpenAI advisor (opt-in, separate consent, structured output), per-image jobs, budgets | planned |
