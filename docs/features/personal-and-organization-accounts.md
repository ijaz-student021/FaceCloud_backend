# Personal accounts and organizations stay apart

The rules, what the code did before this change, what was fixed, and what still needs a decision. Tests: `organizations/test_account_isolation.py` (35).

## The rules and how they are met

| Rule | How it is met |
|---|---|
| Anyone can register a personal account; a public signup never joins an organization | `POST /api/auth/register/` + verification creates the account and **only its own private workspace** (an organization with `is_personal`, which only that person belongs to). Signing up with the address of a pending owner, moderator or member invitation does **not** join anything (tested). |
| Only the Platform Admin creates an organization; the owner needs no account | `POST /api/organizations/admin/` with `owner_email` (see `admin-owner-invitation.md`). |
| Membership comes only from an invitation that is accepted, never from an e-mail match | Every code path that creates a membership is listed in a test (`test_a_new_code_path_that_creates_memberships_has_to_be_reviewed`): the token-based acceptance, the owner when an organization is created, and the administrator's own explicit actions (creating or promoting a moderator). A match of the address alone changes nothing (tested). |
| Owner invites moderators; moderators invite members; nobody invites an owner | Tested end to end (`test_the_chain_of_invitations_owner_moderator_member`): a moderator inviting a moderator is 403, a member inviting is 403, `role: owner` on the member invitation route is 400. |
| Personal images are separate; organization images belong to an explicitly chosen organization | `UploadBatch.organization` says where a photograph is filed. **It is filed under an organization only when the person chose it** (`organization_id`, an active membership of an active organization). Without a choice it goes to the person's private workspace; see "The default workspace" below. |
| Nothing is moved into an organization automatically | Accepting an invitation, being removed from an organization, or getting a workspace created never changes where an existing photograph is filed (tested). |
| A person in both workspaces never exposes personal images to organization staff | Staff endpoints read only `batch.organization == their organization` (never the uploader's memberships): the image list, previews, batch pages, the organization export and the person-folder review show shared photographs only (tested for owner and moderator). |
| Personal users keep their personal downloads; organization users follow the organization's policy | Personal downloads are unchanged (tested); the approval policy belongs to the download-approval PR. |
| Isolation is enforced in backend permissions and queries; no separate physical databases | Everything above is checked by the backend on every request; one PostgreSQL database, as before. |

## What the audit found and what changed

1. **Fixed: an organization could be inferred from a membership.** `get_default_organization` used to fall back to *the oldest organization the person belongs to* when they had no personal workspace. An account made by an administrator (a moderator, for example) had no personal workspace, so **its owner's own uploads were filed under the organization they work for, visible to its staff**; an existing test even asserted that. Now the default is the person's **own personal workspace and nothing else**; without one it is "no organization" (`null`), exactly like a private photograph.
2. **Fixed: administrator-created accounts get their personal workspace** with the account, like a signed-up one. `python manage.py ensure_personal_workspaces` (dry run; `--apply` to create) gives one to every existing account that lacks it; it moves nothing and is safe to repeat. **Not run anywhere.**
3. **Fixed: an administrator could type, and so know, a password.** `password` is now optional when an administrator creates an account: without it the account has no password and the person is e-mailed a link to choose their own. A given password is still accepted, hashed, and never e-mailed or returned. New `POST /api/user/admin/users/{id}/password-reset/` e-mails the person a reset link (never a password or token in the response; audited as `admin_password_reset_sent`; an administrator account needs a superuser; suspended or inactive accounts are refused).
4. **Fixed: personal users and organizations were not separable for the administrator.** `GET /api/user/admin/users/` has `account_type` (`personal` / `organization`) and `shared_organization_count` per row and the filter `?account_type=`; the account detail marks each organization `is_personal` and adds `usage.storage_bytes` (the size of the person's original uploads, trash included). `GET /api/organizations/admin/` takes `?kind=organization|personal|all` (default `all`, unchanged): the Organizations screen should ask for `organization`, because every account's private workspace is also an organization row.
5. **Fixed: a person folder leaked a count.** The moderator folder list showed `member_count`, the folder's total, which includes the member's private photographs; it now shows only the photographs submitted to that organization (the cover photographs and `photos_in_organization` were already limited to the organization).

Existing tests that asserted the old behaviour were updated on purpose: the fallback to a membership, an administrator-created moderator's own uploads being filed under the organization, and an administrator-created account having no membership at all.

## The default workspace

`get_default_organization(user)` -> the person's active personal workspace, else `None`. `resolve_upload_organization(user, organization_id)` -> that default when no `organization_id` is sent; otherwise the person must be an **active member of an active organization** (a suspended one is 403, one that waits for its owner or that they do not belong to is 400). The frontend already sends `organization_id` whenever the person picks an organization as the upload target.

## Still needs a decision (not changed here)

* **"Personal images are distinguished by a null organization association."** They are not, today: every registered account owns a *personal workspace* (an organization row with `is_personal = true`, one member: them) and its photographs are filed under it; `null` appears only for accounts without one. The code treats the two the same everywhere (`is_personal` or `null` = no moderator, never shown to staff, never subject to approval), and the isolation above holds for both. Making personal photographs literally `null` would mean a data migration of every existing batch, removing the personal workspace of every account and changing the download-approval code that relies on `is_personal`. Recommendation: keep it. Please decide.
* **"Personal registration must never create an OrganizationMembership."** Signup creates one membership: the person as owner of their *own private workspace*; never in a shared organization (tested). Same decision as above.
* **Moderators created directly by the Platform Admin.** An administrator can create a moderator account inside an organization, send an account invitation for one, or promote an account (all audited, all existing features). They bypass "the owner invites moderators". Kept as an explicit platform-administrator power; say if you want them restricted to the owner's invitation.
* **Registering through an invitation link accepts the invitation in the same step.** `POST /api/auth/register/` with `invitation_token` creates the verified account (the link proves the mailbox) and accepts the invitation; the token, the matching address and submitting the form are the explicit act. If you want a separate "Accept" confirmation after signup, it is a frontend step plus a small backend change.

## API summary

| Call | Change |
|---|---|
| `POST /api/user/admin/users/` | `password` optional |
| `POST /api/user/admin/users/{id}/password-reset/` | new: sends the person a reset link |
| `GET /api/user/admin/users/` | `?account_type=personal\|organization`; rows have `account_type`, `shared_organization_count` |
| `GET /api/user/admin/users/{id}/` | `organizations[].is_personal`, `usage.storage_bytes` |
| `GET /api/organizations/admin/` | `?kind=organization\|personal\|all` |
| upload without `organization_id` | personal workspace (or `null`), never a membership's organization |

No migration. Not deployed.
