# Plan seat limits (total users)

An organization's plan caps how many **people** it may have. The cap is the plan's headline number and is counted in total.

| Plan | Total users (default) | Moderators (own cap) | Members (own cap) |
|---|---|---|---|
| Free | 5 | 1 | 10 |
| Team | 25 | 5 | 100 |
| Enterprise | 100 | unlimited | unlimited |

Defaults live in `config/settings.py` (`ORGANIZATION_PLAN_LIMITS`) and can be overridden per plan with
`ORG_PLAN_<PLAN>_USERS`, `ORG_PLAN_<PLAN>_MODERATORS` and `ORG_PLAN_<PLAN>_MEMBERS` (a whole number or `unlimited`). A limit that is `null`
in an API answer means unlimited.

> Before this change the plans only capped moderators and members separately (Free 1 / 10, Team 5 / 100, Enterprise unlimited, the owner free). Those per-role
> caps are kept; the total is the stricter new rule. The total of 5 / 25 / 100 is a provisional product default: change the three settings if the business
> rule differs.

## What counts as a seat

Counted once each: the active **owner**, active **moderators**, active **members**, and every **waiting (not expired) invitation**, including a pending owner
invitation and an administrator's moderator-account invitation. A waiting invitation holds its seat until it is accepted, cancelled or it expires; when it is
accepted the person becomes a membership and is still one seat, never two.

Not counted: a suspended, removed or inactive membership; cancelled or expired invitations; the person's private **personal workspace** (it is a different
organization); a FaceCloud administrator's platform access.

## Enforcement (the backend is authoritative)

`organizations.services.assert_seat_available` checks the total first, then the role's own cap, inside a transaction that locks the organization row
(PostgreSQL), so simultaneous invitations cannot both take the last seat. It runs on every path that adds a person: owner invites a moderator, a moderator
invites a member, an administrator creates a moderator account or an account invitation, invitation acceptance (re-checked), reactivating a membership and
role changes. The owner's own seat is never refused. An organization that is already above its limit (older data) keeps everyone and cannot add more until it
is under.

Refusal: `403 {"detail": "Your plan allows 5 users.", "code": "plan_limit_reached", "kind": "users", "limit": 5}` (`kind` is `users`, `moderators` or `members`).

## Changing a plan

An administrator can upgrade at any time (room opens at once). A plan **cannot be lowered below what the organization already uses** (people plus waiting
invitations); nobody is removed to make room:

`400 {"detail": "...", "code": "plan_downgrade_over_capacity", "plan": ["..."], "kind": "users", "limit": 5, "in_use": 8}`

Remove people or cancel invitations first, then lower the plan.

## What the screens read

Organization answers (administrator list and detail, owner and moderator view) carry `limits = {users, moderators, members}` and
`usage = {users, moderators, members}` (the seats taken, invitations included). Show "used / max users" from `usage.users` and `limits.users`; if the fields
are absent (an older backend) hide the line. A member does not receive `limits` or `usage`.
