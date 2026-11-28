# Moderator onboarding: how a person becomes a working moderator, and why they might not be able to sign in

Three supported ways, all reusing the existing models (no second invitation system):

| Way | Who starts it | What the person does | Password |
|---|---|---|---|
| **Account created by an administrator** (`POST /api/user/admin/users/`, role `moderator`, `organization_id`, `reason` of 8+ characters) | Platform administrator | Opens the e-mailed setup link (`{FRONTEND_URL}/reset-password/<uid>/<token>`), chooses a password, signs in | Optional: without one the account has an **unusable password** until the link is used; with one the administrator hands it over (never e-mailed, never returned) |
| **Administrator account invitation** (`/api/user/admin/invitations/`) | Platform administrator | Opens the invitation link, registers (new person: chooses own password, e-mail counts as verified) or signs in (existing) and accepts | Always the person's own |
| **Organization invitation** (owner invites a moderator, `role: moderator`) | Organization owner | Same as above: registers from the link or signs in with the existing account and accepts | Existing account: unchanged; new: own |

An existing account keeps its password, its personal workspace and its other memberships; only the explicitly accepted organization membership is added.

## Why "Active" did not mean "can sign in" (root cause found 2026-11-28)

* The Admin list's *Active* badge only meant `is_active` (and a verified address). It said nothing about the password or the moderator membership.
* An account created **without a password** has an unusable password. Signing in answers the generic **"Invalid username/email or password."** (the same answer as an unknown address or a wrong password, on purpose: no account enumeration). The only way in is the e-mailed setup link.
* That e-mail was fire-and-forget: if the mail server was unreachable (or the backend used the console e-mail backend) nothing told the administrator.
* The same generic answer also appears when the account exists on **another server** (for example created on the live site while signing in against a local backend): the account is simply not in that database. Check which API the page talks to before anything else.

## What the administrator now sees (`GET /api/user/admin/users/`)

`password_setup_pending`, `login_ready`, `login_blockers` (`inactive`, `suspended`, `email_unverified`, `password_setup_pending`, `no_active_moderator_membership`), `moderator_memberships` (`organization_id`, `organization_name`, `status`, `organization_status`) and `setup_email` (`status` `sent`/`failed`/`none`, `at`, `kind`). `sent` means the mail server accepted the message, **not** that it reached an inbox. No response ever carries a link or token.

**Resend setup link:** `POST /api/user/admin/users/{id}/password-reset/` answers `200 {"email_status": "sent"}` or `502 {"code": "email_failed"}` when the mail server refuses.

## Where the e-mail goes

* `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` (the development default): the message, including its link, is printed in the **backend terminal**; nothing reaches a Gmail inbox.
* `locmem` (tests): captured in `django.core.mail.outbox`.
* SMTP: set `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`; `FRONTEND_URL` must be the public frontend (`https://www.facecloudai.com` in production) because every link is built from it. Check with the audit script, then a test to one named mailbox with the owner's approval.

## Not a bug: seats

A full plan stops new people being added; it never stops an already-active moderator from signing in (tested).

## Credential hygiene

If a password was ever shown in a screenshot or message, treat it as exposed: change it through *Forgot password* (or the administrator's resend) and never reuse it.

## Organization invitations (moderator / member): e-mail outcome

`POST /api/organizations/{id}/invitations/` and the resend call no longer answer 500 when the mail server refuses: the invitation is created (it holds its seat) and the answer carries `email_status` (`sent` | `failed`) and `email_failure_reason` (error kind and the start of its message; never the link or token). Show "E-mail not sent: resend" for `failed`, then use the existing resend/cancel actions. `sent` means the mail server accepted the message.
