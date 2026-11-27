# Creating an organization for an owner who has no account yet

The Platform Administrator creates an organization by entering **the organization name, the owner's e-mail address and the plan**. The owner does not need an account. They get an invitation e-mail; only when they accept it, as the invited address, do they become the
owner and the organization become active. Status: backend implemented and tested on SQLite and PostgreSQL; no frontend yet; not deployed.

## The flow

```
Admin: POST /api/organizations/admin/ {name, owner_email, plan}
  -> Organization(status "pending_owner", nobody belongs to it) + OrganizationInvitation(role "owner", hashed token, 7 days)
  -> e-mail queued, sent after the transaction commits (email_status queued -> sent | failed)
Owner opens  {FRONTEND_URL}/invitations/{token}
  -> GET /api/organizations/invitations/{token}/              (public preview: organization, e-mail, role "owner", expiry)
  -> no account:       POST /api/auth/register/ {full_name, email, password, password_confirm, invitation_token}   -> 201 + login tokens
     account exists:   sign in, then POST /api/organizations/invitations/accept/ {token}                           -> 200 membership
  -> ONE transaction: owner membership (active) + organization status "active" + invitation "accepted"
```

**Existing code reused, nothing duplicated:** `OrganizationInvitation` (hashed single-use token, expiry, one pending invitation per organization and address), `preview_invitation`, `accept_invitation`,
`register_from_invitation` (the account is created verified, because the link went to that mailbox, and the invitation accepted, in one transaction), the public registration endpoint's `invitation_token`, the organization audit trail, plan seats (the owner takes none),
`IsAdminRole`, the `org_invite` throttle and `FRONTEND_URL`. The legacy `owner_id` request is unchanged (that existing account becomes the owner at once, no invitation).

## What "waiting for the owner" means

`Organization.status = "pending_owner"`: nobody is a member (not even the administrator; no dummy user is created), so every organization route answers 404 to everybody, no member or moderator can be invited, nothing can be uploaded into it,
`assert_organization_active` refuses it (`403 organization_pending_owner`), it is never anybody's default workspace, and the administrator cannot activate it by hand (`PATCH status: active` is `400`; suspending and then reactivating a waiting organization puts it
back to `pending_owner`, never to `active` without an owner). The invited person has no powers until they accept.

## Who can accept

Only an account that **holds the invited address (case-insensitive)**, is active, not suspended and has a **verified** e-mail. Someone who merely knows the link is refused: `403 Sign in with the invited email address to accept this invitation.`
(Unverified and suspended accounts cannot sign in at all, and the service refuses them too.) A token is good once: it is unknown, used, cancelled, expired, or for a suspended organization -> the preview and registration answer the same `404`, acceptance `409`/`403`.

## API (real responses)

Base `/api/organizations/`; the three admin calls need the Platform Administrator.

**Create** `POST admin/` body `{"name": "Event Planners", "owner_email": "newowner@example.com", "plan": "team"}` (`plan` optional, default `free`; send `owner_email` **or** `owner_id`, not both) -> **201**:
```
{"id": 1, "name": "Event Planners", "slug": "event-planners", "status": "pending_owner", "plan": "team", "owner": null,
 "owner_invitation": {"id": 1, "email": "newowner@example.com", "status": "pending", "expires_at": "2026-12-04T13:32:14Z", "created_at": "...",
                      "accepted_at": null, "email_status": "sent", "email_sent_at": "...", "email_failure_reason": "", "can_resend": true, "accepted_by": null},
 "member_count": 0, "limits": {...}, "usage": {...}, "created": true, "owner_account_exists": false}
```
`owner_account_exists` says whether an account with that address exists (it will be asked to sign in) or not (it will register); `null` for an `owner_id` request. `email_status` is `queued` (about to be sent), `sent` (the mail server accepted it) or `failed`
(see `email_failure_reason`); in normal operation the answer already says `sent`. **Delivery to the inbox is not known** (SMTP gives no such feedback). Sending the **same request again** (same name and e-mail while it still waits) returns **200** with `created: false`, the same organization, and sends no second e-mail.
Errors: `400` (neither or both of `owner_email`/`owner_id`, bad e-mail, empty name), `403` (not the administrator), `401`, `429`.

**Status** `GET admin/{id}/owner-invitation/` -> 200 `{id, email, status, expires_at, created_at, accepted_at, email_status, email_sent_at, email_failure_reason, can_resend, accepted_by}`.
`status`: `pending`, `accepted`, `cancelled`, `expired` (pending whose time ran out), `rejected`. `accepted_by`: `{"id": 2, "username": "newowner"}` once accepted, else `null`. `can_resend`: true while the organization still waits for its owner.
`404` for an organization without an owner invitation (one made with `owner_id`). The administrator list/detail (`GET admin/`, `GET admin/{id}/`) now also carry `owner_invitation` (the same object while `status` is `pending_owner`, otherwise `null`); `owner` is `null` until acceptance.

**Resend** `POST admin/{id}/owner-invitation/resend/` (no body) -> 200 (the same object, `status: pending`, new `expires_at`, `email_status` queued then sent). **A new link is sent and the old one stops working.** Also reopens an expired or cancelled invitation. `409` if the owner already accepted, `403` if the organization is suspended.
**Cancel** `POST admin/{id}/owner-invitation/cancel/` -> 200 (`status: cancelled`); the organization keeps waiting and a resend can invite again. `409` unless pending.

**Public preview** `GET invitations/{token}/` -> `{"organization_name": "Event Planners", "email": "newowner@example.com", "role": "owner", "expires_at": "..."}` (`Cache-Control: no-store`); bad, used, cancelled or expired token: `404 {"detail": "This invitation is invalid, used or expired."}`.

**New user** `POST /api/auth/register/` body `{"full_name", "email", "password", "password_confirm", "invitation_token", "username"?}` -> **201** with the usual login answer; its `organization_roles` is `[{"organization_id": 1, "name": "Event Planners", "role": "owner", "status": "active"}]`. The e-mail must be the invited one (else `404`).
**Existing user** `POST invitations/accept/` `{"token"}` (signed in) -> **200** `{"id", "organization", "user", "role": "owner", "status": "active", ...}`; wrong account `403`, used/expired/cancelled `409`.

## The e-mail

Subject "You have been invited to own {organization} on FaceCloud"; the body names the organization, the invited address, the expiry (`2026-12-04 13:32 UTC`, about N hours) and the link `{FRONTEND_URL}/invitations/{token}`, and says FaceCloud never sends a password. **No password or credential of any kind is sent.**
It is sent in this process right after the database commit (`transaction.on_commit`), not through the task queue: the link holds the raw token, which is stored nowhere (only its SHA-256 is), and a queue would hold it. A delivery failure rolls nothing back: the
organization and the invitation stay, `email_status` becomes `failed` with a short reason (the token removed if it was in the message), and the administrator **resends** (a new link). The token and link are never logged, stored, audited or returned by the API (tested).
Lifetime: `ORG_OWNER_INVITATION_TTL_HOURS` (168). In development the console e-mail backend prints e-mails, as it does for every e-mail; use real SMTP only in production.

## Audit trail

`organization_created` (with `pending_owner_email`), `owner_invited`, `invitation_resent`, `invitation_cancelled`, `invitation_accepted` and `organization_activated` (actor: the accepting user). Visible to the owner afterwards in the organization's audit log.

## Migration (additive, `organizations/0014_pending_owner_invitation`)

Three nullable/blank columns on `OrganizationInvitation` (`email_status`, `email_sent_at`, `email_failure_reason`) and wider choice lists (organization status `pending_owner`, invitation role `owner`, two audit actions: no SQL). Existing organizations, invitations and
memberships are untouched; existing invitations have an empty `email_status`. **PR #175 also adds `organizations/0014`:** whichever merges second needs a merge migration (`makemigrations --merge`).

## A bug found and fixed on the way (it affected existing flows)

`accept_invitation` used `select_for_update()` without a transaction. On **PostgreSQL** (production) that raised `TransactionManagementError`, so **an existing user accepting any organization invitation (member, moderator or owner) answered 500**; SQLite and `TestCase` hid it. `accept_invitation` now runs in its own transaction
(regression test `test_an_existing_user_can_accept_an_organization_invitation_outside_a_test_transaction`, reproduced first on PostgreSQL). The registration path (`register_from_invitation`) was already atomic.

## Frontend: what to build

New Admin screen "Create organization": **Organization Name**, **Owner Email**, **Subscription Plan**, button **Create Organization & Send Invitation** (`POST admin/` with `owner_email`; remove the mandatory existing-user search only once this works end to end). After creating: show the organization as "Waiting for owner" with the
invitation status (`owner_invitation`: pending/expired/accepted, `email_status`, expiry, `email_failure_reason`), a **Resend invitation** button (`can_resend`), **Cancel invitation**, and "Owner accepted by {accepted_by.username}" once accepted. Treat `created: false` as "already created" (not an error). Show `owner_account_exists` as a hint ("will be asked to sign in" / "will register").
Organization status `pending_owner` needs a label ("Waiting for owner") in the admin list and detail; do not offer "Activate" for it. The invitation page `/invitations/{token}` already exists: it must show role "owner" and either the registration form (no account: `POST /api/auth/register/` with `invitation_token`) or the sign-in-then-accept path, and then send the new owner to the owner/organization area (`organization_roles`).

## Known limits

* Delivery to the inbox is not observable; `sent` means the mail server accepted it.
* The owner e-mail cannot be changed on an existing invitation: cancel it and create the organization again (a different address is a different organization), or resend to the same address.
* One owner per waiting organization is invited; nothing here adds a second owner (the existing member routes cannot invite an owner).
* An invitation expires after 7 days (configurable); an expired one is reopened by a resend.
