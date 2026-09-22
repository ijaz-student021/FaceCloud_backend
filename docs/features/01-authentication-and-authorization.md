# Feature 01: Authentication, Account Management, and Authorization

## 1. Overview

This feature establishes the security foundation for FaceCloud. It identifies users through verified accounts and JWT tokens, controls access through trusted database roles, and provides account lifecycle operations such as password reset, suspension, logout, and deletion.

The backend supports three roles: user, moderator, and administrator. A public registration always creates a normal user. Privileged roles can only be assigned by an authorized administrator.

## 2. Implemented requirements

- Registration with username, required email, and password
- Case-insensitive username and email uniqueness validation
- Django password validators and secure password hashing
- Six-digit signup verification with pending registrations
- Verification-code expiry, attempt locking, resend cooldown, and hourly limits
- Login using username, email, or a common identifier field
- JWT access and refresh tokens
- Refresh-token rotation and blacklisting
- Token revocation after password, email, suspension, or role changes
- Profile retrieval and editing
- Password change and password reset
- Current-device and all-device logout
- Account deletion with password confirmation
- User, moderator, and administrator roles
- Administrator user listing, role management, suspension, and reactivation
- Authentication activity records
- Request throttling for sensitive public endpoints

## 3. Architecture and workflow

```text
Registration
  -> serializer validation
  -> pending password and six-digit code hashes
  -> verification-code email
  -> locked pending-row verification
  -> atomic verified user creation
  -> pending secrets cleared

Login
  -> username/email lookup
  -> password verification
  -> active/verified/suspension checks
  -> JWT access and refresh tokens
  -> trusted role and dashboard response

Protected request
  -> JWT signature and expiry validation
  -> user lookup
  -> verification and suspension checks
  -> token-version check
  -> endpoint permission check
```

## 4. Database design

### User

Extends Django's `AbstractUser` with:

- `email`: required and unique
- `role`: user, moderator, or admin
- `is_email_verified`: verification state
- `is_suspended`: account suspension state
- `suspension_reason`: administrator-provided explanation
- `token_version`: invalidates previously issued access and refresh tokens

### AuthEvent

Stores security-relevant events:

- Related user, nullable after deletion
- Event type
- Email or login identifier
- IP address where available
- Structured event details
- Creation timestamp

### PendingRegistration

Stores temporary signup state until email ownership is proven:

- Opaque UUID registration ID
- Normalized username and email
- Django password hash and verification-code hash
- Expiry, resend cooldown, attempt, and hourly-send limits
- Pending, locked, expired, cancelled, or consumed status
- Optional link to the final user for idempotent verification

No permanent `User` exists before successful code verification. Final user
creation, pending-record consumption, and audit events run in one
`transaction.atomic()` database transaction.

The account schema is introduced by migrations `accounts.0001` through
`accounts.0004`.

## 5. API endpoints

### Public authentication endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | Start pending registration and email a six-digit code |
| POST | `/api/auth/login/` | Login using username, email, or identifier |
| POST | `/api/auth/refresh/` | Rotate tokens and return a new access token |
| POST | `/api/auth/verify-email/` | Verify a signup code and atomically create the user |
| POST | `/api/auth/verify-email/resend/` | Replace and resend a signup code |
| GET | `/api/auth/registrations/{registration_id}/status/` | Read masked pending status |
| GET | `/api/auth/verify-email/change/{uid}/{token}/` | Verify an existing user's changed email |
| POST | `/api/auth/password-reset/` | Request password-reset email |
| POST | `/api/auth/password-reset/confirm/` | Set a new password using a valid token |

### Authenticated account endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET/PATCH | `/api/user/profile/` | View or update profile |
| POST | `/api/user/change-password/` | Change password |
| POST | `/api/user/logout/` | Blacklist one refresh token |
| POST | `/api/user/logout-all/` | Revoke all existing tokens |
| DELETE | `/api/user/delete-account/` | Delete account after password confirmation |
| GET | `/api/user/activity/` | View recent authentication events |

### Administrator endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/user/admin/users/` | List users |
| PATCH | `/api/user/admin/users/{id}/` | Change role, suspension, or active state |

## 6. Login request and response

Login accepts exactly one identifier field:

```json
{
  "email": "user@example.com",
  "password": "StrongPassword!123"
}
```

The response provides the frontend routing decision from the trusted stored role:

```json
{
  "access": "access-token",
  "refresh": "refresh-token",
  "role": "user",
  "dashboard": "user",
  "user": {
    "id": 3,
    "username": "example",
    "email": "user@example.com",
    "role": "user",
    "is_email_verified": true
  }
}
```

The frontend must redirect using `dashboard`, but protected backend endpoints still enforce authorization independently.

## 7. Main implementation files

- `accounts/models.py`: user extensions and authentication events
- `accounts/serializers.py`: validation, login, refresh, profile, password, logout, deletion, and admin inputs
- `accounts/authentication.py`: verification, suspension, and token-version enforcement
- `accounts/permissions.py`: administrator and moderator permission classes
- `accounts/services.py`: email generation and activity recording
- `accounts/views.py`: authentication and account API behavior
- `accounts/urls.py`: public authentication routes
- `accounts/user_urls.py`: protected account and administrator routes
- `accounts/admin.py`: custom user and event administration

## 8. Security controls

- Passwords are never stored in plain text.
- Signup codes are generated with `secrets` and stored only as Django hashes.
- Permanent users are created only after verified pending rows are locked.
- Final user creation and pending consumption are atomic.
- Public registration cannot select a moderator or administrator role.
- Email and username duplicates are checked case-insensitively.
- Unverified, suspended, and inactive users cannot log in.
- Suspended users cannot refresh tokens or use existing access tokens.
- Refresh tokens rotate and old refresh tokens are blacklisted.
- Token versions revoke all earlier tokens after sensitive changes.
- Password reset responses do not reveal whether an email exists.
- Sensitive endpoints have scoped throttling.
- Administrators cannot remove their own access accidentally.
- Users see only their own authentication activity.

## 9. Configuration

Important environment settings include:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `PUBLIC_BACKEND_URL`
- `FRONTEND_URL`
- SMTP email settings
- `SIGNUP_VERIFICATION_CODE_LIFETIME_SECONDS`
- `SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS`
- `SIGNUP_VERIFICATION_MAX_ATTEMPTS`
- `SIGNUP_VERIFICATION_MAX_SENDS_PER_HOUR`
- `SIGNUP_VERIFICATION_RETENTION_HOURS`
- `SIGNUP_VERIFICATION_SUPPORT_EMAIL`
- CORS origins
- HTTPS, secure-cookie, and HSTS settings

Development uses the console email backend by default. Production requires real SMTP credentials and HTTPS-safe values from `.env.example`.

## 10. Automated tests

The authentication suite contains 21 test methods covering registration, password validation, email verification, username/email login, role-based dashboard values, suspension, token refresh, profile changes, password reset, logout, account deletion, administrator permissions, and the 2026-09-19 hardening (email change needs the password, logout token ownership, spoof-proof client IP, uniform unknown-account login).

```text
Command: python manage.py test accounts --verbosity 2
Latest result: Passed
Combined authentication/upload suite: 29 tests passed
Verification date: 2026-09-19 (full project suite: 361 tests passed)
```

## 11. Frontend integration

The frontend must provide:

- Registration, verification, login, reset, and profile forms
- Secure token storage strategy
- Access-token refresh handling
- Redirect based on the `dashboard` response
- User, moderator, and administrator route guards
- Logout and session-expiry behavior
- Clear validation and suspension messages

Frontend route guards improve user experience but do not replace backend permissions.

## 12. Limitations and later integration

- Real SMTP delivery requires deployment credentials.
- The visual dashboards belong to the frontend project.
- Moderator permissions exist, while moderator review endpoints belong to the later review-system feature.
- Image and processing events will expand the activity timeline in later features.

## 12a. Security review (2026-09-19)

The authentication layer was reviewed end to end: JWT settings, the custom
authentication class, verification, password reset, revocation, roles,
throttling and the security-relevant settings defaults.

**Confirmed sound, no change needed**

- Access tokens live 15 minutes; refresh tokens rotate and are blacklisted.
- `FaceCloudJWTAuthentication` re-checks verified / suspended / `token_version`
  on every request, so a suspension or password change takes effect at once.
- Verification and reset links use Django's signed token generator: they expire,
  are bound to the user's password, last login and email, and a reset link
  stops working once used. Reset and resend responses do not reveal whether an
  account exists.
- Only the `accounts` app exposes public endpoints; everything else falls back to
  `IsAuthenticated`. Admin endpoints require `IsAdminRole`, and an administrator
  cannot suspend, deactivate or demote themselves.
- Role, verification and suspension flags are read-only on the self-service
  profile serializer, so a user cannot escalate themselves.

**Gaps found and fixed**

| Gap | Fix |
|---|---|
| Throttling and the audit-log IP trusted `X-Forwarded-For`. With `NUM_PROXIES` unset, DRF keys the throttle on the whole header, so rotating the header bypassed every rate limit and forged the recorded address. | `REST_FRAMEWORK["NUM_PROXIES"]` from `TRUSTED_PROXY_COUNT` (default 0); `get_client_ip` now uses the same trusted-hop logic. |
| A stolen access token could change the account email, then use password reset on the new mailbox to take the account over permanently. | Changing the email now requires `current_password`. |
| Login skipped password hashing for unknown accounts, so response time revealed which usernames exist. | A dummy hash is computed for unknown accounts. |
| Logout would blacklist any refresh token presented, including another user's. | The token must belong to the authenticated user. |
| `CORS_ALLOW_ALL_ORIGINS` defaulted to on and the built-in development `SECRET_KEY` was usable with `DEBUG=False`. | CORS defaults to on only when `DEBUG` is on; startup fails if `DEBUG` is off and the key is the development default. |

**Known limits, not changed**

- ~~No per-account lockout~~ Added 2026-09-19, see "Login backoff" below.
- Logout revokes the refresh token; the access token stays valid until it expires
  (at most 15 minutes). "Log out everywhere" revokes both immediately.
- Verification and password-reset links share one token generator, so a link of
  one kind is technically accepted by the other endpoint. Both go only to the
  account's own mailbox, so this is not exploitable today.
- Access and refresh tokens are signed with `SECRET_KEY` (HS256).

Frontend impact: `PATCH /api/user/profile/` must send `current_password` when it
changes `email`; other profile edits are unchanged.

## 12b. Login backoff (2026-09-19)

The per-address throttle limits one client but not a guesser spread over many
addresses. `accounts/lockout.py` slows guessing against a single account:

- After `LOGIN_LOCK_THRESHOLD` (5) consecutive wrong passwords the account locks
  for `LOGIN_LOCK_BASE_SECONDS` (30), doubling with every further failure up to
  `LOGIN_LOCK_MAX_SECONDS` (15 minutes). Failures older than
  `LOGIN_FAILURE_WINDOW_SECONDS` stop counting.
- While locked, login returns **429** with a `Retry-After` header, **even for the
  correct password**. The check happens before the password is verified.
- Username and email of one account share one counter.
- An identifier that matches no account is tracked identically, so neither the
  lock nor its timing reveals which accounts exist.
- The table stores only HMACs of the account, never an email or username.
- A successful login, a password reset or a password change clears the counter.
  The first lock in a run is written to the audit trail as `login_locked`;
  refused attempts are `login_failed` with reason `locked`.
- Idle counters are deleted daily (`accounts.cleanup_login_attempts`).

Trade-off: anyone who knows a victim's email can keep them locked out, but only
for at most 15 minutes at a time, the per-address throttle limits how fast one
machine can do it, and a password reset clears the lock immediately.

## 13. Completion checklist

- [x] Implementation complete
- [x] Migrations applied
- [x] Role permissions verified
- [x] Automated tests passed
- [x] Django system check passed
- [x] API contracts documented
- [x] Manual registration, login, token, and profile flow verified
