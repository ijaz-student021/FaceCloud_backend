# Organization leave request (member asks, moderator decides)

A member never suspends or removes themselves. They **ask to leave one organization**; an active **moderator** of that organization approves (the membership is
**suspended**) or denies. Nothing is deleted: images, folders, upload history, reviews, reports and audit stay stored under the organization's policy.

## What approval does (and does not)

* Suspends **that one membership** (`OrganizationMembership.status = suspended`). The FaceCloud **account** stays active; the personal workspace, personal images,
  albums, person folders and every other organization membership are untouched.
* From that instant every organization-scoped URL answers 404 for that person (the live membership row is checked on each request; an existing JWT changes nothing),
  the organization disappears from `GET /api/organizations/` (only active memberships are listed), and organization-filed content is no longer reachable through the
  owner-scoped endpoints either (see `docs/features/suspended-member-content-lock.md` once the lock PR is merged).
* The person's organization **download requests** that were pending are cancelled and their live **download grants** are revoked. Files already downloaded cannot be recalled.
* Seats: a pending request still holds its seat; an approval releases it (only active memberships count). Reactivating later (`PATCH` the member) needs a free seat; nobody is reactivated automatically.

## States

`pending` -> `approved` | `denied` | `cancelled`. Only `pending` moves. Terminal states are history. One pending request per membership (database constraint).

## Member endpoints (a member of the organization)

| Method | URL | Body | Answer |
|---|---|---|---|
| GET | `/api/organizations/{id}/leave-request/` | | `200 {"request": LeaveRequest \| null}`: the pending one, else the latest, else `null` |
| POST | `/api/organizations/{id}/leave-request/` | `{"reason": "optional, <= 500 chars"}` | `201 LeaveRequest` |
| POST | `/api/organizations/{id}/leave-request/cancel/` | none | `200 LeaveRequest` (status `cancelled`) |

Errors: `403 leave_not_available` (owner or moderator; personal workspace gives `403 personal_workspace_is_private`), `409 leave_request_pending`, `409 no_eligible_reviewer`
(nobody could decide it: ask the owner or the administrator), `409 leave_request_not_pending` (cancelling when the latest request was already decided, e.g. a moderator approved it a moment before), `404` (cancelling when you never asked), `404` (not an active member).

## Moderator endpoints (active moderator; the owner may read)

| Method | URL | Body | Answer |
|---|---|---|---|
| GET | `/api/organizations/{id}/moderator/leave-requests/?status=pending` | | list of `LeaveRequest` (`status` = `pending` (default), `approved`, `denied`, `cancelled`, comma separated, or `all`). A moderator in an `own_members` organization sees only the members they may see. |
| POST | `/api/organizations/{id}/moderator/leave-requests/{leave_id}/decision/` | `{"action": "approve"\|"deny", "reason": "..."}` | `200 LeaveRequest` |

`reason` is required (3+ chars) for `deny`, optional for `approve`. Errors: `403 moderator_required` (owner or member), `404` (another organization's request or a member the
moderator may not see), `409 leave_request_not_pending` (already decided or cancelled: the loser of a race), `400` validation.

## Administrator emergency override

`POST /api/organizations/admin/{id}/leave-requests/{leave_id}/decision/` with `{"action", "reason"}`; `reason` of **8+ characters** is required, the decision is marked
`admin_override: true` and audited. This is the recovery path when an organization has no moderator who can decide (a request cannot even be created then: `409 no_eligible_reviewer`).
An owner can always suspend or remove a member through the members API.

## LeaveRequest

```json
{"id": 7, "organization_id": 3, "organization_name": "Event Planners",
 "member": {"id": 21, "username": "sam", "full_name": "Sam Rao", "email": "sam@example.com"},
 "status": "pending", "reason": "Changing teams", "submitted_at": "...", "reviewed_by": null,
 "decided_at": null, "decision_reason": "", "admin_override": false, "membership_status": "active"}
```

## Notifications and audit

In-app (and e-mail where the person's settings allow): moderators who may see the member get `leave_request_submitted` (action_path `/moderator/leave-requests`); the member gets
`leave_request_received`, then `leave_request_approved` / `leave_request_denied` / `leave_request_cancelled` (action_path `/dashboard/organization`).
Audit events (organization audit trail): `leave_requested`, `leave_request_approved`, `leave_request_denied`, `leave_request_cancelled`, and for an approval also `member_status_changed` (the suspension).

## Migrations (additive)

`organizations/0016_leave_request` (new table `OrganizationLeaveRequest` + audit action choices), `notifications/0012_leave_request` (event types).
