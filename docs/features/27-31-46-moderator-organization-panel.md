# Features 27, 31, and 46: Moderator Organization Panel

## 1. Overview

The FaceCloud moderator panel is an organization-scoped workspace for trusted
customer representatives. A moderator can add and manage organization members,
inspect organization-owned processing data, and review uncertain AI decisions.
The moderator is not a FaceCloud platform administrator and cannot access another
organization's resources or global infrastructure settings.

Current implementation status: **In progress**. T-008 Phase A (organization
core), Phase B (organization-scoping content), T-007 Phase C (the
review-decision core), and identity-membership write-back are all done as of
2026-09-23 — see `organizations/`, `moderation/`, `identities/services.py`'s
`review_membership()`, and the completion checklist in section 15. Two scope
corrections were made during implementation, both documented in their PR/
commit messages rather than silently applied:

- Phase B: rather than an `organization` column on every owner-scoped model,
  only `UploadBatch` and `IdentityGroup` carry one (the two models with no
  single parent to derive it from transitively).
- Phase C initially shipped without identity-membership write-back: the only
  existing write path, `identities.services.review_membership()`, hard-gated
  on `owner=reviewer` and had no way to distinguish "whose content this is"
  from "who is deciding." Resolved the same day rather than left open:
  `review_membership()` now takes the membership's real owner as `reviewer`
  (the authorization gate is unchanged) and a separate `actor` for the
  deciding moderator, so group ownership and the `IdentityChange` audit trail
  stay correctly attributed to the content owner while `reviewed_by` records
  who actually decided. Caught and fixed along the way: the model's
  `identity_group_required_except_review` constraint didn't actually match
  the code's own exemption for `reject` (a real, if previously unreachable,
  inconsistency) -- corrected in the same change (`identities.0006`).

Still open: moderator notifications, org-scoped reports/exports, batch
monitoring/retry/reprocess/cancel, duplicate-group merging, and the frontend
(no frontend repo in this workspace).

## 2. Roles and responsibilities

### FaceCloud administrator

- Controls the complete platform and Django administration.
- Creates, suspends, and manages organizations.
- Approves or revokes organization moderators when required.
- Investigates system-wide security, processing, and audit information.

### Organization owner

- Owns one organization subscription.
- Can invite members and nominate organization moderators.
- Can view organization usage, membership, and review activity.

### Organization moderator

- Can invite and manage normal members in the assigned organization.
- Can view organization-owned uploads, jobs, images, groups, and reports.
- Can review, approve, reject, correct, reprocess, or escalate uncertain results.
- Can remove or deactivate organization members subject to protected-role rules.

### Organization member

- Uploads and manages permitted organization content.
- Can view only resources allowed by organization policy and ownership rules.
- Cannot manage roles, members, or moderation decisions.

## 3. Member-management requirements

The moderator panel must provide:

- Organization member directory with search, filters, status, and pagination.
- Member invitation using a normalized email address.
- Invitation status: pending, accepted, expired, cancelled, or rejected.
- Assignment of the normal member role.
- Resending or cancelling a pending invitation.
- Viewing member role, status, join date, and permitted organization activity.
- Deactivating or reactivating a member within the organization.
- Removing a member from the organization.
- Transferring member-owned organization resources where required.
- Recording every membership and role action in an immutable audit log.

A moderator cannot:

- Invite or create a FaceCloud global administrator.
- Promote themselves or another user to a global role.
- Remove the organization owner or last active owner.
- Manage members of another organization.
- Delete immutable audit records.

## 4. User removal and deletion rules

The moderator interface may label the action **Remove user**, but the normal
operation removes or deactivates the user's membership in the active organization.
It does not immediately delete the global FaceCloud account.

This distinction is required because one account may belong to multiple
organizations, audit history must remain attributable, and organization content
may require reassignment or retention.

Permanent global account deletion is restricted to:

- The account owner through the password-confirmed deletion workflow; or
- A FaceCloud administrator following the privacy and retention process.

When a moderator removes a member, the backend must:

1. Verify both users belong to the same organization.
2. Verify the target is not an owner or protected account.
3. Revoke the target's organization access immediately.
4. Cancel or reassign open review work.
5. Apply the configured resource-retention policy.
6. Record actor, target, reason, IP address, and timestamp.
7. Notify the affected member and organization owner.

## 5. Organization data visibility

Moderators can view organization-scoped operational data needed for their work:

- Members and membership status
- Upload batches and processing jobs
- Original, preprocessed, and enhanced image metadata
- Quality and face-detection results
- Identity groups and duplicate warnings
- Moderator review items and decisions
- Organization reports, notifications, and activity history
- Organization storage and processing usage

Image access must use protected backend endpoints. A moderator never receives raw
storage paths, database credentials, or another organization's records.

### 5.1 Member visibility rule

A moderator can list, search, view, invite, deactivate, reactivate, or remove only
members of the moderator's active organization. The moderator cannot discover or
view unrelated FaceCloud accounts or members of another organization.

If the moderator belongs to multiple organizations, they must select an active
organization. Every member query and management action is then restricted to that
organization. Changing the organization in the frontend does not grant access;
Django revalidates the moderator's active membership from PostgreSQL.

Expected access behavior:

| Requested account | Moderator access |
|---|---|
| Normal member of active organization | View and perform permitted management actions |
| Owner of active organization | View basic membership information; cannot remove or demote |
| Member of another organization | No visibility; return `404` |
| Unrelated FaceCloud user | No visibility; return `404` |
| FaceCloud administrator | No moderator management access |

FaceCloud administrators may view all organizations through administrator-only
interfaces, but this permission is never inherited by organization moderators.

## 6. Architecture and workflow

```text
FaceCloud administrator creates or approves organization
                         |
                         v
              Organization owner/moderator
                         |
             +-----------+-----------+
             |                       |
             v                       v
       Invite/manage members    Review AI decisions
             |                       |
             v                       v
      Membership permission     Approve/reject/correct
             |                       |
             +-----------+-----------+
                         v
                 Immutable audit log
```

Django enforces authorization using the authenticated user, active organization,
membership status, and required organization role. Frontend route protection is a
usability layer, not the security boundary.

## 7. Planned database design

### Organization

- Name, slug, status, and subscription plan
- Storage and upload limits
- Creation and update timestamps

### OrganizationMembership

- Organization and user
- Role: owner, moderator, or member
- Status: invited, active, suspended, or removed
- Inviting user and membership timestamps
- Unique organization/user membership constraint

### OrganizationInvitation

- Organization, normalized email, and requested role
- Cryptographically secure invitation-token hash
- Inviting user, status, expiration, and acceptance timestamps

### ModeratorReview

- Organization, image, and assigned moderator
- Status: pending, assigned, in review, approved, rejected, corrected, or escalated
- AI recommendation and confidence
- Moderator decision, reason, notes, and timestamps

### OrganizationAuditEvent

- Organization, actor, action, and target identifier
- Previous and new structured values
- Reason, IP address, and timestamp

Organization relationships will also be added to uploads, images, enhancement
results, processing jobs, identity groups, reports, and review resources.

**Implementation note (2026-09-22):** rather than a separate `organization`
column on every one of those models, only `UploadBatch` and `IdentityGroup`
carry one -- every other model in that list (images, preprocessing/enhancement
results, quality decisions, duplicate matches, reports) already has a FK back
to one of those two, so its organization is reached transitively through that
existing relationship (e.g. `image.batch.organization`) rather than
duplicated. This avoids ~15 denormalized columns that nothing queries
directly and removes any risk of them drifting out of sync with the anchor
they'd have copied from.

## 8. Planned API endpoints

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| GET | `/api/organizations/` | Authenticated | List caller's organizations |
| GET | `/api/organizations/{id}/` | Organization member | Organization details |
| GET | `/api/organizations/{id}/members/` | Owner or moderator | List members |
| POST | `/api/organizations/{id}/invitations/` | Owner or moderator | Invite member |
| POST | `/api/organizations/{id}/invitations/{id}/resend/` | Owner or moderator | Resend invitation |
| DELETE | `/api/organizations/{id}/invitations/{id}/` | Owner or moderator | Cancel invitation |
| PATCH | `/api/organizations/{id}/members/{id}/` | Permitted manager | Update member status |
| DELETE | `/api/organizations/{id}/members/{id}/` | Permitted manager | Remove membership |
| GET | `/api/moderator/reviews/` | Organization moderator | List reviews |
| GET | `/api/moderator/reviews/{id}/` | Organization moderator | Review details |
| POST | `/api/moderator/reviews/{id}/decision/` | Assigned moderator | Submit decision |
| POST | `/api/moderator/reviews/{id}/escalate/` | Assigned moderator | Escalate case |
| GET | `/api/organizations/{id}/audit/` | Owner or moderator | Audit history |

The final contract will be frozen during implementation and published through
OpenAPI documentation.

## 9. Example requests

Member invitation:

```json
{
  "email": "member@example.com",
  "role": "member"
}
```

Member removal:

```json
{
  "reason": "Member left the organization",
  "resource_action": "retain_with_organization"
}
```

Successful removal returns a membership with `status: "removed"`; it does not
claim that the global account was permanently erased.

## 10. Validation and security

- JWT authentication is required for every operation.
- Organization membership is revalidated from PostgreSQL.
- Querysets are filtered by organization before object lookup.
- Inaccessible cross-organization identifiers return `404`.
- Invitation tokens are random, expiring, single-use, and stored as hashes.
- Email addresses are normalized and checked case-insensitively.
- Invitation and member-management operations are rate limited.
- Sensitive membership changes revoke or refresh relevant access tokens.
- Membership changes, reviews, and protected-data access are audited.
- Concurrent changes use database transactions and row locking.
- The last organization owner cannot be removed or demoted.
- A moderator cannot modify an owner, administrator, or equal/higher role.

## 11. Error handling

| Status | Meaning |
|---:|---|
| 400 | Invalid email, role, reason, status, or ownership policy |
| 401 | Missing or invalid authentication |
| 403 | Insufficient organization permission |
| 404 | Organization resource is unavailable or inaccessible |
| 409 | Existing invitation, membership, protected owner, or concurrent conflict |
| 429 | Invitation or management rate limit exceeded |

## 12. Frontend moderator panel

Recommended routes:

```text
/moderator
/moderator/members
/moderator/invitations
/moderator/queue
/moderator/reviews/:id
/moderator/history
/moderator/settings
```

The member directory displays name, email, role, status, invitation/join date,
recent permitted activity, and open review assignments. It provides invite,
resend, deactivate, reactivate, and remove controls.

Destructive actions require a confirmation dialog and mandatory reason. The remove
dialog clearly states that organization access is removed while the global account
and audit history may remain.

### 12.1 Moderator panel visual layout

The frontend moderator panel should follow this organization-scoped layout:

```text
+--------------------------------------------------------------------------------+
| FaceCloud Moderator                         Organization: ABC Photography   [v] |
+----------------------+---------------------------------------------------------+
| Overview             | Pending Reviews | Active Members | Failed Batches       |
| Members              |       24        |       18       |        2             |
| Invitations          +---------------------------------------------------------+
| Review Queue         | Urgent Reviews          Recent Activity                 |
| Duplicate Review     | - Low-confidence face   - Member invited                |
| Batch Management     | - Duplicate identity    - Batch completed               |
| Notifications        | - Failed enhancement    - Review approved               |
| Reports & Exports    +---------------------------------------------------------+
| Audit History        | Assigned Reviews                                        |
| Settings             | [Image] User | Issue | Priority | Waiting | Action      |
|                      | [Image] User | Issue | Priority | Waiting | Action      |
+----------------------+---------------------------------------------------------+
```

The sidebar contains only organization moderator routes. It must not display or
link to FaceCloud administrator pages.

### 12.2 Main panel navigation

| Navigation item | Purpose |
|---|---|
| Overview | Organization review, member, batch, storage, and activity summary |
| Members | Search, invite, deactivate, reactivate, and remove members |
| Invitations | View, resend, or cancel pending invitations |
| Review Queue | Claim, assign, filter, and complete uncertain AI reviews |
| Duplicate Review | Confirm or dismiss duplicate images, faces, and groups |
| Batch Management | Monitor, retry, reprocess, cancel, and report on batches |
| Notifications | Assignment, deadline, batch, duplicate, and escalation alerts |
| Reports & Exports | Generate protected CSV, PDF, and JSON organization reports |
| Audit History | View immutable moderator and membership activity |
| Settings | Manage permitted notification and organization preferences |

### 12.3 Responsive behavior

- Desktop displays the sidebar and dashboard cards in a multi-column layout.
- Tablet collapses secondary cards while keeping review actions visible.
- Mobile uses a drawer menu and single-column cards.
- Tables become cards or horizontally scrollable views on small screens.
- Approve, reject, remove-member, and bulk actions remain clearly separated.
- Destructive buttons use confirmation dialogs and cannot rely only on color.
- Loading, empty, permission-denied, partial-failure, and retry states are required.

The current authoritative layout reference is the
[moderator-panel wireframe](../images/moderator-panel-wireframe.svg). When the
frontend implementation is available, an approved screenshot may be added beside
the wireframe for implementation comparison.

## 13. Selected moderator management modules

The first moderator-panel scope includes the following approved management
modules.

### 13.1 Dashboard overview

The organization moderator dashboard displays:

- Pending, assigned, urgent, and overdue reviews
- Reviews completed today
- Accepted, rejected, corrected, and escalated totals
- Active, invited, suspended, and removed member totals
- Active and failed batch-processing jobs
- Organization storage and processing usage
- Recent notifications and organization activity

All cards and charts are limited to the active organization. They must use real
backend data rather than frontend-only counters.

### 13.2 Member management

Moderators can:

- Invite normal members by email
- Resend or cancel invitations
- Search and filter the member directory
- View member role, status, join date, and permitted activity
- Deactivate or reactivate organization access
- Remove a normal member from the organization
- Reassign open reviews and apply the resource-retention policy during removal

Moderators cannot create administrators, promote themselves, remove owners, or
manage another organization's members.

### 13.3 Duplicate management

Moderators can:

- View suspected duplicate images and faces
- Compare suspected duplicates side by side
- View checksum, visual-similarity, and face-similarity evidence
- Confirm or dismiss a duplicate warning
- Select the preferred or highest-quality copy
- Merge duplicate identity groups
- Record the decision and reason in immutable audit history

Duplicate actions affect only the active organization. Automatic permanent file
deletion is not performed without the configured retention and recovery policy.

### 13.4 Batch management

Moderators can:

- List and filter organization upload and processing batches
- View total, processed, accepted, rejected, review, and failed counts
- Monitor the current stage and progress percentage
- Inspect per-image failures and safe error messages
- Retry permitted failures or request batch reprocessing
- Select another permitted enhancement level for reprocessing
- Cancel queued or safely cancellable work
- Download authorized results and generate a batch report

Moderators cannot change global Celery, Redis, model, timeout, or worker settings.

### 13.5 Moderator notifications

Moderators receive organization-scoped notifications when:

- A review is assigned or transferred
- An urgent or overdue review requires attention
- A user requests manual review
- A batch completes, partially completes, or fails
- A duplicate image or identity group is suspected
- Another moderator mentions them in an internal note
- A member accepts an invitation or is removed
- A FaceCloud administrator responds to an escalation

Notification preferences may control email and in-app delivery, but mandatory
security and membership notifications cannot be disabled.

### 13.6 Reports and exports

Authorized moderators can generate organization-scoped:

- Review-summary reports
- Rejection-reason reports
- Quality-score reports
- Batch-processing reports
- Duplicate-decision reports
- Identity-correction reports
- Moderator activity reports
- CSV, PDF, and JSON exports

Every generated report and download is permission checked, time limited where
appropriate, and recorded in the organization audit trail. Reports cannot include
another organization's users, images, metrics, or facial data.

## 14. Automated test plan

- Owner and moderator can list members only in their organization.
- Normal members cannot manage membership.
- Moderator can invite a normal member but cannot assign global privileges.
- Moderator cannot access or modify another organization.
- Duplicate and expired invitations are handled safely.
- Owner and protected accounts cannot be removed by a moderator.
- Removed members immediately lose organization access.
- Resource-retention policy is applied transactionally.
- Every membership and review action creates an audit event.
- Concurrent role/removal requests cannot bypass constraints.

## 15. Completion checklist

- [x] Requirements and frontend contract documented
- [x] Existing global moderator permission foundation available
- [x] Organization models and migrations implemented (2026-09-22)
- [x] Existing users backfilled into personal organizations (2026-09-22); existing
      *content* rows are not yet organization-scoped -- that is Phase B
- [x] Organization-scoped permissions implemented (2026-09-22, `OrganizationScopedMixin`)
- [x] Member invitation and management APIs implemented (2026-09-22)
- [x] Moderator review workflow implemented (2026-09-23, core: list/detail/
      decision/escalate for quality, duplicate, and identity reviews, all
      with write-back to the underlying record on decision)
- [x] Organization dashboard metrics implemented (2026-09-23, review-queue
      counts only; batch/storage/member metrics not yet included)
- [ ] Duplicate and batch management implemented (duplicate *review* is done;
      duplicate-group merging and batch monitoring/retry/reprocess/cancel are not)
- [ ] Moderator notifications implemented
- [ ] Reports and exports implemented
- [x] Immutable audit events implemented (2026-09-22/23, membership +
      moderator-review actions)
- [x] Automated security tests passed (2026-09-23, `organizations/tests.py`
      32 tests + `moderation/` 23 tests, incl. cross-org isolation)
- [ ] Manual cross-organization verification completed
- [ ] OpenAPI documentation published
